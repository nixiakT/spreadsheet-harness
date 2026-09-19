#!/usr/bin/env bash
set -euo pipefail

repo=/data/zju-160/tongzeyuan/spreadsheet-harness
root="$repo/benchmarks/results/fin15k-scaling-coevolution-20260919"
runner="$repo/benchmarks/run_fin15k_scaling_evolution_20260919.py"
python="$repo/.venv/bin/python"

cd "$repo"

# A separately launched canary/50-case batch may still own the first stage.
# Wait for that tmux session to finish, then call the resumable command again
# so interrupted/infrastructure cells are repaired before candidate generation.
while tmux has-session -t fin15k_scaling_20260919 2>/dev/null; do
  sleep 30
done

"$python" "$runner" --root "$root" baseline --limit 50 --parallelism 8
"$python" "$runner" --root "$root" evolve --size 50 --parallelism 3

"$python" "$runner" --root "$root" baseline --limit 200 --parallelism 16
"$python" "$runner" --root "$root" evolve --size 200 --parallelism 3

"$python" "$runner" --root "$root" baseline --limit 500 --parallelism 16
"$python" "$runner" --root "$root" evolve --size 500 --parallelism 3

"$python" "$runner" --root "$root" v2 --parallelism 24 --task-limit 30 --evaluation-name canary-30
"$python" "$runner" --root "$root" v2 --parallelism 24 --evaluation-name full
