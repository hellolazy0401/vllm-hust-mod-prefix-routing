"""Explicit source-contract fixtures, NOT a vLLM engine or hash-equivalence test.

Use original PR msgspec wire classes without importing torch. Only byte/int
external-hash conversion is provided. Production render/hash APIs are absent.
"""
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

for name in ["vllm", "vllm.config", "vllm.config.kv_events", "vllm.distributed", "vllm.v1", "vllm.v1.core", "vllm.v1.core.kv_cache_utils", "vllm.logger"]:
    module = ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module

sys.modules["vllm.logger"].init_logger = logging.getLogger
sys.modules["vllm.config.kv_events"].KVEventsConfig = object
utils = sys.modules["vllm.v1.core.kv_cache_utils"]
utils.BlockHash = bytes
utils.ExternalBlockHash = bytes | int
utils.maybe_convert_block_hash = lambda value: value  # source default: byte hashes
spec = importlib.util.spec_from_file_location("vllm.distributed.kv_events", ROOT / "tests/fixtures/event_contract.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("VLLM_HUST_PREFIX_ROUTING"):
            monkeypatch.delenv(key)
    from vllm_hust_prefix_routing import runtime
    runtime._counts.clear()

