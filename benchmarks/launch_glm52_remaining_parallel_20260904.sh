#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset parallelism="${PARALLELISM:-8}"
typeset root="benchmarks/results/spreadsheetbench-v2-glm52-nothinking-remaining-parallel-20260904"
mkdir -p "$root"

typeset -a remaining
remaining=()
while IFS= read -r task_id; do
  remaining+=("$task_id")
done < <(.venv/bin/python - <<'PY'
import json
from pathlib import Path
manifest = Path("benchmarks/results/spreadsheetbench-v2-qwen36-pilot30-5arm-v32-20260902/manifest.json")
current = Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/results.json")
rows = json.loads(current.read_text()) if current.exists() else []
counts = {}
for row in rows:
    counts.setdefault(row.get("task_id"), set()).add(row.get("arm"))
arms = {"bare", "spreadsheet-rl-minimal", "spreadsheet-harness-basic", "spreadsheet-harness-financial"}
for task in json.loads(manifest.read_text())["tasks"]:
    task_id = task["task_id"]
    if counts.get(task_id, set()) != arms:
        print(task_id)
PY
)

if (( ${#remaining} == 0 )); then
  echo "no remaining GLM tasks"
  exit 0
fi

printf '%s\n' "${remaining[@]}" | xargs -P "$parallelism" -I '{}' zsh -c '
  set -euo pipefail
  task="$1"
  slug="${task//\//_}"
  out="benchmarks/results/spreadsheetbench-v2-glm52-nothinking-remaining-parallel-20260904/${slug}"
  log="benchmarks/results/spreadsheetbench-v2-glm52-nothinking-remaining-parallel-20260904/${slug}.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  cd /data/zju-160/tongzeyuan/spreadsheet-harness
  exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset benchmarks/data/spreadsheetbench-v2 \
    --category Debugging --category Financial_Model --category Template \
    --task-id "$task" \
    --arm bare --arm spreadsheet-rl-minimal --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
    --output "$out" \
    --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
    --max-output-tokens 8192 --task-timeout 900 --request-timeout 180 \
    --litellm-timeout 180 --request-retries 1 --arm-order-seed 20260904 \
    --base-url http://47.96.153.159:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model GLM-5.2 --api-protocol chat-completions \
    --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking \
    > "$log" 2>&1
' _ '{}'

echo "parallel launch complete: ${#remaining} remaining tasks, ${parallelism} concurrent workers"
