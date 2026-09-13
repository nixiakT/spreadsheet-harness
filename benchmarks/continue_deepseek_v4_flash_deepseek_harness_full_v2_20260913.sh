#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

run_root="$(realpath benchmarks/results/deepseek-v4-flash-deepseek-harness-full-v2-20260913)"
repo_root="$(pwd -P)"
dataset="$(realpath benchmarks/data/spreadsheetbench-v2)"
task_root="$run_root/tasks"
pending_plan="$run_root/continuation-plan.tsv"
status_file="$run_root/continuation-status.tsv"
skill_root="$run_root/skill-root"
frozen_source_root="$run_root/frozen-source"
isolated_codex_home="$run_root/isolated-codex-home"
base_url="http://10.130.138.46:8010/v1"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
model="DeepSeek-V4-Flash"
composition="spreadsheet-harness-financial"
parallelism="${PARALLELISM:-6}"
visual_evaluator="/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py"

[[ "$parallelism" =~ ^[1-9][0-9]*$ ]] || {
  echo "PARALLELISM must be a positive integer" >&2
  exit 2
}
[[ -r "$api_key_file" ]] || {
  echo "Missing readable API key file: $api_key_file" >&2
  exit 2
}
[[ -d "$frozen_source_root/spreadsheet_harness" ]] || {
  echo "Missing frozen harness source: $frozen_source_root" >&2
  exit 2
}
[[ -d "$skill_root" ]] || {
  echo "Missing frozen skill root: $skill_root" >&2
  exit 2
}
[[ "$(sha256sum "$visual_evaluator" | awk '{print $1}')" == \
   "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703" ]] || {
  echo "Official visualization evaluator hash mismatch" >&2
  exit 2
}

mkdir -p "$isolated_codex_home"
chmod 700 "$isolated_codex_home"
export CODEX_HOME="$isolated_codex_home"

if [[ ! -f "$pending_plan" ]]; then
  "$repo_root/.venv/bin/python" - "$run_root/task-plan.tsv" "$task_root" "$pending_plan" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
task_root = Path(sys.argv[2])
pending = Path(sys.argv[3])
rows = []
for line in source.read_text(encoding="utf-8").splitlines():
    task_id, category = line.split("\t", 1)
    if not (task_root / task_id.replace("/", "__")).exists():
        rows.append((task_id, category))
pending.write_text(
    "".join(f"{task_id}\t{category}\n" for task_id, category in rows),
    encoding="utf-8",
)
print(f"continuation_tasks={len(rows)}")
PY
fi

if [[ ! -f "$status_file" ]]; then
  printf 'timestamp\ttask_id\tstatus\texit_code\toutput\n' > "$status_file"
fi

export run_root repo_root dataset task_root pending_plan status_file skill_root
export frozen_source_root base_url api_key_file model composition visual_evaluator
xargs -P "$parallelism" -d '\n' -I '{}' bash -c '
  set -uo pipefail
  line="$1"
  task_id="${line%%$'"'"'\t'"'"'*}"
  category="${line#*$'"'"'\t'"'"'}"
  slug="${task_id//\//__}"
  output="$task_root/$slug"
  log="$task_root/$slug.log"

  if [[ -f "$output/summary.json" ]] && "$repo_root/.venv/bin/python" - "$output/summary.json" <<'"'"'PY'"'"'
import json
import sys
summary = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if summary.get("study_complete") or summary.get("generation_complete") else 1)
PY
  then
    printf "%s\t%s\tskipped-complete\t0\t%s\n" "$(date -Is)" "$task_id" "$output" >> "$status_file"
    exit 0
  fi

  resume=()
  if [[ -e "$output" ]]; then
    resume=(--resume --seal-interrupted-current)
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
    "${resume[@]}"
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
' _ '{}' < "$pending_plan"

"$repo_root/.venv/bin/python" - "$run_root/task-plan.tsv" "$task_root" <<'PY'
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
raise SystemExit(0 if counts.get("missing", 0) == 0 else 2)
PY
