#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

RUN_ROOT="${RUN_ROOT:-benchmarks/results/four-models-90-4arm-20260905}"
RETRY_ROOT="${RETRY_ROOT:-benchmarks/results/four-models-90-4arm-timeout-retry-20260905}"
PARALLELISM_PER_MODEL="${PARALLELISM_PER_MODEL:-2}"
mkdir -p "$RETRY_ROOT"

.venv/bin/python - "$RUN_ROOT" "$RETRY_ROOT" <<'PY'
import json, sys
from pathlib import Path
from collections import defaultdict

run_root, retry_root = map(Path, sys.argv[1:])
tasks = [x.strip() for x in (run_root / "canonical_90_task_ids.txt").read_text().splitlines() if x.strip()]
arms = ["bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial"]
models = ["deepseek-v4-flash", "kimi-k2.6", "minimax-m2.7", "glm52"]

for slug in models:
    latest = {}
    for path in (run_root / slug).glob("*/results.json"):
        try: rows = json.loads(path.read_text())
        except Exception: continue
        if isinstance(rows, dict): rows = [rows]
        for row in rows:
            key = (row.get("task_id"), row.get("arm"))
            stamp = row.get("finished_at") or row.get("started_at") or ""
            if key[0] and key[1] and (key not in latest or stamp >= latest[key][0]):
                latest[key] = (stamp, row)
    grouped = defaultdict(list)
    for task in tasks:
        for arm in arms:
            if latest.get((task, arm), ("", {}))[1].get("status") != "completed":
                grouped[task].append(arm)
    plan = retry_root / f"{slug}.pending.tsv"
    plan.write_text("".join(f"{task}\t{','.join(sorted(a))}\n" for task, a in sorted(grouped.items())))
    print(slug, "retry_tasks", len(grouped), "retry_arms", sum(map(len, grouped.values())))
(retry_root / "canonical_90_task_ids.txt").write_text("\n".join(tasks) + "\n")
PY

run_model() {
  local slug="$1" model="$2" plan="$RETRY_ROOT/${slug}.pending.tsv" model_root="$RETRY_ROOT/$slug"
  mkdir -p "$model_root"
  [[ -s "$plan" ]] || { echo "$slug: no timeout/error rows"; return 0; }
  xargs -P "$PARALLELISM_PER_MODEL" -I '{}' bash -c '
    set -euo pipefail
    line="$1"; task="${line%%$'"'"'\t'"'"'*}"; csv="${line#*$'"'"'\t'"'"'}"
    slug_task="${task//\//_}"; out="$2/$slug_task"; log="$2/$slug_task.log"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    category="${task%%/*}"; args=(); IFS="," read -ra listed <<< "$csv"
    for arm in "${listed[@]}"; do args+=(--arm "$arm"); done
    exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
      --dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task" \
      "${args[@]}" --output "$out" --max-model-calls 8 --max-turns-per-arm 8 \
      --max-total-tokens 140000 --max-output-tokens 8192 --task-timeout 1800 \
      --request-timeout 600 --litellm-timeout 600 --request-retries 2 \
      --arm-order-seed 20260904 --base-url http://47.96.153.159:8010/v1 \
      --api-key-file /tmp/spreadsheet-harness-litellm.key --model "$3" \
      --api-protocol chat-completions --reasoning-effort medium --seed 41 \
      --temperature 1 --top-p 1 --disable-thinking > "$log" 2>&1
  ' _ '{}' "$model_root" "$model" < "$plan"
}

run_model deepseek-v4-flash DeepSeek-V4-Flash & p1=$!
run_model kimi-k2.6 Kimi-K2.6 & p2=$!
run_model minimax-m2.7 MiniMax-M2.7 & p3=$!
run_model glm52 GLM-5.2 & p4=$!
wait "$p1" "$p2" "$p3" "$p4"
echo "four-model timeout retry complete"
