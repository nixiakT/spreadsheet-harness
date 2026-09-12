#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
root=${RUN_ROOT:-benchmarks/results/qwen36-paper-rl-90-20260906}
mkdir -p "$root"
task_file="$root/tasks.tsv"
.venv/bin/python - <<'PY' > "$task_file"
import json
from pathlib import Path
for c in ('Debugging','Financial_Model','Template'):
    rows=json.loads((Path('benchmarks/data/spreadsheetbench-v2')/c/'dataset.json').read_text())
    for r in rows[:30]: print(f'{c}/{r["id"]}')
PY
run_one() {
  local task="$1" arm="$2" slug="${task//\//_}_${arm}"
  local category="${task%%/*}"
  .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task" --arm "$arm" \
    --output "$root/$slug" --max-model-calls 20 --max-turns-per-arm 20 \
    --max-total-tokens 140000 --max-output-tokens 4096 --task-timeout 1800 \
    --request-timeout 600 --litellm-timeout 600 --request-retries 2 \
    --arm-order-seed 20260906 --base-url http://47.96.153.159:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key --model qwen36-35b-a3b \
    --api-protocol chat-completions --reasoning-effort medium --seed 41 \
    --temperature 1 --top-p 1 --disable-thinking > "$root/$slug.log" 2>&1
}
while IFS= read -r task; do
  for arm in paper spreadsheet-rl-minimal; do
    run_one "$task" "$arm" &
    while (( $(jobs -p | wc -l) >= ${PARALLELISM:-4} )); do wait -n || true; done
  done
done < "$task_file"
wait
