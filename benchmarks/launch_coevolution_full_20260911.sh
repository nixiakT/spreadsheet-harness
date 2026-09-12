#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

full_root="${FULL_ROOT:-benchmarks/results/coevolution-full-v07-deepseekpro-fsmfix-20260911}"
cache_root="${CACHE_ROOT:-benchmarks/data/normalized-harbor}"
skill_base="${SKILL_ROOT_BASE:-tmp/coevolution-roots-deepseek-20260910}"
canary_summary="${CANARY_SUMMARY:-benchmarks/results/coevolution-canary-v07-deepseekpro-fsmfix-20260911/h0d1/Financial_Model_fina_Debu_01_c0/summary.json}"
canary_session="${CANARY_SESSION:-coevo_canary_20260911}"
base_url="${BASE_URL:-http://10.130.138.46:8010/v1}"
api_key_file="${API_KEY_FILE:-/tmp/spreadsheet-harness-litellm.key}"
model="${MODEL:-DeepSeek-V4-Pro}"
parallelism="${PARALLELISM:-8}"
task_attempts="${TASK_ATTEMPTS:-2}"
request_interval="${REQUEST_INTERVAL_SECONDS:-0.5}"
script_path="$(realpath "$0")"
status_file="$full_root/batch-status.tsv"

v06_archive="benchmarks/data/SpreadsheetBench-v0.6-Financial_Model-269.tar.gz"
enhanced_archive="benchmarks/data/SpreadsheetBench-v2-enhanced-Financial_Model-1565.tar.gz"
v06_dataset="$cache_root/v06-financial-269"
enhanced_dataset="$cache_root/v2-enhanced-financial-1565"

[[ "$parallelism" =~ ^[1-9][0-9]*$ ]] || {
  echo "PARALLELISM must be a positive integer" >&2
  exit 2
}
[[ "$task_attempts" =~ ^[1-9][0-9]*$ ]] || {
  echo "TASK_ATTEMPTS must be a positive integer" >&2
  exit 2
}
[[ -r "$api_key_file" ]] || {
  echo "Missing readable API key file: $api_key_file" >&2
  exit 2
}

summary_complete() {
  local summary="$1"
  [[ -f "$summary" ]] || return 1
  .venv/bin/python - "$summary" <<'PY'
import json
import sys

try:
    document = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if document.get("study_complete") else 1)
PY
}

record_status() {
  local dataset_label="$1" variant="$2" task="$3" status="$4" code="$5" attempt="$6"
  (
    flock 9
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -Is)" "$dataset_label" "$variant" "$task" "$status" "$code" "$attempt" \
      >> "$status_file"
  ) 9>"$status_file.lock"
}

archive_incomplete() {
  local output="$1" log="$2" attempt="$3"
  local archived="${output}.interrupted.$(date +%Y%m%dT%H%M%S).a${attempt}"
  mv "$output" "$archived"
  if [[ -f "$log" ]]; then
    mv "$log" "${archived}.log"
  fi
}

run_worker() {
  local line="$1"
  local dataset_label dataset variant task
  IFS=$'\t' read -r dataset_label dataset variant task <<< "$line"
  local slug="${task//\//_}"
  local output="$full_root/$dataset_label/$variant/$slug"
  local log="$full_root/$dataset_label/$variant/$slug.log"
  local lock="$full_root/.locks/${dataset_label}-${variant}-${slug}.lock"
  mkdir -p "$(dirname "$output")" "$full_root/.locks"
  exec 8>"$lock"
  if ! flock -n 8; then
    record_status "$dataset_label" "$variant" "$task" locked 0 0
    return 0
  fi
  if summary_complete "$output/summary.json"; then
    record_status "$dataset_label" "$variant" "$task" skipped-complete 0 0
    return 0
  fi

  local attempt code=2
  for ((attempt = 1; attempt <= task_attempts; attempt++)); do
    if [[ -e "$output" ]]; then
      archive_incomplete "$output" "$log" "$attempt"
    fi
    if .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
        --dataset "$dataset" --category Financial_Model --task-id "$task" \
        --arm spreadsheet-harness-financial --skill-root "$skill_base/$variant" \
        --output "$output" --max-model-calls 50 --max-turns-per-arm 50 \
        --max-total-tokens unlimited --max-output-tokens unlimited \
        --task-timeout 3600 --request-timeout 1800 --litellm-timeout 1800 \
        --request-retries 5 --request-interval-seconds "$request_interval" \
        --arm-order-seed 20260911 --base-url "$base_url" \
        --api-key-file "$api_key_file" --model "$model" \
        --api-protocol chat-completions --reasoning-effort medium --seed 41 \
        --temperature 0 --top-p 1 --enable-thinking > "$log" 2>&1; then
      record_status "$dataset_label" "$variant" "$task" complete 0 "$attempt"
      return 0
    else
      code=$?
    fi
    record_status "$dataset_label" "$variant" "$task" failed-attempt "$code" "$attempt"
  done
  return "$code"
}

if [[ "${1:-}" == "--worker" ]]; then
  run_worker "${2:?worker plan row is required}"
  exit $?
fi

mkdir -p "$full_root" "$cache_root" "$full_root/.locks"
if [[ ! -f "$status_file" ]]; then
  printf 'timestamp\tdataset\tarm\ttask\tstatus\texit_code\tattempt\n' > "$status_file"
fi

