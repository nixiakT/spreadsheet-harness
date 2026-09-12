#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY
# The lab environment exports an outbound HTTP proxy.  The LiteLLM service is
# directly reachable from this host; leaving the proxy enabled causes long
# header waits for GLM requests.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset -a task_args
for task_id in \
  Debugging/01_04 Financial_Model/05_04 Template/01_04 \
  Debugging/09_01 Financial_Model/13_05 Template/08_03 \
  Debugging/04_06 Financial_Model/04_01 Template/02_03 \
  Debugging/09_06 Financial_Model/01_01 Template/15_02 \
  Debugging/08_05 Financial_Model/20_02 Template/06_09 \
  Debugging/08_08 Financial_Model/01_02 Template/02_05 \
  Debugging/06_01 Financial_Model/09_01 Template/13_06 \
  Debugging/05_03 Financial_Model/16_04 Template/14_07 \
  Debugging/10_08 Financial_Model/07_01 Template/06_02 \
  Debugging/02_08 Financial_Model/02_02 Template/02_01; do
  task_args+=(--task-id "$task_id")
done

exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm bare --arm spreadsheet-rl-minimal --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
  --output benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904 \
  "${task_args[@]}" \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 900 --request-timeout 180 \
  --litellm-timeout 180 --request-retries 1 --arm-order-seed 20260904 \
  --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --model GLM-5.2 --api-protocol chat-completions \
  --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking
