#!/usr/bin/env zsh
set -euo pipefail

source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd /data/zju-160/tongzeyuan/spreadsheet-harness

typeset parallelism="${PARALLELISM:-2}"
typeset old_root="benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904"
typeset remaining_root="benchmarks/results/spreadsheetbench-v2-glm52-nothinking-remaining-parallel-20260904"
typeset root="${RESULT_ROOT:-benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-timeout-retry-20260904}"
mkdir -p "$root"
typeset plan="$root/retry_plan.tsv"

.venv/bin/python - "$old_root" "$remaining_root" > "$plan" <<'PY'
import json, sys
from pathlib import Path
from collections import defaultdict

old_roots = [Path(sys.argv[1]), Path(sys.argv[2])]
manifest = json.loads((old_roots[0] / "manifest.json").read_text())
expected = {(t["task_id"], arm) for t in manifest["tasks"] for arm in manifest["arms"]}
latest = {}
for root in old_roots:
    for p in root.glob("*/results.json"):
        try: rows = json.loads(p.read_text())
        except Exception: continue
        if isinstance(rows, dict): rows = [rows]
        for row in rows:
            key = (row.get("task_id"), row.get("arm"))
            if key in expected:
                ts = row.get("finished_at") or row.get("started_at", "")
                if key not in latest or ts > (latest[key].get("finished_at") or latest[key].get("started_at", "")):
                    latest[key] = row
retry = defaultdict(set)
for task, arm in sorted(expected):
    if latest.get((task, arm), {}).get("status") != "completed":
        retry[task].add(arm)
for task in sorted(retry):
    print(task + "\t" + ",".join(sorted(retry[task])))
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
    --model GLM-5.2 --api-protocol chat-completions \
    --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking \
    > "$log" 2>&1
' _ '{}'

echo "GLM timeout retry launch complete: $(wc -l < "$plan") tasks, ${parallelism} concurrent workers"
