"""Fail closed on any host other than the exact patched historical carrier."""
import hashlib
import importlib.util
import json
from functools import lru_cache
from pathlib import Path
import subprocess
import os


def contract() -> dict:
    return json.loads((Path(__file__).parents[2] / "manifests/host-contract.json").read_text(encoding="utf-8"))


def check_host(root: str | Path, *, stage: str = "patched") -> dict:
    if os.getenv("VLLM_HUST_PREFIX_ROUTING_BACKEND") == "runtime023-v1":
        from .runtime023 import check_host as check_runtime
        return check_runtime(root)
    if os.getenv("VLLM_HUST_PREFIX_ROUTING_BACKEND") == "source023-v1":
        from .source023 import check_host as check_source023
        return check_source023(root, stage)
    if stage not in {"base", "patched"}:
        raise ValueError("stage must be base or patched")
    root = Path(root).resolve()
    spec = contract()
    errors = []
    try:
        head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    if head != spec["base_sha"]:
        errors.append("Git HEAD must equal the exact carrier base SHA; unqualified hosts are rejected")
    for name, hashes in spec["files"].items():
        path = root / name
        if not path.is_file():
            errors.append(f"missing host file: {name}")
            continue
        # Canonical LF text allows ordinary Windows checkout line endings.
        digest = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
        if digest != hashes[stage + "_sha256"]:
            errors.append(f"source contract mismatch: {name}")
    if head == spec["base_sha"]:
        changed = subprocess.check_output(["git", "-C", str(root), "diff", "HEAD", "--name-only"], text=True).splitlines()
        allowed = set(spec["files"]) if stage == "patched" else set()
        if unexpected := set(changed) - allowed:
            errors.append(f"unreviewed tracked changes: {sorted(unexpected)}")
    return {"compatible": not errors, "protocol": spec["protocol"], "stage": stage,
            "core_sha": head, "errors": errors, "hardware_qualified": False}


def ensure_compatible() -> None:
    if os.getenv("VLLM_HUST_PREFIX_ROUTING_BACKEND") == "runtime023-v1":
        from .runtime023 import ensure_compatible as ensure_runtime
        return ensure_runtime()
    if os.getenv("VLLM_HUST_PREFIX_ROUTING_BACKEND") == "source023-v1":
        from .source023 import require_utility_off
        require_utility_off()
    module = importlib.util.find_spec("vllm")
    if module is None or module.origin is None:
        raise RuntimeError("vLLM source checkout is required")
    result = check_host(Path(module.origin).resolve().parent.parent)
    if not result["compatible"]:
        raise RuntimeError("Prefix routing host rejected: " + "; ".join(result["errors"]))
