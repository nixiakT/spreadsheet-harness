# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.7 canary1 run2

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

Exact repeat of v1.7 canary1 after a parser-only compatibility fix: a complete YAML body with an
opening `yaml` fence and omitted closing fence is accepted, while YAML syntax and provenance remain
fail-closed. The prior real planner output was replayed locally and passes normalization.

Acceptance and all provider/generation/resource settings are unchanged: paired
`Financial_Model/13_05`, open-source `qwen36-35b-a3b`, 8 calls, 120,000 tokens, seed 41,
arm-order seed 20260820, and `policy-ours` 1.7.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v17-canary1-20260820-run2`.
