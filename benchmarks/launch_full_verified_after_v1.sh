#!/usr/bin/env zsh
set -euo pipefail

v1_pid="${1:?usage: launch_full_verified_after_v1.sh V1_PID_OR_0}"
if [[ "$v1_pid" != 0 ]]; then
  while kill -0 "$v1_pid" 2>/dev/null; do
    sleep 30
  done
fi

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
cd /data/zju-160/tongzeyuan/spreadsheet-harness
exec .venv/bin/python -m spreadsheet_harness.cli benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --arm bare --arm ours --composition ours=spreadsheet-harness-basic \
  --output benchmarks/results/spreadsheetbench-verified-qwen36-full-basic-fresh-v2-20260829 \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --circuit-breaker 3 --base-url http://47.96.153.159:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking
