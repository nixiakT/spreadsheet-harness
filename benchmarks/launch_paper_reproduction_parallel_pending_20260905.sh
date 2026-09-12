#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

manifest=benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json

run_pending() {
  local label="$1" arm="$2" model="$3" base_url="$4" check_root="$5" out_root="$6" disable="$7"
  local pending
  pending=$(MODEL_ROOT="$check_root" EXTRA_ROOTS="${EXTRA_ROOTS:-}" ARM="$arm" .venv/bin/python - <<'PY'
import json, os
from pathlib import Path
m=json.load(open('benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json'))
roots=[Path(os.environ['MODEL_ROOT'])]+[Path(x) for x in os.environ.get('EXTRA_ROOTS','').split(':') if x]
arm=os.environ['ARM']
for row in m['tasks']:
    task=row['task_id']; cat,item=task.split('/')
    # Skip only when a completed row already exists in the base tree.
    done=False
    files=[]
    for root in roots:
        files += list(root.glob(f'{cat}/{item}/results.json'))
        if (root/'results.json').exists(): files.append(root/'results.json')
    for f in files:
        try:
            payload=json.loads(f.read_text()); rows=payload if isinstance(payload,list) else payload.get('results',[])
            done |= any(x.get('status')=='completed' for x in rows if x.get('task_id')==task)
        except Exception: pass
    if not done: print(task)
PY
)
  mkdir -p "benchmarks/results/$out_root"
  if [[ -z "$pending" ]]; then print "$label: no pending cases"; return 0; fi
  print "$label pending cases: ${(f)pending}"
  print -rl -- ${(f)pending} | xargs -P "${JOBS:-2}" -I {} zsh -c '
    set -euo pipefail
    task="$1"; slug="${task//\//_}"; category="${task%%/*}"
    out="$2/$slug"; log="$2/$slug.log"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    args=(--dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task"
      --arm "$3" --output "$out" --max-model-calls 20 --max-turns-per-arm 20
      --max-total-tokens 140000 --max-output-tokens 8192 --task-timeout 1800
      --request-timeout 600 --litellm-timeout 600 --request-retries 1
      --arm-order-seed 20260904 --base-url "$4" --api-key-file /tmp/spreadsheet-harness-litellm.key
      --model "$5" --api-protocol chat-completions --reasoning-effort medium
      --seed 41 --temperature 1 --top-p 1)
    if [[ "$6" == 1 ]]; then args+=(--disable-thinking); fi
    .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare "${args[@]}" > "$log" 2>&1
  ' _ {} "benchmarks/results/$out_root" "$arm" "$base_url" "$model" "$disable"
}

# These are continuation trees. Existing sequential runs are left untouched;
# the final merger may choose the first successful result for a case.
suffix="${SUFFIX:-parallel-pending}"
if [[ "${ONLY:-all}" == all || "${ONLY}" == rl ]]; then
  run_pending "Spreadsheet-RL official checkpoint (thinking)" spreadsheet-rl-native Spreadsheet-RL-4B http://127.0.0.1:8626/v1 benchmarks/results/spreadsheet-rl-4b-v2-30-native-20260905-official-checkpoint-20260905 "spreadsheet-rl-4b-v2-30-native-$suffix-20260905" 0
fi
if [[ "${ONLY:-all}" == all || "${ONLY}" == agents ]]; then
  run_pending "SpreadsheetAgent paper-vision proxy" paper-vision qwen36-35b-a3b http://47.96.153.159:8010/v1 benchmarks/results/spreadsheetagent-paper-vision-proxy-qwen36-v2-30-20260905 "spreadsheetagent-paper-vision-proxy-$suffix-20260905" 1
fi
if [[ "${ONLY:-all}" == all || "${ONLY}" == agents || "${ONLY}" == compass ]]; then
  run_pending "SheetCompass graph-memory proxy" spreadsheet-harness-basic qwen36-35b-a3b http://47.96.153.159:8010/v1 benchmarks/results/sheetcompass-graph-memory-proxy-qwen36-v2-30-20260905 "sheetcompass-graph-memory-proxy-$suffix-20260905" 1
fi
