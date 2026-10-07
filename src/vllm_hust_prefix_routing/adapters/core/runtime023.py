"""Source 0.23 runtime integration. No source writes or API function patches.

Uses the existing --middleware entrypoint and a guarded process-local replacement
of the native zmq publisher registry entry. Full disable requires process restart.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ...runtime import enabled, record


def specification():
    return json.loads((Path(__file__).parents[2] / "manifests/runtime023-contract.json").read_text(encoding="utf-8"))


def check_host(root):
    root = Path(root).resolve()
    spec = specification()
    errors = []
    for name, allowed in spec["files"].items():
        try:
            value = hashlib.sha256((root / name).read_text(encoding="utf-8").encode()).hexdigest()
        except OSError:
            value = None
        if value not in allowed:
            errors.append("unqualified source: " + name)
    return {"compatible": not errors, "protocol": spec["protocol"], "errors": errors,
            "source_modified": False, "hardware_qualified": False}


def ensure_compatible():
    from .source023 import require_utility_off
    require_utility_off()
    module = importlib.util.find_spec("vllm")
    if module is None or module.origin is None:
        raise RuntimeError("Cannot locate installed vLLM source")
    result = check_host(Path(module.origin).resolve().parent.parent)
    if not result["compatible"]:
        raise RuntimeError(json.dumps(result))
    if enabled("http_events"):
        raise RuntimeError("runtime023-v1 supports ZMQ only; HTTP upload requires unavailable host hooks")


def install_publisher():
    from vllm.distributed.kv_events import EventPublisherFactory, ZmqEventPublisher as Native
    from ...components.cache_events import ZmqEventPublisher as Prefix
    if not enabled("zmq_replay"):
        return
    current = EventPublisherFactory._registry.get("zmq")
    if current is Prefix:
        return
    if current is not Native:
        raise RuntimeError("Another plugin already replaced the zmq publisher; refusing to mix implementations")
    # Literal['null','zmq'] prevents an additional name in this host's config.
    # Replace only this registry entry; no classes/functions/files are rewritten.
    EventPublisherFactory._registry["zmq"] = Prefix
    record("zmq_replay", "installed")


class PrefixRoutingASGIMiddleware:
    """Mount through vLLM's existing --middleware dotted class interface."""
    def __init__(self, app):
        self.app = app
        self.proxy = None
        self.inner = None

    async def startup(self, scope):
        if not enabled():
            return
        ensure_compatible()
        state = scope["app"].state
        from .source023 import validate_config
        validate_config(state.vllm_config)
        raw = os.getenv("VLLM_HUST_PREFIX_ROUTING_CONFIG")
        if not raw:
            return  # Non-routing replica still validates its runtime shape.
        from ...components.openai_proxy import init_prefix_routing, PrefixRoutingMiddleware
        args = SimpleNamespace(enable_prefix_routing=True, prefix_routing_config=json.loads(raw))
        await init_prefix_routing(state, args)
        self.proxy = state.prefix_routing_proxy
        self.inner = PrefixRoutingMiddleware(self.app)
        record("routing", "installed")

    async def shutdown(self):
        if self.proxy is not None:
            proxy, self.proxy = self.proxy, None
            await proxy.shutdown()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def lifecycle_receive():
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await self.startup(scope)
                elif message["type"] == "lifespan.shutdown":
                    await self.shutdown()
                return message
            try:
                await self.app(scope, lifecycle_receive, send)
            finally:
                await self.shutdown()
        elif self.inner is not None:
            await self.inner(scope, receive, send)
        else:
            await self.app(scope, receive, send)
