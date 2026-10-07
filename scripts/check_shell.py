"""Linux syntax-only validation of delivered shell entrypoints."""
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
paths = [*ROOT.glob("deployment/*.sh"), *ROOT.glob("scripts/start*prefix*.sh")]
for path in paths:
    if b'\r' in path.read_bytes():
        raise RuntimeError(f"Shell script contains CR/CRLF: {path}")
    subprocess.run(["bash", "-n", str(path)], check=True)
print(f"bash -n PASS: {len(paths)} scripts; no model launched")
