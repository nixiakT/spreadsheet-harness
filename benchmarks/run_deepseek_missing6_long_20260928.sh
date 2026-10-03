#!/usr/bin/env bash
set -e
ROOT=/data/zju-160/tongzeyuan/spreadsheet-harness
OUT=$ROOT/benchmarks/results/v2-sota-final-control16-20260927
PY=$ROOT/.venv/bin/python
RUN=$ROOT/benchmarks/run_v2_target_category_parallel_20260925.py
PYTHONPATH=$ROOT/src $PY $RUN --category Debugging --arm spreadsheet-harness-basic --model dashscope/deepseek-v4-flash-0731 --model-slug deepseek-v4-flash-0731 --output "$OUT" --parallelism 1 --task-timeout 7200 --request-timeout 1200 --task-ids 10_04 10_06 --base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key > "$OUT/missing6-logs/basic-long.log" 2>&1 &
PYTHONPATH=$ROOT/src $PY $RUN --category Debugging --arm spreadsheet-harness-financial --model dashscope/deepseek-v4-flash-0731 --model-slug deepseek-v4-flash-0731 --output "$OUT" --parallelism 1 --task-timeout 7200 --request-timeout 1200 --task-ids 10_04 10_05 10_09 10_10 --base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key > "$OUT/missing6-logs/financial-long.log" 2>&1 &
wait
