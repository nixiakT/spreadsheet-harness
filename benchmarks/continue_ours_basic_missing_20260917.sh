#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

VERSION="${1:?usage: $0 v1|v2 MODEL ROOT}"
MODEL="${2:?missing model}"
ROOT="${3:?missing root}"
WORKERS="${WORKERS:-4}"
CONT="$ROOT/continuation-4w"
TASK_ROOT="$CONT/tasks"
PLAN="$CONT/pending.tsv"
INTERRUPTED="$CONT/interrupted"
VISUAL_EVALUATOR="/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py"

[[ "$VERSION" == v1 || "$VERSION" == v2 ]] || { echo "invalid version: $VERSION" >&2; exit 2; }
[[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || { echo "invalid WORKERS: $WORKERS" >&2; exit 2; }
mkdir -p "$TASK_ROOT" "$INTERRUPTED"

.venv/bin/python - "$VERSION" "$ROOT" "$TASK_ROOT" "$PLAN" "$INTERRUPTED" <<'PY'
import json
import shutil
import sys
from pathlib import Path

version, root, task_root, plan, interrupted = sys.argv[1], *map(Path, sys.argv[2:])

def load_rows(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return value if isinstance(value, list) else [value]

completed = set()
if version == "v1":
    tasks = [
        (line.strip(), "v1")
        for line in Path("benchmarks/results/spreadsheetbench-v1-representative-200-seed42/task_ids.txt").read_text().splitlines()
        if line.strip()
    ]
    source_results = list(root.glob("workers/worker-*/results.json")) + list(task_root.glob("*/results.json"))
else:
    tasks = []
    data = Path("benchmarks/data/spreadsheetbench-v2")
    expected = {"Debugging": 100, "Financial_Model": 100, "Template": 97, "Visualization": 24}
    for category, count in expected.items():
        payload = json.loads((data / category / "dataset.json").read_text(encoding="utf-8"))
        if len(payload) != count:
            raise SystemExit(f"{category}: expected {count}, found {len(payload)}")
        for item in sorted(payload, key=lambda value: str(value.get("id") or value.get("task_id"))):
            item_id = str(item.get("id") or item.get("task_id"))
            tasks.append((f"{category}/{item_id}", category))
    source_results = list(root.glob("*/results.json")) + list(task_root.glob("*/results.json"))

for result_file in source_results:
    for row in load_rows(result_file):
        task_id = row.get("task_id")
        if task_id and row.get("arm") == "spreadsheet-harness-basic":
            completed.add(str(task_id))

pending = [(task, category) for task, category in tasks if task not in completed]
for task, _ in pending:
    slug = task.replace("/", "_")
    path = task_root / slug
    if path.exists():
        destination = interrupted / slug
        suffix = 1
        while destination.exists():
            suffix += 1
            destination = interrupted / f"{slug}__{suffix}"
        shutil.move(str(path), str(destination))
    log = task_root / f"{slug}.log"
    if log.exists():
        destination = interrupted / log.name
        suffix = 1
        while destination.exists():
            suffix += 1
            destination = interrupted / f"{log.stem}__{suffix}{log.suffix}"
        shutil.move(str(log), str(destination))

plan.write_text("".join(f"{task}\t{category}\n" for task, category in pending), encoding="utf-8")
print(f"version={version} completed_preserved={len(completed)} pending={len(pending)} total={len(tasks)}", flush=True)
PY

[[ -s "$PLAN" ]] || { echo "Nothing pending"; exit 0; }
if [[ "$VERSION" == v2 ]]; then
  [[ "$(sha256sum "$VISUAL_EVALUATOR" | awk '{print $1}')" == \
     "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703" ]] || {
    echo "Visual evaluator missing or hash mismatch" >&2
    exit 2
  }
fi

export VERSION MODEL TASK_ROOT VISUAL_EVALUATOR
set +e
xargs -P "$WORKERS" -I '{}' bash -c '
  set -euo pipefail
  line="$1"
  task="${line%%$'"'"'\t'"'"'*}"
  category="${line#*$'"'"'\t'"'"'}"
  slug="${task//\//_}"
  out="$TASK_ROOT/$slug"
  log="$TASK_ROOT/$slug.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  provider=(
    --base-url http://10.130.138.46:8010/v1
    --api-key-file /tmp/spreadsheet-harness-litellm.key
    --model "$MODEL"
    --api-protocol chat-completions
    --reasoning-effort medium
    --seed 41
    --temperature 0
    --top-p 1
    --request-timeout 1800
    --litellm-timeout 1800
    --request-retries 5
    --enable-thinking
  )
  resources=(
    --max-model-calls 50
    --max-turns-per-arm 50
    --max-total-tokens 10000000
    --max-output-tokens 8192
    --task-timeout 21600
    --arm-order-seed 20260917
  )
  if [[ "$VERSION" == v1 ]]; then
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
      --dataset benchmarks/data/spreadsheetbench_912_v0.1 \
      --task-id "$task" --arm spreadsheet-harness-basic --output "$out" \
      "${resources[@]}" "${provider[@]}" >"$log" 2>&1
  elif [[ "$category" == Visualization ]]; then
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
      --dataset benchmarks/data/spreadsheetbench-v2 \
      --visual-evaluator "$VISUAL_EVALUATOR" \
      --task-id "$task" --arm spreadsheet-harness-basic --output "$out" \
      "${resources[@]}" "${provider[@]}" >"$log" 2>&1
  else
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
      --dataset benchmarks/data/spreadsheetbench-v2 \
      --category "$category" --task-id "$task" \
      --arm spreadsheet-harness-basic --output "$out" \
      "${resources[@]}" "${provider[@]}" >"$log" 2>&1
  fi
' _ '{}' < "$PLAN"
rc=$?
set -e
echo "continuation finished version=$VERSION model=$MODEL xargs_rc=$rc"
exit 0
