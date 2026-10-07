#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ARM=${1:-off}
MODEL=${2:-${MODEL_PATH:-/models/Qwen3.5-35B-A3B}}
OUT=${3:-"$HOME/workspace/results/prefix-routing-$ARM-$(date +%Y%m%d-%H%M%S)"}
exec bash "$ROOT/scripts/start_prefix_ab.sh" "$ARM" "$MODEL" \
  "$ROOT/deployment/configs/qwen35-dual.json" "$OUT"
