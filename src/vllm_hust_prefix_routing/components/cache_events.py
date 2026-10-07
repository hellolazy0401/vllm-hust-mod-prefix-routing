# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import contextlib
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict
from queue import Queue
from typing import Any

import msgspec
import zmq

from vllm.config.kv_events import KVEventsConfig
from vllm.logger import init_logger
from vllm.v1.core.kv_cache_utils import ExternalBlockHash
from vllm_hust_prefix_routing.runtime import record, dump_runtime_status

logger = init_logger(__name__)


from vllm.distributed.kv_events import (
    EventBatch, KVCacheEvent, BlockStored, BlockRemoved, AllBlocksCleared,
    KVEventBatch, EventPublisher, NullEventPublisher,
)

class ZmqEventReplayRequest(msgspec.Struct, omit_defaults=True):
    """Request replay from ``start_seq`` or a full reconciliation snapshot."""

    publisher_epoch: str | None = None
    start_seq: int = 0
    force_snapshot: bool = False


class ZmqEventReplayResponse(msgspec.Struct, omit_defaults=True):
    """Atomic response from the ZMQ replay control plane."""

    publisher_epoch: str
    next_seq: int
    replayed_batches: list[tuple[int, bytes]]
    snapshot: bytes | None = None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_prefix_cache_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


