#!/usr/bin/env bash
# Source this file in terminals A and B after adjusting the paths/profile.
export MOD=${MOD:-"$HOME/workspace/prefix-routing-campaign-dev4"}
export MODEL_PATH=${MODEL_PATH:-/models/Qwen3.5-35B-A3B}
# Selected by user: four cards in one container, two TP2 replicas.
export PROFILE=${PROFILE:-"$MOD/deployment/configs/qwen35-dual.json"}
export CAMPAIGN=${CAMPAIGN:-prefix-routing-source023}
export RESULTS=${RESULTS:-"$HOME/workspace/results/$CAMPAIGN"}
export BENCH=${BENCH:-"$HOME/workspace/swe-prefix-reuse"}
export VLLM_HUST_UTILITY_VICTIM_ENABLE=0
export VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH=1
export VLLM_HUST_UTILITY_VICTIM_EVIDENCE=1
export VLLM_HUST_PREFIX_ROUTING_BACKEND=runtime023-v1
mkdir -p "$RESULTS"
