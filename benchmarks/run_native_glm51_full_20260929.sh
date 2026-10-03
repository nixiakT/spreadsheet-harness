#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

dataset_v1=benchmarks/data/spreadsheetbench_912_v0.1
dataset_v2=benchmarks/data/spreadsheetbench-v2
evaluator_v2=benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py
visual_evaluator=/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py
root=benchmarks/results/glm51-spreadsheet-rl-native-v1-v2-full-20260929
parallelism=20

[[ -r /tmp/spreadsheet-harness-litellm.key ]] || { echo "Missing LiteLLM key file" >&2; exit 2; }
[[ -r "$evaluator_v2" ]] || { echo "Missing v2 evaluator" >&2; exit 2; }
[[ -r "$visual_evaluator" ]] || { echo "Missing visual evaluator" >&2; exit 2; }
mkdir -p "$root/v1/tasks" "$root/v2/tasks"

.venv/bin/python - "$dataset_v1" "$dataset_v2" "$root/task-plan.tsv" <<'PY'
import json
import sys
from pathlib import Path

v1, v2, plan = map(Path, sys.argv[1:])
rows = []
v1_rows = json.loads((v1 / "dataset.json").read_text(encoding="utf-8"))
if len(v1_rows) != 912:
    raise SystemExit(f"expected 912 v1 tasks, found {len(v1_rows)}")
for item in v1_rows:
    rows.append(("v1", str(item["id"]), ""))
expected = {"Debugging": 100, "Financial_Model": 100, "Template": 97, "Visualization": 24}
for category, count in expected.items():
    payload = json.loads((v2 / category / "dataset.json").read_text(encoding="utf-8"))
    if len(payload) != count:
        raise SystemExit(f"expected {count} {category} tasks, found {len(payload)}")
    for item in payload:
        rows.append(("v2", f"{category}/{item.get('id') or item.get('task_id')}", category))
plan.write_text("".join("\t".join(row) + "\n" for row in rows), encoding="utf-8")
print(f"planned_v1={sum(r[0]=='v1' for r in rows)} planned_v2={sum(r[0]=='v2' for r in rows)} total={len(rows)}")
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
    if [[ -e "$out" ]]; then echo "existing incomplete v1 output: $out" >&2; exit 3; fi
    .venv/bin/python -m spreadsheet_harness.cli benchmark v1-compare \
      --dataset "$dataset_v1" --task-id "$task" --arm spreadsheet-rl-native --output "$out" \
      --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
      --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 \
      --litellm-timeout 600 --request-retries 5 --request-interval-seconds 1.1 \
      --arm-order-seed 20260929 --base-url http://10.130.138.46:8010/v1 \
      --api-key-file /tmp/spreadsheet-harness-litellm.key --model dashscope/glm-5.1 \
      --api-protocol chat-completions --reasoning-effort medium --seed 41 \
      --temperature 0 --top-p 1 --enable-thinking >"$log" 2>&1
  else
    out="$root/v2/tasks/$slug"; log="$root/v2/tasks/$slug.log"
    if [[ -f "$out/results.json" ]]; then exit 0; fi
    if [[ -e "$out" ]]; then echo "existing incomplete v2 output: $out" >&2; exit 3; fi
    if [[ "$category" == Visualization ]]; then
      .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
        --dataset "$dataset_v2" --visual-evaluator "$visual_evaluator" \
        --task-id "$task" --arm spreadsheet-rl-native --output "$out" \
        --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 \
        --max-output-tokens 32768 --task-timeout 21600 --request-timeout 600 \
        --litellm-timeout 600 --request-retries 5 --request-interval-seconds 1.1 \
        --arm-order-seed 20260929 --base-url http://10.130.138.46:8010/v1 \
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
        --arm-order-seed 20260929 --base-url http://10.130.138.46:8010/v1 \
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

root = Path(sys.argv[1])
plan = [line.split("\t") for line in (root / "task-plan.tsv").read_text().splitlines() if line.strip()]
counts = {"v1": {"expected": 0, "results": 0}, "v2": {"expected": 0, "results": 0}}
missing = []
for kind, task, _category in plan:
    counts[kind]["expected"] += 1
    slug = task.replace("/", "__").replace(" ", "_")
    out = root / kind / "tasks" / slug
    if (out / "results.json").is_file():
        counts[kind]["results"] += 1
    else:
        missing.append(f"{kind}:{task}")
summary = {"model": "dashscope/glm-5.1", "arm": "spreadsheet-rl-native",
           "parallelism": 20, "counts": counts, "missing": missing}
(root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary, ensure_ascii=False))
raise SystemExit(0 if not missing else 2)
PY