class HttpPrefixCacheEventUploader(EventPublisher):
    """Non-blocking HTTP uploader with snapshot-based reconciliation.

    This is intentionally not registered as a normal KV event publisher. It is
    a side channel used by prefix-aware routing so that enabling it does not
    replace the existing ZMQ/null publisher selected by ``KVEventsConfig``.
    """

    SHUTDOWN_TIMEOUT: float = 1.0

    def __init__(
        self,
        data_parallel_rank: int,
        endpoint: str,
        max_queue_size: int = 100_000,
        request_timeout: float = 5.0,
        token: str | None = None,
        initial_snapshot: Any | None = None,
        snapshot_interval: float = 30.0,
        retry_interval: float = 0.1,
        max_retry_interval: float = 5.0,
        _start_thread: bool = True,
        _opener: urllib.request.OpenerDirector | None = None,
        **_: Any,
    ) -> None:
        super().__init__(data_parallel_rank)
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(
                "HTTP prefix-cache upload endpoint must start with "
                f"http:// or https://, got {endpoint!r}"
            )
        if (
            not isinstance(max_queue_size, int)
            or isinstance(max_queue_size, bool)
            or max_queue_size <= 0
        ):
            raise ValueError("max_queue_size must be a positive integer")
        if (
            not isinstance(request_timeout, int | float)
            or isinstance(request_timeout, bool)
            or request_timeout <= 0
        ):
            raise ValueError("request_timeout must be positive")
        if (
            not isinstance(snapshot_interval, int | float)
            or isinstance(snapshot_interval, bool)
            or snapshot_interval < 0
        ):
            raise ValueError("snapshot_interval must be non-negative")
        self._endpoint = endpoint
        self._request_timeout = request_timeout
        self._opener = _opener or _build_prefix_cache_opener()
        self._headers = {"Content-Type": "application/msgpack"}
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._event_queue = Queue[EventBatch | None](maxsize=max_queue_size)
        self._pack = msgspec.msgpack.Encoder()
        self._retry_interval = retry_interval
        self._max_retry_interval = max_retry_interval
        self._state_lock = threading.Lock()
        self._group_block_sizes: dict[int, int] = {}
        self._group_hashes: dict[int, set[ExternalBlockHash]] = {}
        self._snapshot_interval = float(snapshot_interval)
        self._next_snapshot_at = time.monotonic() + self._snapshot_interval
        self._needs_snapshot = threading.Event()
        self._reconcile_generation = 0
        if initial_snapshot is not None:
            self._group_block_sizes = dict(initial_snapshot.group_block_sizes)
            self._group_hashes = {
                group_id: set(hashes)
                for group_id, hashes in initial_snapshot.group_hashes.items()
            }
            self._mark_snapshot_needed()
        self._running = True
        self._thread = threading.Thread(
            target=self._publisher_thread,
            daemon=True,
            name="prefix-cache-http-uploader",
        )
        if _start_thread:
            self._thread.start()

    def publish(self, events: EventBatch) -> None:
        if not self._running:
            return
        record("http_events", "runtime_calls")
        if events.data_parallel_rank is None:
            events.data_parallel_rank = self._data_parallel_rank
        self._apply_to_mirror(events.events)
        try:
            self._event_queue.put_nowait(events)
        except queue.Full:
            record("http_events", "queue_overflows")
            self._mark_snapshot_needed()
            logger.warning(
                "Prefix-cache upload queue is full; scheduling reconciliation"
            )

    def _apply_to_mirror(self, events: list[Any]) -> None:
        with self._state_lock:
            for event in events:
                if isinstance(event, BlockStored):
                    group_idx = 0 if event.group_idx is None else event.group_idx
                    self._group_block_sizes[group_idx] = event.block_size
                    self._group_hashes.setdefault(group_idx, set()).update(
                        event.block_hashes
                    )
                elif isinstance(event, BlockRemoved):
                    group_idx = 0 if event.group_idx is None else event.group_idx
                    self._group_hashes.setdefault(group_idx, set()).difference_update(
                        event.block_hashes
                    )
                elif isinstance(event, AllBlocksCleared):
                    self._group_hashes.clear()

    def _mark_snapshot_needed(self) -> None:
        with self._state_lock:
            self._reconcile_generation += 1
            self._needs_snapshot.set()

    def _maybe_mark_periodic_snapshot(self) -> None:
        if self._snapshot_interval <= 0:
            return
        with self._state_lock:
            if (
                not self._needs_snapshot.is_set()
                and time.monotonic() >= self._next_snapshot_at
            ):
                self._reconcile_generation += 1
                self._needs_snapshot.set()

    def _build_snapshot_batch(self) -> tuple[KVEventBatch, int]:
        record("http_events", "snapshots_built")
        with self._state_lock:
            generation = self._reconcile_generation
            events: list[KVCacheEvent] = [AllBlocksCleared()]
            for group_idx, block_size in self._group_block_sizes.items():
                events.append(
                    BlockStored(
                        block_hashes=list(self._group_hashes.get(group_idx, set())),
                        parent_block_hash=None,
                        token_ids=[],
                        block_size=block_size,
                        lora_id=None,
                        medium=None,
                        lora_name=None,
                        group_idx=group_idx,
                    )
                )
        return (
            KVEventBatch(
                ts=time.time(),
                events=events,
                data_parallel_rank=self._data_parallel_rank,
            ),
            generation,
        )

    def _discard_queued_batches(self) -> None:
        while True:
            try:
                queued = self._event_queue.get_nowait()
            except queue.Empty:
                return
            self._event_queue.task_done()
            if queued is None:
                return

    def shutdown(self) -> None:
        self._running = False
        with contextlib.suppress(queue.Full):
            self._event_queue.put_nowait(None)

        start = time.time()
        while not self._event_queue.empty() and (
            time.time() - start < self.SHUTDOWN_TIMEOUT
        ):
            time.sleep(0.1)

        if self._thread.is_alive():
            self._thread.join(timeout=self.SHUTDOWN_TIMEOUT)
        dump_runtime_status()

    def _publisher_thread(self) -> None:
        retry_delay = self._retry_interval
        while self._running or self._event_queue.qsize() > 0:
            self._maybe_mark_periodic_snapshot()
            is_snapshot = self._needs_snapshot.is_set()
            generation = None
            event: EventBatch | None
            if is_snapshot:
                self._discard_queued_batches()
                event, generation = self._build_snapshot_batch()
            else:
                try:
                    event = self._event_queue.get(timeout=0.1)
                    if event is None:
                        self._event_queue.task_done()
                        break
                except queue.Empty:
                    continue
            try:
                assert event is not None
                payload = self._pack.encode(event)
                request = urllib.request.Request(
                    self._endpoint,
                    data=payload,
                    method="POST",
                    headers=self._headers,
                )
                with self._opener.open(
                    request, timeout=self._request_timeout
                ) as response:
                    if response.status >= 300:
                        raise urllib.error.HTTPError(
                            self._endpoint,
                            response.status,
                            "prefix-cache upload rejected",
                            response.headers,
                            None,
                        )
                retry_delay = self._retry_interval
                record("http_events", "upload_successes")
                if is_snapshot:
                    with self._state_lock:
                        if generation == self._reconcile_generation:
                            self._needs_snapshot.clear()
                            self._next_snapshot_at = (
                                time.monotonic() + self._snapshot_interval
                            )
            except urllib.error.URLError as exc:
                record("http_events", "runtime_failures")
                logger.warning("HTTP prefix-cache upload failed: %s", exc)
                self._mark_snapshot_needed()
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self._max_retry_interval)
            except Exception as exc:
                record("http_events", "runtime_failures")
                logger.exception("Unexpected HTTP prefix-cache upload error: %s", exc)
                self._mark_snapshot_needed()
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self._max_retry_interval)
            finally:
                if not is_snapshot:
                    self._event_queue.task_done()


