#!/usr/bin/env zsh
set -euo pipefail

v2_pid="${1:?usage: launch_full_v1_after_v2.sh V2_PID_OR_0}"
if [[ "$v2_pid" != 0 ]]; then
  while kill -0 "$v2_pid" 2>/dev/null; do
    sleep 30
  done
fi

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
cd /data/zju-160/tongzeyuan/spreadsheet-harness
exec .venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
  --dataset benchmarks/data/spreadsheetbench_912_v0.1 \
  --output benchmarks/results/spreadsheetbench-v1-qwen36-full-basic-fresh-v2-20260829 \
  --arm bare --arm spreadsheet-harness-basic \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --base-url http://47.96.153.159:8010/v1 --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking
