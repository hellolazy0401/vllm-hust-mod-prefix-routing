"""Verify bundle hashes and install bundled wheels in a disposable environment."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
if b'\r' in (ROOT / "SHA256SUMS.txt").read_bytes():
    raise RuntimeError("SHA256SUMS.txt must use LF for GNU sha256sum compatibility")
if sys.platform != "win32":
    subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS.txt"], cwd=ROOT, check=True)
for line in (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
    value, name = line.split("  ", 1)
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT): raise RuntimeError("invalid manifest path")
    if hashlib.sha256(path.read_bytes()).hexdigest() != value:
        raise RuntimeError("Hash mismatch: " + name)
with tempfile.TemporaryDirectory(prefix="prefix-offline-") as directory:
    env = Path(directory) / "env"
    venv.EnvBuilder(with_pip=True).create(env)
    python = env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(ROOT / "wheels"),
                    "vllm-hust-ext==0.2.0.dev0", "vllm-hust-prefix-routing==0.1.0.dev4"], check=True)
    subprocess.run([str(python), "-I", str(ROOT / "scripts/verify_wheel.py")], check=True)
    isolated = {**os.environ, "VLLM_HUST_EXT_CONFIG": str(Path(directory) / "settings.json")}
    subprocess.run([str(python), str(ROOT / "scripts/check_manager.py")], env=isolated, check=True)
print("Offline hashes, dependency resolution, default-OFF wheel and manager lifecycle PASS; no vLLM/NPU started")
