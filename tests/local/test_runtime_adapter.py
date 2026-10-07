import hashlib
import json
from types import SimpleNamespace as NS
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from vllm_hust_prefix_routing.adapters.core import runtime023, source023
from vllm_hust_prefix_routing.components import cache_events, openai_proxy


def enable(monkeypatch):
    for key, value in {"ENABLE":"1", "ROUTING":"1", "ZMQ_REPLAY":"1", "BACKEND":"runtime023-v1"}.items():
        monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_"+key, value)
    monkeypatch.setenv("VLLM_HUST_UTILITY_VICTIM_ENABLE", "0")
    monkeypatch.setenv("VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH", "1")


def factory_fixture(monkeypatch):
    native = type("Native", (), {})
    class Factory:
        _registry = {"zmq": native}
        def create(): pass
    module = sys.modules["vllm.distributed.kv_events"]
    monkeypatch.setattr(module, "EventPublisherFactory", Factory, raising=False)
    monkeypatch.setattr(module, "ZmqEventPublisher", native, raising=False)
    return Factory, native


def test_registry_only_no_function_replacement_idempotent(monkeypatch):
    enable(monkeypatch)
    factory, native = factory_fixture(monkeypatch)
    original = factory.create
    runtime023.install_publisher()
    runtime023.install_publisher()
    assert factory._registry["zmq"] is cache_events.ZmqEventPublisher
    assert factory.create is original


def test_off_and_kill_preserve_native_registry(monkeypatch):
    factory, native = factory_fixture(monkeypatch)
    runtime023.install_publisher()
    assert factory._registry["zmq"] is native
    enable(monkeypatch)
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH", "1")
    runtime023.install_publisher()
    assert factory._registry["zmq"] is native


def test_existing_other_plugin_refused(monkeypatch):
    enable(monkeypatch)
    factory, native = factory_fixture(monkeypatch)
    factory._registry["zmq"] = object()
    with pytest.raises(RuntimeError, match="Another plugin"):
        runtime023.install_publisher()


def test_check_is_read_only_and_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "host.py"
    path.write_text("x = 1\n")
    before = path.read_bytes()
    expected = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
    monkeypatch.setattr(runtime023, "specification", lambda: {"protocol":"test", "files":{"host.py":[expected]}})
    assert runtime023.check_host(tmp_path)["compatible"]
    assert path.read_bytes() == before
    path.write_text("x = 2\n")
    assert not runtime023.check_host(tmp_path)["compatible"]
    assert path.read_text() == "x = 2\n"


def test_real_fastapi_lifespan_initializes_and_cleans_proxy(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_CONFIG", '{"nodes":[]}')
    monkeypatch.setattr(runtime023, "ensure_compatible", lambda: None)
    monkeypatch.setattr(source023, "validate_config", lambda c: None)
    events = []
    class Proxy:
        async def shutdown(self): events.append("shutdown")
    async def init(state, args):
        events.append("startup")
        assert args.enable_prefix_routing
        state.prefix_routing_proxy = Proxy()
    monkeypatch.setattr(openai_proxy, "init_prefix_routing", init)
    app = FastAPI()
    app.state.vllm_config = NS()
    app.add_middleware(runtime023.PrefixRoutingASGIMiddleware)
    @app.get("/health")
    def health(): return {"ok":True}
    with TestClient(app) as client:
        assert events == ["startup"]
        assert client.get("/health").status_code == 200
    assert events == ["startup", "shutdown"]


def test_disabled_middleware_does_not_check_host(monkeypatch):
    monkeypatch.setattr(runtime023, "ensure_compatible", lambda: pytest.fail("OFF touched host"))
    app = FastAPI()
    app.add_middleware(runtime023.PrefixRoutingASGIMiddleware)
    with TestClient(app) as client:
        assert client.get("/not-found").status_code == 404
