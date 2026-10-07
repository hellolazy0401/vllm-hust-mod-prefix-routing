#!/usr/bin/env bash
set -euo pipefail
: "${MOD:?source deployment/init.sh first}"
ARM=${ARM:-off}
C=${C:-2}
R=${R:-1}
NAME="${ARM}-c${C}-r${R}"
npu-smi info > "$RESULTS/${NAME}.hardware.txt"
python "$MOD/scripts/run_prefix_client.py" --run-dir "$RESULTS/$NAME" \
  --concurrency "$C" --kind prefix --label formal
python -m json.tool "$RESULTS/$NAME/prefix-formal/result.json"
