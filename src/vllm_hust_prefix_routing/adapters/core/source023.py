"""Explicit, file-fingerprinted Source 0.23 integration, separate from legacy."""
import hashlib
import json
import os
from pathlib import Path


def specification():
    return json.loads((Path(__file__).parents[2] / "manifests/source023-contract.json").read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").encode()).hexdigest()


def check_host(root, stage="patched"):
    if stage not in {"base", "patched"}:
        raise ValueError("stage must be base or patched")
    root = Path(root).resolve()
    spec = specification()
    errors, variants = [], {}
    key = "after" if stage == "patched" else "before"
    for name, options in spec["files"].items():
        try:
            value = file_hash(root / name)
        except OSError:
            value = None
        matched = next((o for o in options if o[key] == value), None)
        if matched is None:
            errors.append("source contract mismatch: " + name)
        else:
            variants[name] = matched["name"]
    for name, value in spec["references"].items():
        try:
            actual = file_hash(root / name)
        except OSError:
            actual = None
        if actual != value:
            errors.append("dependent API mismatch: " + name)
    return {"compatible": not errors, "protocol": spec["protocol"], "stage": stage,
            "variants": variants, "errors": errors, "hardware_qualified": False}


def require_utility_off():
    if os.getenv("VLLM_HUST_UTILITY_VICTIM_ENABLE", "0") != "0" or os.getenv(
        "VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH", "0") != "1":
        raise RuntimeError("Prefix routing experiment requires utility-victim ENABLE=0 and KILL_SWITCH=1")


def validate_config(config):
    from ...runtime import enabled
    if os.getenv("VLLM_HUST_PREFIX_ROUTING_BACKEND") not in {"source023-v1", "runtime023-v1"}:
        return
    require_utility_off()
    if not enabled():
        return
    if not config.cache_config.enable_prefix_caching:
        raise RuntimeError("Prefix routing requires prefix caching in BOTH arms")
    if config.speculative_config is not None or config.kv_transfer_config is not None:
        raise RuntimeError("source023-v1 does not admit speculative decoding or KV connectors")
    p = config.parallel_config
    if p.data_parallel_size != 1 or p.pipeline_parallel_size != 1:
        raise RuntimeError("Use independent replicas, DP=1 and PP=1 per replica")
    if getattr(p, "decode_context_parallel_size", 1) != 1 or getattr(p, "prefill_context_parallel_size", 1) != 1:
        raise RuntimeError("Context parallelism is not admitted by source023-v1")
    if getattr(config.scheduler_config, "async_scheduling", False):
        raise RuntimeError("source023-v1 requires --no-async-scheduling")
    if os.getenv("VLLM_ASCEND_BALANCE_SCHEDULING", "0") != "0":
        raise RuntimeError("Disable Ascend balance scheduling for this experiment")
