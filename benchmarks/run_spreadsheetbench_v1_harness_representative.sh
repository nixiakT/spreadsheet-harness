#!/usr/bin/env bash
# Run our agent on the fixed 200-task SpreadsheetBench v1 subset.
set -euo pipefail

REPO_ROOT="/data/zju-160/tongzeyuan/spreadsheet-harness"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
DATASET="$REPO_ROOT/benchmarks/data/spreadsheetbench_912_v0.1"
SUBSET_FILE="$REPO_ROOT/benchmarks/results/spreadsheetbench-v1-representative-200-seed42/task_ids.txt"
OUTPUT="$REPO_ROOT/benchmarks/results/spreadsheetbench-v1-harness-representative-200-seed42"
API_BASE_URL="http://10.130.138.46:8010/v1"
API_KEY_FILE="/tmp/spreadsheet-harness-litellm.key"
MODEL="DeepSeek-V4-Flash"
ARM="spreadsheet-harness-financial"
WORKERS=4
MAX_TURNS=50
MAX_MODEL_CALLS=50
MAX_TOTAL_TOKENS=10000000
MAX_OUTPUT_TOKENS=8192
TASK_TIMEOUT=21600
REQUEST_TIMEOUT=1800
LITELLM_TIMEOUT=1800
REQUEST_RETRIES=5
SEED=41
TEMPERATURE=0.0
TOP_P=1.0
RESUME=0

usage() {
  cat <<'EOF'
Usage: benchmarks/run_spreadsheetbench_v1_harness_representative.sh [options]

Runs spreadsheet-harness-financial on the fixed 200-task v1 subset, split into
four parallel workers. Each task retains all v1 sibling cases.

Options: --dataset PATH --subset-file PATH --output PATH --model NAME
  --workers N --max-turns N --temperature X --top-p X --seed N
  --api-base-url URL --api-key-file PATH --resume -h|--help
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset) DATASET="${2:?missing value}"; shift 2 ;;
    --subset-file) SUBSET_FILE="${2:?missing value}"; shift 2 ;;
    --output) OUTPUT="${2:?missing value}"; shift 2 ;;
    --model) MODEL="${2:?missing value}"; shift 2 ;;
    --workers) WORKERS="${2:?missing value}"; shift 2 ;;
    --max-turns) MAX_TURNS="${2:?missing value}"; shift 2 ;;
    --temperature) TEMPERATURE="${2:?missing value}"; shift 2 ;;
    --top-p) TOP_P="${2:?missing value}"; shift 2 ;;
    --seed) SEED="${2:?missing value}"; shift 2 ;;
    --api-base-url) API_BASE_URL="${2:?missing value}"; shift 2 ;;
    --api-key-file) API_KEY_FILE="${2:?missing value}"; shift 2 ;;
    --resume) RESUME=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$WORKERS" =~ ^[1-9][0-9]*$ ]] || die "--workers must be positive"
[[ "$MAX_TURNS" =~ ^[1-9][0-9]*$ ]] || die "--max-turns must be positive"
[[ -d "$DATASET" ]] || die "dataset not found: $DATASET"
[[ -r "$SUBSET_FILE" ]] || die "subset file not readable: $SUBSET_FILE"
[[ -r "$API_KEY_FILE" ]] || die "API key file not readable: $API_KEY_FILE"
DATASET="$(realpath "$DATASET")"
SUBSET_FILE="$(realpath "$SUBSET_FILE")"
OUTPUT="$(realpath -m "$OUTPUT")"
if (( RESUME == 0 )) && [[ -e "$OUTPUT" ]]; then
  die "output exists; use another --output or --resume: $OUTPUT"
fi
mkdir -p "$OUTPUT/workers" "$OUTPUT/logs"

# Validate the frozen list and distribute it round-robin across workers.
"$PYTHON_BIN" - "$SUBSET_FILE" "$OUTPUT/task_ids.txt" "$WORKERS" <<'PY'
import sys
from pathlib import Path

