#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

run_root="${RUN_ROOT:-benchmarks/results/deepseek-v4-flash-codex-core-v2-full-20260911}"
dataset="${DATASET:-benchmarks/data/spreadsheetbench-v2}"
base_url="${BASE_URL:-http://10.130.138.46:8010/v1}"
api_key_file="${API_KEY_FILE:-/tmp/spreadsheet-harness-litellm.key}"
model="${MODEL:-DeepSeek-V4-Flash}"
composition="${COMPOSITION:-spreadsheet-harness-core}"
parallelism="${PARALLELISM:-6}"
task_ids="${TASK_IDS:-}"
categories="${CATEGORIES:-}"
visual_evaluator="${VISUAL_EVALUATOR:-/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py}"
repo_root="$(pwd -P)"
run_root="$(realpath -m "$run_root")"
dataset="$(realpath "$dataset")"
api_key_file="$(realpath "$api_key_file")"
visual_evaluator="$(realpath "$visual_evaluator")"
skill_root="$run_root/skill-root"
frozen_source_root="$run_root/frozen-source"
frozen_source_package="$frozen_source_root/spreadsheet_harness"
task_root="$run_root/tasks"
status_file="$run_root/status.tsv"

[[ "$parallelism" =~ ^[1-9][0-9]*$ ]] || {
  echo "PARALLELISM must be a positive integer" >&2
  exit 2
}
[[ -n "$composition" ]] || {
  echo "COMPOSITION must not be empty" >&2
  exit 2
}
[[ -r "$api_key_file" ]] || {
  echo "Missing readable API key file: $api_key_file" >&2
  exit 2
}
[[ -f "$visual_evaluator" ]] || {
  echo "Missing pinned official visualization evaluator: $visual_evaluator" >&2
  exit 2
}
[[ "$(sha256sum "$visual_evaluator" | awk '{print $1}')" == \
   "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703" ]] || {
  echo "Official visualization evaluator hash mismatch" >&2
  exit 2
}

mkdir -p "$task_root"
if [[ -d "$skill_root" ]]; then
  diff -qr \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    skills "$skill_root" >/dev/null || {
    echo "Frozen skills differ from the current skills; use a new RUN_ROOT" >&2
    exit 2
  }
elif [[ -e "$skill_root" ]]; then
  echo "Frozen skill root exists but is not a directory" >&2
  exit 2
else
  cp -a skills "$skill_root"
  find "$skill_root" -type d -name __pycache__ -prune -exec rm -rf -- {} +
  find "$skill_root" -type f -name '*.pyc' -delete
fi
(cd "$skill_root" && find . -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  | sha256sum \
  | awk '{print $1}') > "$run_root/frozen-skills-sha256.txt"

if [[ -d "$frozen_source_package" ]]; then
  diff -qr \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    src/spreadsheet_harness "$frozen_source_package" >/dev/null || {
      echo "Frozen harness source differs from the current source; use a new RUN_ROOT" >&2
      exit 2
    }
else
  mkdir -p "$frozen_source_root"
  cp -a src/spreadsheet_harness "$frozen_source_package"
  find "$frozen_source_package" -type d -name __pycache__ -prune -exec rm -rf -- {} +
  find "$frozen_source_package" -type f -name '*.pyc' -delete
fi
(cd "$frozen_source_package" && find . -type f -name '*.py' -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  | sha256sum \
  | awk '{print $1}') > "$run_root/frozen-source-sha256.txt"
benchmark_link="$frozen_source_root/benchmarks"
if [[ -L "$benchmark_link" ]]; then
  [[ "$(realpath "$benchmark_link")" == "$repo_root/benchmarks" ]] || {
    echo "Frozen benchmark link points outside the current repository" >&2
    exit 2
  }
elif [[ -e "$benchmark_link" ]]; then
  echo "Frozen source benchmark path exists but is not a symlink" >&2
  exit 2
else
  ln -s "$repo_root/benchmarks" "$benchmark_link"
fi

plan_file="$run_root/task-plan.tsv"
TASK_IDS="$task_ids" CATEGORIES="$categories" .venv/bin/python - "$dataset" "$plan_file" <<'PY'
import json
import os
import sys
from pathlib import Path

dataset = Path(sys.argv[1])
plan = Path(sys.argv[2])
expected = {"Debugging": 100, "Financial_Model": 100, "Template": 97, "Visualization": 24}
requested = {
    item.strip() for item in os.environ.get("TASK_IDS", "").split("|") if item.strip()
}
requested_categories = {
    item.strip() for item in os.environ.get("CATEGORIES", "").split(",") if item.strip()
}
unknown_categories = requested_categories - set(expected)
if unknown_categories:
    raise SystemExit("Unknown CATEGORIES: " + ", ".join(sorted(unknown_categories)))
