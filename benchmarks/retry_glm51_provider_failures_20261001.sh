#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

source_root=benchmarks/results/glm51-spreadsheet-rl-native-v1-v2-full-20260929
root=benchmarks/results/glm51-spreadsheet-rl-native-provider-retry-20261001
dataset_v1=benchmarks/data/spreadsheetbench_912_v0.1
dataset_v2=benchmarks/data/spreadsheetbench-v2
evaluator_v2=benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py
visual_evaluator=/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py
parallelism=20

mkdir -p "$root/v1/tasks" "$root/v2/tasks"

.venv/bin/python - "$source_root" "$root/task-plan.tsv" <<'PY'
import json
import sys
from pathlib import Path

src, plan = map(Path, sys.argv[1:])
rows = []
for kind in ("v1", "v2"):
    for p in sorted((src / kind / "tasks").glob("*/results.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            # Only provider/API failures are eligible for a retry. RenderError,
            # turn-limit, routing, malformed-workbook and empty-instruction
            # failures remain zero-denominator failures under this protocol.
            err_type = str(item.get("error_type") or "")
            err = (str(item.get("error") or "") + " " + str(item.get("outcome_kind") or "")).lower()
            provider = (
                err_type == "ProviderError"
                or "ratelimit" in err
                or "rate limit" in err
                or "no deployments available" in err
                or "deployment" in err
                or "quota" in err
            )
            if not provider:
                continue
            task = str(item.get("task_id") or p.parent.name.replace("__", "/"))
            category = task.split("/", 1)[0] if kind == "v2" else ""
            rows.append((kind, task, category))
seen = set(); unique=[]
for row in rows:
    key=(row[0],row[1])
    if key not in seen:
        seen.add(key); unique.append(row)
plan.write_text("".join("\t".join(r)+"\n" for r in unique), encoding="utf-8")
print(f"provider_retry_tasks={len(unique)} v1={sum(r[0]=='v1' for r in unique)} v2={sum(r[0]=='v2' for r in unique)}")
PY

export dataset_v1 dataset_v2 evaluator_v2 visual_evaluator root
xargs -P "$parallelism" -d "\n" -I __ROW__ zsh -c '
  set -uo pipefail
  line="$1"
  kind="${line%%$'"'"'\t'"'"'*}"
  rest="${line#*$'"'"'\t'"'"'}"
  task="${rest%%$'"'"'\t'"'"'*}"
  category="${rest#*$'"'"'\t'"'"'}"
  slug="${task//\//__}"; slug="${slug// /_}"
  if [[ "$kind" == v1 ]]; then
    out="$root/v1/tasks/$slug"; log="$root/v1/tasks/$slug.log"
    if [[ -f "$out/results.json" ]]; then exit 0; fi
    .venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
      --dataset "$dataset_v1" --task-id "$task" --arm spreadsheet-rl-native --output "$out" \
      --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
      --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 \
      --litellm-timeout 600 --request-retries 5 --request-interval-seconds 1.1 \
      --arm-order-seed 20261001 --base-url http://10.130.138.46:8010/v1 \
      --api-key-file /tmp/spreadsheet-harness-litellm.key --model dashscope/glm-5.1 \
      --api-protocol chat-completions --reasoning-effort medium --seed 41 \
      --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1
  else
    out="$root/v2/tasks/$slug"; log="$root/v2/tasks/$slug.log"
    if [[ -f "$out/results.json" ]]; then exit 0; fi
    if [[ "$category" == Visualization ]]; then
      .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
        --dataset "$dataset_v2" --visual-evaluator "$visual_evaluator" \
        --task-id "$task" --arm spreadsheet-rl-native --output "$out" \
        --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
        --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 \
        --litellm-timeout 600 --request-retries 5 --request-interval-seconds 1.1 \
        --arm-order-seed 20261001 --base-url http://10.130.138.46:8010/v1 \
        --api-key-file /tmp/spreadsheet-harness-litellm.key --model dashscope/glm-5.1 \
        --api-protocol chat-completions --reasoning-effort medium --seed 41 \
        --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1
    else
      .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
        --dataset "$dataset_v2" --official-evaluator "$evaluator_v2" \
        --category "$category" --task-id "$task" --arm spreadsheet-rl-native --output "$out" \
        --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
        --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 \
        --litellm-timeout 600 --request-retries 5 --request-interval-seconds 1.1 \
        --arm-order-seed 20261001 --base-url http://10.130.138.46:8010/v1 \
        --api-key-file /tmp/spreadsheet-harness-litellm.key --model dashscope/glm-5.1 \
        --api-protocol chat-completions --reasoning-effort medium --seed 41 \
        --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1
    fi
  fi
' _ __ROW__ < "$root/task-plan.tsv"

.venv/bin/python - "$root" <<'PY'
import json
import sys
from pathlib import Path
root=Path(sys.argv[1])
plan=[line.split("\t") for line in (root/"task-plan.tsv").read_text().splitlines() if line.strip()]
rows=[]
for kind,task,_ in plan:
    slug=task.replace("/","__").replace(" ","_")
    rows.append({"kind":kind,"task_id":task,"result":(root/kind/"tasks"/slug/"results.json").is_file()})
summary={"model":"dashscope/glm-5.1","arm":"spreadsheet-rl-native","policy":"provider failures retried; retry failures excluded; non-provider failures count zero","expected":len(rows),"result_files":sum(r["result"] for r in rows),"missing":[r["task_id"] for r in rows if not r["result"]]}
(root/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
print(json.dumps(summary,ensure_ascii=False))
PY
