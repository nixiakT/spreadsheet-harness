#!/usr/bin/env zsh
set -euo pipefail

typeset model="${MODEL:?MODEL is required}"
typeset slug="${MODEL_SLUG:?MODEL_SLUG is required}"
typeset parallelism="${PARALLELISM:-2}"
typeset root="${RESULT_ROOT:-benchmarks/results/${slug}-30-4arm-20260904}"
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness
mkdir -p "$root"

typeset task_file="$root/task_ids.txt"
.venv/bin/python - > "$task_file" <<'PY'
import json
from pathlib import Path
manifest = json.loads(Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json").read_text())
for task in manifest["tasks"]:
    print(task["task_id"])
PY

printf '%s\n' "model=$model" "task_count=$(wc -l < "$task_file")" "parallelism=$parallelism" "root=$root"
cat "$task_file" | xargs -P "$parallelism" -I '{}' zsh -c '
  set -euo pipefail
  task="$1"; slug="${task//\//_}"
  out="'"$root"'/${slug}"; log="'"$root"'/${slug}.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  cd /data/zju-160/tongzeyuan/spreadsheet-harness
  category="${task%%/*}"
  exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task" \
    --arm bare --arm spreadsheet-rl-minimal --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
    --output "$out" --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
    --max-output-tokens 8192 --task-timeout 1800 --request-timeout 600 --litellm-timeout 600 \
    --request-retries 1 --arm-order-seed 20260904 --base-url http://47.96.153.159:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key --model "'"$model"'" \
    --api-protocol chat-completions --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking \
    > "$log" 2>&1
' _ '{}'
echo "model launch complete: $model"
