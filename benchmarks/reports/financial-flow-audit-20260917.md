# Financial flow audit — 2026-09-17

## Reference results and limits

The independent Financial batch `deepseek-v4-flash-harness-financial-tools-v26-full-20260914`
has 88 scored tasks and 26 passes. It is a reference, not a reproducible single-version
baseline: manifests show six distinct `(arms.py, financial_model_repairs.py)` hash pairs.
The newer Financial full run passes 22/100, and Basic passes 4/100. Do not select the best
output across these runs, or pool successful attempts as a new full result.

Five old-pass/new-fail cases: 02_01, 09_02, 11_04, 16_01, 19_01.
One new-pass/old-not-passed case: 13_03.

## Confirmed flow defects

Basic's Financial category has 77/100 records with `interaction_turns=1`.
The full launcher uses `--arm ours --composition ours=spreadsheet-harness-basic`.
The direct Basic executor route used the *arm name*, so the composition alias missed it.
The fallback path could then skip execution after three persisted planner writes.
For example, Basic 02_01 records 33 verified writes, but only one model turn and fails.
Persistence is not proof of correct formulas, appropriate targets, or complete instructions.

A separate Financial warm-start shortcut treated 20 edits touching all named sheets as
completion, despite lacking clause-level completeness evidence.

## Scoped changes

- Resolve Basic-style Financial execution from the actual tool mode and absence of the
  Financial runtime, within the existing `policy-ours` branch, not the arm label.
- Never omit Financial execution merely because planner writes persisted.
- Retain Financial domain warm-start and planner/executor, but remove the sheet-count
  shortcut that bypassed all model verification.
- No task-ID, answer-coordinate, golden workbook, or evaluator changes.
- No change to the bare execution body or `kernel.py`.

Three regression scenarios fail before the patch and pass after it. The test command
`pytest -q tests/test_arms.py tests/test_plugins.py tests/test_financial_model_repairs.py`
passes all 160 tests, including bare isolation.

## Controlled development experiment (running; not a held-out result)

Location: `benchmarks/results/financial-flow-paired-20260917`.
Launcher: `benchmarks/run_financial_flow_paired_20260917.py`.

Cases selected before candidate scores:

| Composition | Cases | Purpose |
| --- | --- | --- |
| Basic | 02_01, 09_02 | Old-pass/new-fail, premature Basic finalization |
| Financial | 01_04, 11_04 | Warm-start success preservation; old-pass/new-fail |

Each case has a control and candidate, 8 jobs total, concurrency 4. A control copy restores
only the three old routing/early-stop decisions; all other source and skills are identical.
This tests the flow patch, not a claim of reproducing every version in the old mixed run.
There is no best-of sampling or automatic repeat-until-pass.

Both use the independent Financial batch budget: DashScope DeepSeek V4 Flash, thinking
enabled, temperature 0, top_p 1, 50 model calls/turns, 32768 output-token limit,
10M total-token budget, 21600-second task budget, request timeout 700 seconds,
LiteLLM timeout 600 seconds, retries 5, arm-order seed 20260908.

Frozen control/candidate trees and source hashes are in `experiment.json`. Only `arms.py`
differs. Both bare execution bodies have AST SHA256
`634b93a812d2c0ddc3da205016553386820f613bd875ae0aeb017a0910dd822d`.
All prior result directories remain untouched.

Do not promote this candidate or claim a score increase before checking paired official
scores, regression loss, executor use, and provider failures. Full confirmation will require
a fixed source snapshot and consistent settings across all 100 Financial cases.
