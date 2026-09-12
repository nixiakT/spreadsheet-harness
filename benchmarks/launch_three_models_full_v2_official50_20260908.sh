#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

RUN_ROOT="${RUN_ROOT:-benchmarks/results/three-models-v2-official50-20260908-r1}"
RUN_MODELS="${RUN_MODELS:-deepseek-v4-flash minimax-m2.7 glm52}"
ARMS_CSV="${ARMS_CSV:-bare,spreadsheet-harness-basic,spreadsheet-harness-financial}"
DEEPSEEK_PARALLELISM="${DEEPSEEK_PARALLELISM:-8}"
MINIMAX_PARALLELISM="${MINIMAX_PARALLELISM:-8}"
GLM_PARALLELISM="${GLM_PARALLELISM:-6}"
MAX_MODEL_CALLS="${MAX_MODEL_CALLS:-50}"
MAX_TURNS_PER_ARM="${MAX_TURNS_PER_ARM:-50}"
MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-10000000}"
MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-8192}"
TASK_TIMEOUT="${TASK_TIMEOUT:-21600}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-700}"
LITELLM_TIMEOUT="${LITELLM_TIMEOUT:-600}"
REQUEST_RETRIES="${REQUEST_RETRIES:-5}"
VISUAL_EVALUATOR="${VISUAL_EVALUATOR:-/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py}"

[[ "$MAX_MODEL_CALLS" == 50 && "$MAX_TURNS_PER_ARM" == 50 ]] || {
  echo "SpreadsheetBench v2 official protocol requires 50 calls/turns per arm" >&2
  exit 2
}
[[ -f "$VISUAL_EVALUATOR" ]] || {
  echo "Missing pinned official visualization evaluator: $VISUAL_EVALUATOR" >&2
  exit 2
}
[[ "$(sha256sum "$VISUAL_EVALUATOR" | awk '{print $1}')" == \
   "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703" ]] || {
  echo "Official visualization evaluator hash mismatch" >&2
  exit 2
}
[[ ! -e "$RUN_ROOT" ]] || {
  echo "Fresh official run root already exists: $RUN_ROOT" >&2
  exit 2
}

mkdir -p "$RUN_ROOT"

.venv/bin/python - "$RUN_ROOT" <<'PY'
import json
import os
import sys
from pathlib import Path

out = Path(sys.argv[1])
data = Path("benchmarks/data/spreadsheetbench-v2")
arms_csv = os.environ.get("ARMS_CSV", "bare,spreadsheet-harness-basic,spreadsheet-harness-financial")
allowed_arms = {"bare", "ours", "native", "paper", "spreadsheet-rl-minimal", "spreadsheet-rl-native", "paper-vision", "spreadsheet-harness-basic", "spreadsheet-harness-financial"}
if not arms_csv or any(arm not in allowed_arms for arm in arms_csv.split(",")):
    raise SystemExit(f"invalid ARMS_CSV={arms_csv!r}")
expected = {"Debugging": 100, "Financial_Model": 100, "Template": 97, "Visualization": 24}
rows = []
for category, expected_count in expected.items():
    payload = json.loads((data / category / "dataset.json").read_text(encoding="utf-8"))
    if len(payload) != expected_count:
        raise SystemExit(f"{category}: expected {expected_count} tasks, found {len(payload)}")
    for item in sorted(payload, key=lambda value: str(value.get("id") or value.get("task_id"))):
        item_id = str(item.get("id") or item.get("task_id"))
        rows.append((f"{category}/{item_id}", category))
if len(rows) != 321:
    raise SystemExit(f"expected 321 v2 tasks, found {len(rows)}")
(out / "canonical_v2_task_ids.txt").write_text(
    "".join(f"{task_id}\n" for task_id, _ in rows), encoding="utf-8"
)
for slug in ("deepseek-v4-flash", "minimax-m2.7", "glm52"):
    (out / f"{slug}.pending.tsv").write_text(
        "".join(
        f"{task_id}\t{category}\t{arms_csv}\n"
            for task_id, category in rows
        ),
        encoding="utf-8",
    )
