# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.6 canary1

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

One-task paired canary on `Financial_Model/13_05`. v1.5 reached all three stages but its model
inspector covered only one of five requested sheets, so the planner emitted reads and the executor
made no edit. v1.6 adds bounded deterministic task-keyword row evidence across every explicitly
named sheet and forbids read actions in the plan.

Acceptance requires both arms officially scored, ours modification accuracy at least `1.2 *` bare,
no strict regression, and ours regression accuracy no more than 0.01 below bare. One-task success
only authorizes a broader paired canary.

Settings remain laboratory open-source `qwen36-35b-a3b`, Chat Completions, thinking disabled,
8 calls, 120,000 tokens, 4,096 output, seed 41, arm-order seed 20260820, and
`ours=plugevolve-seed`; `policy-ours` is 1.6.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v16-canary1-20260820`.
