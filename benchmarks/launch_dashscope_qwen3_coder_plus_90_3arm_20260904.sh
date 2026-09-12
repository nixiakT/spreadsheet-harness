#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset parallelism="${PARALLELISM:-8}"
typeset model="${MODEL:-dashscope/qwen3-coder-plus}"
typeset root="${RESULT_ROOT:-benchmarks/results/spreadsheetbench-v2-dashscope-qwen3-coder-plus-90-3arm-20260904}"
mkdir -p "$root"

typeset task_file="${root}/task_ids.txt"
.venv/bin/python - > "$task_file" <<'PY'
import json
from pathlib import Path

for category in ("Debugging", "Financial_Model", "Template"):
    rows = json.loads((Path("benchmarks/data/spreadsheetbench-v2") / category / "dataset.json").read_text())
    ids = sorted(str(row.get("id") or row.get("task_id")) for row in rows)[:30]
    for item_id in ids:
        print(f"{category}/{item_id}")
PY

printf '%s\n' "task_count=$(wc -l < "$task_file")" "parallelism=$parallelism" "root=$root"

cat "$task_file" | xargs -P "$parallelism" -I '{}' zsh -c '
  set -euo pipefail
  task="$1"
  slug="${task//\//_}"
  out="${RESULT_ROOT}/${slug}"
  log="${RESULT_ROOT}/${slug}.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  cd /data/zju-160/tongzeyuan/spreadsheet-harness
  category="${task%%/*}"
  exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset benchmarks/data/spreadsheetbench-v2 \
    --category "$category" \
    --task-id "$task" \
    --arm bare --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
    --output "$out" \
    --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
    --max-output-tokens 8192 --task-timeout 900 --request-timeout 180 \
    --litellm-timeout 180 --request-retries 1 --arm-order-seed 20260904 \
    --base-url http://47.96.153.159:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model "${MODEL}" --api-protocol chat-completions \
    --reasoning-effort none --seed 41 --temperature 1 --top-p 1 --disable-thinking \
    > "$log" 2>&1
' _ '{}'

echo "parallel task launch complete: $(wc -l < "$task_file") tasks, ${parallelism} concurrent workers"
