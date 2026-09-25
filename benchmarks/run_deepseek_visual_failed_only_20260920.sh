#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

dataset=benchmarks/data/spreadsheetbench-v2
evaluator=/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py
source_root=benchmarks/results/native-recovery-20260917/visualization/v2-deepseek-rerun1
root=benchmarks/results/native-recovery-20260917/visualization/v2-deepseek-dashscope-failed-rerun
mkdir -p "$root"

.venv/bin/python - "$source_root" "$root" <<'PY'
import json, glob, sys
from pathlib import Path

src, out = map(Path, sys.argv[1:])
failed = []
for path in sorted(src.glob("tasks/*/results.json")):
    value = json.loads(path.read_text())
    row = value[0] if isinstance(value, list) else value
    if row.get("error_type") == "ProviderError":
        failed.append(str(row["task_id"]))
(out / "task-plan.txt").write_text("\n".join(failed) + "\n")
print(f"failed_tasks={len(failed)}")
PY

while IFS= read -r task; do
  [[ -n "$task" ]] || continue
  slug="${task//\//__}"
  slug="${slug// /_}"
  out="$root/$slug"
  log="$root/$slug.log"
  if [[ -f "$out/results.json" ]]; then
    continue
  fi
  .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
    --dataset "$dataset" --visual-evaluator "$evaluator" --task-id "$task" \
    --arm spreadsheet-rl-native --output "$out" \
    --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
    --max-output-tokens 32768 --task-timeout 21600 --request-timeout 1800 \
    --litellm-timeout 1800 --request-retries 5 --arm-order-seed 20260820 \
    --base-url http://10.130.138.46:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model dashscope/deepseek-v4-flash \
    --api-protocol chat-completions --reasoning-effort medium \
    --seed 41 --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1 || true
done < "$root/task-plan.txt"

date -Is > "$root/DONE"
