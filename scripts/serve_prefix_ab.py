"""Own one OFF/ON run: replicas plus the same ingress proxy in both arms.

Ctrl+C stops only processes created here. --dry-run never imports vLLM.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.run_prefix_routing_performance import ServerProcess, _wait_for_health


def validate_profile(profile):
    groups = profile["device_groups"]
    if not 1 <= len(groups) <= 2:
        raise ValueError("one negative-control replica or two independent replicas required")
    devices = []
    for group in groups:
        values = group.split(",")
        if len(values) != profile["tensor_parallel_size"] or any(not x.isdigit() for x in values):
            raise ValueError("device group must contain exactly TP distinct logical device IDs")
        devices.extend(values)
    split = profile.get("placement") == "two-containers"
    if split:
        if len(groups) != 2 or len(profile.get("hosts", [])) != 2:
            raise ValueError("two-containers requires exactly two hosts/replicas")
        for host in profile["hosts"]:
            if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for c in host):
                raise ValueError("hosts must be IPv4 addresses or DNS names, without scheme/port")
    if (not split and len(devices) != len(set(devices))) or any(len(g.split(',')) != len(set(g.split(','))) for g in groups):
        raise ValueError("replicas must not share devices")
    if profile["negative_control"] != (len(groups) == 1):
        raise ValueError("single replica must be labelled negative_control")
    if not 0 < profile["gpu_memory_utilization"] < 1:
        raise ValueError("invalid KV/memory fraction")
    return len(devices)


def make_plan(profile, arm, model, out, base_port=18180):
    chips = validate_profile(profile)
    groups = profile["device_groups"]
    split = profile.get("placement") == "two-containers"
    token = os.getenv("PREFIX_ROUTING_TOKEN", "DRY_RUN_TOKEN_NOT_FOR_USE") if split else secrets.token_urlsafe(32)
    socket_dir = Path("/tmp") / ("pr173-" + secrets.token_hex(6))
    routing = {"nodes": [], "routing_token": token, "event_sync_interval": 1.0,
               "event_replay_timeout": 2.0, "request_timeout": 1800.0}
    for i in range(len(groups)):
        host = profile["hosts"][i] if split else "127.0.0.1"
        http_port = base_port+1 if split else base_port+1+i
        node = {"id": f"node{i}", "data_parallel_rank": 0, "local": i == 0,
                "event_endpoint": f"tcp://{host}:19557" if split else f"ipc://{socket_dir}/pub{i}",
                "replay_endpoint": f"tcp://{host}:19558" if split else f"ipc://{socket_dir}/replay{i}"}
        if i:
            node.update(url=f"http://{host}:{http_port}", routing_token=token)
        routing["nodes"].append(node)
    common = {"VLLM_HUST_UTILITY_VICTIM_ENABLE": "0", "VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH": "1",
              "VLLM_HUST_UTILITY_VICTIM_EVIDENCE": "1", "VLLM_HUST_PREFIX_ROUTING_BACKEND": "runtime023-v1",
              "VLLM_HUST_PREFIX_ROUTING_ENABLE": "1", "VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH": "0" if arm == "on" else "1",
              "VLLM_HUST_PREFIX_ROUTING_ROUTING": "1", "VLLM_HUST_PREFIX_ROUTING_ZMQ_REPLAY": "1",
              "VLLM_HUST_PREFIX_ROUTING_HTTP_EVENTS": "0", "VLLM_HUST_PREFIX_ROUTING_EVIDENCE": "1",
              "VLLM_ASCEND_BALANCE_SCHEDULING": "0", "PYTHONHASHSEED": "0"}
    plans = []
    for i, group in enumerate(groups):
        host = profile["hosts"][i] if split else "127.0.0.1"
        http_port = base_port+1 if split else base_port+1+i
        env = {**common, "ASCEND_RT_VISIBLE_DEVICES": group,
               "VLLM_HUST_PREFIX_ROUTING_CONFIG": json.dumps(routing) if i == 0 else "",
               "VLLM_HUST_PREFIX_ROUTING_STATUS_DIR": str(out / f"node{i}-counters")}
        cmd = ["vllm-hust-ext", "run", "--", "vllm", "serve", str(model),
               "--served-model-name", profile["served_model_name"], "--host", "0.0.0.0" if split else "127.0.0.1",
               "--port", str(http_port), "--dtype", "bfloat16", "--kv-cache-dtype", "auto",
               "--block-size", str(profile["block_size"]), "--tensor-parallel-size", str(profile["tensor_parallel_size"]),
               "--pipeline-parallel-size", "1", "--data-parallel-size", "1",
               "--max-model-len", str(profile["max_model_len"]),
               "--gpu-memory-utilization", str(profile["gpu_memory_utilization"]),
               "--max-num-seqs", str(profile["max_num_seqs"]), "--max-num-batched-tokens", "8192",
               "--enable-prefix-caching", "--prefix-caching-hash-algo", "sha256",
               "--enable-chunked-prefill", "--no-async-scheduling", "--seed", "0", "--scheduling-policy", "fcfs",
               "--distributed-executor-backend", "mp", "--disable-custom-all-reduce",
               "--no-trust-remote-code", "--load-format", "auto", "--no-enable-log-requests",
               "--uvicorn-log-level", "info",
               "--compilation-config", '{"mode":3,"cudagraph_mode":"FULL_DECODE_ONLY"}',
               "--cudagraph-capture-sizes", "1", "2", "4", "8", "16",
               "--kv-events-config", json.dumps({"enable_kv_cache_events": True, "publisher": "zmq",
                     "endpoint": "tcp://*:19557" if split else f"ipc://{socket_dir}/pub{i}",
                     "replay_endpoint": "tcp://*:19558" if split else f"ipc://{socket_dir}/replay{i}"})]
        if profile["expert_parallel"]:
            cmd += ["--enable-expert-parallel"]
        if profile.get("mamba_cache_mode"):
            cmd += ["--mamba-cache-mode", profile["mamba_cache_mode"]]
        cmd += ["--middleware", "vllm_hust_prefix_routing.adapters.core.runtime023.PrefixRoutingASGIMiddleware"]
        plans.append({"node": i, "command": cmd, "environment": env,
                      "url": f"http://{host}:{http_port}", "health_url": f"http://127.0.0.1:{http_port}"})
    upstreams = [p["url"] for p in plans] if arm != "on" else [plans[0]["url"]]
    # The unmodified historical proxy requires two arguments; duplicating one
    # URL is an equivalent single-upstream relay (not a second engine replica).
    proxy_urls = upstreams if len(upstreams) == 2 else upstreams * 2
    proxy = [sys.executable, str(ROOT / "benchmarks/prefix_routing_random_proxy.py"),
             "--port", str(base_port), "--stats-file", str(out / "ingress-stats.json"), "--seed", "0"]
    for url in proxy_urls:
        proxy += ["--upstream", url]
    return {"arm": arm, "profile": profile, "chips": chips, "replicas": len(groups),
            "endpoint": f"http://127.0.0.1:{base_port}", "servers": plans,
            "proxy_command": proxy, "negative_control": len(groups) == 1,
            "performance_verified": False, "socket_dir": str(socket_dir)}


def public_plan(plan):
    copy = json.loads(json.dumps(plan))
    for server in copy["servers"]:
        raw = server["environment"].get("VLLM_HUST_PREFIX_ROUTING_CONFIG")
        if raw:
            config = json.loads(raw)
            config["routing_token"] = "REDACTED"
            for node in config["nodes"]:
                if "routing_token" in node: node["routing_token"] = "REDACTED"
            server["environment"]["VLLM_HUST_PREFIX_ROUTING_CONFIG"] = json.dumps(config)
        cmd = server["command"]
        if "--prefix-routing-config" in cmd:
            idx = cmd.index("--prefix-routing-config") + 1
            config = json.loads(cmd[idx])
            config["routing_token"] = "REDACTED"
            for node in config["nodes"]:
                if "routing_token" in node: node["routing_token"] = "REDACTED"
            cmd[idx] = json.dumps(config)
    return copy


def check_weight_budget(model, profile):
    """Conservative weight-only check; does not assert runtime capacity."""
    if "max_weight_gib" not in profile:
        return None
    indexes = list(model.glob("*.safetensors.index.json")) + list(model.glob("pytorch_model.bin.index.json"))
    size = None
    if len(indexes) == 1:
        size = json.loads(indexes[0].read_text(encoding="utf-8")).get("metadata", {}).get("total_size")
    if size is None:
        files = list(model.glob("*.safetensors")) or list(model.glob("pytorch_model*.bin"))
        if files: size = sum(p.stat().st_size for p in files)
    if not isinstance(size, int) or size <= 0:
        raise RuntimeError("Cannot verify single-card weight size; supply a complete local model checkpoint")
    budget = profile["max_weight_gib"] * 1024**3
    if size > budget:
        raise RuntimeError(f"Weights {size/1024**3:.1f} GiB exceed this TP1 profile's {profile['max_weight_gib']} GiB weight budget; BF16 Qwen35 requires TP2")
    return {"checkpoint_weight_bytes": size, "capacity_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=["off", "on", "kill"])
    parser.add_argument("model", type=Path)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18180)
    parser.add_argument("--startup-timeout", type=float, default=1200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--node", type=int, choices=[0, 1], help="local node for the two-containers profile")
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    plan = make_plan(profile, args.arm, args.model.resolve(), args.output.resolve(), args.port)
    if args.dry_run:
        print(json.dumps(public_plan(plan), indent=2)); return
    split = profile.get("placement") == "two-containers"
    if split and (args.node is None or "CONTAINER_A_IP" in profile["hosts"] or "CONTAINER_B_IP" in profile["hosts"] or len(os.getenv("PREFIX_ROUTING_TOKEN", "")) < 32):
        parser.error("two-containers needs --node, actual hosts and shared PREFIX_ROUTING_TOKEN (>=32 chars)")
    if not split and args.node is not None:
        parser.error("--node only applies to two-containers")
    if not args.model.is_dir():
        parser.error("model directory does not exist")
    weight_check = check_weight_budget(args.model, profile)
    env = {**os.environ, **plan["servers"][0]["environment"]}
    os.environ.update(plan["servers"][0]["environment"])
    from vllm_hust_prefix_routing.adapters.core.runtime023 import check_host
    spec = importlib.util.find_spec("vllm")
    if spec is None or spec.origin is None:
        raise RuntimeError("serving Python cannot find vLLM")
    receipt = check_host(Path(spec.origin).resolve().parent.parent)
    if not receipt["compatible"]:
        raise RuntimeError(json.dumps(receipt))
    ext = shutil.which("vllm-hust-ext")
    if ext is None: raise RuntimeError("vllm-hust-ext is not installed")
    listing = subprocess.check_output([ext, "extension", "list"], text=True, env=env)
    if "org.vllm-hust.utility-victim " in listing:
        subprocess.run([ext, "extension", "disable", "org.vllm-hust.utility-victim"], check=True, env=env)
    subprocess.run([ext, "extension", "enable", "org.vllm-hust.prefix-routing"], check=True, env=env)
    ports = ([args.port+1, 19557, 19558] + ([args.port] if args.node == 0 else [])) if split else [args.port, *[args.port+1+i for i in range(plan["replicas"])]]
    if len(ports) != len(set(ports)): raise RuntimeError("port ranges overlap")
    for port in ports:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if os.name=='nt' else socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(1)
            except OSError as exc:
                raise RuntimeError(f"Port {port} is unavailable for a new listener: {exc}") from exc
    args.output.mkdir(parents=True, exist_ok=False)
    socket_dir = Path(plan["socket_dir"])
    socket_dir.mkdir(mode=0o700, exist_ok=False)
    metadata = public_plan(plan)
    metadata["local_node"] = args.node
    metadata["host_contract"] = receipt
    metadata["started_at_unix"] = time.time()
    metadata["model_revision"] = None
    metadata["weight_check"] = weight_check
    (args.output / "server-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (args.output / "extensions.txt").write_text(subprocess.check_output([ext,"extension","list"],text=True,env=env),encoding="utf-8")
    servers = []
    stopping = False
    def stop_signal(signum, frame):
        nonlocal stopping
        if stopping: return
        stopping = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        for node in reversed(plan["servers"]):
            if split and node["node"] != args.node:
                continue
            server = ServerProcess(node["command"], {**env, **node["environment"]}, args.output / f"node{node['node']}.log")
            servers.append(server)
            server.start()
            _wait_for_health(server, node["health_url"], args.startup_timeout)
        if not split or args.node == 0:
            if split:
                import urllib.request
                with urllib.request.urlopen(plan["servers"][1]["url"] + "/health", timeout=30) as response:
                    if response.status != 200: raise RuntimeError("remote replica is not ready")
            proxy = ServerProcess(plan["proxy_command"], env, args.output / "ingress.log")
            servers.append(proxy)
            proxy.start()
            _wait_for_health(proxy, plan["endpoint"], 30)
        (args.output / "ready.json").write_text(json.dumps({"arm": args.arm, "ready": True, "endpoint": plan["endpoint"]}),encoding="utf-8")
        print(f"READY {plan['endpoint']} arm={args.arm} chips={plan['chips']} replicas={plan['replicas']} negative_control={plan['negative_control']}", flush=True)
        while True:
            for server in servers: server.assert_running()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for server in reversed(servers): server.stop(timeout=120)
        metadata["stopped_at_unix"] = time.time()
        metadata["child_exit_codes"] = [s.process.returncode if s.process is not None else None for s in servers]
        metadata["forced_kill_observed"] = -9 in metadata["child_exit_codes"]
        (args.output / "server-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        ready = args.output / "ready.json"
        if ready.exists(): ready.unlink()
        for item in socket_dir.iterdir():
            item.unlink()
        socket_dir.rmdir()


if __name__ == "__main__":
    main()
