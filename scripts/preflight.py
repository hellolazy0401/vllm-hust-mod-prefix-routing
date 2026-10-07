"""Container preflight: dependencies, installed source identity, environment."""
import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--core-root", type=Path, required=True)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    from vllm_hust_prefix_routing.adapters.core.runtime023 import check_host
    from vllm_hust_prefix_routing.adapters.core.source023 import require_utility_off
    require_utility_off()
    if sys.version_info < (3, 11): raise RuntimeError("Python >=3.11 required")
    versions = {}
    for name in ("vllm", "vllm-ascend", "vllm-hust-prefix-routing", "vllm-hust-ext", "torch", "torch-npu"):
        versions[name] = importlib.metadata.version(name)
    for name in ("aiohttp", "msgspec", "zmq", "fastapi", "pydantic"):
        importlib.import_module(name)
    spec = importlib.util.find_spec("vllm")
    actual = Path(spec.origin).resolve().parent.parent if spec and spec.origin else None
    if actual != a.core_root.resolve():
        raise RuntimeError(f"Installed vllm source is {actual}, not {a.core_root}")
    receipt = check_host(actual)
    receipt.update(versions=versions, python=sys.version, utility_enable=os.getenv("VLLM_HUST_UTILITY_VICTIM_ENABLE"),
                   utility_kill=os.getenv("VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH"), model_loaded=False)
    text = json.dumps(receipt, indent=2)
    print(text)
    if a.output: a.output.write_text(text, encoding="utf-8")
    if not receipt["compatible"]: raise SystemExit(2)


if __name__ == "__main__":
    main()
