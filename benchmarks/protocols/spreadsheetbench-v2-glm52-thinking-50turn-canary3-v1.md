# SpreadsheetBench-v2 GLM-5.2 thinking/50-turn canary3

Status: completed development canary on 2026-08-29 (Asia/Shanghai); a latest-code
`Debugging/02_01` confirmation run is separate and does not alter this frozen run.

## Purpose

Validate the published SpreadsheetBench-v2 interaction regime before spending
resources on a stratified 30-task pilot.  This is not SOTA evidence.

## Fixed tasks and arms

Tasks, in order:

1. `Debugging/02_01`
2. `Financial_Model/05_04`
3. `Template/02_01`

Arms are `bare` and `spreadsheet-harness-basic`, with deterministic alternating
arm order.  Every arm receives the same public source-workbook basename and
bounded workbook preview.  Evaluator-only positions and golden workbooks never
enter model context.

## Execution and evaluation

- Model: laboratory `GLM-5.2`, thinking enabled
- Maximum interaction turns/model calls per arm: `50 / 50`
- Maximum provider-reported tokens per arm: `600000`
- Maximum output tokens per call: `8192`
- Task deadline: `3600` seconds
- Temperature/top-p/seed: `1 / 1 / 41`
- Official evaluator SHA-256:
  `04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0`

Provider, deployment, timeout, and evaluator failures are `not_scored`, never
zero.  The run advances to the frozen stratified 30-task pilot only if both arms
produce at least two common official scores, workbook regression remains
auditable, and the service sustains the 50-turn configuration without systemic
infrastructure failure.

The published open-source reference is GLM-5 at 17.14% overall accuracy under
thinking mode and at most 50 interaction turns.  GLM-5.2 is a newer available
backbone, so results must identify the exact model alias and must not be called a
literal reproduction of the GLM-5 row.

## Completed outcome

All six arms received an official numerical score. `Template/02_01` was an exact
basic-harness win (`1` versus bare `0`). `Debugging/02_01` tied at exact `0`
(both modification `0.9665`) because this process loaded the code version before
the later deterministic Double Counting detector. `Financial_Model/05_04` was
non-exact for both arms (basic modification `0.22`, bare `0`). The canary passes
the execution/service gate but is not category or SOTA evidence.
