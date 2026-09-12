#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset -a task_args
task_args=()
while IFS= read -r task_id; do
  task_args+=(--task-id "$task_id")
done < <(.venv/bin/python - <<'PY'
import json
from pathlib import Path
manifest = Path("benchmarks/results/spreadsheetbench-v2-qwen36-pilot30-5arm-v32-20260902/manifest.json")
for task in json.loads(manifest.read_text())["tasks"]:
    print(task["task_id"])
PY
)

exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm bare --arm spreadsheet-rl-minimal --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
  --output benchmarks/results/spreadsheetbench-v2-dashscope-qwen3-coder-plus-30-4arm-20260904 \
  "${task_args[@]}" \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 900 --request-timeout 180 \
  --litellm-timeout 180 --request-retries 1 --arm-order-seed 20260904 \
  --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --model dashscope/qwen3-coder-plus --api-protocol chat-completions \
  --reasoning-effort none --seed 41 --temperature 1 --top-p 1 --disable-thinking
