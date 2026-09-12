#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

SOURCE_ROOT="${SOURCE_ROOT:-benchmarks/results/three-models-v2-official50-20260908-r3}"
RUN_ROOT="${RUN_ROOT:-benchmarks/results/three-models-v2-official50-20260908-r4}"
DEEPSEEK_PARALLELISM="${DEEPSEEK_PARALLELISM:-8}"
MINIMAX_PARALLELISM="${MINIMAX_PARALLELISM:-8}"
GLM_PARALLELISM="${GLM_PARALLELISM:-6}"
MAX_MODEL_CALLS=50
MAX_TURNS_PER_ARM=50
MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-10000000}"
MAX_OUTPUT_TOKENS=8192
TASK_TIMEOUT="${TASK_TIMEOUT:-21600}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-700}"
LITELLM_TIMEOUT="${LITELLM_TIMEOUT:-600}"
REQUEST_RETRIES="${REQUEST_RETRIES:-5}"
VISUAL_EVALUATOR="${VISUAL_EVALUATOR:-/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py}"
REUSE_COMPLETED="${REUSE_COMPLETED:-0}"

[[ ! -e "$RUN_ROOT" ]] || { echo "Fresh continuation root already exists: $RUN_ROOT" >&2; exit 2; }
[[ -d "$SOURCE_ROOT" ]] || { echo "Missing source root: $SOURCE_ROOT" >&2; exit 2; }
[[ -f "$VISUAL_EVALUATOR" ]] || { echo "Missing official visualization evaluator" >&2; exit 2; }
[[ "$(sha256sum "$VISUAL_EVALUATOR" | awk '{print $1}')" == 8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703 ]] || { echo "Visualization evaluator hash mismatch" >&2; exit 2; }
mkdir -p "$RUN_ROOT"

.venv/bin/python - "$SOURCE_ROOT" "$RUN_ROOT" "$REUSE_COMPLETED" <<'PY'
import json, sys
from collections import defaultdict
from pathlib import Path

source, out = Path(sys.argv[1]), Path(sys.argv[2])
reuse_completed = sys.argv[3] == "1"
data = Path("benchmarks/data/spreadsheetbench-v2")
arms = ("bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial")
tasks = []
for category in ("Debugging", "Financial_Model", "Template", "Visualization"):
    payload = json.loads((data / category / "dataset.json").read_text(encoding="utf-8"))
    for item in sorted(payload, key=lambda x: str(x.get("id") or x.get("task_id"))):
        item_id = str(item.get("id") or item.get("task_id"))
        tasks.append((f"{category}/{item_id}", category))
completed = {slug: set() for slug in ("deepseek-v4-flash", "minimax-m2.7", "glm52")}
if reuse_completed:
    for slug in completed:
        for result_file in (source / slug).glob("*/results.json"):
            try: payload = json.loads(result_file.read_text(encoding="utf-8"))
            except Exception: continue
            for row in payload if isinstance(payload, list) else [payload]:
                if row.get("status") == "completed":
                    completed[slug].add((row.get("task_id"), row.get("arm")))
(out / "canonical_v2_task_ids.txt").write_text("".join(f"{t}\n" for t, _ in tasks), encoding="utf-8")
for slug in completed:
    grouped = defaultdict(list)
    for task, category in tasks:
        missing = [arm for arm in arms if (task, arm) not in completed[slug]]
        if missing: grouped[(task, category)] = missing
    plan = out / f"{slug}.pending.tsv"
    plan.write_text("".join(f"{task}\t{category}\t{','.join(missing)}\n" for (task, category), missing in sorted(grouped.items())), encoding="utf-8")
    print(slug, "reuse_completed_arms", len(completed[slug]), "retry_or_missing_arms", sum(len(v) for v in grouped.values()), "tasks", len(grouped))
PY

run_model() {
  local slug="$1" model="$2" parallelism="$3"
  local plan="$RUN_ROOT/$slug.pending.tsv" root="$RUN_ROOT/$slug"
  mkdir -p "$root"
  [[ -s "$plan" ]] || { echo "$slug: nothing to retry"; return 0; }
  set +e
  xargs -P "$parallelism" -I '{}' bash -c '
    set -euo pipefail
    line="$1"; task="${line%%$'"'"'\t'"'"'*}"; rest="${line#*$'"'"'\t'"'"'}"; category="${rest%%$'"'"'\t'"'"'*}"; csv="${rest#*$'"'"'\t'"'"'}"
    out="$2/${task//\//_}"; log="$2/${task//\//_}.log"; model="$3"; slug="$4"; visual="$5"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    args=(); IFS="," read -ra selected <<< "$csv"; for arm in "${selected[@]}"; do args+=(--arm "$arm"); done
    provider=(--base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key --model "$model" --api-protocol chat-completions --reasoning-effort medium --temperature 0 --top-p 1 --request-timeout "$REQUEST_TIMEOUT" --litellm-timeout "$LITELLM_TIMEOUT" --request-retries "$REQUEST_RETRIES")
    provider+=(--enable-thinking)
    resources=(--max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens "$MAX_TOTAL_TOKENS" --max-output-tokens 8192 --task-timeout "$TASK_TIMEOUT" --arm-order-seed 20260908)
    if [[ "$category" == Visualization ]]; then
      exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate --dataset benchmarks/data/spreadsheetbench-v2 --visual-evaluator "$visual" --task-id "$task" "${args[@]}" --output "$out" "${resources[@]}" "${provider[@]}" >"$log" 2>&1
    else
      exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare --dataset benchmarks/data/spreadsheetbench-v2 --category "$category" --task-id "$task" "${args[@]}" --output "$out" "${resources[@]}" "${provider[@]}" >"$log" 2>&1
    fi
  ' _ '{}' "$root" "$model" "$slug" "$VISUAL_EVALUATOR" < "$plan"
  local rc=$?
  set -e
  echo "$slug: xargs finished with rc=$rc; individual failures were retained" >&2
  return 0
}

export MAX_TOTAL_TOKENS TASK_TIMEOUT REQUEST_TIMEOUT LITELLM_TIMEOUT REQUEST_RETRIES
declare -a pids=()
run_model deepseek-v4-flash DeepSeek-V4-Flash "$DEEPSEEK_PARALLELISM" & pids+=("$!")
run_model minimax-m2.7 MiniMax-M2.7 "$MINIMAX_PARALLELISM" & pids+=("$!")
run_model glm52 GLM-5.2 "$GLM_PARALLELISM" & pids+=("$!")
wait "${pids[@]}"
echo "official 50-call continuation finished"