source, destination, workers = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
ids = [line.strip() for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
if len(ids) != 200 or len(ids) != len(set(ids)):
    raise SystemExit(f"subset must contain 200 unique task IDs, found {len(ids)}")
destination.write_text("".join(f"{item}\n" for item in ids), encoding="utf-8")
for index in range(workers):
    shard = ids[index::workers]
    (destination.parent / "workers" / f"worker-{index + 1}.ids").write_text(
        "".join(f"{item}\n" for item in shard), encoding="utf-8"
    )
print(f"validated_tasks={len(ids)} workers={workers}")
PY

run_worker() {
  local worker="$1"
  local shard="$OUTPUT/workers/worker-${worker}.ids"
  local worker_output="$OUTPUT/workers/worker-${worker}"
  local log="$OUTPUT/logs/worker-${worker}.log"
  local -a task_args=() resume_args=() command=()
  local task_id
  while IFS= read -r task_id; do
    [[ -n "$task_id" ]] && task_args+=(--task-id "$task_id")
  done < "$shard"
  if (( RESUME == 1 )); then
    resume_args=(--resume --seal-interrupted-current)
  fi
  command=(
    "$PYTHON_BIN" -m spreadsheet_harness.cli benchmark v1-compare
    --dataset "$DATASET" --output "$worker_output" --arm "$ARM"
    "${task_args[@]}"
    --max-model-calls "$MAX_MODEL_CALLS" --max-turns-per-arm "$MAX_TURNS"
    --max-total-tokens "$MAX_TOTAL_TOKENS" --max-output-tokens "$MAX_OUTPUT_TOKENS"
    --task-timeout "$TASK_TIMEOUT" --arm-order-seed 20260913
    --base-url "$API_BASE_URL" --api-key-file "$API_KEY_FILE" --model "$MODEL"
    --api-protocol chat-completions --reasoning-effort medium
    --seed "$SEED" --temperature "$TEMPERATURE" --top-p "$TOP_P"
    --request-timeout "$REQUEST_TIMEOUT" --litellm-timeout "$LITELLM_TIMEOUT"
    --request-retries "$REQUEST_RETRIES" "${resume_args[@]}" --enable-thinking
  )
  (cd "$REPO_ROOT" && "${command[@]}" >"$log" 2>&1)
}

pids=()
for ((worker = 1; worker <= WORKERS; worker++)); do
  run_worker "$worker" &
  pids+=("$!")
done
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done

"$PYTHON_BIN" - "$OUTPUT" "$WORKERS" "$MODEL" "$ARM" "$DATASET" "$SUBSET_FILE" "$MAX_TURNS" "$TEMPERATURE" "$TOP_P" "$SEED" <<'PY'
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

output, workers, model, arm, dataset, subset, max_turns, temperature, top_p, seed = sys.argv[1:]
root = Path(output)
rows = []
worker_summaries = []
for index in range(1, int(workers) + 1):
    worker_root = root / "workers" / f"worker-{index}"
    result_path = worker_root / "results.json"
    summary_path = worker_root / "summary.json"
    if result_path.is_file():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                rows.extend(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    worker_summary = None
    if summary_path.is_file():
        try:
            worker_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    worker_summaries.append({"worker": index, "summary": worker_summary})
ids = [str(row.get("task_id")) for row in rows if isinstance(row, dict)]
expected = [line.strip() for line in (root / "task_ids.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
scored = [row for row in rows if isinstance(row, dict) and row.get("status") == "completed"]
soft = [float(row["soft"]) for row in scored if row.get("soft") is not None]
hard = [float(row["hard"]) for row in scored if row.get("hard") is not None]
aggregate = {
    "schema_version": "spreadsheetbench-v1-harness-sharded-run-v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "dataset": str(dataset), "subset_file": str(subset),
    "expected_tasks": len(expected), "recorded_tasks": len(set(ids)),
    "duplicate_task_records": len(ids) - len(set(ids)), "workers": int(workers),
    "model": model, "arm": arm,
    "generation": {"max_turns": int(max_turns), "temperature": float(temperature), "top_p": float(top_p), "seed": int(seed), "thinking": True},
    "scored_tasks": len(scored), "not_scored_tasks": len(rows) - len(scored),
    "soft_mean_scored": statistics.fmean(soft) if soft else None,
    "hard_mean_scored": statistics.fmean(hard) if hard else None,
    "complete": len(set(ids)) == len(expected) == 200 and not (set(expected) - set(ids)),
    "worker_summaries": worker_summaries,
}
(root / "aggregate_summary.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(aggregate, ensure_ascii=False))
PY

if (( failed != 0 )); then
  echo "One or more workers failed; inspect $OUTPUT/logs and $OUTPUT/aggregate_summary.json" >&2
  exit 1
fi
echo "v1 representative harness run complete: $OUTPUT"
