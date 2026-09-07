#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

RUN_ROOT="${RUN_ROOT:-benchmarks/results/four-models-90-complete-retry-20260906}"
PARALLELISM_PER_MODEL="${PARALLELISM_PER_MODEL:-4}"
RUN_MODELS="${RUN_MODELS:-deepseek-v4-flash kimi-k2.6 minimax-m2.7 glm52}"
TASK_TIMEOUT="${TASK_TIMEOUT:-1200}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-600}"
LITELLM_TIMEOUT="${LITELLM_TIMEOUT:-600}"
export TASK_TIMEOUT REQUEST_TIMEOUT LITELLM_TIMEOUT
mkdir -p "$RUN_ROOT"

.venv/bin/python - "$RUN_ROOT" <<'PY'
import json, sys
from collections import defaultdict
from pathlib import Path

out = Path(sys.argv[1])
data = Path("benchmarks/data/spreadsheetbench-v2")
tasks = []
for cat in ("Debugging", "Financial_Model", "Template"):
    rows = json.loads((data / cat / "dataset.json").read_text())
    tasks += [f"{cat}/{str(r.get('id') or r.get('task_id'))}" for r in sorted(rows, key=lambda r: str(r.get('id') or r.get('task_id')))[:30]]
(out / "canonical_90_task_ids.txt").write_text("\n".join(tasks) + "\n")
arms = ["bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial"]
sources = {
 "deepseek-v4-flash": [Path("benchmarks/results/deepseek-v4-flash-30-4arm-20260904"), Path("benchmarks/results/four-models-90-3arm-fixed-20260906-rerun/deepseek-v4-flash"), Path("benchmarks/results/four-models-90-complete-retry-20260906/deepseek-v4-flash"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r2/deepseek-v4-flash"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r3/deepseek-v4-flash"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r4/deepseek-v4-flash")],
 "kimi-k2.6": [Path("benchmarks/results/kimi-k2.6-30-4arm-20260904"), Path("benchmarks/results/four-models-90-3arm-fixed-20260906-rerun/kimi-k2.6"), Path("benchmarks/results/four-models-90-complete-retry-20260906/kimi-k2.6"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r2/kimi-k2.6"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r3/kimi-k2.6"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r4/kimi-k2.6")],
 "minimax-m2.7": [Path("benchmarks/results/minimax-m2.7-30-4arm-20260904"), Path("benchmarks/results/four-models-90-3arm-fixed-20260906-rerun/minimax-m2.7"), Path("benchmarks/results/four-models-90-complete-retry-20260906/minimax-m2.7"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r2/minimax-m2.7"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r3/minimax-m2.7"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r4/minimax-m2.7")],
 "glm52": [Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904"), Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-remaining-parallel-20260904"), Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-timeout-retry-20260904"), Path("benchmarks/results/four-models-90-3arm-fixed-20260906-rerun/glm52"), Path("benchmarks/results/four-models-90-complete-retry-20260906/glm52"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r2/glm52"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r3/glm52"), Path("benchmarks/results/four-models-90-complete-retry-20260906-r4/glm52")],
}
sources["minimax-m2.7"].append(Path("benchmarks/results/four-models-90-complete-retry-20260906-r5/minimax-m2.7"))
sources["glm52"].append(Path("benchmarks/results/four-models-90-complete-retry-20260906-r5/glm52"))
sources["kimi-k2.6"].append(Path("benchmarks/results/four-models-90-complete-retry-20260906-r5/kimi-k2.6"))
for slug, roots in sources.items():
    done = set()
    for root in roots:
        for f in root.glob("*/results.json"):
            try: payload = json.loads(f.read_text())
            except Exception: continue
            for row in (payload if isinstance(payload, list) else [payload]):
                if row.get("status") == "completed" and row.get("task_id") in tasks and row.get("arm") in arms:
                    done.add((row["task_id"], row["arm"]))
    grouped = defaultdict(list)
    for task in tasks:
        missing = [a for a in arms if (task, a) not in done]
        if missing: grouped[task] = missing
    plan = out / f"{slug}.pending.tsv"
    plan.write_text("".join(f"{t}\t{','.join(a)}\n" for t, a in sorted(grouped.items())))
    print(slug, "reuse_arms", len(done), "retry_tasks", len(grouped), "retry_arms", sum(map(len, grouped.values())))
PY

run_model() {
  local slug="$1" model="$2"; local plan="$RUN_ROOT/$slug.pending.tsv"; local root="$RUN_ROOT/$slug"
  mkdir -p "$root"
  [[ -s "$plan" ]] || { echo "$slug: nothing to retry"; return 0; }
  echo "$slug: launching $(wc -l < "$plan") tasks at parallelism=$PARALLELISM_PER_MODEL"
  xargs -P "$PARALLELISM_PER_MODEL" -I '{}' bash -c '
    set -euo pipefail
    line="$1"; task="${line%%$'"'"'\t'"'"'*}"; csv="${line#*$'"'"'\t'"'"'}"; out="$2/${task//\//_}"; log="$2/${task//\//_}.log"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    category="${task%%/*}"; args=(); IFS="," read -ra aa <<< "$csv"; for a in "${aa[@]}"; do args+=(--arm "$a"); done
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare --dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task" "${args[@]}" --output "$out" --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 --max-output-tokens 8192 --task-timeout "$TASK_TIMEOUT" --request-timeout "$REQUEST_TIMEOUT" --litellm-timeout "$LITELLM_TIMEOUT" --request-retries 2 --arm-order-seed 20260904 --base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key --model "$3" --api-protocol chat-completions --reasoning-effort medium --seed 41 --temperature 1 --top-p 1 --disable-thinking > "$log" 2>&1
  ' _ '{}' "$root" "$model" < "$plan"
}
declare -a pids=()
if [[ " $RUN_MODELS " == *" deepseek-v4-flash "* ]]; then run_model deepseek-v4-flash DeepSeek-V4-Flash & pids+=("$!"); fi
if [[ " $RUN_MODELS " == *" kimi-k2.6 "* ]]; then run_model kimi-k2.6 Kimi-K2.6 & pids+=("$!"); fi
if [[ " $RUN_MODELS " == *" minimax-m2.7 "* ]]; then run_model minimax-m2.7 MiniMax-M2.7 & pids+=("$!"); fi
if [[ " $RUN_MODELS " == *" glm52 "* ]]; then run_model glm52 GLM-5.2 & pids+=("$!"); fi
(( ${#pids[@]} == 0 )) || wait "${pids[@]}"
echo "complete retry finished"
