#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
root=benchmarks/results/spreadsheet-rl-4b-v2-30-native-20260905-official-checkpoint-20260905
typeset -a task_args
while IFS= read -r task_id; do task_args+=(--task-id "$task_id"); done < <(
  .venv/bin/python - <<'PY'
import json
x=json.load(open('benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json'))
for row in x['tasks']:
 print(row['task_id'])
PY
)
exec .venv/bin/python \
  -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm spreadsheet-rl-native --output "$root" "${task_args[@]}" \
  --max-model-calls 20 --max-turns-per-arm 20 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 1800 --request-timeout 600 \
  --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260904 \
  --base-url http://127.0.0.1:8626/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --model Spreadsheet-RL-4B --api-protocol chat-completions \
  --reasoning-effort medium --seed 41 --temperature 1 --top-p 1
