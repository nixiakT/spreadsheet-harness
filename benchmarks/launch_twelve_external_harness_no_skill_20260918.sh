#!/usr/bin/env bash
set -euo pipefail

repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
v1_runner="$repo_root/benchmarks/run_external_harness_spreadsheetbench_v1.py"
results_root="$repo_root/benchmarks/results"
logs_root="$repo_root/benchmarks/logs"
tmux_socket="spreadsheetbench-no-skill-20260918"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
base_url="http://10.130.138.46:8010/v1"
parallelism="${PARALLELISM:-6}"

mkdir -p "$logs_root"

launch_v1() {
  local session="$1" harness="$2" model="$3" output="$4" log="$5"
  [[ ! -e "$output" ]] || { echo "output already exists: $output" >&2; return 2; }
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$v1_runner' \
      --harness '$harness' --model '$model' --run-root '$output' \
      --dataset '$repo_root/benchmarks/data/spreadsheetbench_912_v0.1' \
      --no-skill --parallelism '$parallelism' --max-turns 50 \
      --max-output-tokens 32768 --task-timeout 21600 --replay-timeout 1800 \
      --base-url '$base_url' --api-key-file '$api_key_file' \
      >> '$log' 2>&1"
}

launch_v2() {
  local session="$1" harness="$2" model="$3" output="$4" log="$5" runner
  case "$harness" in
    codex) runner="$repo_root/benchmarks/run_codex_spreadsheetbench_v2.py" ;;
    claude) runner="$repo_root/benchmarks/run_claude_spreadsheetbench_v2.py" ;;
    dsh) runner="$repo_root/benchmarks/run_deepseek_harness_spreadsheetbench_v2.py" ;;
    *) echo "unknown harness: $harness" >&2; return 2 ;;
  esac
  [[ ! -e "$output" ]] || { echo "output already exists: $output" >&2; return 2; }
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$runner' \
      --dataset '$repo_root/benchmarks/data/spreadsheetbench-v2' \
      --run-root '$output' --no-skill --parallelism '$parallelism' \
      --max-turns 50 --task-timeout 21600 --model '$model' \
      --base-url '$base_url' --api-key-file '$api_key_file' \
      --recalculate-before-evaluation >> '$log' 2>&1"
}

for benchmark in v1 v2; do
  for harness in codex claude dsh; do
    for family in deepseek qwen; do
      if [[ "$family" == deepseek ]]; then
        model="DeepSeek-V4-Flash"
        model_slug="deepseek-v4-flash"
      else
        model="dashscope/qwen3-coder-480b-a35b-instruct"
        model_slug="qwen3-coder-480b"
      fi
      session="${benchmark}_${family}_${harness}_noskill_20260918"
      output="$results_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-no-skill-full-20260918"
      log="$logs_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-no-skill-full-20260918.log"
      if [[ "$benchmark" == v1 ]]; then
        launch_v1 "$session" "$harness" "$model" "$output" "$log"
      else
        launch_v2 "$session" "$harness" "$model" "$output" "$log"
      fi
      printf '%s\t%s\t%s\n' "$session" "$model" "$output"
    done
  done
done

