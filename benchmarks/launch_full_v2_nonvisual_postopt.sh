#!/usr/bin/env zsh
set -euo pipefail

# Queue a post-optimization full non-visual v2 run behind the legacy run.  The
# legacy run is retained as development evidence; this output identity binds
# to the current source manifest and must be scored independently.
legacy_pid="${1:-0}"
if [[ "$legacy_pid" != 0 ]]; then
  while kill -0 "$legacy_pid" 2>/dev/null; do
    sleep 30
  done
fi

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
cd /data/zju-160/tongzeyuan/spreadsheet-harness
exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm bare --arm spreadsheet-harness-basic \
  --output benchmarks/results/spreadsheetbench-v2-qwen36-full-nonvisual-postopt-v15-20260901 \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 900 --request-timeout 180 \
  --litellm-timeout 180 --request-retries 1 --arm-order-seed 20260905 \
  --base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --model qwen36-35b-a3b --api-protocol chat-completions --reasoning-effort none \
  --seed 41 --temperature 1 --top-p 1 --disable-thinking
