import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from vllm_hust_prefix_routing import __version__, plugin, runtime

ROOT = Path(__file__).resolve().parents[2]


def test_manifest_matches_version_and_real_manager():
    code = """
import sys
from vllm_hust_ext.discovery import discover_bundles
b = discover_bundles(('org.vllm-hust.prefix-routing',))[0]
assert b.manifest.bundle_version == VERSION
assert dict(b.manifest.activation.environment)['VLLM_HUST_PREFIX_ROUTING_ENABLE'] == '1'
assert 'VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH' not in b.manifest.activation.environment
assert 'vllm_hust_prefix_routing' not in sys.modules
assert 'vllm' not in sys.modules and 'torch' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code.replace("VERSION", repr(__version__))], check=True)


def test_enable_alias_precedence_and_kill(monkeypatch):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING", "1")
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_ENABLE", "0")
    assert not runtime.enabled()
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_ENABLE", "bad")
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH", "1")
    plugin.register()
    assert not runtime.enabled()


def test_first_effective_event_is_bounded_and_not_performance_claim(monkeypatch, capsys):
    monkeypatch.setenv("VLLM_HUST_PREFIX_ROUTING_EVIDENCE", "1")
    runtime.record("routing_policy", "runtime_calls")
    assert capsys.readouterr().err == ""
    for _ in range(3):
        runtime.record("routing_policy", "prefix_hit_decisions")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("LEGACY017_EVIDENCE runtime_effective ")
    data = json.loads(lines[0].split(" ", 2)[2])
    assert data["actual_cache_hit_verified"] is False
    assert data["performance_verified"] is False


def test_installed_wheel_off_does_not_import_runtime_dependencies():
    code = """
import sys
from importlib.metadata import entry_points
ep = next(x for x in entry_points(group='vllm.general_plugins') if x.name == 'prefix_routing_pr173')
ep.load()()
assert not any(x in sys.modules for x in ('vllm', 'torch', 'aiohttp', 'zmq', 'msgspec'))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VLLM_HUST_PREFIX_ROUTING")}
    subprocess.run([sys.executable, "-I", "-c", code], env=env, check=True)
