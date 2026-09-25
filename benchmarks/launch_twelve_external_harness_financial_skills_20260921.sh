#!/usr/bin/env bash
set -euo pipefail

# Fresh 2 (model) x 3 (harness) x 2 (benchmark) matrix using only the four
# financial skill packages under skills/financial-skills/.  The official xlsx
# skill is deliberately not installed or mentioned in these runs.
repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
v1_runner="$repo_root/benchmarks/run_external_harness_spreadsheetbench_v1.py"
# Keep model workspaces outside the repository so the agent cannot discover
# sibling benchmark skills (notably skills/xlsx) by walking the git tree.
results_root="/data/zju-160/tongzeyuan/spreadsheetbench-financial-skills-only-isolated-20260921"
logs_root="$repo_root/benchmarks/logs"
tmux_socket="spreadsheetbench-financial-skills-isolated-20260921"
api_key_file="/tmp/spreadsheet-harness-litellm.key"
base_url="http://10.130.138.46:8010/v1"
skill="$repo_root/skills/financial-skills"

mkdir -p "$logs_root"
[[ -x "$python_bin" ]] || { echo "missing executable: $python_bin" >&2; exit 1; }
[[ -d "$skill" ]] || { echo "missing financial skill directory: $skill" >&2; exit 1; }
[[ -s "$api_key_file" ]] || { echo "missing API key file: $api_key_file" >&2; exit 1; }
for package in 3-statement-model comps-analysis dcf-model lbo-model; do
  [[ -f "$skill/$package/SKILL.md" ]] || { echo "missing skill package: $skill/$package" >&2; exit 1; }
done

launch_v1() {
  local session="$1" harness="$2" model="$3" output="$4" log="$5"
  [[ ! -e "$output" ]] || { echo "output already exists: $output" >&2; return 2; }
  if tmux -L "$tmux_socket" has-session -t "$session" 2>/dev/null; then
    echo "tmux session already exists: $session" >&2; return 2
  fi
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$v1_runner' \
      --harness '$harness' --model '$model' --run-root '$output' \
      --dataset '$repo_root/benchmarks/data/spreadsheetbench_912_v0.1' \
      --skill '$skill' --parallelism 6 --max-turns 50 \
      --max-output-tokens 32768 --task-timeout 21600 --replay-timeout 1800 \
      --base-url '$base_url' --api-key-file '$api_key_file' \
      >> '$log' 2>&1"
  printf 'started\t%s\t%s\t%s\n' "$session" "v1/$harness/$model" "$output"
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
  if tmux -L "$tmux_socket" has-session -t "$session" 2>/dev/null; then
    echo "tmux session already exists: $session" >&2; return 2
  fi
  tmux -L "$tmux_socket" new-session -d -s "$session" \
    "cd '$repo_root' && exec '$python_bin' '$runner' \
      --dataset '$repo_root/benchmarks/data/spreadsheetbench-v2' \
      --run-root '$output' --skill '$skill' --parallelism 6 \
      --max-turns 50 --task-timeout 21600 --model '$model' \
      --base-url '$base_url' --api-key-file '$api_key_file' \
      --recalculate-before-evaluation >> '$log' 2>&1"
  printf 'started\t%s\t%s\t%s\n' "$session" "v2/$harness/$model" "$output"
}

for benchmark in v1 v2; do
  for harness in codex claude dsh; do
    for family in deepseek qwen; do
      if [[ "$family" == deepseek ]]; then
        model="DeepSeek-V4-Flash"; model_slug="deepseek-v4-flash"
      else
        model="dashscope/qwen3-coder-480b-a35b-instruct"; model_slug="qwen3-coder-480b"
      fi
      session="${benchmark}_${family}_${harness}_financial_20260921"
      output="$results_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-financial-skills-only-isolated-full-20260921"
      log="$logs_root/spreadsheetbench-${benchmark}-${model_slug}-${harness}-financial-skills-only-isolated-full-20260921.log"
      if [[ "$benchmark" == v1 ]]; then
        launch_v1 "$session" "$harness" "$model" "$output" "$log"
      else
        launch_v2 "$session" "$harness" "$model" "$output" "$log"
      fi
    done
  done
done

echo "matrix launch complete: 12 sessions on tmux socket $tmux_socket"
