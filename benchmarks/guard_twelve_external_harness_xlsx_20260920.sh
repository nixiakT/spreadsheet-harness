#!/usr/bin/env bash
set -euo pipefail

# Keep the official-xlsx matrix alive across transient CLI/proxy failures.  A
# completed task is never rerun; V1 replay failures and V2 failures that have
# not consumed all 50 accepted model requests are resumed from the same roots.
repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
results_root="$repo_root/benchmarks/results"
logs_root="$repo_root/benchmarks/logs"
tmux_socket="spreadsheetbench-xlsx-20260920"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
base_url="http://10.130.138.46:8010/v1"
skill="$repo_root/skills/xlsx/SKILL.md"
parallelism="${PARALLELISM:-6}"
interval="${GUARD_INTERVAL:-60}"
guardian_log="$logs_root/spreadsheetbench-xlsx-20260920-guardian.log"

declare -a specs=(
  "v1 deepseek codex DeepSeek-V4-Flash deepseek-v4-flash"
  "v1 qwen codex dashscope/qwen3-coder-480b-a35b-instruct qwen3-coder-480b"
  "v1 deepseek claude DeepSeek-V4-Flash deepseek-v4-flash"
  "v1 qwen claude dashscope/qwen3-coder-480b-a35b-instruct qwen3-coder-480b"
  "v1 deepseek dsh DeepSeek-V4-Flash deepseek-v4-flash"
  "v1 qwen dsh dashscope/qwen3-coder-480b-a35b-instruct qwen3-coder-480b"
  "v2 deepseek codex DeepSeek-V4-Flash deepseek-v4-flash"
  "v2 qwen codex dashscope/qwen3-coder-480b-a35b-instruct qwen3-coder-480b"
  "v2 deepseek claude DeepSeek-V4-Flash deepseek-v4-flash"
  "v2 qwen claude dashscope/qwen3-coder-480b-a35b-instruct qwen3-coder-480b"
  "v2 deepseek dsh DeepSeek-V4-Flash deepseek-v4-flash"
  "v2 qwen dsh dashscope/qwen3-coder-480b-a35b-instruct qwen3-coder-480b"
)

terminal_state() {
  local benchmark="$1" root="$2"
  "$python_bin" - "$benchmark" "$root" <<'PY'
import json, sys
from pathlib import Path
benchmark, root = sys.argv[1], Path(sys.argv[2])
expected = 912 if benchmark == "v1" else 321
paths = list((root / "tasks").glob("*/status.json"))
if len(paths) < expected:
    print("incomplete")
    raise SystemExit
for path in paths:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("incomplete")
        raise SystemExit
    if benchmark == "v1":
        if row.get("status") != "completed":
            print("incomplete")
            raise SystemExit
    else:
        status = row.get("status")
        requests = int(row.get("model_requests") or 0)
        # completed and an exhausted 50-request turn_limit are terminal.  A
        # failed/timeout task is retryable while it has request budget left.
        if status in {"failed", "timeout"} and requests < int(row.get("max_turns") or 50):
            print("incomplete")
            raise SystemExit
print("complete" if len(paths) == expected else "incomplete")
PY
}

launch_resume() {
  local benchmark="$1" family="$2" harness="$3" model="$4" model_slug="$5"
  local session="${benchmark}_${family}_${harness}_xlsx_20260920"
  local root="$results_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-xlsx-full-20260920"
  local log="$logs_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-xlsx-full-20260920.log"
  local runner args
  if [[ "$benchmark" == v1 ]]; then
    runner="$repo_root/benchmarks/run_external_harness_spreadsheetbench_v1.py"
    args="--harness '$harness' --model '$model' --run-root '$root' --dataset '$repo_root/benchmarks/data/spreadsheetbench_912_v0.1' --skill '$skill' --parallelism '$parallelism' --max-turns 50 --max-output-tokens 32768 --task-timeout 21600 --replay-timeout 1800 --base-url '$base_url' --api-key-file '$api_key_file'"
  else
    case "$harness" in
      codex) runner="$repo_root/benchmarks/run_codex_spreadsheetbench_v2.py" ;;
      claude) runner="$repo_root/benchmarks/run_claude_spreadsheetbench_v2.py" ;;
      dsh) runner="$repo_root/benchmarks/run_deepseek_harness_spreadsheetbench_v2.py" ;;
    esac
    args="--dataset '$repo_root/benchmarks/data/spreadsheetbench-v2' --run-root '$root' --skill '$skill' --parallelism '$parallelism' --max-turns 50 --task-timeout 21600 --model '$model' --base-url '$base_url' --api-key-file '$api_key_file' --recalculate-before-evaluation"
  fi
  printf '%s restarting %s\n' "$(date --iso-8601=seconds)" "$session" >> "$guardian_log"
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$runner' $args >> '$log' 2>&1"
}

mkdir -p "$logs_root"
while :; do
  remaining=0
  for spec in "${specs[@]}"; do
    read -r benchmark family harness model model_slug <<< "$spec"
    root="$results_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-xlsx-full-20260920"
    session="${benchmark}_${family}_${harness}_xlsx_20260920"
    if [[ "$(terminal_state "$benchmark" "$root")" == complete ]]; then
      continue
    fi
    remaining=$((remaining + 1))
    if ! tmux -L "$tmux_socket" has-session -t "$session" 2>/dev/null; then
      launch_resume "$benchmark" "$family" "$harness" "$model" "$model_slug"
    fi
  done
  (( remaining > 0 )) || exit 0
  sleep "$interval"
done
