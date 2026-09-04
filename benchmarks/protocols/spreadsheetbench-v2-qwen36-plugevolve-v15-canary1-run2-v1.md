# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.5 canary1 run2

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

One-task paired canary on `Financial_Model/13_05`. Run 1 established that Qwen may emit multiple
fenced YAML revisions; the harness now deterministically selects the last complete fenced revision
and applies the same provenance validation. No model prompt, budget, or generation setting changed.

Acceptance requires both arms officially scored, ours modification accuracy at least `1.2 *` bare,
no strict regression, and ours regression accuracy no more than 0.01 below bare. Passing one task
only authorizes the two-task canary; it is not a final +20% claim.

Settings: laboratory open-source `qwen36-35b-a3b`; Chat Completions; thinking disabled; 8 calls,
120,000 tokens, 4,096 output; seed 41; arm-order seed 20260820; `ours=plugevolve-seed` and
`policy-ours` 1.5.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v15-canary1-20260820-run2`.
