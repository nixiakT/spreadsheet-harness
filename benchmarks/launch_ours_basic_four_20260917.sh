#!/usr/bin/env bash
set -uo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

V1_DEEPSEEK="benchmarks/results/ours-basic-deepseek-v1-20260917"
V1_QWEN="benchmarks/results/ours-basic-qwen-v1-20260917"
V2_DEEPSEEK="benchmarks/results/ours-basic-deepseek-v2-20260917"
V2_QWEN="benchmarks/results/ours-basic-qwen-v2-20260917"
VISUAL_EVALUATOR="/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py"

for root in "$V1_DEEPSEEK" "$V1_QWEN" "$V2_DEEPSEEK" "$V2_QWEN"; do
  [[ ! -e "$root" ]] || { echo "Fresh output already exists: $root" >&2; exit 2; }
done
[[ -f "$VISUAL_EVALUATOR" ]] || { echo "Missing visual evaluator" >&2; exit 2; }
[[ "$(sha256sum "$VISUAL_EVALUATOR" | awk '{print $1}')" == \
   "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703" ]] || {
  echo "Visual evaluator hash mismatch" >&2
  exit 2
}

run_v1() {
  local output="$1" model="$2"
  ARM=spreadsheet-harness-basic \
    bash benchmarks/run_spreadsheetbench_v1_harness_representative.sh \
      --workers 4 \
      --output "$output" \
      --model "$model" \
      --api-base-url http://10.130.138.46:8010/v1
}

run_v2() {
  local root="$1" model="$2"
  mkdir -p "$root"
  .venv/bin/python - "$root/pending.tsv" <<'PY'
import json
import sys
from pathlib import Path

plan = Path(sys.argv[1])
data = Path("benchmarks/data/spreadsheetbench-v2")
expected = {"Debugging": 100, "Financial_Model": 100, "Template": 97, "Visualization": 24}
rows = []
for category, expected_count in expected.items():
    payload = json.loads((data / category / "dataset.json").read_text(encoding="utf-8"))
    if len(payload) != expected_count:
        raise SystemExit(f"{category}: expected {expected_count}, found {len(payload)}")
    for item in sorted(payload, key=lambda value: str(value.get("id") or value.get("task_id"))):
        item_id = str(item.get("id") or item.get("task_id"))
        rows.append((f"{category}/{item_id}", category))
if len(rows) != 321:
    raise SystemExit(f"expected 321 tasks, found {len(rows)}")
plan.write_text("".join(f"{task}\t{category}\n" for task, category in rows), encoding="utf-8")
PY

  export BASIC_V2_MODEL="$model" BASIC_V2_ROOT="$root" VISUAL_EVALUATOR
  set +e
  xargs -P 4 -I '{}' bash -c '
    set -euo pipefail
    line="$1"
    task="${line%%$'"'"'\t'"'"'*}"
    category="${line#*$'"'"'\t'"'"'}"
    slug="${task//\//_}"
    out="$BASIC_V2_ROOT/$slug"
    log="$BASIC_V2_ROOT/$slug.log"
    source /data/zju-160/tongzeyuan/.config/litellm/lab.env
    unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
    provider=(
      --base-url http://10.130.138.46:8010/v1
      --api-key-file /tmp/spreadsheet-harness-litellm.key
      --model "$BASIC_V2_MODEL"
      --api-protocol chat-completions
      --reasoning-effort medium
      --seed 41
      --temperature 0
      --top-p 1
      --request-timeout 1800
      --litellm-timeout 1800
      --request-retries 5
      --enable-thinking
    )
    resources=(
      --max-model-calls 50
      --max-turns-per-arm 50
      --max-total-tokens 10000000
      --max-output-tokens 8192
      --task-timeout 21600
      --arm-order-seed 20260917
    )
    if [[ "$category" == Visualization ]]; then
      exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-visual-generate \
        --dataset benchmarks/data/spreadsheetbench-v2 \
        --visual-evaluator "$VISUAL_EVALUATOR" \
        --task-id "$task" --arm spreadsheet-harness-basic --output "$out" \
        "${resources[@]}" "${provider[@]}" >"$log" 2>&1
    else
      exec .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
        --dataset benchmarks/data/spreadsheetbench-v2 \
        --category "$category" --task-id "$task" \
        --arm spreadsheet-harness-basic --output "$out" \
        "${resources[@]}" "${provider[@]}" >"$log" 2>&1
    fi
  ' _ '{}' < "$root/pending.tsv"
  local rc=$?
  set -e
  echo "V2 model=$model finished xargs_rc=$rc"
  return 0
}

declare -a pids=()
run_v1 "$V1_DEEPSEEK" DeepSeek-V4-Flash \
  >"$V1_DEEPSEEK.launcher.log" 2>&1 & pids+=("$!")
run_v1 "$V1_QWEN" dashscope/qwen3-coder-480b-a35b-instruct \
  >"$V1_QWEN.launcher.log" 2>&1 & pids+=("$!")
run_v2 "$V2_DEEPSEEK" DeepSeek-V4-Flash \
  >"$V2_DEEPSEEK.launcher.log" 2>&1 & pids+=("$!")
run_v2 "$V2_QWEN" dashscope/qwen3-coder-480b-a35b-instruct \
  >"$V2_QWEN.launcher.log" 2>&1 & pids+=("$!")

failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done
echo "All four Basic experiment launchers finished; child_failure=$failed"
exit "$failed"
