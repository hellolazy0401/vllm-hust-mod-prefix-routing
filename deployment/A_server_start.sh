#!/usr/bin/env bash
set -euo pipefail
: "${MOD:?source deployment/init.sh first}"
ARM=${ARM:-off}
C=${C:-2}
R=${R:-1}
NAME="${ARM}-c${C}-r${R}"
# serve_prefix_ab owns all children; Ctrl+C here stops just this run.
NODE_ARGS=()
if [[ -n "${NODE_ID:-}" ]]; then NODE_ARGS=(--node "$NODE_ID"); fi
bash "$MOD/scripts/start_prefix_ab.sh" "$ARM" "$MODEL_PATH" "$PROFILE" "$RESULTS/$NAME" "${NODE_ARGS[@]}" \
  2>&1 | tee "$RESULTS/${NAME}.supervisor.log"