class ZmqEventPublisher(EventPublisher):
    """Reliable PUB/ROUTER publisher with an in-memory replay buffer.

    Spawns a separate thread to handle publishing from a queue.

    Parameters
    ----------
    endpoint:
        PUB address. Use `tcp://*:5557` to bind or `tcp://host:5557` to
        connect.
    replay_endpoint:
        Optional ROUTER address for replay requests. When given, subscribers can
        request missed batches or a reconciliation snapshot. The original
        8-byte sequence request remains supported for existing consumers.
    buffer_steps:
        Number of past batches to keep for replay.
    hwm:
        ZeroMQ high-water-mark for PUB socket.
    max_queue_size:
        Maximum number of events to buffer in memory.
    topic:
        Topic to publish events to.
    """

    SHUTDOWN_TIMEOUT: float = 1.0
    END_SEQ = (-1).to_bytes(8, "big", signed=True)

    def __init__(
        self,
        data_parallel_rank: int,
        endpoint: str = "tcp://*:5557",
        replay_endpoint: str | None = None,
        buffer_steps: int = 10_000,
        hwm: int = 100_000,
        max_queue_size: int = 100_000,
        topic: str = "",
    ) -> None:
        # Storage
        super().__init__(data_parallel_rank)
        self._event_queue = Queue[EventBatch | None](maxsize=max_queue_size)
        self._buffer = deque[tuple[int, bytes]](maxlen=buffer_steps)
        self._state_lock = threading.Lock()
        self._group_metadata: dict[int, tuple[str | None, int | None]] = {}
        self._group_block_sizes: dict[int, int] = {}
        self._group_hashes: dict[int, set[ExternalBlockHash]] = {}

        # ZMQ sockets
        self._ctx = zmq.Context.instance()
        self._pub: zmq.Socket | None = None
        self._replay: zmq.Socket | None = None
        self._dp_rank = data_parallel_rank

        self._endpoint = self.offset_endpoint_port(endpoint, self._dp_rank)
        self._replay_endpoint = self.offset_endpoint_port(
            replay_endpoint, self._dp_rank
        )
        self._hwm = hwm
        self._socket_setup()

        # Payload
        self._publisher_epoch = uuid.uuid4().hex
        self._next_seq = 0
        self._topic_bytes = topic.encode("utf-8")

        # Thread
        self._running = True
        logger.info("Starting ZMQ publisher thread")

        self._thread = threading.Thread(
            target=self._publisher_thread, daemon=True, name="zmq-publisher"
        )
        self._thread.start()

    def publish(self, events: EventBatch) -> None:
        if not self._running:
            raise RuntimeError("Publisher is closed")
        record("zmq_replay", "runtime_calls")
        if events.data_parallel_rank is None:
            events.data_parallel_rank = self._data_parallel_rank
        if self._replay_endpoint is not None:
            self._apply_to_mirror(events.events)
        self._event_queue.put(events)

    def shutdown(self) -> None:
        """Stop the publisher thread and clean up resources."""
        self._running = False
        self._event_queue.put_nowait(None)

        start = time.time()
        pending_items = True
        while pending_items and (time.time() - start < self.SHUTDOWN_TIMEOUT):
            pending_items = not self._event_queue.empty()
            if pending_items:
                time.sleep(0.1)

        if pending_items:
            logger.warning(
                "Warning: Queue still has %s items after %s seconds timeout",
                self._event_queue.qsize(),
                self.SHUTDOWN_TIMEOUT,
            )

        if self._thread.is_alive():
            self._thread.join(timeout=self.SHUTDOWN_TIMEOUT)

        # Clean up ZMQ resources
        try:
            if self._pub is not None:
                self._pub.close(linger=0)
            if self._replay is not None:
                self._replay.close(linger=0)
        finally:
            pass  # Do not terminate context; other sockets may use it
        dump_runtime_status()

    def _socket_setup(self) -> None:
        """Initialize sockets
        https://pyzmq.readthedocs.io/en/v19.0.0/morethanbindings.html#thread-safety
        """
        if self._pub is None:
            self._pub = self._ctx.socket(zmq.PUB)
            self._pub.set_hwm(self._hwm)
            # Heuristic: bind if wildcard / * present, else connect.
            # bind stable, connect volatile convention
            if self._endpoint is not None and (
                "*" in self._endpoint
                or "::" in self._endpoint
                or self._endpoint.startswith("ipc://")
                or self._endpoint.startswith("inproc://")
            ):
                self._pub.bind(self._endpoint)
            elif self._endpoint is not None:
                self._pub.connect(self._endpoint)

        # Set up replay socket: use ROUTER
        # 1) handles multiple REQ clients (identities)
        # 2) lets us send back one request → many replies (streamed events)
        # 3) works in our non‑blocking poll loop alongside PUB
        if self._replay_endpoint is not None:
            self._replay = self._ctx.socket(zmq.ROUTER)
            self._replay.bind(self._replay_endpoint)

    def _publisher_thread(self) -> None:
        """Background thread that processes the event queue."""
        self._pack = msgspec.msgpack.Encoder()

        assert self._pub is not None  # narrows type for mypy

        while self._running or self._event_queue.qsize() > 0:
            # --- replay (non-critical) ---------------------------------
            if self._replay is not None and self._replay.poll(0):
                try:
                    self._service_replay()
                except Exception as e:
                    logger.exception("Error in replay: %s", e)

            # --- main queue (critical) ---------------------------------
            try:
                event = self._event_queue.get(timeout=0.1)
                if event is None:
                    break  # Sentinel received, exit thread
            except queue.Empty:
                continue

            try:
                seq = self._next_seq
                self._next_seq += 1

                payload = self._pack.encode(event)
                seq_bytes = seq.to_bytes(8, "big")
                self._pub.send_multipart((self._topic_bytes, seq_bytes, payload))

                self._buffer.append((seq, payload))
                self._event_queue.task_done()

            except Exception as e:
                # Publishing failed;  back-off a bit to avoid a tight error loop
                logger.exception("Error in publisher thread: %s", e)
                time.sleep(0.1)

    def _service_replay(self) -> None:
        """If a replay request is waiting, send buffered batches."""
        assert self._replay is not None  # narrows type for mypy

        frame = self._replay.recv_multipart()
        if len(frame) not in (2, 3) or (len(frame) == 3 and frame[1] != b""):
            logger.warning("Invalid replay request: %s", frame)
            return
        client_id = frame[0]
        delimiter = (b"",) if len(frame) == 3 else ()
        request_payload = frame[-1]

        try:
            request = msgspec.msgpack.decode(
                request_payload,
                type=ZmqEventReplayRequest,
            )
        except msgspec.DecodeError:
            if len(request_payload) == 8:
                self._service_legacy_replay(client_id, delimiter, request_payload)
            else:
                logger.warning("Invalid replay request payload")
            return

        response = self._build_replay_response(request)
        self._replay.send_multipart(
            (client_id, *delimiter, self._pack.encode(response))
        )

    def _service_legacy_replay(
        self,
        client_id: bytes,
        delimiter: tuple[bytes, ...],
        start_seq_bytes: bytes,
    ) -> None:
        """Serve the original streamed replay protocol for existing consumers."""
        assert self._replay is not None
        start_seq = int.from_bytes(start_seq_bytes, "big")

        for seq, buf in self._buffer:
            if seq >= start_seq:
                self._replay.send_multipart(
                    (client_id, *delimiter, seq.to_bytes(8, "big"), buf)
                )
        self._replay.send_multipart((client_id, *delimiter, self.END_SEQ, b""))

    def _build_replay_response(
        self, request: ZmqEventReplayRequest
    ) -> ZmqEventReplayResponse:
        record("zmq_replay", "recovery_requests")
        oldest_seq = self._buffer[0][0] if self._buffer else self._next_seq
        replayed_batches = [
            (seq, payload) for seq, payload in self._buffer if seq >= request.start_seq
        ]
        replay_is_contiguous = (
            request.start_seq >= 0
            and len(replayed_batches) == self._next_seq - request.start_seq
            and all(
                seq == request.start_seq + index
                for index, (seq, _) in enumerate(replayed_batches)
            )
        )
        needs_snapshot = (
            request.force_snapshot
            or request.publisher_epoch != self._publisher_epoch
            or request.start_seq < oldest_seq
            or request.start_seq > self._next_seq
            or not replay_is_contiguous
        )
        if needs_snapshot:
            record("zmq_replay", "snapshot_responses")
            with self._state_lock:
                snapshot = self._pack.encode(self._build_snapshot_batch())
            return ZmqEventReplayResponse(
                publisher_epoch=self._publisher_epoch,
                next_seq=self._next_seq,
                replayed_batches=[],
                snapshot=snapshot,
            )

        return ZmqEventReplayResponse(
            publisher_epoch=self._publisher_epoch,
            next_seq=self._next_seq,
            replayed_batches=replayed_batches,
        )

    def _apply_to_mirror(self, events: list[Any]) -> None:
        with self._state_lock:
            for event in events:
                if isinstance(event, BlockStored):
                    group_idx = 0 if event.group_idx is None else event.group_idx
                    self._group_metadata[group_idx] = (
                        getattr(event, "kv_cache_spec_kind", None),
                        getattr(event, "kv_cache_spec_sliding_window", None),
                    )
                    self._group_block_sizes[group_idx] = event.block_size
                    self._group_hashes.setdefault(group_idx, set()).update(
                        event.block_hashes
                    )
                elif isinstance(event, BlockRemoved):
                    group_idx = 0 if event.group_idx is None else event.group_idx
                    self._group_hashes.setdefault(group_idx, set()).difference_update(
                        event.block_hashes
                    )
                elif isinstance(event, AllBlocksCleared):
                    self._group_hashes.clear()

    def _build_snapshot_batch(self) -> KVEventBatch:
        events: list[KVCacheEvent] = [AllBlocksCleared()]
        for group_idx, block_size in self._group_block_sizes.items():
            events.append(
                BlockStored(
                    block_hashes=list(self._group_hashes.get(group_idx, set())),
                    parent_block_hash=None,
                    token_ids=[],
                    block_size=block_size,
                    lora_id=None,
                    medium=None,
                    lora_name=None,
                    group_idx=group_idx,
                    kv_cache_spec_kind=self._group_metadata.get(group_idx, (None, None))[0],
                    kv_cache_spec_sliding_window=self._group_metadata.get(group_idx, (None, None))[1],
                )
            )
        return KVEventBatch(
            ts=time.time(),
            events=events,
            data_parallel_rank=self._data_parallel_rank,
        )

    @staticmethod
    def offset_endpoint_port(
        endpoint: str | None, data_parallel_rank: int
    ) -> str | None:
        """Helper function to offset the port in an endpoint by
            the data parallel rank.

        Args:
            endpoint: The endpoint string
                (e.g., "tcp://*:5557" or "inproc://cache")
            data_parallel_rank: The data parallel rank to offset by

        Returns:
            The endpoint with the port offset by data_parallel_rank
                or suffix appended
        """
        # Do nothing if input is None or data_parallel_rank is 0
        if not endpoint or data_parallel_rank == 0:
            return endpoint

        if "inproc" in endpoint:
            return f"{endpoint}_dp{data_parallel_rank}"
        if "tcp" in endpoint:
            if endpoint and ":" in endpoint:
                # Get everything after the last colon (the port)
                last_colon_idx = endpoint.rfind(":")
                base_addr = endpoint[:last_colon_idx]
                base_port = int(endpoint[last_colon_idx + 1 :])
                new_port = base_port + data_parallel_rank
                return f"{base_addr}:{new_port}"
            return endpoint
        raise ValueError("Invalid endpoint: must contain 'inproc' or 'tcp'")


class PrefixCacheEventUploaderFactory:
    @classmethod
    def create(
        cls,
        config: KVEventsConfig | None,
        data_parallel_rank: int = 0,
        initial_snapshot: Any | None = None,
    ) -> EventPublisher:
        """Create the independent prefix-cache event upload side channel."""
        if config is None or config.prefix_cache_upload_endpoint is None:
            return NullEventPublisher()

        return HttpPrefixCacheEventUploader(
            data_parallel_rank=data_parallel_rank,
            endpoint=config.prefix_cache_upload_endpoint,
            max_queue_size=config.prefix_cache_upload_max_queue_size,
            request_timeout=config.prefix_cache_upload_timeout,
            snapshot_interval=config.prefix_cache_upload_snapshot_interval,
            token=config.prefix_cache_upload_token,
            initial_snapshot=initial_snapshot,
        )
