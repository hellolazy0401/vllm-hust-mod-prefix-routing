"""Client B: fixed prefix-repetition gate or existing SWE exact-token client."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request


def snapshot(metadata, target, label):
    for node in metadata["servers"]:
        with urllib.request.urlopen(node["url"] + "/metrics", timeout=15) as response:
            (target / f"node{node['node']}.{label}.prom").write_bytes(response.read())
    with urllib.request.urlopen(metadata["endpoint"] + "/_prefix_routing_benchmark/stats", timeout=15) as response:
        (target / f"ingress.{label}.json").write_bytes(response.read())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--kind", choices=["prefix", "swe"], default="prefix")
    p.add_argument("--label", default="formal")
    p.add_argument("--num-prompts", type=int, default=400)
    p.add_argument("--num-prefixes", type=int, default=200)
    p.add_argument("--prefix-len", type=int, default=8192,
                   help="Shared tokens; must cover actual runtime cache blocks (Ascend may enlarge them)")
    p.add_argument("--suffix-len", type=int, default=64)
    p.add_argument("--output-len", type=int, default=64)
    p.add_argument("--duration", type=int, default=900)
    p.add_argument("--workload", type=Path)
    p.add_argument("--swe-client", type=Path)
    args = p.parse_args()
    if args.concurrency < 1 or args.duration < 1 or args.num_prompts < 1 or args.num_prefixes < 1:
        p.error("counts and duration must be positive")
    if args.num_prompts % args.num_prefixes:
        p.error("num-prompts must divide evenly by num-prefixes")
    if min(args.prefix_len, args.suffix_len, args.output_len) < 1:
        p.error("prefix/suffix/output lengths must be positive")
    if Path(args.label).name != args.label or args.label in (".", ".."):
        p.error("label must be a simple filename")
    metadata_path = args.run_dir / "server-metadata.json"
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    if meta.get("local_node") == 1:
        p.error("run the client on container A (node0), where ingress is listening")
    if not (args.run_dir / "ready.json").exists():
        p.error("server is not marked ready; check terminal A")
    for node in meta["servers"]:
        env = node["environment"]
        if env["VLLM_HUST_UTILITY_VICTIM_ENABLE"] != "0" or env["VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH"] != "1":
            p.error("utility-victim must remain OFF")
    profile = meta["profile"]
    model = meta["servers"][0]["command"][5]
    out = args.run_dir / f"{args.kind}-{args.label}"
    if args.kind == "prefix":
        if args.prefix_len + args.suffix_len + args.output_len > profile["max_model_len"]:
            p.error("prefix + suffix + output exceeds profile max_model_len; adjust lengths")
        cmd = [str(Path(sys.executable).with_name("vllm")), "bench", "serve", "--backend", "openai",
               "--base-url", meta["endpoint"], "--endpoint", "/v1/completions",
               "--model", profile["served_model_name"], "--tokenizer", model,
               "--dataset-name", "prefix_repetition", "--num-prompts", str(args.num_prompts),
               "--prefix-repetition-prefix-len", str(args.prefix_len), "--prefix-repetition-suffix-len", str(args.suffix_len),
               "--prefix-repetition-output-len", str(args.output_len), "--prefix-repetition-num-prefixes", str(args.num_prefixes),
               "--request-rate", "inf", "--max-concurrency", str(args.concurrency), "--seed", "0",
               "--ignore-eos", "--percentile-metrics", "ttft,tpot,itl,e2el", "--metric-percentiles", "50,95,99",
               "--save-result", "--result-dir", str(out), "--result-filename", "result.json"]
    else:
        if not args.workload or not args.swe_client or not args.workload.is_file() or not args.swe_client.is_file():
            p.error("SWE requires an existing --workload and --swe-client")
        cmd = [str(args.swe_client), "run", "--workload", str(args.workload),
               "--endpoint", meta["endpoint"] + "/v1/completions", "--model", profile["served_model_name"],
               "--server-max-context", str(profile["max_model_len"]), "--concurrency", str(args.concurrency),
               "--duration", str(args.duration), "--chips", str(meta["chips"]), "--seed", "0",
               "--server-metadata", str(metadata_path), "--output", str(out / "measurement")]
        # Deliberately omit native DP-affinity: these are independent DP1 replicas.
    out.mkdir(parents=True, exist_ok=False)
    receipt = {"command": cmd, "arm": meta["arm"], "concurrency": args.concurrency,
               "negative_control": meta["negative_control"], "chip_count": meta["chips"],
               "server_metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest()}
    if args.workload:
        receipt["workload_sha256"] = hashlib.sha256(args.workload.read_bytes()).hexdigest()
    (out / "invocation.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    snapshot(meta, out, "before")
    result_code = 1
    try:
        with (out / "client.log").open("w", encoding="utf-8") as log:
            result_code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    finally:
        (out / "exit-code.txt").write_text(str(result_code) + "\n", encoding="utf-8")
        snapshot(meta, out, "after")
    print(f"client exit={result_code}; results: {out}")
    raise SystemExit(result_code)


if __name__ == "__main__":
    main()
