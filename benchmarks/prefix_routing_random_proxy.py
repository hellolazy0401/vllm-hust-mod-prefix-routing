# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Deterministic random proxy used by the prefix-routing benchmark."""

import argparse
import asyncio
import json
import logging
import os
import time
import random
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class RandomProxy:
    def __init__(self, upstreams: list[str], seed: int, stats_file: Path,
                 connection_policy: str = "keepalive") -> None:
        if connection_policy not in ("keepalive", "close"):
            raise ValueError("connection_policy must be keepalive or close")
        self.connection_policy = connection_policy
        self.upstream_errors = 0
        self.upstreams = [url.rstrip("/") for url in upstreams]
        self.random = random.Random(seed)
        self.stats_file = stats_file
        self.counts = [0 for _ in upstreams]
        self.session: ClientSession | None = None

    async def start(self, _: web.Application) -> None:
        self.session = ClientSession(timeout=ClientTimeout(total=None),
                                     connector=TCPConnector(force_close=self.connection_policy == "close"))

    async def stop(self, _: web.Application) -> None:
        if self.session is not None:
            await self.session.close()
        self.write_stats()

    def write_stats(self) -> None:
        payload = {
            "policy": "seeded-random",
            "connection_policy": self.connection_policy,
            "upstream_errors": self.upstream_errors,
            "upstreams": self.upstreams,
            "request_counts": {
                upstream: count for upstream, count in zip(self.upstreams, self.counts)
            },
            "total_requests": sum(self.counts),
        }
        self.stats_file.parent.mkdir(parents=True, exist_ok=True)
        self.stats_file.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

    async def health(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def stats(self, _: web.Request) -> web.Response:
        self.write_stats()
        return web.json_response(
            {
                "upstreams": self.upstreams,
                "request_counts": self.counts,
                "total_requests": sum(self.counts),
                "connection_policy": self.connection_policy,
                "upstream_errors": self.upstream_errors,
            }
        )

    async def forward(self, request: web.Request) -> web.StreamResponse:
        assert self.session is not None
        index = self.random.randrange(len(self.upstreams))
        self.counts[index] += 1
        upstream = self.upstreams[index]
        target = f"{upstream}{request.rel_url}"
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() != "host"
        }
        body = await request.read()
        response = None
        try:
            async with self.session.request(
                request.method,
                target,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream_response:
                response_headers = {
                    name: value
                    for name, value in upstream_response.headers.items()
                    if name.lower() not in _HOP_BY_HOP_HEADERS
                }
                response_headers["x-prefix-routing-benchmark-upstream"] = str(index)
                response = web.StreamResponse(
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                    headers=response_headers,
                )
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, OSError, asyncio.TimeoutError) as exc:
            self.upstream_errors += 1
            logging.getLogger(__name__).error("PREFIX_PROXY_ERROR %s", json.dumps({
                "unix": time.time(), "upstream_index": index, "upstream": upstream,
                "error_type": type(exc).__name__, "response_started": bool(response and response.prepared),
                "connection_policy": self.connection_policy, "request_retried": False,
            }), exc_info=True)
            self.write_stats()
            if response is not None and response.prepared:
                # Headers may already be HTTP 200. Do not append a fake success
                # EOF or attempt a second HTTP response after a truncated stream.
                if request.transport is not None:
                    request.transport.close()
                return response
            return web.json_response(
                {"error": f"upstream request failed: {type(exc).__name__}"},
                status=502,
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", action="append", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--connection-policy", choices=["keepalive", "close"],
                        default=os.getenv("PREFIX_ROUTING_PROXY_CONNECTION_POLICY", "keepalive"))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if len(args.upstream) < 2:
        raise SystemExit("at least two --upstream values are required")
    proxy = RandomProxy(args.upstream, args.seed, args.stats_file, args.connection_policy)
    app = web.Application(client_max_size=16 * 1024**2)
    app.on_startup.append(proxy.start)
    app.on_cleanup.append(proxy.stop)
    app.router.add_get("/health", proxy.health)
    app.router.add_get("/_prefix_routing_benchmark/stats", proxy.stats)
    app.router.add_route("*", "/{path:.*}", proxy.forward)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
