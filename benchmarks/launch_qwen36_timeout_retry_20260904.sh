#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset parallelism="${PARALLELISM:-3}"
typeset old_root="benchmarks/results/spreadsheetbench-v2-qwen36-35b-a3b-90-3arm-20260904-retry"
typeset root="${RESULT_ROOT:-benchmarks/results/spreadsheetbench-v2-qwen36-35b-a3b-90-3arm-timeout-retry-20260904}"
mkdir -p "$root"
typeset plan="$root/retry_plan.tsv"

.venv/bin/python - "$old_root" > "$plan" <<'PY'
import json, sys
from pathlib import Path
from collections import defaultdict
root = Path(sys.argv[1])
failed = defaultdict(set)
for p in root.glob("*/results.json"):
    try:
        rows = json.loads(p.read_text())
    except Exception:
        continue
    if isinstance(rows, dict): rows = [rows]
    for row in rows:
        if row.get("status") == "error":
            failed[row["task_id"]].add(row["arm"])
for task in sorted(failed):
    print(task + "\t" + ",".join(sorted(failed[task])))
PY

printf '%s\n' "retry_tasks=$(wc -l < "$plan")" "parallelism=$parallelism" "root=$root"

cat "$plan" | xargs -P "$parallelism" -I '{}' zsh -c '
  set -euo pipefail
  line="$1"
  task="${line%%$'"'"'\t'"'"'*}"
  arm_csv="${line#*$'"'"'\t'"'"'}"
  slug="${task//\//_}"
  out="'"$root"'/${slug}"
  log="'"$root"'/${slug}.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  cd /data/zju-160/tongzeyuan/spreadsheet-harness
  category="${task%%/*}"
  arm_args=()
  for arm in ${(s:,:)arm_csv}; do arm_args+=(--arm "$arm"); done
  exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset benchmarks/data/spreadsheetbench-v2 \
    --category "$category" --task-id "$task" \
    "${arm_args[@]}" --output "$out" \
    --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
    --max-output-tokens 8192 --task-timeout 1800 --request-timeout 600 \
    --litellm-timeout 600 --request-retries 1 --arm-order-seed 20260904 \
    --base-url http://47.96.153.159:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model qwen36-35b-a3b --api-protocol chat-completions \
    --reasoning-effort none --seed 41 --temperature 1 --top-p 1 --disable-thinking \
    > "$log" 2>&1
' _ '{}'

echo "timeout retry launch complete: $(wc -l < "$plan") tasks, ${parallelism} concurrent workers"
