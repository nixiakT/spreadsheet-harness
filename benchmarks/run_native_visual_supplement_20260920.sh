#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

dataset=benchmarks/data/spreadsheetbench-v2
evaluator=/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py
root=benchmarks/results/native-recovery-20260917/visualization

make_plan() {
  local outroot="$1"
  mkdir -p "$outroot/tasks"
  .venv/bin/python - "$dataset" "$outroot" <<'PY'
import json
import sys
from pathlib import Path

data = Path(sys.argv[1])
out = Path(sys.argv[2])
ids = [str(item.get("id") or item.get("task_id"))
       for item in json.loads((data / "Visualization" / "dataset.json").read_text())]
(out / "task-plan.txt").write_text("\n".join(ids) + "\n")
print(f"planned={len(ids)}")
PY
}

run_model() {
  local name="$1" model="$2" outroot="$3"
  make_plan "$outroot"
  export dataset evaluator model outroot
  xargs -P 1 -I __TASK__ zsh -c '
    set -euo pipefail
    task="$1"
    slug="${task//\//__}"
    slug="${slug// /_}"
    out="$outroot/tasks/$slug"
    log="$outroot/tasks/$slug.log"
    if [[ -f "$out/results.json" ]]; then
      exit 0
    fi
    .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
      --dataset "$dataset" --visual-evaluator "$evaluator" --task-id "$task" \
      --arm spreadsheet-rl-native --output "$out" \
      --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
      --max-output-tokens 32768 --task-timeout 21600 --request-timeout 1800 \
      --litellm-timeout 1800 --request-retries 5 --arm-order-seed 20260820 \
      --base-url http://10.130.138.46:8010/v1 \
      --api-key-file /tmp/spreadsheet-harness-litellm.key \
      --model "$model" --api-protocol chat-completions --reasoning-effort medium \
      --seed 41 --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1
  ' _ __TASK__ < "$outroot/task-plan.txt"
  printf '%s finished %s/24\n' "$name" \
    "$(find "$outroot/tasks" -mindepth 2 -maxdepth 2 -type f -name results.json | wc -l)" \
    > "$outroot/status.log"
}

run_model v2-deepseek dashscope/deepseek-v4-flash "$root/v2-deepseek-dashscope-rerun1" & deepseek_pid=$!
run_model v2-qwen-plus dashscope/qwen3-coder-plus "$root/v2-qwen-plus-rerun1" & qwen_pid=$!
wait "$deepseek_pid" || true
wait "$qwen_pid" || true
date -Is > "$root/ALL_DONE"
