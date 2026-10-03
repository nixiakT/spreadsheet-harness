#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

dataset=benchmarks/data/spreadsheetbench-v2
evaluator=/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py
root=benchmarks/results/visual-native-glm51-20260929

[[ -r /tmp/spreadsheet-harness-litellm.key ]] || {
  echo "Missing LiteLLM key file" >&2
  exit 2
}
[[ -r "$evaluator" ]] || {
  echo "Missing visual evaluator: $evaluator" >&2
  exit 2
}

mkdir -p "$root/tasks"
.venv/bin/python - "$dataset" "$root/task-plan.txt" <<'PY'
import json
import sys
from pathlib import Path

dataset = Path(sys.argv[1])
plan = Path(sys.argv[2])
rows = json.loads((dataset / "Visualization" / "dataset.json").read_text(encoding="utf-8"))
if len(rows) != 24:
    raise SystemExit(f"expected 24 Visualization tasks, found {len(rows)}")
plan.write_text(
    "\n".join(f"Visualization/{item.get('id') or item.get('task_id')}" for item in rows) + "\n",
    encoding="utf-8",
)
print(f"planned={len(rows)}")
PY

export dataset evaluator root
xargs -P 1 -I __TASK__ zsh -c '
  set -euo pipefail
  task="$1"
  slug="${task//\//__}"
  slug="${slug// /_}"
  out="$root/tasks/$slug"
  log="$root/tasks/$slug.log"
  if [[ -f "$out/results.json" ]]; then
    exit 0
  fi
  if [[ -e "$out" ]]; then
    echo "refusing to overwrite incomplete output: $out" >&2
    exit 3
  fi
  .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
    --dataset "$dataset" --visual-evaluator "$evaluator" --task-id "$task" \
    --arm spreadsheet-rl-native --output "$out" \
    --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
    --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 \
    --litellm-timeout 600 --request-retries 5 --arm-order-seed 20260929 \
    --base-url http://10.130.138.46:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model dashscope/glm-5.1 --api-protocol chat-completions \
    --reasoning-effort medium --seed 41 --temperature 0 --top-p 1 --enable-thinking \
    >"$log" 2>&1
' _ __TASK__ < "$root/task-plan.txt"

.venv/bin/python - "$root" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
plan = [line.strip() for line in (root / "task-plan.txt").read_text().splitlines() if line.strip()]
complete = []
missing = []
for task in plan:
    out = root / "tasks" / task.replace("/", "__").replace(" ", "_")
    result = out / "results.json"
    if result.is_file():
        complete.append(task)
    else:
        missing.append(task)
summary = {"model": "dashscope/glm-5.1", "arm": "spreadsheet-rl-native",
           "expected": len(plan), "complete": len(complete), "missing": missing}
(root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary, ensure_ascii=False))
raise SystemExit(0 if not missing else 2)
PY
