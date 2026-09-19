#!/usr/bin/env bash
set -euo pipefail

repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
results_root="$repo_root/benchmarks/results"
logs_root="$repo_root/benchmarks/logs"
tmux_socket="spreadsheetbench-no-skill-20260918"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
base_url="http://10.130.138.46:8010/v1"
parallelism="${PARALLELISM:-6}"
interval="${GUARD_INTERVAL:-60}"

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
statuses = list((root / "tasks").glob("*/status.json")) if benchmark == "v2" else list((root / "tasks").glob("*/status.json"))
if len(statuses) < expected:
    print("incomplete")
    raise SystemExit(0)
seen = set()
for path in statuses:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("incomplete")
        raise SystemExit(0)
    key = str(row.get("task_id") or "")
    if not key:
        print("incomplete")
        raise SystemExit(0)
    seen.add(key)
    if benchmark == "v1":
        if row.get("status") not in {"completed", "not_scored"}:
            print("incomplete")
            raise SystemExit(0)
        if row.get("status") == "not_scored" and not isinstance(row.get("generation"), dict):
            print("incomplete")
            raise SystemExit(0)
    elif row.get("status") in {"failed", "timeout"} and int(row.get("model_requests") or 0) < int(row.get("max_turns") or 50):
        print("incomplete")
        raise SystemExit(0)
print("complete" if len(seen) == expected else "incomplete")
PY
}

launch_resume() {
  local benchmark="$1" family="$2" harness="$3" model="$4" model_slug="$5"
  local session="${benchmark}_${family}_${harness}_noskill_20260918"
  local root="$results_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-no-skill-full-20260918"
  local log="$logs_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-no-skill-full-20260918.log"
  local runner args
  if [[ "$benchmark" == v1 ]]; then
    runner="$repo_root/benchmarks/run_external_harness_spreadsheetbench_v1.py"
    args="--harness '$harness' --model '$model' --run-root '$root' --dataset '$repo_root/benchmarks/data/spreadsheetbench_912_v0.1' --no-skill --parallelism '$parallelism' --max-turns 50 --max-output-tokens 32768 --task-timeout 21600 --replay-timeout 1800 --base-url '$base_url' --api-key-file '$api_key_file'"
  else
    case "$harness" in
      codex) runner="$repo_root/benchmarks/run_codex_spreadsheetbench_v2.py" ;;
      claude) runner="$repo_root/benchmarks/run_claude_spreadsheetbench_v2.py" ;;
      dsh) runner="$repo_root/benchmarks/run_deepseek_harness_spreadsheetbench_v2.py" ;;
    esac
    args="--dataset '$repo_root/benchmarks/data/spreadsheetbench-v2' --run-root '$root' --no-skill --parallelism '$parallelism' --max-turns 50 --task-timeout 21600 --model '$model' --base-url '$base_url' --api-key-file '$api_key_file' --recalculate-before-evaluation"
  fi
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$runner' $args >> '$log' 2>&1"
}

mkdir -p "$logs_root"
while :; do
  remaining=0
  for spec in "${specs[@]}"; do
    read -r benchmark family harness model model_slug <<< "$spec"
    root="$results_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-no-skill-full-20260918"
    session="${benchmark}_${family}_${harness}_noskill_20260918"
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
