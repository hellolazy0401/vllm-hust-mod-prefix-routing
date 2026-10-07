#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ARM=${1:?Usage: start_prefix_ab.sh off|on|kill MODEL PROFILE OUTPUT}
MODEL=${2:?Missing model path}
PROFILE=${3:?Missing profile JSON}
OUTPUT=${4:?Missing output directory (must not exist)}
export VLLM_HUST_UTILITY_VICTIM_ENABLE=0
export VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH=1
export VLLM_HUST_UTILITY_VICTIM_EVIDENCE=1
export VLLM_HUST_PREFIX_ROUTING_BACKEND=runtime023-v1
export VLLM_ASCEND_BALANCE_SCHEDULING=0
shift 4
exec python "$ROOT/scripts/serve_prefix_ab.py" "$ARM" "$MODEL" --profile "$PROFILE" --output "$OUTPUT" "$@"
