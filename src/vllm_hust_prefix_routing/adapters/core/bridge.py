"""Explicit Core source hooks. No monkey patching and no registry replacement."""
from ...runtime import enabled, record
from .contract import ensure_compatible


def select_publisher(kind, native_constructor):
    if kind != "zmq" or not enabled("zmq_replay"):
        return native_constructor
    ensure_compatible()
    from ...components.cache_events import ZmqEventPublisher
    record("zmq_replay", "installed")
    return ZmqEventPublisher


class PrefixCacheEventUploaderFactory:
    @classmethod
    def create(cls, config, data_parallel_rank=0, initial_snapshot=None):
        from vllm.distributed.kv_events import NullEventPublisher
        if not enabled("http_events") or config is None or config.prefix_cache_upload_endpoint is None:
            return NullEventPublisher()
        ensure_compatible()
        from ...components.cache_events import PrefixCacheEventUploaderFactory as Factory
        uploader = Factory.create(config, data_parallel_rank, initial_snapshot)
        record("http_events", "installed")
        return uploader

