# Qwen3.5 post-fix development-16 protocol v1

## Scope

This is an exploratory paired development run for the post-fix v28 harness. It
does not provide held-out or confirmatory evidence and must not be pooled with a
later formal evaluation.

The source pool is the pinned SpreadsheetBench-Verified raw row interval
`[100, 200)`. The repository protocol assigns all rows `[0, 200)` to
evolution/development and reserves `[200, 400)` for held-out evaluation. This
run does not use raw rows `[0, 100)`, any protected comparison cohort, or any
v27 reserve task.

## Frozen selection

Freeze the exact private task list before any new outcome is observed:

1. Traverse raw rows `[100, 200)` in original dataset order.
2. Exclude every task already named by an existing local comparison manifest or
   task-ID list at freeze time, including owner-only development experiments.
3. Select the first eight remaining `Cell-Level Manipulation` tasks and the
   first eight remaining `Sheet-Level Manipulation` tasks.
4. Store the exact IDs and selection provenance only in an owner-readable
   private preregistration. Do not commit task IDs or cohort hashes.

Selection may use only original row order, instruction type, and prior-exposure
membership. It must not inspect instructions, workbooks, golden values, answer
ranges, model trajectories, or outcomes. No task substitution is allowed after
model sampling begins.

The balanced 8/8 composition estimates performance on this balanced development
mixture, not on the natural 75/25 composition of the source interval. Report
both aggregate and instruction-type-stratified outcomes.

## Execution contract

- Model route: `qwen36-35b-a3b`
- API protocol: Chat Completions
- Reasoning effort: `none`
- Thinking: disabled
- Arms: `bare`, `ours`
- Generation seed: `41`
- Arm-order seed: `20260815`
- Temperature/top-p/presence penalty: `1.0` / `1.0` / `2.0`
- Top-k/min-p/repetition penalty: `40` / `0.0` / `1.0`
- Maximum model calls and turns per arm: `12` / `12`
- Maximum total tokens per arm: `180000`
- Maximum output tokens per call: `4096`
- Task timeout: `1800` seconds
- Provider request timeout: `700` seconds
- LiteLLM upstream timeout: `600` seconds
- Automatic request retries: `0`
- Circuit-breaker threshold: `3`
- LibreOffice recalculation: enabled

Use the repository commit containing this protocol. Require a clean worktree,
`HEAD == origin/main`, an owner-only API-key file, successful provider tool
canary, LibreOffice, and strict Bubblewrap isolation before launch.

## Collection and reporting

Run all 32 arm-tasks without inspecting interim scores and without a
score-dependent early stop. An ambiguous delivered request is never replayed.
Infrastructure failures remain no-score outcomes and invalidate primary paired
inference; they are not converted to model failures.

After collection, run the comparison audit and report:

- audit, journal, study-completeness, and inference-validity flags;
- end-to-end accuracy by arm and by instruction type;
- paired accuracy delta, exact McNemar result, and paired interval estimates;
- anonymous ordinal paired outcomes;
- model-execution, provider, routing, recalculation, and scoring failures;
- calls, tokens, and wall-clock distributions.

The run is development evidence regardless of outcome. If it informs another
harness change, the entire cohort becomes ordinary tuning data and cannot later
confirm that changed method.
