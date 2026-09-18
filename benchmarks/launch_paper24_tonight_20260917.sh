#!/usr/bin/env zsh
set -euo pipefail

repo=/data/zju-160/tongzeyuan/spreadsheet-harness
shared=$repo/benchmarks/results/paper36-shared-baseline-qwen36plus-20260917
h_root=$repo/benchmarks/results/paper36-search-h-only-qwen36plus-20260917
d_root=$repo/benchmarks/results/paper36-search-d-only-qwen36plus-20260917
alt_root=$repo/benchmarks/results/paper36-search-alternating-qwen36plus-20260917
pilot=$repo/benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917
logs=$repo/benchmarks/logs

mkdir -p $logs

launch_extended_search() {
  local session=$1
  local destination=$2
  local sequence=$3
  local rounds=$4
  local log=$5
  if [[ -f $destination/search-final.json ]]; then
    return
  fi
  if tmux has-session -t $session 2>/dev/null; then
    return
  fi
  tmux new-session -d -s $session \
    "cd $repo && .venv/bin/python benchmarks/run_true_coevolution_20260911.py \
    --result-root $destination \
    --model qwen3.6-plus --generator-model dashscope/glm-5.2 \
    --coordinate-sequence $sequence --target-rounds $rounds --search-only --fast-gate \
    --parallelism 4 --task-attempts 2 --candidate-attempts 6 \
    --gate-model-calls 50 --task-timeout 3600 --request-interval 0.5 \
    --min-quality-delta 0.000001 --min-modification-delta 0 \
    2>&1 | tee -a $log"
}

# The initial three-attempt searches are already active.  If any exits without
# an acceptable endpoint, restart from its saved evidence with six attempts.
while true; do
  launch_extended_search paper36_h_only_20260917 $h_root H 1 \
    $logs/paper36-search-h-only-qwen36plus-20260917.log
  launch_extended_search paper36_d_only_20260917 $d_root D 1 \
    $logs/paper36-search-d-only-qwen36plus-20260917.log
  launch_extended_search paper36_alternating_20260917 $alt_root H,D 2 \
    $logs/paper36-search-alternating-qwen36plus-20260917.log
  if [[ -f $h_root/search-final.json && -f $d_root/search-final.json && -f $alt_root/search-final.json ]]; then
    break
  fi
  sleep 20
done

if [[ ! -f $pilot/report.json ]] && ! tmux has-session -t paper24_heldout_20260917 2>/dev/null; then
  tmux new-session -d -s paper24_heldout_20260917 \
    "cd $repo && .venv/bin/python benchmarks/run_paper24_pilot_20260917.py \
    --run --parallelism 12 --task-attempts 2 \
    2>&1 | tee $logs/paper24-heldout-qwen36plus-20260917.log"
fi
