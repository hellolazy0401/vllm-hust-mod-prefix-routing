"""Exercise the real manager with a temporary config, never user state."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

IDENTITY = "org.vllm-hust.prefix-routing"
with tempfile.TemporaryDirectory(prefix="prefix-routing-manager-") as directory:
    env = {**os.environ, "VLLM_HUST_EXT_CONFIG": str(Path(directory) / "extensions.json")}
    def run(*args):
        result = subprocess.run([sys.executable, "-c", "from vllm_hust_ext.cli import main; raise SystemExit(main())", "extension", *args],
                                env=env, capture_output=True, text=True, check=True)
        return result.stdout
    observed = []
    for action, state in ((None, "disabled"), ("enable", "enabled"), ("disable", "disabled")):
        if action:
            run(action, IDENTITY)
        listing = run("list")
        line = next(line for line in listing.splitlines() if line.startswith(IDENTITY + " "))
        assert line.endswith(" " + state), line
        observed.append(line)
    print(json.dumps({"passed": True, "states": observed, "host_launch_tested": False}, indent=2))
