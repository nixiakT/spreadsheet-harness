#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

hard_root="${HARD_ROOT:-benchmarks/results/coevolution-hard-v07-deepseekpro-fsmfix-20260911}"
cache_root="${CACHE_ROOT:-benchmarks/data/normalized-harbor}"
skill_base="${SKILL_ROOT_BASE:-tmp/coevolution-roots-deepseek-20260910}"
base_url="${BASE_URL:-http://10.130.138.46:8010/v1}"
api_key_file="${API_KEY_FILE:-/tmp/spreadsheet-harness-litellm.key}"
model="${MODEL:-DeepSeek-V4-Pro}"
parallelism="${PARALLELISM:-2}"
task_attempts="${TASK_ATTEMPTS:-2}"
max_calls="${MAX_MODEL_CALLS:-30}"
max_per_complexity="${MAX_PER_COMPLEXITY:-20}"
dataset_filter="${HARD_DATASETS:-both}"
request_interval="${REQUEST_INTERVAL_SECONDS:-0.8}"
script_path="$(realpath "$0")"
status_file="$hard_root/batch-status.tsv"

[[ "$parallelism" =~ ^[1-9][0-9]*$ ]] || { echo "PARALLELISM must be positive" >&2; exit 2; }
[[ "$task_attempts" =~ ^[1-9][0-9]*$ ]] || { echo "TASK_ATTEMPTS must be positive" >&2; exit 2; }
[[ "$max_calls" =~ ^[1-9][0-9]*$ ]] || { echo "MAX_MODEL_CALLS must be positive" >&2; exit 2; }
[[ "$max_per_complexity" =~ ^[1-9][0-9]*$ ]] || { echo "MAX_PER_COMPLEXITY must be positive" >&2; exit 2; }
[[ -r "$api_key_file" ]] || { echo "Missing readable API key file: $api_key_file" >&2; exit 2; }

summary_complete() {
  local summary="$1"
  [[ -f "$summary" ]] || return 1
  .venv/bin/python - "$summary" <<'PY'
import json, sys
try:
    document = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if document.get("study_complete") else 1)
PY
}

record_status() {
  local dataset_label="$1" variant="$2" complexity="$3" task="$4" status="$5" code="$6" attempt="$7"
  (
    flock 9
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -Is)" "$dataset_label" "$variant" "$complexity" "$task" "$status" "$code" "$attempt" >> "$status_file"
  ) 9>"$status_file.lock"
}

archive_incomplete() {
  local output="$1" log="$2" attempt="$3"
  local archived="${output}.interrupted.$(date +%Y%m%dT%H%M%S).a${attempt}"
  mv "$output" "$archived"
  [[ -f "$log" ]] && mv "$log" "${archived}.log"
}

run_worker() {
  local line="$1"
  local dataset_label dataset variant complexity task
  IFS=$'\t' read -r dataset_label dataset variant complexity task <<< "$line"
  local slug="${task//\//_}"
  local output="$hard_root/$dataset_label/$variant/$slug"
  local log="$hard_root/$dataset_label/$variant/$slug.log"
  local lock="$hard_root/.locks/${dataset_label}-${variant}-${slug}.lock"
  mkdir -p "$(dirname "$output")" "$hard_root/.locks"
  exec 8>"$lock"
  if ! flock -n 8; then
    record_status "$dataset_label" "$variant" "$complexity" "$task" locked 0 0
    return 0
  fi
  if summary_complete "$output/summary.json"; then
    record_status "$dataset_label" "$variant" "$complexity" "$task" skipped-complete 0 0
    return 0
  fi

  local attempt code=2
  for ((attempt = 1; attempt <= task_attempts; attempt++)); do
    [[ -e "$output" ]] && archive_incomplete "$output" "$log" "$attempt"
    if .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
        --dataset "$dataset" --category Financial_Model --task-id "$task" \
        --arm spreadsheet-harness-financial --skill-root "$skill_base/$variant" \
        --output "$output" --max-model-calls "$max_calls" --max-turns-per-arm "$max_calls" \
        --max-total-tokens unlimited --max-output-tokens unlimited \
        --task-timeout 3600 --request-timeout 1800 --litellm-timeout 1800 \
        --request-retries 5 --request-interval-seconds "$request_interval" \
        --arm-order-seed 20260911 --base-url "$base_url" \
        --api-key-file "$api_key_file" --model "$model" \
        --api-protocol chat-completions --reasoning-effort medium --seed 41 \
        --temperature 0 --top-p 1 --enable-thinking > "$log" 2>&1; then
      record_status "$dataset_label" "$variant" "$complexity" "$task" complete 0 "$attempt"
      return 0
    else
      code=$?
    fi
    record_status "$dataset_label" "$variant" "$complexity" "$task" failed-attempt "$code" "$attempt"
  done
  return "$code"
}

