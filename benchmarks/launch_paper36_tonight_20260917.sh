#!/usr/bin/env zsh
set -euo pipefail

repo=/data/zju-160/tongzeyuan/spreadsheet-harness
shared=$repo/benchmarks/results/paper36-shared-baseline-qwen36plus-20260917
h_root=$repo/benchmarks/results/paper36-search-h-only-qwen36plus-20260917
d_root=$repo/benchmarks/results/paper36-search-d-only-qwen36plus-20260917
alt_root=$repo/benchmarks/results/paper36-search-alternating-qwen36plus-20260917
pilot=$repo/benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917
logs=$repo/benchmarks/logs

mkdir -p $logs
while [[ ! -f $shared/baseline-final.json ]]; do
  sleep 15
done

launch_search() {
  local session=$1
  local destination=$2
  local sequence=$3
  local rounds=$4
  local log=$5
  if [[ -f $destination/search-final.json ]]; then
    return
  fi
  if [[ ! -d $destination ]]; then
    cp -a $shared $destination
  fi
  if ! tmux has-session -t $session 2>/dev/null; then
    tmux new-session -d -s $session \
      "cd $repo && .venv/bin/python benchmarks/run_true_coevolution_20260911.py \
      --result-root $destination \
      --model qwen3.6-plus --generator-model dashscope/glm-5.2 \
      --coordinate-sequence $sequence --target-rounds $rounds --search-only --fast-gate \
      --parallelism 4 --task-attempts 2 --candidate-attempts 3 \
      --gate-model-calls 50 --task-timeout 3600 --request-interval 0.5 \
      --min-quality-delta 0.000001 --min-modification-delta 0 \
      2>&1 | tee $log"
  fi
}

launch_search paper36_h_only_20260917 $h_root H 1 \
  $logs/paper36-search-h-only-qwen36plus-20260917.log
launch_search paper36_d_only_20260917 $d_root D 1 \
  $logs/paper36-search-d-only-qwen36plus-20260917.log
launch_search paper36_alternating_20260917 $alt_root H,D 2 \
  $logs/paper36-search-alternating-qwen36plus-20260917.log

while true; do
  if [[ -f $h_root/search-final.json && -f $d_root/search-final.json && -f $alt_root/search-final.json ]]; then
    break
  fi
  for session in paper36_h_only_20260917 paper36_d_only_20260917 paper36_alternating_20260917; do
    if ! tmux has-session -t $session 2>/dev/null; then
      case $session in
        paper36_h_only_20260917) endpoint=$h_root/search-final.json ;;
        paper36_d_only_20260917) endpoint=$d_root/search-final.json ;;
        paper36_alternating_20260917) endpoint=$alt_root/search-final.json ;;
      esac
      if [[ ! -f $endpoint ]]; then
        print -u2 "search exited without endpoint: $session"
        exit 2
      fi
    fi
  done
  sleep 30
done

if [[ ! -f $pilot/report.json ]] && ! tmux has-session -t paper36_heldout_20260917 2>/dev/null; then
  tmux new-session -d -s paper36_heldout_20260917 \
    "cd $repo && .venv/bin/python benchmarks/run_paper36_pilot_20260917.py \
    --run --parallelism 8 --task-attempts 3 \
    2>&1 | tee $logs/paper36-heldout-qwen36plus-20260917.log"
fi
