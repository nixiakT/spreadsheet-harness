#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
source_root=benchmarks/results/glm51-spreadsheet-rl-native-v1-v2-full-20260929
root=benchmarks/results/glm51-spreadsheet-rl-native-provider-retry-20261001-p4b
dataset_v1=benchmarks/data/spreadsheetbench_912_v0.1
dataset_v2=benchmarks/data/spreadsheetbench-v2
evaluator_v2=benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py
visual_evaluator=/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py
parallelism=4
mkdir -p "$root/v1/tasks" "$root/v2/tasks"
.venv/bin/python - "$source_root" "$root/task-plan.tsv" <<'PY'
import json,sys
from pathlib import Path
src,plan=map(Path,sys.argv[1:]); rows=[]
for kind in ('v1','v2'):
  for p in sorted((src/kind/'tasks').glob('*/results.json')):
    try: data=json.loads(p.read_text())
    except Exception: continue
    for item in (data if isinstance(data,list) else [data]):
      et=str(item.get('error_type') or ''); e=(str(item.get('error') or '')+' '+str(item.get('outcome_kind') or '')).lower()
      if et=='ProviderError' or any(x in e for x in ('ratelimit','rate limit','no deployments available','deployment','quota','429','timeout')):
        t=str(item.get('task_id') or p.parent.name.replace('__','/')); rows.append((kind,t,t.split('/',1)[0] if kind=='v2' else ''))
seen=set(); out=[]
for r in rows:
  if (r[0],r[1]) not in seen: seen.add((r[0],r[1])); out.append(r)
plan.write_text(''.join('\t'.join(r)+'\n' for r in out)); print(f'planned={len(out)}')
PY
export dataset_v1 dataset_v2 evaluator_v2 visual_evaluator root
xargs -P "$parallelism" -d '\n' -I __ROW__ zsh -c '
 set -uo pipefail; line="$1"; kind="${line%%$'"'"'\t'"'"'*}"; rest="${line#*$'"'"'\t'"'"'}"; task="${rest%%$'"'"'\t'"'"'*}"; category="${rest#*$'"'"'\t'"'"'}"; slug="${task//\//__}"; slug="${slug// /_}"; out="$root/$kind/tasks/$slug"; log="$out.log"; [[ -f "$out/results.json" ]] && exit 0;
 if [[ "$kind" == v1 ]]; then cmd=(.venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare --dataset "$dataset_v1" --task-id "$task" --arm spreadsheet-rl-native --output "$out"); else cmd=(.venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare --dataset "$dataset_v2" --official-evaluator "$evaluator_v2" --category "$category" --task-id "$task" --arm spreadsheet-rl-native --output "$out"); fi
 "${cmd[@]}" --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 --litellm-timeout 600 --request-retries 5 --request-interval-seconds 1.1 --arm-order-seed 20261001 --base-url http://10.130.138.46:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key --model dashscope/glm-5.1 --api-protocol chat-completions --reasoning-effort medium --seed 41 --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1
' _ __ROW__ < "$root/task-plan.tsv"
PY
