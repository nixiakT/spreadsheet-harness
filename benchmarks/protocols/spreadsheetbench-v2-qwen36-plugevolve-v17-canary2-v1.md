# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.7 canary2

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

Paired canary on `Financial_Model/13_05` and `Template/13_06`, authorized by the audited one-task
run where ours modification was 0.6087 versus bare 0.0 with regression delta -0.0048.

Acceptance requires all four arms officially scored, mean ours modification accuracy at least
`1.2 *` fresh bare, no per-task strict regression, and mean ours regression accuracy no more than
0.01 below bare. Passing authorizes the five-task challenger, but is not yet the final 30-task claim.

Settings: laboratory open-source `qwen36-35b-a3b`, Chat Completions, thinking disabled, 8 calls
(ours plan 1 + execute 7), 120,000 tokens, 4,096 output, seed 41, arm-order seed 20260820,
`ours=plugevolve-seed`, `policy-ours` 1.7.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v17-canary2-20260820`.
