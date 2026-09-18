#!/usr/bin/env bash
set -euo pipefail

repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
dataset="$repo_root/benchmarks/data/spreadsheetbench_912_v0.1"
output="${1:-$repo_root/benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-full-20260914}"
workers="${V1_WORKERS:-8}"
base_url="${V1_BASE_URL:-http://47.96.153.159:8010/v1}"
api_key_file="${V1_API_KEY_FILE:-/tmp/spreadsheet-harness-litellm.key}"
model="${V1_MODEL:-dashscope/deepseek-v4-flash}"
seed="${V1_SEED:-41}"

[[ -d "$dataset" ]] || { echo "dataset not found: $dataset" >&2; exit 2; }
[[ -r "$api_key_file" ]] || { echo "API key file not readable: $api_key_file" >&2; exit 2; }
[[ ! -e "$output" ]] || { echo "fresh output already exists: $output" >&2; exit 2; }
[[ "$workers" =~ ^[1-9][0-9]*$ ]] || { echo "V1_WORKERS must be positive" >&2; exit 2; }

mkdir -p "$output/shards" "$output/logs"
"$python_bin" - "$dataset" "$output" "$workers" <<'PY'
import json, sys
from pathlib import Path

dataset, output, workers = sys.argv[1:]
rows = json.loads((Path(dataset) / "dataset.json").read_text(encoding="utf-8"))
ids = [str(row["id"]) for row in rows]
if len(ids) != 912 or len(set(ids)) != 912:
    raise SystemExit(f"expected 912 unique V1 task IDs, got {len(ids)}")
root = Path(output)
(root / "task_ids.txt").write_text("".join(f"{task_id}\n" for task_id in ids), encoding="utf-8")
for index in range(int(workers)):
    shard = ids[index::int(workers)]
    (root / "shards" / f"worker-{index + 1}.ids").write_text(
        "".join(f"{task_id}\n" for task_id in shard), encoding="utf-8"
    )
PY

run_worker() {
  local index="$1"
  local shard="$output/shards/worker-${index}.ids"
  local worker_output="$output/shards/worker-${index}"
  local log="$output/logs/worker-${index}.log"
  local -a task_args=()
  while IFS= read -r task_id; do
    [[ -n "$task_id" ]] && task_args+=(--task-id "$task_id")
  done < "$shard"
  "$python_bin" -m spreadsheet_harness.cli benchmark v1-compare \
    --dataset "$dataset" --output "$worker_output" --arm bare \
    "${task_args[@]}" \
    --max-model-calls 50 --max-turns-per-arm 50 \
    --max-total-tokens 10000000 --max-output-tokens 8192 \
    --task-timeout 21600 --arm-order-seed 20260914 \
    --base-url "$base_url" --api-key-file "$api_key_file" --model "$model" \
    --api-protocol chat-completions --reasoning-effort medium \
    --seed "$seed" \
    --temperature 0 --top-p 1 --enable-thinking \
    --request-timeout 1800 --litellm-timeout 1800 --request-retries 5 \
    > "$log" 2>&1
}

pids=()
for index in $(seq 1 "$workers"); do
  run_worker "$index" &
  pids+=("$!")
done
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done

"$python_bin" - "$output" "$workers" "$model" "$seed" <<'PY'
import json, statistics, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
workers = int(sys.argv[2])
model = sys.argv[3]
seed = int(sys.argv[4])
rows = []
for index in range(1, workers + 1):
    path = root / "shards" / f"worker-{index}" / "results.json"
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            rows.extend(loaded)
expected = [line.strip() for line in (root / "task_ids.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
completed = [row for row in rows if row.get("status") == "completed"]
soft = [float(row["soft"]) for row in completed if row.get("soft") is not None]
hard = [float(row["hard"]) for row in completed if row.get("hard") is not None]
def metric(key):
    values = [float(row[key]) for row in completed if row.get(key) is not None]
    return statistics.fmean(values) if values else None
def sum_nested(key):
    return sum(float((row.get("budget") or {}).get("used", {}).get(key, 0) or 0) for row in rows)
errors = Counter(str(row.get("error_type")) for row in rows if row.get("error_type"))
summary = {
    "schema_version": "spreadsheetbench-v1-bare-full-sharded-v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "model": model,
    "arm": "bare",
    "generation": {"temperature": 0.0, "top_p": 1.0, "thinking": True, "seed": seed, "max_turns": 50},
    "expected_tasks": len(expected), "recorded_tasks": len(rows),
    "unique_recorded_tasks": len({str(row.get("task_id")) for row in rows}),
    "duplicate_task_records": len(rows) - len({str(row.get("task_id")) for row in rows}),
    "study_complete": len(rows) == len(expected) == 912 and all(row.get("status") == "completed" for row in rows),
    "completed_tasks": len(completed), "not_completed_tasks": len(rows) - len(completed),
    "soft": statistics.fmean(soft) if soft else None,
    "hard": statistics.fmean(hard) if hard else None,
    "completion_rate": len(completed) / len(expected) if expected else None,
    "execution_success_rate": sum(row.get("outcome_kind") == "scored" for row in completed) / len(expected) if expected else None,
    "tokens_total": sum_nested("total_tokens"),
    "model_calls_total": sum_nested("model_calls"),
    "turns_mean_completed": metric("turns"),
    "latency_seconds_total": sum(float(row.get("elapsed_seconds", 0) or 0) for row in rows),
    "latency_seconds_mean_completed": metric("elapsed_seconds"),
    "error_types": dict(sorted(errors.items())),
    "shards": workers,
}
(root / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
PY

(( failed == 0 )) || { echo "one or more V1 workers failed; inspect $output/logs" >&2; exit 1; }
echo "V1 bare full run complete: $output"
