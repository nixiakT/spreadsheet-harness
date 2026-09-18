#!/usr/bin/env bash
# Copy a Trace2Skill V2 run, recalculate only cell-based splits, and score it.
set -euo pipefail

REPO="/data/zju-160/tongzeyuan/spreadsheet-harness"
PY="$REPO/.venv/bin/python"
EXP="${1:?usage: evaluate_trace2skill_v2_snapshot.sh EXPERIMENT [RESULTS_FILE] [SNAPSHOT_DIR]}"
RESULTS_FILE="${2:-$EXP/eval_official_results_v2.json}"
SNAPSHOT="${3:-$EXP/eval_recalculated_outputs_v2}"

[[ "$SNAPSHOT" == "$EXP/"* ]] || {
  echo "snapshot directory must be inside the experiment directory" >&2
  exit 2
}
if [[ -d "$SNAPSHOT" ]]; then
  rm -rf -- "$SNAPSHOT"
fi
mkdir -p "$SNAPSHOT"
cp -a "$EXP/outputs/." "$SNAPSHOT/"

# Visualization has a separate official Windows COM + VLM protocol.  Keep its
# original OOXML untouched; LibreOffice can rewrite chart objects.
while IFS= read -r -d '' workbook; do
  "$PY" "$REPO/tmp/paper_repos/Trace2Skill/spreadsheet_agent/skills/xlsx/recalc.py" \
    "$workbook" 120 >/dev/null || true
done < <(
  find "$SNAPSHOT" -type f -name '*_output.xlsx' \
    ! -path "$SNAPSHOT/Visualization_*/*" -print0
)

"$PY" "$REPO/tools/evaluate_trace2skill_v2_official.py" \
  --data-root "$REPO/benchmarks/data/spreadsheetbench-v2" \
  --outputs "$SNAPSHOT" \
  --results-file "$RESULTS_FILE" \
  --workers 4
