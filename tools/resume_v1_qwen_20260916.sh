#!/usr/bin/env bash
set -euo pipefail

REPO="/data/zju-160/tongzeyuan/spreadsheet-harness"
PY="$REPO/.venv/bin/python"
DATA="$REPO/benchmarks/data/spreadsheetbench_912_v0.1"
EXP="$REPO/benchmarks/results/trace2skill-v1-formal-qwen3-coder-480b-20260913"
STAGE="$EXP/resume_stage_20260916"
LOG="$EXP/resume_stage_20260916.log"

mkdir -p "$STAGE"
if [[ -f "$EXP/eval_official_results.json" && ! -f "$EXP/eval_official_results_before_resume_20260916.json" ]]; then
  cp -p "$EXP/eval_official_results.json" "$EXP/eval_official_results_before_resume_20260916.json"
fi

ids="$($PY - "$DATA/dataset.json" "$EXP/outputs" <<'PY'
import json, sys, zipfile
from pathlib import Path

dataset = json.load(open(sys.argv[1], encoding="utf-8"))
outputs = Path(sys.argv[2])
missing = []
for row in dataset:
    task_id = str(row["id"])
    spreadsheet = Path(row.get("spreadsheet_path") or task_id)
    data_dir = Path(sys.argv[1]).parent
    source = data_dir / spreadsheet
    if not source.is_dir():
        source = data_dir / "spreadsheet" / spreadsheet
    inputs = sorted(source.glob("*_input.xlsx"))
    if not inputs:
        inputs = sorted(source.glob("*_init.xlsx"))
    if not inputs:
        inputs = [source / "initial.xlsx"] if (source / "initial.xlsx").exists() else [source / "input.xlsx"]
    out_dir = outputs / spreadsheet
    complete = True
    for inp in inputs:
        name = inp.name
        if "_input.xlsx" in name:
            out_name = name.replace("_input.xlsx", "_output.xlsx")
        elif "_init.xlsx" in name:
            out_name = name.replace("_init.xlsx", "_output.xlsx")
        else:
            out_name = inp.stem + "_output.xlsx"
        target = out_dir / out_name
        if not target.is_file() or not zipfile.is_zipfile(target):
            complete = False
            break
    if not complete:
        missing.append(task_id)
print(",".join(missing))
print(f"missing_tasks={len(missing)}", file=sys.stderr)
PY
)"

if [[ -z "$ids" ]]; then
  echo "No incomplete V1 Qwen tasks found."
  exit 0
fi

echo "Starting V1 Qwen resume; staging outputs in $STAGE" | tee "$LOG"
echo "Task IDs are selected from incomplete canonical tasks only." | tee -a "$LOG"

"$REPO/benchmarks/run_trace2skill.sh" \
  --version v1 \
  --dataset "$DATA" \
  --output "$STAGE" \
  --model dashscope/qwen3-coder-480b-a35b-instruct \
  --workers 2 \
  --max-turns 50 \
  --seed 41 \
  --temperature 0.0 \
  --instance-ids "$ids" \
  --skip-eval \
  --verbose 2>&1 | tee -a "$LOG"

"$PY" - "$STAGE/outputs" "$EXP/outputs" "$EXP/resume_stage_20260916_merge.json" <<'PY'
import hashlib, json, shutil, sys
from pathlib import Path

stage, canonical, report = map(Path, sys.argv[1:])
copied, skipped, invalid = [], [], []
for src in stage.rglob("*_output.xlsx"):
    rel = src.relative_to(stage)
    dst = canonical / rel
    if not src.is_file():
        continue
    if not src.suffix.lower() == ".xlsx":
        continue
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(rel))
    else:
        skipped.append(str(rel))
payload = {
    "staging_outputs": sum(1 for _ in stage.rglob("*_output.xlsx")),
    "copied_missing_only": len(copied),
    "skipped_existing": len(skipped),
    "copied": copied,
    "skipped": skipped,
}
report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: payload[k] for k in ("staging_outputs", "copied_missing_only", "skipped_existing")}, indent=2))
PY

"$PY" "$REPO/tmp/paper_repos/Trace2Skill/evaluate_with_official.py" \
  --data_path "$DATA" \
  --output_dir "$EXP/outputs" \
  --start_idx 0 \
  --end_idx 912 \
  --results_file "$EXP/eval_official_results.json" \
  --verbose 2>&1 | tee -a "$LOG"

echo "V1 Qwen resume and evaluation finished." | tee -a "$LOG"
