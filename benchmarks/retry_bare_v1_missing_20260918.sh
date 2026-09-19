#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

model_key="${1:?usage: $0 deepseek|qwen}"
workers="${V1_RETRY_WORKERS:-2}"
dataset="benchmarks/data/spreadsheetbench_912_v0.1"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
base_url="http://10.130.138.46:8010/v1"
frozen_source="benchmarks/results/deepseek-v4-flash-harness-v26-basic-v2-p6-20260915/frozen-source"

case "$model_key" in
  deepseek)
    model="DeepSeek-V4-Flash"
    arm_order_seed="20260915"
    root="benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-retry-missing-20260918"
    sources=(
      "benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915"
      "benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-remaining-712-20260915"
    )
    expected_missing=54
    ;;
  qwen)
    model="dashscope/qwen3-coder-480b-a35b-instruct"
    arm_order_seed="20260914"
    root="benchmarks/results/spreadsheetbench-v1-bare-qwen3-coder-480b-retry-missing-20260918"
    sources=(
      "benchmarks/results/spreadsheetbench-v1-bare-qwen3-coder-480b-full-20260915-r3"
    )
    expected_missing=44
    ;;
  *)
    echo "unknown model key: $model_key" >&2
    exit 2
    ;;
esac

[[ "$workers" =~ ^[1-9][0-9]*$ ]] || { echo "V1_RETRY_WORKERS must be positive" >&2; exit 2; }
[[ -d "$dataset" ]] || { echo "dataset missing: $dataset" >&2; exit 2; }
[[ -r "$api_key_file" ]] || { echo "API key file missing: $api_key_file" >&2; exit 2; }
[[ -f "$frozen_source/spreadsheet_harness/cli.py" ]] || {
  echo "frozen 2026-09-15 source missing: $frozen_source" >&2
  exit 2
}
if [[ -e "$root" ]]; then
  [[ "${V1_RETRY_RESUME:-0}" == 1 ]] || {
    echo "retry root already exists; set V1_RETRY_RESUME=1 after auditing it: $root" >&2
    exit 2
  }
  [[ -f "$root/retry_provenance.json" && -f "$root/task_ids.txt" ]] || {
    echo "refusing an unrecognized retry root: $root" >&2
    exit 2
  }
fi

mkdir -p "$root/tasks" "$root/logs"
.venv/bin/python - "$model_key" "$root" "$expected_missing" "${sources[@]}" <<'PY'
import json
import sys
from pathlib import Path

model_key, root, expected_missing, *source_names = sys.argv[1:]
root = Path(root)
expected_missing = int(expected_missing)
rows = {}
source_files = []
for source_name in source_names:
    source = Path(source_name)
    candidates = sorted(source.glob("workers/worker-*/results.json"))
    candidates += sorted(source.glob("shards/worker-*/results.json"))
    if not candidates:
        raise SystemExit(f"no worker results under {source}")
    source_files.extend(str(path) for path in candidates)
    for path in candidates:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise SystemExit(f"expected list in {path}")
        for row in payload:
            task_id = str(row.get("task_id"))
            if task_id in rows:
                raise SystemExit(f"duplicate task ID across frozen sources: {task_id}")
            rows[task_id] = row

if len(rows) != 912:
    raise SystemExit(f"expected 912 unique original rows, got {len(rows)}")

def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)

missing = sorted(
    task_id
    for task_id, row in rows.items()
    if not numeric(row.get("soft")) or not numeric(row.get("hard"))
)
if len(missing) != expected_missing:
    raise SystemExit(f"expected {expected_missing} missing rows, got {len(missing)}")

# These pinned-dataset rows have empty instructions. The runner fails before a
# model request, so retain them as explicit dataset-invalid exclusions.
dataset_invalid = sorted(set(missing) & {"55224", "55457", "55877"})
if dataset_invalid != ["55224", "55457", "55877"]:
    raise SystemExit(f"unexpected dataset-invalid set: {dataset_invalid}")
pending = [task_id for task_id in missing if task_id not in dataset_invalid]

