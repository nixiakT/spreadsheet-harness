# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.5 smoke1

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

One-task paired functional smoke test on `Financial_Model/13_05`, following the v1.4 planner
terminal failure. Its purpose is to prove that the inspector → one-shot YAML planner → executor
path reaches an official score and compare it with a fresh bare arm before spending on two tasks.
Acceptance for expansion requires both arms scored and ours modification accuracy at least
`1.2 *` bare without strict or regression guardrail failure.

Fixed settings match the v1.4 canary: laboratory open-source `qwen36-35b-a3b`, Chat Completions,
thinking disabled, 8 calls/turns (inspect 4, plan 1, execute 3), 120,000 total tokens, 4,096 output,
and `ours=plugevolve-seed` with `policy-ours` 1.5.0. Generation seed is 41 and arm-order seed is
20260820.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v15-smoke1-20260820`.