rows = []
for category, expected_count in expected.items():
    payload = json.loads((dataset / category / "dataset.json").read_text(encoding="utf-8"))
    if len(payload) != expected_count:
        raise SystemExit(f"{category}: expected {expected_count} tasks, found {len(payload)}")
    for item in payload:
        item_id = str(item.get("id") or item.get("task_id") or "").strip()
        task_id = f"{category}/{item_id}"
        if (not requested_categories or category in requested_categories) and (
            not requested or task_id in requested
        ):
            rows.append((task_id, category))
if requested:
    found = {task_id for task_id, _ in rows}
    missing = sorted(requested - found)
    if missing:
        raise SystemExit("Unknown TASK_IDS: " + ", ".join(missing))
expected_selected = sum(
    count
    for category, count in expected.items()
    if not requested_categories or category in requested_categories
)
if not requested and len(rows) != expected_selected:
    raise SystemExit(f"expected {expected_selected} selected v2 tasks, found {len(rows)}")
plan.write_text("".join(f"{task_id}\t{category}\n" for task_id, category in rows), encoding="utf-8")
print(f"selected_tasks={len(rows)}")
PY

if [[ ! -f "$status_file" ]]; then
  printf 'timestamp\ttask_id\tstatus\texit_code\toutput\n' > "$status_file"
fi

export repo_root dataset base_url api_key_file model composition visual_evaluator skill_root frozen_source_root task_root status_file
xargs -P "$parallelism" -d '\n' -I '{}' bash -c '
  set -uo pipefail
  line="$1"
  task_id="${line%%$'"'"'\t'"'"'*}"
  category="${line#*$'"'"'\t'"'"'}"
  slug="${task_id//\//__}"
  output="$task_root/$slug"
  log="$task_root/$slug.log"

  if [[ -f "$output/summary.json" ]] && .venv/bin/python - "$output/summary.json" <<'"'"'PY'"'"'
import json
import sys
summary = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if summary.get("study_complete") or summary.get("generation_complete") else 1)
PY
  then
    printf "%s\t%s\tskipped-complete\t0\t%s\n" "$(date -Is)" "$task_id" "$output" >> "$status_file"
    exit 0
  fi
  if [[ -e "$output" ]]; then
    printf "%s\t%s\tblocked-existing-incomplete\t2\t%s\n" "$(date -Is)" "$task_id" "$output" >> "$status_file"
    exit 0
  fi

  common=(
    --dataset "$dataset"
    --task-id "$task_id"
    --arm ours
    --composition "ours=$composition"
    --skill-root "$skill_root"
    --output "$output"
    --max-model-calls 50
    --max-turns-per-arm 50
    --max-total-tokens unlimited
    --max-output-tokens unlimited
    --task-timeout 3600
    --arm-order-seed 20260911
    --base-url "$base_url"
    --api-key-file "$api_key_file"
    --model "$model"
    --api-protocol chat-completions
    --reasoning-effort medium
    --request-timeout 1800
    --litellm-timeout 1800
    --request-retries 5
    --request-interval-seconds 1.1
    --temperature 0
    --top-p 1
    --enable-thinking
  )
  if [[ "$category" == Visualization ]]; then
    command=("$repo_root/.venv/bin/python" -m spreadsheet_harness.cli benchmark v2-visual-generate
      --visual-evaluator "$visual_evaluator" "${common[@]}")
  else
    command=("$repo_root/.venv/bin/python" -m spreadsheet_harness.cli benchmark v2-compare
      --category "$category" "${common[@]}")
  fi

  if (cd "$frozen_source_root" && "${command[@]}") > "$log" 2>&1; then
    code=0
    status=complete
  else
    code=$?
    status=failed
  fi
  printf "%s\t%s\t%s\t%s\t%s\n" "$(date -Is)" "$task_id" "$status" "$code" "$output" >> "$status_file"
' _ '{}' < "$plan_file"

.venv/bin/python - "$plan_file" "$task_root" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

plan = Path(sys.argv[1])
task_root = Path(sys.argv[2])
counts = Counter()
for line in plan.read_text(encoding="utf-8").splitlines():
    task_id, _ = line.split("\t", 1)
    summary_path = task_root / task_id.replace("/", "__") / "summary.json"
    if not summary_path.is_file():
        counts["missing"] += 1
        continue
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("study_complete") or summary.get("generation_complete"):
        counts["complete"] += 1
    else:
        counts["incomplete"] += 1
print(json.dumps(dict(sorted(counts.items())), sort_keys=True))
raise SystemExit(0 if counts == {"complete": sum(counts.values())} else 2)
PY