task_ids_path = root / "task_ids.txt"
expected_text = "".join(f"{x}\n" for x in pending)
if task_ids_path.exists() and task_ids_path.read_text(encoding="utf-8") != expected_text:
    raise SystemExit("existing retry task list does not match the frozen missing-row selection")
task_ids_path.write_text(expected_text, encoding="utf-8")
(root / "dataset_invalid.json").write_text(
    json.dumps(
        {
            "task_ids": dataset_invalid,
            "reason": "Pinned SpreadsheetBench-v1 rows have empty instructions; no model request is valid.",
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
(root / "retry_provenance.json").write_text(
    json.dumps(
        {
            "model_key": model_key,
            "original_result_files": source_files,
            "original_unique_rows": len(rows),
            "original_missing_scores": len(missing),
            "dataset_invalid_excluded": dataset_invalid,
            "retry_task_count": len(pending),
            "selection_rule": "retry iff original soft or hard is non-numeric",
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)
print(f"{model_key}: original_missing={len(missing)} dataset_invalid=3 retry={len(pending)}", flush=True)
PY

export model root dataset api_key_file base_url arm_order_seed
export frozen_source
set +e
xargs -P "$workers" -I '{}' bash -c '
  set -euo pipefail
  task_id="$1"
  task_root="$root/tasks/$task_id"
  log="$root/logs/$task_id.log"
  if [[ -f "$task_root/results.json" ]]; then
    if .venv/bin/python - "$task_root/manifest.json" "$task_root/results.json" \
      "$model" "$arm_order_seed" <<'"'"'PY'"'"'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
rows = json.load(open(sys.argv[2], encoding="utf-8"))
row = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
valid = (
    numeric(row.get("soft"))
    and numeric(row.get("hard"))
    and manifest.get("provider", {}).get("model") == sys.argv[3]
    and manifest.get("resources", {}).get("arm_order_seed") == int(sys.argv[4])
    and manifest.get("compositions", {}).get("bare", {}).get("composition_sha256")
        == "f851a48df9cec2072fce722f87dc37b0b30d74332beaf78e70fc1476791b1cb0"
    and manifest.get("evaluator", {}).get("sha256")
        == "4ae77cee8df01d1f34684fceab972810d696886533d33be2e89373de6b4d3de3"
)
raise SystemExit(0 if valid else 1)
PY
    then
      exit 0
    fi
    interrupted="$root/interrupted/$task_id-$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$root/interrupted"
    mv "$task_root" "$interrupted"
  elif [[ -e "$task_root" ]]; then
    interrupted="$root/interrupted/$task_id-$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$root/interrupted"
    mv "$task_root" "$interrupted"
  fi
  PYTHONPATH="$frozen_source" .venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
    --dataset "$dataset" --output "$task_root" --task-id "$task_id" --arm bare \
    --max-model-calls 50 --max-turns-per-arm 50 \
    --max-total-tokens 10000000 --max-output-tokens 8192 \
    --task-timeout 21600 --arm-order-seed "$arm_order_seed" \
    --base-url "$base_url" --api-key-file "$api_key_file" --model "$model" \
    --api-protocol chat-completions --reasoning-effort medium \
    --seed 41 --temperature 0 --top-p 1 --enable-thinking \
    --request-timeout 1800 --litellm-timeout 1800 --request-retries 5 \
    >"$log" 2>&1
' _ '{}' < "$root/task_ids.txt"
rc=$?
set -e

.venv/bin/python - "$root" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected = [line.strip() for line in (root / "task_ids.txt").read_text().splitlines() if line.strip()]
rows = []
for task_id in expected:
    path = root / "tasks" / task_id / "results.json"
    if not path.is_file():
        continue
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list) and len(payload) == 1:
        rows.append(payload[0])
numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
scored = [row for row in rows if numeric(row.get("soft")) and numeric(row.get("hard"))]
summary = {
    "expected_retry_tasks": len(expected),
    "recorded_retry_tasks": len(rows),
    "scored_retry_tasks": len(scored),
    "not_scored_retry_tasks": len(rows) - len(scored),
    "missing_retry_tasks": len(expected) - len(rows),
}
(root / "retry_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
(root / "retry_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary), flush=True)
PY

exit "$rc"