if [[ "${1:-}" == "--worker" ]]; then
  run_worker "${2:?worker plan row is required}"
  exit $?
fi

mkdir -p "$hard_root" "$hard_root/.locks"
if [[ ! -f "$status_file" ]]; then
  printf 'timestamp\tdataset\tarm\tcomplexity\ttask\tstatus\texit_code\tattempt\n' > "$status_file"
fi

v06_dataset="$cache_root/v06-financial-269"
enhanced_dataset="$cache_root/v2-enhanced-financial-1565"
plan_file="$hard_root/task-plan.tsv"
.venv/bin/python - "$v06_dataset" "$enhanced_dataset" "$plan_file" "$max_per_complexity" "$dataset_filter" <<'PY'
import json, sys
from collections import defaultdict
from pathlib import Path

filter_value = sys.argv[5]
all_sources = (("v06", Path(sys.argv[1]), ("C3", "C2")), ("enhanced-v2", Path(sys.argv[2]), ("C3", "C2")))
sources = tuple(item for item in all_sources if filter_value in {"both", item[0]})
if not sources:
    raise SystemExit(f"HARD_DATASETS={filter_value!r} selects no dataset")
limit = int(sys.argv[4])
arms = ("h0d0", "h1d0", "h0d1", "h1d1")
rows = []
for label, dataset, complexities in sources:
    payload = json.loads((dataset / "Financial_Model" / "dataset.json").read_text(encoding="utf-8"))
    for complexity in complexities:
        candidates = [item for item in payload if item.get("complexity") == complexity]
        # Round-robin families first: every selected stratum covers distinct
        # source workbooks before using a second task from a family.
        buckets = defaultdict(list)
        for item in sorted(candidates, key=lambda x: str(x.get("id"))):
            buckets[str(item.get("source_workbook", ""))].append(item)
        selected = []
        while len(selected) < min(limit, len(candidates)):
            progressed = False
            for family in sorted(buckets):
                if buckets[family]:
                    selected.append(buckets[family].pop(0))
                    progressed = True
                    if len(selected) >= min(limit, len(candidates)):
                        break
            if not progressed:
                break
        for item in selected:
            task = "Financial_Model/" + str(item["id"])
            for arm in arms:
                rows.append((label, str(dataset), arm, complexity, task))
print(f"planned_runs={len(rows)}")
Path(sys.argv[3]).write_text("".join("\t".join(row) + "\n" for row in rows), encoding="utf-8")
PY

export HARD_ROOT="$hard_root" CACHE_ROOT="$cache_root" SKILL_ROOT_BASE="$skill_base"
export BASE_URL="$base_url" API_KEY_FILE="$api_key_file" MODEL="$model"
export TASK_ATTEMPTS="$task_attempts" MAX_MODEL_CALLS="$max_calls" REQUEST_INTERVAL_SECONDS="$request_interval"
set +e
xargs -P "$parallelism" -d '\n' -I '{}' "$script_path" --worker '{}' < "$plan_file"
xargs_code=$?
set -e
echo "worker batch finished with xargs_code=$xargs_code" >&2

.venv/bin/python - "$plan_file" "$hard_root" <<'PY'
import json, sys
from collections import Counter
from pathlib import Path
plan, root = Path(sys.argv[1]), Path(sys.argv[2])
counts = Counter()
for line in plan.read_text(encoding="utf-8").splitlines():
    dataset, _, _, _, task = line.split("\t")
    output = root / dataset / line.split("\t")[2] / task.replace("/", "_")
    path = output / "summary.json"
    if not path.is_file():
        counts["missing"] += 1
    else:
        summary = json.loads(path.read_text(encoding="utf-8"))
        counts["complete" if summary.get("study_complete") else "incomplete"] += 1
report = {"expected": sum(counts.values()), **dict(sorted(counts.items()))}
(root / "completion-summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, sort_keys=True))
raise SystemExit(0 if report.get("missing", 0) == 0 and report.get("incomplete", 0) == 0 else 2)
PY
