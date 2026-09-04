# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.7 canary1

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

One-task paired canary on `Financial_Model/13_05`. v1.6 was stopped before planning by an
unnecessary inspector terminal response hitting the provider output limit. v1.7 removes the model
inspector entirely: bounded deterministic task-keyword evidence feeds one planner call, leaving
seven calls for execution.

Acceptance requires both arms officially scored, ours modification accuracy at least `1.2 *` bare,
no strict regression, and ours regression accuracy no more than 0.01 below bare. One-task success
only authorizes a broader paired canary.

Settings remain laboratory open-source `qwen36-35b-a3b`, Chat Completions, thinking disabled,
8 calls (plan 1, execute 7), 120,000 tokens, 4,096 output, seed 41, arm-order seed 20260820,
`ours=plugevolve-seed`, and `policy-ours` 1.7.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v17-canary1-20260820`.
