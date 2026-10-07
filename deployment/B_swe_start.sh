#!/usr/bin/env bash
set -euo pipefail
: "${MOD:?source deployment/init.sh first}"
ARM=${ARM:-off}
C=${C:-2}
R=${R:-1}
NAME="${ARM}-c${C}-r${R}"
DURATION=${DURATION:-900}
LABEL=${LABEL:-formal}
npu-smi info > "$RESULTS/${NAME}.hardware.txt"
python "$MOD/scripts/run_prefix_client.py" --run-dir "$RESULTS/$NAME" \
  --concurrency "$C" --kind swe --label "$LABEL" --duration "$DURATION" \
  --swe-client "$BENCH/.venv-bench/bin/swe-prefix-reuse" \
  --workload "${PREPARED_WORKLOAD:-$BENCH/prepared/qwen35.json}"
python -m json.tool "$RESULTS/$NAME/swe-$LABEL/measurement/summary.json"
