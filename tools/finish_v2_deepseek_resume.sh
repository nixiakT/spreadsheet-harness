#!/usr/bin/env bash
set -euo pipefail

REPO="/data/zju-160/tongzeyuan/spreadsheet-harness"
EXP="$REPO/benchmarks/results/trace2skill-v2-formal-deepseek-20260914"
DATA="$REPO/tmp/trace2skill_spreadsheetbench_v2_full"
PY="$REPO/.venv/bin/python"
CURRENT_RUNNER_PID="${1:?current runner PID required}"

while kill -0 "$CURRENT_RUNNER_PID" 2>/dev/null; do
  sleep 30
done

cp -p "$EXP/runner_results.json" "$EXP/runner_results_resume_19.json"

retry_ids="$($PY - "$DATA/dataset.json" "$EXP/outputs" <<'PY'
import json, sys, zipfile
from pathlib import Path
dataset = json.load(open(sys.argv[1]))
outputs = Path(sys.argv[2])
ids = []
for item in dataset:
    output = outputs / item["spreadsheet_path"] / "initial_output.xlsx"
    if not output.is_file() or not zipfile.is_zipfile(output):
        ids.append(str(item["id"]))
print(",".join(ids))
PY
)"

if [[ -n "$retry_ids" ]]; then
  "$REPO/benchmarks/run_trace2skill.sh" \
    --version v2 \
    --dataset "$DATA" \
    --output "$EXP" \
    --model deepseek-v4-flash \
    --workers 1 \
    --max-turns 50 \
    --seed 41 \
    --temperature 0.0 \
    --instance-ids "$retry_ids" \
    --skip-eval \
    --verbose
  cp -p "$EXP/runner_results.json" "$EXP/runner_results_final_retry.json"
fi

"$PY" "$REPO/tools/finalize_trace2skill_v2_resume.py" \
  --dataset "$DATA" \
  --experiment "$EXP" \
  --model deepseek-v4-flash \
  --results-file "$EXP/runner_results_321.json"

"$REPO/tools/evaluate_trace2skill_v2_snapshot.sh" \
  "$EXP" \
  "$EXP/eval_official_results_v2_final.json" \
  "$EXP/eval_recalculated_outputs_final"
