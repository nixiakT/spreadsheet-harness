#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

typeset -a task_args
while IFS= read -r task_id; do
  task_args+=(--task-id "$task_id")
done < <(.venv/bin/python - <<'PY'
import json
manifest = json.load(open("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json"))
for row in manifest["tasks"]:
    print(row["task_id"])
PY
)

exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm spreadsheet-harness-basic \
  --output benchmarks/results/sheetcompass-graph-memory-proxy-gpt55-30-20260905 \
  "${task_args[@]}" \
  --max-model-calls 20 --max-turns-per-arm 20 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 1800 --request-timeout 600 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260904 \
  --base-url https://bobdong.cn/v1 \
  --api-key-file /tmp/bobdong-litellm.key \
  --model gpt-5.5-openai-compact --api-protocol chat-completions \
  --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking
