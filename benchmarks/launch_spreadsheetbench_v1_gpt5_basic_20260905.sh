#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

exec .venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
  --dataset benchmarks/data/spreadsheetbench_912_v0.1 \
  --output benchmarks/results/spreadsheetbench-v1-openai-gpt5-harness-basic-20260905 \
  --arm spreadsheet-harness-basic \
  --max-model-calls 8 \
  --max-turns-per-arm 8 \
  --max-total-tokens 140000 \
  --max-output-tokens 8192 \
  --task-timeout 1800 \
  --request-timeout 600 \
  --litellm-timeout 600 \
  --request-retries 1 \
  --arm-order-seed 20260905 \
  --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --model openai/gpt-5 \
  --api-protocol chat-completions \
  --reasoning-effort medium \
  --seed 41 \
  --temperature 1 \
  --top-p 1
