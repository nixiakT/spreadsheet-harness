#!/usr/bin/env bash
set -u

repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
runner="$repo_root/benchmarks/run_external_harness_spreadsheetbench_v1.py"
results_root="$repo_root/benchmarks/results"
logs_root="$repo_root/benchmarks/logs"
dataset="$repo_root/benchmarks/data/spreadsheetbench_912_v0.1"
skill="$repo_root/skills/spreadsheet-core/SKILL.md"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
base_url="http://10.130.138.46:8010/v1"
guardian_log="$logs_root/spreadsheetbench-v1-six-core-guardian-20260917.log"
tmux_socket="spreadsheetbench-v1-six-core-20260917"

entries=(
  "v1_qwen_codex_core_20260917|codex|dashscope/qwen3-coder-480b-a35b-instruct|spreadsheetbench-v1-qwen3-coder-480b-codex-core-full-20260917"
  "v1_qwen_claude_core_20260917|claude|dashscope/qwen3-coder-480b-a35b-instruct|spreadsheetbench-v1-qwen3-coder-480b-claude-core-full-20260917"
  "v1_qwen_dsh_core_20260917|dsh|dashscope/qwen3-coder-480b-a35b-instruct|spreadsheetbench-v1-qwen3-coder-480b-dsh-core-full-20260917"
  "v1_deepseek_codex_core_20260917|codex|DeepSeek-V4-Flash|spreadsheetbench-v1-deepseek-v4-flash-codex-core-full-20260917"
  "v1_deepseek_claude_core_20260917|claude|DeepSeek-V4-Flash|spreadsheetbench-v1-deepseek-v4-flash-claude-core-full-20260917"
  "v1_deepseek_dsh_core_20260917|dsh|DeepSeek-V4-Flash|spreadsheetbench-v1-deepseek-v4-flash-dsh-core-full-20260917"
)

recorded_count() {
  local output="$1"
  "$python_bin" - "$output/results.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = []
print(len(value) if isinstance(value, list) else 0)
PY
}

while true; do
  all_recorded=1
  for entry in "${entries[@]}"; do
    IFS='|' read -r session harness model leaf <<< "$entry"
    output="$results_root/$leaf"
    log="$logs_root/$leaf.log"
    count="$(recorded_count "$output")"
    if (( count < 912 )); then
      all_recorded=0
      if ! tmux -L "$tmux_socket" has-session -t "$session" 2>/dev/null; then
        printf '%s restarting %s at %s/912\n' "$(date --iso-8601=seconds)" "$session" "$count" >> "$guardian_log"
        tmux -L "$tmux_socket" new-session -d -s "$session" \
          "cd '$repo_root' && exec '$python_bin' '$runner' \
            --harness '$harness' --model '$model' --run-root '$output' \
            --dataset '$dataset' --skill '$skill' --parallelism 6 \
            --max-turns 50 --max-output-tokens 32768 --task-timeout 21600 \
            --replay-timeout 1800 --base-url '$base_url' \
            --api-key-file '$api_key_file' >> '$log' 2>&1"
      fi
    fi
  done
  if (( all_recorded == 1 )); then
    printf '%s all six runs recorded 912 instructions\n' "$(date --iso-8601=seconds)" >> "$guardian_log"
    exit 0
  fi
  sleep 60
done
