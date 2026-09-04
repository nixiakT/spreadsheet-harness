#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
cd /data/zju-160/tongzeyuan/spreadsheet-harness

.venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm bare --arm spreadsheet-harness-basic \
  --output benchmarks/results/spreadsheetbench-v2-qwen36-full-nonvisual-basic-fresh-v4-20260829 \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --base-url http://47.96.153.159:8010/v1 --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking

.venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --visual-evaluator /tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py \
  --arm bare --arm spreadsheet-harness-basic \
  --output benchmarks/results/spreadsheetbench-v2-qwen36-full-visualization-basic-fresh-v4-20260829 \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --base-url http://47.96.153.159:8010/v1 --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking

.venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
  --dataset benchmarks/data/spreadsheetbench_912_v0.1 \
  --output benchmarks/results/spreadsheetbench-v1-qwen36-full-basic-fresh-v4-20260829 \
  --arm bare --arm spreadsheet-harness-basic \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --base-url http://47.96.153.159:8010/v1 --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking

.venv/bin/python -m spreadsheet_harness.cli benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --arm bare --arm ours --composition ours=spreadsheet-harness-basic \
  --output benchmarks/results/spreadsheetbench-verified-qwen36-full-basic-fresh-v4-20260829 \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 120000 \
  --max-output-tokens 4096 --task-timeout 1200 --request-timeout 700 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260829 \
  --circuit-breaker 3 --base-url http://47.96.153.159:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions --seed 41 --temperature 1 --top-p 1 --disable-thinking
