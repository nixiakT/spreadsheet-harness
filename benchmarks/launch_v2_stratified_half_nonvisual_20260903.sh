#!/usr/bin/env zsh
set -euo pipefail

# Deterministic half-v2 probe: every other task within each non-visual category.
# This is a resource/protocol probe, not a complete leaderboard submission.
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
# The runner intentionally rejects passing the same credential both through the
# ambient environment and the explicit experiment key file.
unset OPENAI_API_KEY
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset -a task_ids
task_ids=()
while IFS= read -r task_id; do
  task_ids+=("$task_id")
done < <(.venv/bin/python - <<'PY'
import json
from pathlib import Path

root = Path("benchmarks/data/spreadsheetbench-v2")
for category in ("Debugging", "Financial_Model", "Template"):
    rows = json.loads((root / category / "dataset.json").read_text())
    for index, row in enumerate(sorted(rows, key=lambda item: str(item["id"]))):
        if index % 2 == 0:
            print(f"{category}/{row['id']}")
PY
)

typeset -a task_args
task_args=()
for task_id in "${task_ids[@]}"; do
  task_args+=(--task-id "$task_id")
done

exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Debugging --category Financial_Model --category Template \
  --arm bare --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
  --output benchmarks/results/spreadsheetbench-v2-qwen36-stratified-half-nonvisual-v1-20260903 \
  "${task_args[@]}" \
  --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
  --max-output-tokens 8192 --task-timeout 900 --request-timeout 180 \
  --litellm-timeout 180 --request-retries 1 --arm-order-seed 20260903 \
  --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --model qwen36-35b-a3b --api-protocol chat-completions \
  --reasoning-effort none --seed 41 --temperature 1 --top-p 1 --disable-thinking
