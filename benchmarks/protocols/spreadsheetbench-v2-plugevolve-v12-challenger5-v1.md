# SpreadsheetBench-v2 PlugEvolve v1.2 challenger5

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

This is a development challenger, not a leaderboard result. It replays five tasks that exposed
large regressions in the 30-task pilot. The adapter's paired-run integrity rule requires both a
fresh bare arm and the changed `ours=plugevolve-seed` arm under one manifest.

## Tasks

- Financial_Model/01_02
- Financial_Model/13_05
- Financial_Model/16_04
- Financial_Model/05_04
- Template/13_06

## Frozen acceptance gate

- Primary diagnostic: arithmetic mean of official modification accuracy on the five tasks.
- Required relative improvement: challenger ours mean >= `1.2 *` the fresh paired bare mean.
- Strict task accuracy must not be below bare on any task.
- Mean regression accuracy must be at least bare mean minus 0.01.
- Every challenger output must receive an official score and pass fresh audit.
- Failure of any gate rejects this composition; it does not justify a 30- or 321-task run.

## Fixed execution settings

- Dataset/evaluator pins and generation settings are inherited from
  `spreadsheetbench-v2-deepseek-v4-flash-pilot30-v2.md`.
- Model: `DeepSeek-V4-Flash`, Chat Completions, thinking disabled.
- Temperature/top-p: 1/1; seed: 41; presence penalty: 2; top-k: 40; min-p: 0;
  repetition penalty: 1.
- Request timeout/retries/interval: 180 seconds / 0 / 5.5 seconds.
- Limits per task: 20 model calls, 20 turns, 200,000 total tokens, 4,096 output tokens per
  response, 1,800 seconds.
- Composition: `ours=plugevolve-seed`, with `policy-ours` version 1.2.0.
- Fresh output identity:
  `benchmarks/results/spreadsheetbench-v2-deepseek-v4-flash-plugevolve-v12-challenger5-20260820`.
