# Qwen3.5 post-fix development-32 protocol v1

## Scope

This is a fresh exploratory paired development run for the v29 harness. It is
not held-out or confirmatory evidence, and it must not be pooled with a later
formal evaluation. The source pool is the pinned SpreadsheetBench-Verified raw
row interval `[100, 200)`. Raw rows `[0, 200)` remain development-only, while
raw rows `[200, 400)` remain untouched held-out reserve.

The run evaluates the direct `bare` baseline and the full `ours` system on the
same tasks with counterbalanced arm order and matched model resources. `bare`
is the five-row preview plus isolated code-interpreter baseline; it is not a
Trace2Skill reproduction. `ours` adds the deterministic profile, frozen skills,
six spreadsheet tools, and the formula runtime gate.

## Frozen selection

Before any model sampling, use `tools/freeze_development_cohort.py` to build a
conservative exposure inventory. The inventory must include every task named by
public or owner-only protocol task lists, manifests, result journals, inflight
markers, interrupted seals, structured run records, preregistrations, and
code-owned protected or quarantined cohorts. A task is exposed when it appears
in any such source, even if its arm-task did not complete.

At preregistration time the expected inventory for raw rows `[100, 200)` is 31
exposed tasks (21 Cell, 10 Sheet) and 69 eligible tasks (54 Cell, 15 Sheet).
Freeze the first 24 eligible `Cell-Level Manipulation` tasks and the first eight
eligible `Sheet-Level Manipulation` tasks in original dataset order. Selection
may use only raw row order, instruction type, and exposure membership. It must
not inspect task instructions, workbooks, golden artifacts, answer metadata,
trajectories, scores, or prior outcomes. No substitution is permitted after
the private task list is created.

Store exact IDs and inventory digests only in an owner-readable preregistration
and task-list outside the repository. The private directory must be mode 0700
and both files mode 0600. The public protocol contains no task ID or cohort
digest. Verify the frozen files and current exposure inventory again immediately
before launch.

The 24/8 composition estimates performance on a 3:1 Cell/Sheet development
mixture. Report both aggregate and instruction-type-stratified outcomes.

## Execution contract

- Comparison protocol/schema: `resource_matched_multi_arm_v29` / `18`
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
`HEAD == origin/main`, an owner-only API-key file, a successful online function
calling canary, LibreOffice, strict Bubblewrap isolation, and a successful
frozen-cohort verification before launch. Use a fresh private output directory;
do not resume or modify any v28 run.

## Failure semantics

An HTTP-200 response that ends at the provider output limit is a delivered
model outcome. The harness discards partial text and tool arguments without
executing them, records digest-only evidence plus exact usage and timing, and
scores the arm-task as a completed known model failure. A truncated forced
terminal submission remains `terminal_submission_truncated`; any other
delivered output-limit response is `model_response_truncated`.

A genuine provider failure with no delivered response is an infrastructure
no-score. Its row must contain exact failed-request attempt evidence, a bound
managed artifact and trajectory, and `replay_permitted=false`. An ambiguous
post-send request is never replayed. Any infrastructure no-score makes primary
arm accuracy, paired delta, confidence intervals, and hypothesis tests null;
known-outcome and jointly observed descriptions may still be reported as
explicitly non-primary summaries.

## Collection and reporting

Run all 64 arm-tasks without inspecting interim scores and without a
score-dependent early stop. A nonzero collection exit can represent a sealed
provider no-score and is not by itself evidence of a corrupt run. After the
process finishes, run a fresh comparison audit and report:

- audit, journal-integrity, study-completeness, and inference-validity flags;
- primary arm and paired estimates only when the audit permits them;
- otherwise, missingness-safe bounds and labeled known-outcome descriptions;
- aggregate and Cell/Sheet outcomes with anonymous ordinal pair categories;
- model-execution and provider infrastructure counts and reasons;
- request-attempt completeness, calls, tokens, and wall-clock distributions.

The entire cohort becomes development-exposed at freeze time regardless of
collection success. Any change informed by this run must be evaluated on a new
cohort.
