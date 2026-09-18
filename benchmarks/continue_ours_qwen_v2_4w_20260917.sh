#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

RUN_ROOT="${RUN_ROOT:-benchmarks/results/ours-qwen-v2-run2-20260916}"
MODEL_ROOT="$RUN_ROOT/minimax-m2.7"
PLAN="$RUN_ROOT/qwen3-coder-480b.pending-4w.tsv"
INTERRUPTED_ROOT="$RUN_ROOT/interrupted-attempts-4w"
PARALLELISM="${PARALLELISM:-4}"
VISUAL_EVALUATOR="${VISUAL_EVALUATOR:-/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py}"

[[ "$PARALLELISM" == "4" ]] || { echo "This continuation is pinned to 4 workers" >&2; exit 2; }
[[ -d "$MODEL_ROOT" ]] || { echo "Missing source model root: $MODEL_ROOT" >&2; exit 2; }
[[ -f "$VISUAL_EVALUATOR" ]] || { echo "Missing visualization evaluator: $VISUAL_EVALUATOR" >&2; exit 2; }
[[ "$(sha256sum "$VISUAL_EVALUATOR" | awk '{print $1}')" == \
   "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703" ]] || {
  echo "Visualization evaluator hash mismatch" >&2
  exit 2
}

mkdir -p "$INTERRUPTED_ROOT"

.venv/bin/python - "$MODEL_ROOT" "$PLAN" "$INTERRUPTED_ROOT" <<'PY'
import json
import shutil
import sys
from pathlib import Path

model_root, plan, interrupted = map(Path, sys.argv[1:])
dataset = Path("benchmarks/data/spreadsheetbench-v2")
tasks = []
for category in ("Debugging", "Financial_Model", "Template", "Visualization"):
    payload = json.loads((dataset / category / "dataset.json").read_text(encoding="utf-8"))
    for item in sorted(payload, key=lambda value: str(value.get("id") or value.get("task_id"))):
        item_id = str(item.get("id") or item.get("task_id"))
        tasks.append((f"{category}/{item_id}", category))

completed = set()
for result_file in model_root.glob("*/results.json"):
    try:
        payload = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        continue
    rows = payload if isinstance(payload, list) else [payload]
    if any(
        row.get("status") == "completed"
        and row.get("task_id")
        and row.get("arm") == "spreadsheet-harness-financial"
        for row in rows
    ):
        completed.update(
            str(row["task_id"])
            for row in rows
            if row.get("status") == "completed"
            and row.get("task_id")
            and row.get("arm") == "spreadsheet-harness-financial"
        )

pending = [(task, category) for task, category in tasks if task not in completed]
for task, _ in pending:
    slug = task.replace("/", "_")
    task_dir = model_root / slug
    log_file = model_root / f"{slug}.log"
    if task_dir.exists():
        destination = interrupted / slug
        suffix = 1
        while destination.exists():
            suffix += 1
            destination = interrupted / f"{slug}__{suffix}"
        shutil.move(str(task_dir), str(destination))
    if log_file.exists():
        destination = interrupted / log_file.name
        suffix = 1
        while destination.exists():
            suffix += 1
            destination = interrupted / f"{log_file.stem}__{suffix}{log_file.suffix}"
        shutil.move(str(log_file), str(destination))

plan.write_text(
    "".join(f"{task}\t{category}\n" for task, category in pending),
    encoding="utf-8",
)
print(f"completed_preserved={len(completed)} pending={len(pending)} total={len(tasks)}")
PY

export VISUAL_EVALUATOR
xargs -P "$PARALLELISM" -I '{}' bash -c '
  set -euo pipefail
  line="$1"
  task="${line%%$'"'"'\t'"'"'*}"
  category="${line#*$'"'"'\t'"'"'}"
  root="$2"
  slug="${task//\//_}"
  out="$root/$slug"
  log="$root/$slug.log"
  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  provider=(
    --base-url http://10.130.138.46:8010/v1
    --api-key-file /tmp/spreadsheet-harness-litellm.key
    --model dashscope/qwen3-coder-480b-a35b-instruct
    --api-protocol chat-completions
    --reasoning-effort medium
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
    --arm-order-seed 20260908
  )
  if [[ "$category" == Visualization ]]; then
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
      --dataset benchmarks/data/spreadsheetbench-v2 \
      --visual-evaluator "$VISUAL_EVALUATOR" \
      --task-id "$task" --arm spreadsheet-harness-financial --output "$out" \
      "${resources[@]}" "${provider[@]}" >"$log" 2>&1
  else
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
      --dataset benchmarks/data/spreadsheetbench-v2 \
      --category "$category" --task-id "$task" \
      --arm spreadsheet-harness-financial --output "$out" \
      "${resources[@]}" "${provider[@]}" >"$log" 2>&1
  fi
' _ '{}' "$MODEL_ROOT" < "$PLAN"

echo "Qwen V2 four-worker continuation finished"