while [[ ! -f "$canary_summary" ]]; do
  if ! tmux has-session -t "$canary_session" 2>/dev/null; then
    echo "Canary exited without a summary: $canary_summary" >&2
    exit 2
  fi
  sleep 30
done

.venv/bin/python - "$canary_summary" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
summary = json.loads(summary_path.read_text(encoding="utf-8"))
arms = summary.get("arms", {})
if len(arms) != 1:
    raise SystemExit("Canary summary must contain exactly one arm")
arm = next(iter(arms.values()))
trajectory_paths = sorted(summary_path.parent.rglob("trajectory.jsonl"))
if not trajectory_paths:
    raise SystemExit("Canary trajectory is missing")
events = []
for path in trajectory_paths:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            raise SystemExit(f"Canary trajectory is malformed: {path}")
provider_failures = [
    event for event in events
    if event.get("event") == "model.failed"
]
if provider_failures:
    raise SystemExit("Canary encountered provider/model request failures")
# A canary may exhaust its 50-call quality budget without reaching submit_result.
# That is a scored model-quality outcome, not a transport or harness-integrity
# failure; the full matrix must still collect it rather than blocking forever.
print(json.dumps({
    "canary_gate": "passed",
    "study_complete": bool(summary.get("study_complete")),
    "quality_errors": int(arm.get("errors") or 0),
    "model_calls": int(arm.get("model_calls") or 0),
}))
PY

normalize_once() {
  local archive="$1" destination="$2" expected="$3"
  if [[ -d "$destination" ]]; then
    .venv/bin/python - "$archive" "$destination" "$expected" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

archive, destination, expected = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
provenance = json.loads((destination / "harbor_source.json").read_text(encoding="utf-8"))
digest = hashlib.sha256()
with archive.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
rows = json.loads((destination / "Financial_Model" / "dataset.json").read_text(encoding="utf-8"))
if provenance.get("archive_sha256") != digest.hexdigest() or len(rows) != expected:
    raise SystemExit(f"Normalized Harbor cache mismatch: {destination}")
PY
    return
  fi
  local staging
  staging="$(mktemp -d "${cache_root}/.normalizing.XXXXXX")"
  .venv/bin/python -m spreadsheet_harness.cli benchmark harbor-normalize \
    "$archive" --output "$staging/canonical"
  .venv/bin/python - "$staging/canonical" "$expected" <<'PY'
import json
import sys
from pathlib import Path

root, expected = Path(sys.argv[1]), int(sys.argv[2])
rows = json.loads((root / "Financial_Model" / "dataset.json").read_text(encoding="utf-8"))
if len(rows) != expected:
    raise SystemExit(f"expected {expected} tasks, found {len(rows)}")
PY
  mv "$staging/canonical" "$destination"
  rmdir "$staging"
}

normalize_once "$v06_archive" "$v06_dataset" 269
normalize_once "$enhanced_archive" "$enhanced_dataset" 1565

plan_file="$full_root/task-plan.tsv"
.venv/bin/python - "$v06_dataset" "$enhanced_dataset" "$plan_file" <<'PY'
import json
import sys
from pathlib import Path

sources = (("v06", Path(sys.argv[1]), 269), ("enhanced-v2", Path(sys.argv[2]), 1565))
arms = ("h0d0", "h1d0", "h0d1", "h1d1")
rows = []
for label, dataset, expected in sources:
    payload = json.loads((dataset / "Financial_Model" / "dataset.json").read_text(encoding="utf-8"))
    if len(payload) != expected:
        raise SystemExit(f"{label}: expected {expected} tasks, found {len(payload)}")
    for item in payload:
        task = "Financial_Model/" + str(item["id"])
        for arm in arms:
            rows.append((label, str(dataset), arm, task))
Path(sys.argv[3]).write_text(
    "".join("\t".join(row) + "\n" for row in rows), encoding="utf-8"
)
print(f"planned_runs={len(rows)}")
PY

export FULL_ROOT="$full_root" CACHE_ROOT="$cache_root" SKILL_ROOT_BASE="$skill_base"
export BASE_URL="$base_url" API_KEY_FILE="$api_key_file" MODEL="$model"
export TASK_ATTEMPTS="$task_attempts" REQUEST_INTERVAL_SECONDS="$request_interval"
# xargs returns 123 when any worker fails.  Keep the matrix alive and let the
# completion report expose missing/incomplete tasks so a later invocation can
# resume them; do not abort the remaining thousands of independent workers.
set +e
xargs -P "$parallelism" -d '\n' -I '{}' "$script_path" --worker '{}' < "$plan_file"
xargs_code=$?
set -e
echo "worker batch finished with xargs_code=$xargs_code" >&2

.venv/bin/python - "$plan_file" "$full_root" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

plan, root = Path(sys.argv[1]), Path(sys.argv[2])
counts = Counter()
for line in plan.read_text(encoding="utf-8").splitlines():
    dataset_label, _, variant, task = line.split("\t")
    output = root / dataset_label / variant / task.replace("/", "_")
    summary_path = output / "summary.json"
    if not summary_path.is_file():
        counts["missing"] += 1
        continue
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    counts["complete" if summary.get("study_complete") else "incomplete"] += 1
report = {"expected": sum(counts.values()), **dict(sorted(counts.items()))}
(root / "completion-summary.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, sort_keys=True))
raise SystemExit(0 if report == {"expected": 7336, "complete": 7336} else 2)
PY
