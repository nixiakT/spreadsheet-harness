#!/usr/bin/env zsh
set -euo pipefail

nonvisual_pid="${1:?usage: launch_full_v2_visual_after_nonvisual.sh NONVISUAL_PID_OR_0}"
if [[ "$nonvisual_pid" != 0 ]]; then
  while kill -0 "$nonvisual_pid" 2>/dev/null; do
    sleep 30
  done
fi

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
cd /data/zju-160/tongzeyuan/spreadsheet-harness
exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --visual-evaluator /tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py \
  --arm bare --arm spreadsheet-harness-basic \
  --output benchmarks/results/spreadsheetbench-v2-qwen36-full-visualization-basic-fresh-v2-20260829 \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --base-url http://47.96.153.159:8010/v1 --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking
