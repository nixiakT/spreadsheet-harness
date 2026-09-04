# SpreadsheetBench-v2 DeepSeek-V4-Flash paired pilot30 v2

Status: frozen before run2 launch on 2026-08-20 (Asia/Shanghai).

This is a development-protocol correction after run1 stopped during the first
task pair. It inherits the complete task list, sampling rule, execution order,
provider settings, compositions, and resource limits from
`spreadsheetbench-v2-deepseek-v4-flash-pilot30-v1.md`, whose SHA-256 is
`09dd5ec8299668e42065313c0e4f8fd2b8d104c6430e5dc4398e74037ad72dd7`.
No task, model parameter, arm order, timeout, or token/call limit changes.

## Reason for the correction

In run1, the first arm (`Debugging/01_04`, ours) crossed the fixed 200,000
cumulative-token budget after its ninth provider response. The v2 adapter
incorrectly classified this known model-budget termination as `not_scored`.
The established comparison-runner contract instead evaluates the final
workbook after a model reaches a call, token, turn, or elapsed budget.

Run1 was stopped immediately after this diagnosis. Its partial result identity
is retained as evidence and is never resumed, overwritten, or included in the
pilot estimate. Because this correction used a selected pilot arm, run2 remains
development evidence rather than a confirmatory result.

## Corrected outcome contract

- Harness protocol: `paired_official_evaluator_v3`.
- A known `AgentExecutionFailure` with auditable partial evidence is a scored
  model outcome: recalculate the final workbook, invoke the pinned official
  evaluator, and record `outcome_kind=model_execution_failure` plus its reason.
- Its official workbook score is not forced to zero. Model-execution-failure
  counts are reported separately.
- Provider timeout, network failure, missing/corrupt output, scorer failure, or
  other infrastructure failure remains `not_scored`.
- Primary arm metrics and paired deltas remain null unless all 60 final
  workbooks receive official scores.

## Fixed output identity

`benchmarks/results/spreadsheetbench-v2-deepseek-v4-flash-pilot30-paired-plugevolve-v2-20260820-run2`
