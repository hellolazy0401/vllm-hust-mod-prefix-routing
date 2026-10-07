"""Summarize per-process evidence after shutdown. Missing is never zero."""
import argparse
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    a = p.parse_args()
    meta = json.loads((a.run_dir / "server-metadata.json").read_text(encoding="utf-8"))
    events, counters, utility = [], [], []
    for path in a.run_dir.glob("node*.log"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "LEGACY017_EVIDENCE" in line:
                if "org.vllm-hust.prefix-routing" in line: events.append(line)
                elif "utility" in line.lower(): utility.append(line)
    for path in a.run_dir.glob("node*-counters/*.json"):
        counters.append({"file": str(path), "data": json.loads(path.read_text(encoding="utf-8"))})
    report = {"arm": meta["arm"], "negative_control": meta["negative_control"],
              "utility_events": utility, "prefix_events": events, "process_counters": counters,
              "counters_missing": not counters, "performance_verified": False}
    print(json.dumps(report, indent=2))
    (a.run_dir / "runtime-evidence.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if utility: raise SystemExit("INVALID: utility-victim activity detected")
    if meta["arm"] in ("off", "kill") and (events or counters):
        raise SystemExit("INVALID: Prefix Routing activity present in disabled arm")
    if meta["arm"] == "on" and not any("prefix_hit_decisions" in line for line in events):
        raise SystemExit("NO EFFECTIVE ROUTING MATCH: retain as no-trigger result; do not claim benefit")


if __name__ == "__main__":
    main()
