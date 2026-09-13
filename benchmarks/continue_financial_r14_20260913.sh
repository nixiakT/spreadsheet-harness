#!/usr/bin/env bash
set -uo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
root="benchmarks/results/deepseek-v4-flash-harness-financial-thinking-full-r14-20260912/deepseek-v4-flash"
plan="benchmarks/results/deepseek-v4-flash-harness-financial-thinking-full-r14-20260912/deepseek-v4-flash.continue.pending.tsv"

run_one() {
  local line="$1"
  local task="${line%%$'\t'*}"
  local rest="${line#*$'\t'}"
  local category="${rest%%$'\t'*}"
  local out="$root/${task//\//_}"
  local log="$root/${task//\//_}.continue.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  local common=(
    --dataset benchmarks/data/spreadsheetbench-v2
    --task-id "$task"
    --arm spreadsheet-harness-financial
    --output "$out"
    --max-model-calls 50
    --max-turns-per-arm 50
    --max-total-tokens 10000000
    --max-output-tokens 8192
    --task-timeout 21600
    --arm-order-seed 20260908
    --base-url http://47.96.153.159:8010/v1
    --api-key-file /tmp/spreadsheet-harness-litellm.key
    --model DeepSeek-V4-Flash
    --api-protocol chat-completions
    --reasoning-effort medium
    --temperature 0
    --top-p 1
    --request-timeout 700
    --litellm-timeout 600
    --request-retries 5
    --enable-thinking
  )
  if [[ "$category" == Visualization ]]; then
    .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
      --visual-evaluator /tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py \
      "${common[@]}" >"$log" 2>&1
  else
    .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
      --category "$category" "${common[@]}" >"$log" 2>&1
  fi
}

export -f run_one
export root
xargs -P 8 -I '{}' bash -c 'run_one "$1"' _ '{}' < "$plan"
