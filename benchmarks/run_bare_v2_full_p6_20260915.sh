#!/usr/bin/env bash
set -uo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

run_root="${RUN_ROOT:-benchmarks/results/deepseek-v4-flash-bare-v2-p6-20260915}"
dataset="${DATASET:-benchmarks/data/spreadsheetbench-v2}"
mkdir -p "$run_root/tasks"
export run_root dataset

.venv/bin/python - "$dataset" "$run_root/task-plan.tsv" <<'PY' | xargs -P "${PARALLELISM:-6}" -n 1 bash -c 'task="$1"; slug="${task//\//__}"; out="$run_root/tasks/$slug"; log="$run_root/tasks/$slug.log"; if [[ -e "$out" ]]; then exit 0; fi; category="${task%%/*}"; common=(--dataset "$dataset" --task-id "$task" --arm bare --output "$out" --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens unlimited --max-output-tokens unlimited --task-timeout 3600 --arm-order-seed 20260911 --base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key --model dashscope/deepseek-v4-flash --api-protocol chat-completions --reasoning-effort medium --request-timeout 1800 --litellm-timeout 1800 --request-retries 5 --request-interval-seconds 1.1 --temperature 0 --top-p 1 --enable-thinking); if [[ "$category" == Visualization ]]; then cmd=(.venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate --visual-evaluator /tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py "${common[@]}"); else cmd=(.venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare --category "$category" "${common[@]}"); fi; "${cmd[@]}" >"$log" 2>&1' _
import json,sys
from pathlib import Path
dataset=Path(sys.argv[1]); plan=Path(sys.argv[2]); rows=[]
for category in ("Debugging","Financial_Model","Template","Visualization"):
    for item in json.loads((dataset/category/"dataset.json").read_text()):
        rows.append(f"{category}/{item.get('id') or item.get('task_id')}\n")
plan.write_text(''.join(rows))
PY
