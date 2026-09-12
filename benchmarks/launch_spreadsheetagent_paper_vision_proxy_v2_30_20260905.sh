#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
typeset -a task_args
while IFS= read -r task_id; do task_args+=(--task-id "$task_id"); done < <(.venv/bin/python - <<'PY'
import json
x=json.load(open('benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json'))
for row in x['tasks']: print(row['task_id'])
PY
)
exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 --category Debugging --category Financial_Model --category Template \
  --arm paper-vision --output benchmarks/results/spreadsheetagent-paper-vision-proxy-qwen36-v2-30-20260905 \
  "${task_args[@]}" --max-model-calls 20 --max-turns-per-arm 20 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 1800 --request-timeout 600 --litellm-timeout 600 \
  --request-retries 1 --arm-order-seed 20260904 --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/spreadsheet-harness-litellm.key --model qwen36-35b-a3b \
  --api-protocol chat-completions --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking
