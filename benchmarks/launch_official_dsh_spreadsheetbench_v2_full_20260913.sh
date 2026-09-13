#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

exec .venv/bin/python benchmarks/run_deepseek_harness_spreadsheetbench_v2.py \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --run-root benchmarks/results/deepseek-v4-flash-official-dsh-full-v2-20260913 \
  --parallelism 6 \
  --max-turns 50 \
  --task-timeout 5400 \
  --model DeepSeek-V4-Flash \
  --base-url http://10.130.138.46:8010/v1 \
  --api-key-file /tmp/spreadsheet-harness-litellm.key \
  --skill skills/spreadsheet-core/SKILL.md \
  --recalculate-before-evaluation
