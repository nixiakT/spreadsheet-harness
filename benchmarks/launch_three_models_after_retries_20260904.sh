#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
while kill -0 3267290 2>/dev/null || kill -0 3440127 2>/dev/null; do
  sleep 60
done
for spec in \
  'DeepSeek-V4-Flash deepseek-v4-flash' \
  'Kimi-K2.6 kimi-k2.6' \
  'MiniMax-M2.7 minimax-m2.7'; do
  model="${spec%% *}"; slug="${spec#* }"
  env MODEL="$model" MODEL_SLUG="$slug" PARALLELISM=2 \
    zsh benchmarks/launch_model_30_4arm_20260904.sh \
    > "benchmarks/launch_${slug}_30_4arm_20260904.log" 2>&1
done
