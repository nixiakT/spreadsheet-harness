#!/usr/bin/env bash
# Resume only missing formal Qwen V2 tasks, audit preservation, score, update analyse.
set -euo pipefail

REPO="/data/zju-160/tongzeyuan/spreadsheet-harness"
EXP="$REPO/benchmarks/results/trace2skill-v2-formal-qwen3-coder-480b-20260914"
DATA="$REPO/tmp/trace2skill_spreadsheetbench_v2_full"
PY="$REPO/.venv/bin/python"
MODEL="dashscope/qwen3-coder-480b-a35b-instruct"
BASELINE="$EXP/pre_resume_241_sha256.json"
ORIGINAL_CONFIG="$EXP/trace2skill_run_config_before_resume_80.json"

cp -p "$EXP/trace2skill_run_config.json" "$ORIGINAL_CONFIG"
cp -p "$EXP/runner_results.json" "$EXP/runner_results_before_resume_80.json"
restore_config() {
  if [[ -f "$ORIGINAL_CONFIG" ]]; then
    cp -p "$ORIGINAL_CONFIG" "$EXP/trace2skill_run_config.json"
  fi
}
trap restore_config EXIT

"$PY" - "$DATA/dataset.json" "$EXP/outputs" "$BASELINE" <<'PY'
import hashlib, json, sys, zipfile
from pathlib import Path
dataset = json.load(open(sys.argv[1]))
outputs = Path(sys.argv[2])
records = {}
for item in dataset:
    path = outputs / item["spreadsheet_path"] / "initial_output.xlsx"
    if path.is_file() and zipfile.is_zipfile(path):
        records[str(item["id"])] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
Path(sys.argv[3]).write_text(json.dumps({"files": records}, indent=2) + "\n")
print(f"recorded {len(records)} valid pre-resume outputs")
PY

missing_ids="$($PY - "$DATA/dataset.json" "$EXP/outputs" <<'PY'
import json, sys, zipfile
from pathlib import Path
dataset = json.load(open(sys.argv[1]))
outputs = Path(sys.argv[2])
ids = []
for item in dataset:
    path = outputs / item["spreadsheet_path"] / "initial_output.xlsx"
    if not path.is_file() or not zipfile.is_zipfile(path):
        ids.append(str(item["id"]))
print(",".join(ids))
PY
)"

if [[ -n "$missing_ids" ]]; then
  "$REPO/benchmarks/run_trace2skill.sh" \
    --version v2 \
    --dataset "$DATA" \
    --output "$EXP" \
    --model "$MODEL" \
    --workers 2 \
    --max-turns 50 \
    --seed 41 \
    --temperature 0.0 \
    --instance-ids "$missing_ids" \
    --skip-eval \
    --verbose
  cp -p "$EXP/runner_results.json" "$EXP/runner_results_resume_80.json"
fi
cp -p "$ORIGINAL_CONFIG" "$EXP/trace2skill_run_config.json"

# One bounded residual retry.  This catches transient 429/deployment failures
# without repeatedly rerunning a persistent quota exhaustion or model failure.
residual_ids="$($PY - "$DATA/dataset.json" "$EXP/outputs" <<'PY'
import json, sys, zipfile
from pathlib import Path
dataset = json.load(open(sys.argv[1]))
outputs = Path(sys.argv[2])
ids = []
for item in dataset:
    path = outputs / item["spreadsheet_path"] / "initial_output.xlsx"
    if not path.is_file() or not zipfile.is_zipfile(path):
        ids.append(str(item["id"]))
print(",".join(ids))
PY
)"
if [[ -n "$residual_ids" ]]; then
  "$REPO/benchmarks/run_trace2skill.sh" \
    --version v2 \
    --dataset "$DATA" \
    --output "$EXP" \
    --model "$MODEL" \
    --workers 1 \
    --max-turns 50 \
    --seed 41 \
    --temperature 0.0 \
    --instance-ids "$residual_ids" \
    --skip-eval \
    --verbose
  cp -p "$EXP/runner_results.json" "$EXP/runner_results_final_retry.json"
fi
cp -p "$ORIGINAL_CONFIG" "$EXP/trace2skill_run_config.json"

"$PY" "$REPO/tools/finalize_trace2skill_v2_resume.py" \
  --dataset "$DATA" \
  --experiment "$EXP" \
  --model "$MODEL" \
  --results-file "$EXP/runner_results_321.json"

"$PY" - "$BASELINE" "$EXP/pre_resume_241_preservation.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
baseline = json.load(open(sys.argv[1]))["files"]
changed, missing, unchanged = [], [], []
for task_id, item in baseline.items():
    path = Path(item["path"])
    if not path.is_file():
        missing.append(task_id)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        changed.append(task_id)
    else:
        unchanged.append(task_id)
payload = {
    "baseline_files": len(baseline), "unchanged": len(unchanged),
    "changed": len(changed), "missing": len(missing),
    "changed_ids": changed, "missing_ids": missing,
}
Path(sys.argv[2]).write_text(json.dumps(payload, indent=2) + "\n")
print(json.dumps(payload, indent=2))
if changed or missing:
    raise SystemExit("pre-resume output preservation check failed")
PY

"$REPO/tools/evaluate_trace2skill_v2_snapshot.sh" \
  "$EXP" \
  "$EXP/eval_official_results_v2_final.json" \
  "$EXP/eval_recalculated_outputs_v2_final"

"$PY" "$REPO/tools/update_trace2skill_v2_qwen_analysis.py" \
  --analysis "$REPO/analyse/analyse.md" \
  --status "$EXP/runner_results_321.json" \
  --evaluation "$EXP/eval_official_results_v2_final.json" \
  --preservation "$EXP/pre_resume_241_preservation.json"

trap - EXIT
restore_config
