#!/usr/bin/env zsh
set -euo pipefail

repo=/data/zju-160/tongzeyuan/spreadsheet-harness
root=$repo/benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917
primary_session=paper24_heldout_20260917
recovery_session=paper24_infra_recovery_v2_20260918
log=$repo/benchmarks/logs/paper24-infrastructure-recovery-v2-20260918.log

while tmux has-session -t $primary_session 2>/dev/null; do
  sleep 30
done

# A report filename alone is not proof of a complete, canonical matrix.
# The runner verifies all cells before regenerating the report.
if tmux has-session -t $recovery_session 2>/dev/null; then
  exit 0
fi

tmux new-session -d -s $recovery_session \
  "cd $repo && PYTHONPATH=$root/frozen-runtime-source \
  .venv/bin/python benchmarks/recover_paper24_infrastructure_v2_20260918.py \
  --parallelism 12 --task-attempts 2 2>&1 | tee -a $log"
