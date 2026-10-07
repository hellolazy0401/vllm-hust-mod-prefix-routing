import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vllm_hust_prefix_routing import plugin, runtime
from vllm_hust_prefix_routing.adapters.core import bridge, contract
from vllm_hust_prefix_routing.components.openai_proxy import _parse_prefix_routing_config


def test_default_off_register_never_reads_host(monkeypatch):
    monkeypatch.setattr(contract, "ensure_compatible", lambda: pytest.fail("default-off inspected host"))
    plugin.register()
    assert not runtime.enabled()


def test_kill_switch_wins_invalid_subordinate_config(monkeypatch):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING", "broken")
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH", "1")
    plugin.register()
    assert not runtime.enabled("routing")


def test_off_on_off_startup_decisions(monkeypatch):
    assert not runtime.enabled("routing")
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING", "1")
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_ROUTING", "1")
    assert runtime.enabled("routing")
    assert not runtime.enabled("http_events")
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH", "1")
    assert not runtime.enabled("routing")


def test_invalid_enabled_flag_rejected(monkeypatch):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING", "yes")
    with pytest.raises(ValueError):
        plugin.register()


def test_no_component_rejected(monkeypatch):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING", "1")
    with pytest.raises(ValueError, match="without any component"):
        plugin.register()


def test_native_publisher_preserved_when_disabled():
    native = object()
    assert bridge.select_publisher("zmq", native) is native


def test_http_events_off_does_not_construct_uploader():
    uploader = bridge.PrefixCacheEventUploaderFactory.create(SimpleNamespace(prefix_cache_upload_endpoint="http://unused"))
    from vllm.distributed.kv_events import NullEventPublisher
    assert isinstance(uploader, NullEventPublisher)


def test_unqualified_host_rejected(tmp_path):
    result = contract.check_host(tmp_path)
    assert result["compatible"] is False
    assert len(result["errors"]) == 10


@pytest.mark.parametrize("extra", [{"typo": 1}, {"hash_block_size": 8}, {"nodes": [{"id": "a", "local": "false"}]}])
def test_strict_config_rejects_typos_and_hash_mismatch(extra):
    conf = {"nodes": [{"id": "a", "local": True}], **extra}
    with pytest.raises(ValueError):
        _parse_prefix_routing_config(conf, SimpleNamespace(cache_config=SimpleNamespace(hash_block_size=16, block_size=16)))


def test_counter_does_not_appear_until_path_runs():
    assert runtime.runtime_status()["counters"] == {}
    runtime.record("proxy", "local_fallbacks")
    assert runtime.runtime_status()["counters"]["proxy"]["local_fallbacks"] == 1

