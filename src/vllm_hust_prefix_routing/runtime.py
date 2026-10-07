"""Startup switches and process-local evidence; contains no vLLM imports."""
import os
import atexit
import json
import sys
from pathlib import Path
from collections import Counter, defaultdict
from threading import Lock

_lock = Lock()
_counts = defaultdict(Counter)
_prefix = "VLLM_HUST_PREFIX_ROUTING"
SWITCHES = {"routing": "ROUTING", "http_events": "HTTP_EVENTS", "zmq_replay": "ZMQ_REPLAY"}


def _bool(name: str) -> bool:
    value = os.environ.get(name, "0").strip().lower()
    if value not in {"0", "1", "false", "true"}:
        raise ValueError(f"{name} must be 0/1/false/true")
    return value in {"1", "true"}


def enabled(component: str | None = None) -> bool:
    # Kill switch deliberately wins even over invalid subordinate configuration.
    if _bool(_prefix + "_KILL_SWITCH"):
        return False
    # The utility-victim convention takes precedence; retain dev0 profiles.
    switch = _prefix + "_ENABLE" if _prefix + "_ENABLE" in os.environ else _prefix
    if not _bool(switch):
        return False
    return component is None or _bool(_prefix + "_" + SWITCHES[component])


def record(component: str, name: str, amount: int = 1) -> None:
    with _lock:
        first = _counts[component][name] == 0
        _counts[component][name] += amount
    if first and os.getenv(_prefix + "_EVIDENCE", "0") == "1":
        # A routing decision is not proof of engine cache reuse or speedup.
        effective = {("routing_policy", "prefix_hit_decisions"),
                     ("http_events", "upload_successes"),
                     ("zmq_replay", "snapshot_responses")}
        if name == "installed" or (component, name) in effective:
            event = "installed" if name == "installed" else "runtime_effective"
            print("LEGACY017_EVIDENCE " + event + " " + json.dumps({
                "extension_id": "org.vllm-hust.prefix-routing", "component": component,
                "observation": name, "pid": os.getpid(),
                "actual_cache_hit_verified": False, "performance_verified": False,
            }), file=sys.stderr, flush=True)


def runtime_status() -> dict:
    with _lock:
        return {"enabled": enabled(), "switches": {c: enabled(c) for c in SWITCHES},
                "counters": {c: dict(v) for c, v in _counts.items()},
                "scope": "current process only", "restart_required_for_disable": True}


def dump_runtime_status() -> None:
    directory = os.getenv(_prefix + "_STATUS_DIR")
    if directory and _counts:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        data = {"pid": os.getpid(), **runtime_status()}
        (target / f"prefix-routing-{os.getpid()}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


atexit.register(dump_runtime_status)
