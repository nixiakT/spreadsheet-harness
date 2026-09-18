#!/usr/bin/env zsh
set -euo pipefail

repo=/data/zju-160/tongzeyuan/spreadsheet-harness
root=$repo/benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917
primary_session=paper24_heldout_20260917
recovery_session=paper24_infra_recovery_20260918
log=$repo/benchmarks/logs/paper24-infrastructure-recovery-20260918.log

# Do not interrupt or overlap the active preregistered 3600-second workers.
while tmux has-session -t $primary_session 2>/dev/null; do
  sleep 30
done

if [[ -f $root/report.json ]]; then
  exit 0
fi
if tmux has-session -t $recovery_session 2>/dev/null; then
  exit 0
fi

tmux new-session -d -s $recovery_session \
  "cd $repo && PYTHONPATH=$root/frozen-runtime-source \
  .venv/bin/python benchmarks/recover_paper24_infrastructure_20260918.py \
  --parallelism 12 --task-attempts 2 2>&1 | tee -a $log"