print("official_v2_tasks=321 arms_per_model=", len(arms_csv.split(",")) * 321)
PY

run_model() {
  local slug="$1" model="$2" parallelism="$3"
  local plan="$RUN_ROOT/$slug.pending.tsv" root="$RUN_ROOT/$slug"
  mkdir -p "$root"
  echo "$slug: launching 321 tasks / 963 arms at parallelism=$parallelism"
  xargs -P "$parallelism" -I '{}' bash -c '
    set -euo pipefail
    line="$1"
    task="${line%%$'"'"'\t'"'"'*}"
    rest="${line#*$'"'"'\t'"'"'}"
    category="${rest%%$'"'"'\t'"'"'*}"
    csv="${rest#*$'"'"'\t'"'"'}"
    out="$2/${task//\//_}"
    log="$2/${task//\//_}.log"
    model="$3"
    slug="$4"
    visual_evaluator="$5"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    arms=()
    IFS="," read -ra selected <<< "$csv"
    for arm in "${selected[@]}"; do arms+=(--arm "$arm"); done
    provider_args=(
      --base-url http://47.96.153.159:8010/v1
      --api-key-file /tmp/spreadsheet-harness-litellm.key
      --model "$model"
      --api-protocol chat-completions
      --reasoning-effort medium
      --temperature 0
      --top-p 1
      --request-timeout "$REQUEST_TIMEOUT"
      --litellm-timeout "$LITELLM_TIMEOUT"
      --request-retries "$REQUEST_RETRIES"
    )
    provider_args+=(--enable-thinking)
    resource_args=(
      --max-model-calls "$MAX_MODEL_CALLS"
      --max-turns-per-arm "$MAX_TURNS_PER_ARM"
      --max-total-tokens "$MAX_TOTAL_TOKENS"
      --max-output-tokens "$MAX_OUTPUT_TOKENS"
      --task-timeout "$TASK_TIMEOUT"
      --arm-order-seed 20260908
    )
    if [[ "$category" == Visualization ]]; then
      exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
        --dataset benchmarks/data/spreadsheetbench-v2 \
        --visual-evaluator "$visual_evaluator" \
        --task-id "$task" "${arms[@]}" --output "$out" \
        "${resource_args[@]}" "${provider_args[@]}" >"$log" 2>&1
    else
      exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
        --dataset benchmarks/data/spreadsheetbench-v2 \
        --category "$category" --task-id "$task" "${arms[@]}" --output "$out" \
        "${resource_args[@]}" "${provider_args[@]}" >"$log" 2>&1
    fi
  ' _ '{}' "$root" "$model" "$slug" "$VISUAL_EVALUATOR" < "$plan"
}

export MAX_MODEL_CALLS MAX_TURNS_PER_ARM MAX_TOTAL_TOKENS MAX_OUTPUT_TOKENS
export TASK_TIMEOUT REQUEST_TIMEOUT LITELLM_TIMEOUT REQUEST_RETRIES

declare -a pids=()
if [[ " $RUN_MODELS " == *" deepseek-v4-flash "* ]]; then
  run_model deepseek-v4-flash DeepSeek-V4-Flash "$DEEPSEEK_PARALLELISM" & pids+=("$!")
fi
if [[ " $RUN_MODELS " == *" minimax-m2.7 "* ]]; then
  run_model minimax-m2.7 MiniMax-M2.7 "$MINIMAX_PARALLELISM" & pids+=("$!")
fi
if [[ " $RUN_MODELS " == *" glm52 "* ]]; then
  run_model glm52 GLM-5.2 "$GLM_PARALLELISM" & pids+=("$!")
fi
(( ${#pids[@]} > 0 )) || { echo "No selected models" >&2; exit 2; }
wait "${pids[@]}"
echo "official 50-call SpreadsheetBench v2 run finished"
