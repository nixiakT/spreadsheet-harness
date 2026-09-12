#!/usr/bin/env bash
set -euo pipefail

# Extend the existing runs to the same 90-case manifest used by the Qwen36 run
# (the first 30 lexicographic tasks in each category), using the current
# three-arm experiment only.  spreadsheet-rl-minimal is intentionally excluded.
# Existing completed arms are reused logically; only missing/error arms are run.
# This launcher uses the longer timeout policy requested for the slow relays.

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

RUN_ROOT="${RUN_ROOT:-benchmarks/results/four-models-90-4arm-20260905}"
PARALLELISM_PER_MODEL="${PARALLELISM_PER_MODEL:-2}"
TASK_TIMEOUT="${TASK_TIMEOUT:-900}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-300}"
LITELLM_TIMEOUT="${LITELLM_TIMEOUT:-300}"
export TASK_TIMEOUT REQUEST_TIMEOUT LITELLM_TIMEOUT
mkdir -p "$RUN_ROOT"

python_bin=".venv/bin/python"

# Build one canonical 90-task list and one pending plan per model.  The old
# 30-case folders are read-only inputs; this run writes to a fresh root.
"$python_bin" - "$RUN_ROOT" <<'PY'
import json, sys
from pathlib import Path
from collections import defaultdict

out = Path(sys.argv[1])
data_root = Path("benchmarks/data/spreadsheetbench-v2")
tasks = []
for category in ("Debugging", "Financial_Model", "Template"):
    rows = json.loads((data_root / category / "dataset.json").read_text())
    ids = sorted(str(row.get("id") or row.get("task_id")) for row in rows)[:30]
    tasks.extend(f"{category}/{item_id}" for item_id in ids)
(out / "canonical_90_task_ids.txt").write_text("\n".join(tasks) + "\n")

arms = ["bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial"]
models = {
    "deepseek-v4-flash": ("DeepSeek-V4-Flash", [Path("benchmarks/results/deepseek-v4-flash-30-4arm-20260904")]),
    "kimi-k2.6": ("Kimi-K2.6", [Path("benchmarks/results/kimi-k2.6-30-4arm-20260904")]),
    "minimax-m2.7": ("MiniMax-M2.7", [Path("benchmarks/results/minimax-m2.7-30-4arm-20260904")]),
    "glm52": ("GLM-5.2", [
        Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904"),
        Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-remaining-parallel-20260904"),
        Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-timeout-retry-20260904"),
    ]),
}

def load_latest(roots):
    latest = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*/results.json"):
            try:
                rows = json.loads(path.read_text())
            except Exception:
                continue
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                key = (row.get("task_id"), row.get("arm"))
                if key[0] is None or key[1] is None:
                    continue
                stamp = row.get("finished_at") or row.get("started_at") or ""
                if key not in latest or stamp >= latest[key][0]:
                    latest[key] = (stamp, row)
    return latest

for slug, (_, roots) in models.items():
    latest = load_latest(roots)
    plan = []
    for task in tasks:
        for arm in arms:
            row = latest.get((task, arm), ("", {}))[1]
            # Keep completed rows from the earlier 30-case runs.  Re-run all
            # errors/not-scored rows with the 600/1800 second policy.
            if row.get("status") == "completed":
                continue
            plan.append((task, arm))
    # Group arms by task so each workbook task gets one isolated CLI invocation.
    grouped = defaultdict(list)
    for task, arm in plan:
        grouped[task].append(arm)
    plan_path = out / f"{slug}.pending.tsv"
    plan_path.write_text("".join(f"{task}\t{','.join(sorted(grouped[task]))}\n" for task in sorted(grouped)))
    print(slug, "pending_tasks", len(grouped), "pending_arms", len(plan), "plan", plan_path)
PY

run_model() {
  local slug="$1" model="$2" api_model="$3"
  local plan="$RUN_ROOT/${slug}.pending.tsv"
  local model_root="$RUN_ROOT/$slug"
  mkdir -p "$model_root"
  if [[ ! -s "$plan" ]]; then
    echo "$slug: no pending tasks"
    return 0
  fi
  echo "$slug: launching $(wc -l < "$plan") tasks at parallelism=$PARALLELISM_PER_MODEL"
  xargs -P "$PARALLELISM_PER_MODEL" -I '{}' bash -c '
    set -euo pipefail
    line="$1"; task="${line%%$'"'"'\t'"'"'*}"; csv="${line#*$'"'"'\t'"'"'}"
    slug_task="${task//\//_}"; out="$2/$slug_task"; log="$2/$slug_task.log"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    category="${task%%/*}"; args=()
    IFS="," read -ra listed <<< "$csv"
    for arm in "${listed[@]}"; do args+=(--arm "$arm"); done
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
      --dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task" \
      "${args[@]}" --output "$out" \
      --max-model-calls 8 --max-turns-per-arm 8 --max-total-tokens 140000 \
      --max-output-tokens 8192 --task-timeout "$TASK_TIMEOUT" --request-timeout "$REQUEST_TIMEOUT" \
      --litellm-timeout "$LITELLM_TIMEOUT" --request-retries 1 --arm-order-seed 20260904 \
      --base-url http://47.96.153.159:8010/v1 \
      --api-key-file /tmp/spreadsheet-harness-litellm.key \
      --model "$3" --api-protocol chat-completions --reasoning-effort medium \
      --seed 41 --temperature 1 --top-p 1 --disable-thinking \
      > "$log" 2>&1
  ' _ '{}' "$model_root" "$model" < "$plan"
  echo "$slug: launch complete"
}

run_model deepseek-v4-flash DeepSeek-V4-Flash DeepSeek-V4-Flash & p1=$!
run_model kimi-k2.6 Kimi-K2.6 dashscope/Kimi-K2.6 & p2=$!
run_model minimax-m2.7 MiniMax-M2.7 dashscope/MiniMax-M2.7 & p3=$!
run_model glm52 GLM-5.2 dashscope/GLM-5.2 & p4=$!
wait "$p1" "$p2" "$p3" "$p4"
echo "four-model 90-case extension complete"
