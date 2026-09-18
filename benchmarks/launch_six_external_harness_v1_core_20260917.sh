#!/usr/bin/env bash
set -euo pipefail

repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
runner="$repo_root/benchmarks/run_external_harness_spreadsheetbench_v1.py"
results_root="$repo_root/benchmarks/results"
logs_root="$repo_root/benchmarks/logs"
tmux_socket="spreadsheetbench-v1-six-core-20260917"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
skill="$repo_root/skills/spreadsheet-core/SKILL.md"
base_url="http://10.130.138.46:8010/v1"

mkdir -p "$logs_root"

launch() {
  local session="$1"
  local harness="$2"
  local model="$3"
  local output="$4"
  local log="$5"

  [[ ! -e "$output" ]] || { echo "output already exists: $output" >&2; return 2; }
  if tmux -L "$tmux_socket" has-session -t "$session" 2>/dev/null; then
    echo "tmux session already exists: $session" >&2
    return 2
  fi
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$runner' \
      --harness '$harness' --model '$model' --run-root '$output' \
      --dataset '$repo_root/benchmarks/data/spreadsheetbench_912_v0.1' \
      --skill '$skill' --parallelism 6 --max-turns 50 \
      --max-output-tokens 32768 --task-timeout 21600 --replay-timeout 1800 \
      --base-url '$base_url' --api-key-file '$api_key_file' \
      >> '$log' 2>&1"
  echo "$session $harness $model $output"
}

launch \
  "v1_qwen_codex_core_20260917" codex \
  "dashscope/qwen3-coder-480b-a35b-instruct" \
  "$results_root/spreadsheetbench-v1-qwen3-coder-480b-codex-core-full-20260917" \
  "$logs_root/spreadsheetbench-v1-qwen3-coder-480b-codex-core-full-20260917.log"
launch \
  "v1_qwen_claude_core_20260917" claude \
  "dashscope/qwen3-coder-480b-a35b-instruct" \
  "$results_root/spreadsheetbench-v1-qwen3-coder-480b-claude-core-full-20260917" \
  "$logs_root/spreadsheetbench-v1-qwen3-coder-480b-claude-core-full-20260917.log"
launch \
  "v1_qwen_dsh_core_20260917" dsh \
  "dashscope/qwen3-coder-480b-a35b-instruct" \
  "$results_root/spreadsheetbench-v1-qwen3-coder-480b-dsh-core-full-20260917" \
  "$logs_root/spreadsheetbench-v1-qwen3-coder-480b-dsh-core-full-20260917.log"
launch \
  "v1_deepseek_codex_core_20260917" codex \
  "DeepSeek-V4-Flash" \
  "$results_root/spreadsheetbench-v1-deepseek-v4-flash-codex-core-full-20260917" \
  "$logs_root/spreadsheetbench-v1-deepseek-v4-flash-codex-core-full-20260917.log"
launch \
  "v1_deepseek_claude_core_20260917" claude \
  "DeepSeek-V4-Flash" \
  "$results_root/spreadsheetbench-v1-deepseek-v4-flash-claude-core-full-20260917" \
  "$logs_root/spreadsheetbench-v1-deepseek-v4-flash-claude-core-full-20260917.log"
launch \
  "v1_deepseek_dsh_core_20260917" dsh \
  "DeepSeek-V4-Flash" \
  "$results_root/spreadsheetbench-v1-deepseek-v4-flash-dsh-core-full-20260917" \
  "$logs_root/spreadsheetbench-v1-deepseek-v4-flash-dsh-core-full-20260917.log"
