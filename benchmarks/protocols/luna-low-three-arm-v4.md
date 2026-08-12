# Luna low three-arm SpreadsheetBench protocol v4

> **Observed status (2026-08-12): failed and permanently retired.** The fresh
> three-arm smoke passed, but the first formal canary row ended in a Relay
> transport error. Do not resume, resample, or use the commands in this file to
> start another v4 run. See `../diagnostics/relay-493-18-v4-canary.md`.

This is a fixed, paired, directional harness study rather than an exact
reproduction of SpreadsheetAgent or an unbiased estimate from its canary and
pilot subsets. Every selected task uses the same model, effort, workbook input,
LibreOffice backend, corrected value scorer, preview policy, and model-resource
limits.

The arms are:

1. `bare`: the small model plus an isolated local Python interpreter.
2. `paper` (reported as `paper-inspired`): a clean-room, small-model-only
   adaptation of task-independent structural extraction and complementary
   visual/LaTeX verification. It is not the paper's released implementation or
   an absolute-score reproduction.
3. `ours`: the local spreadsheet tools, original-image vision path, and frozen
   spreadsheet skills.

## Why v4 exists

V3 removed `auto` routing. Every frozen prefix response exposed only its
prescribed operational tool under `tool_choice: required`; later tool-using
turns exposed the stage tools plus `submit_result`, also under `required`.
Fresh v3 bare and paper-inspired smoke runs passed. Nevertheless, the first
formal v3 canary row (`493-18/paper`) failed before any model response: its two
300-second attempts both ended in a read timeout with no HTTP headers or SSE
events. The row used zero successful model responses and zero reported tokens.

The v3 canary is failed and retired. It must not be resumed, selectively
resampled, imported, or combined with v4. All v1-v3 result directories remain
diagnostic artifacts only. An older v2 canary that accidentally completed one
paper row during review is also permanently excluded.

V4 keeps the v3 model, prompts, tools, required routing, stage caps, token caps,
scoring, task IDs, arm order, and isolation policy. It changes only the
pre-registered transport envelope: a no-header read timeout waits 30 seconds
before retry 1 and 60 seconds before retries 2 and 3; each provider response
allows three transient retries (four real HTTP attempts total), and the
arm-task wall-time bound rises to 1,800 seconds so those attempts can occur.
This is a Relay long-tail mitigation, not an identified provider root-cause fix.

## Frozen settings

- Dataset: `KAKA22/SpreadsheetBench@ab0b742b0fc95b946f212d80ac7771b5531272e4`
- Model: `gpt-5.6-luna`
- Reasoning effort: `low`
- Maximum successful provider responses per arm-task: 20, including
  `submit_result`
- Stage response caps: bare solve `20`; paper
  extract/vision/LaTeX/reconcile/solve `6+3+3+1+7=20`; ours solve `20`
- Maximum provider-reported tokens per arm-task: 100,000
- Maximum output tokens per response: 4,096
- End-to-end arm-task wall time: 1,800 seconds
- Per-attempt request timeout: 300 seconds
- Transient retries per provider response: three, for at most four real HTTP
  attempts
- No-header read-timeout cooldown bases: 30, 60, and 60 seconds before retries
  1, 2, and 3 respectively. Random jitter is at most 0.25 seconds before the
  global 60-second cap, so the first delay may be 30-30.25 seconds and later
  delays are capped at 60 seconds.
- Explicit overload retry backoff: at least 15 seconds when the provider does
  not supply a valid `Retry-After`
- Other transient retry backoff: bounded exponential delay; a valid provider
  `Retry-After` takes precedence. Every requested delay is then clipped to the
  60-second global backoff cap and remaining arm-task deadline.
- Per-attempt audit: status, headers latency, first SSE latency, terminal event
  and latency, SSE count, transport exception class, retryability,
  `Retry-After`, no-header classification, backoff reason, and requested/actual
  backoff. A deadline-expired request that never starts is not counted as an
  HTTP attempt.
- Semantic task retries: zero
- Circuit breaker: three exhausted transient arm-tasks or three required-routing
  protocol failures; any fatal provider error stops immediately
- Arm-order seed: `20260811`, with deterministic cyclic counterbalancing
- Strict Linux Bubblewrap workspace isolation with no unsandboxed fallback
- Shared preview: bounded `flat-workbook-preview-v1` records with canonical
  escaping, coordinates, values, formulas, types, ranges, merges, tables,
  truncation metadata, and a content hash
- Frozen forced-tool prefix sequences; each prefix response exposes only the
  listed operational tool and uses `tool_choice: required`:
  - bare solve: `code_interpreter`, `code_interpreter`
  - paper extract: `list_sheets`, `inspect_range`
  - paper vision: `render_workbook`, `view_image`
  - paper LaTeX: `range_to_latex`
  - paper solve: `code_interpreter`, `code_interpreter`
  - ours solve: `list_sheets`, `inspect_range`
- Post-prefix routing for every tool-using stage: `required`, terminated only by
  one `submit_result` call
- Paper reconciliation: one direct, tool-free response

Requested and observed prefix tools, post-prefix routing, and terminal-tool
observation are recorded for every stage. Any mismatch fails closed. Paper
vision reserves exactly three responses: render, attached original-image view,
and `submit_result` with evidence YAML.

A `submit_result` response consumes one stage turn, one shared arm-task model
response, its provider-reported tokens, elapsed time, and HTTP attempt. It is
not included in `agent.tool_calls` or `tool_trace`; those fields record
operational workbook-tool attempts. Terminal submissions and total function
calls are audited separately. Paper extraction, vision, and LaTeX submissions
undergo bounded YAML parsing, non-empty provenance validation, normalization,
and read-only workbook-integrity checks.

Outcome differences are attributable to each complete arm policy; this study
does not isolate a tools-, vision-, routing-, or skills-only causal effect.

## Differences from the released paper system

The `paper` arm is a fixed single pass through extraction, vision, LaTeX,
reconciliation, and solving. It does not implement the paper's per-sheet
extraction or up-to-three-round verifier feedback loop into the extractor. It
uses the same Luna small model for every role rather than the released
Qwen3-Coder-480B extractor/solver plus GLM-4.5V vision model. For fairness, it
withholds evaluator-only `instruction_type`, `answer_position`, and the golden
workbook from the solver. The released code's solver constraints and evaluator
metadata are not reproduced.

The paper reports an original 912-task soft/hard-restriction setting, whereas
this study uses Verified 398, a corrected value scorer, and LibreOffice. Its
absolute scores must not be compared directly with the paper's tables.

## Gates and reporting

A completely new, non-scored v4 smoke directory must first demonstrate at
least one successful end-to-end request path for each arm without reusing any
v1-v3 result row.

The six-task canary covers formula repair, merged cells, a large range,
ordinary sheet editing, multi-sheet reasoning, and an explicitly
color-dependent task. It passes only when all 18 arm-tasks finish without an
infrastructure, routing, or budget error; every output opens; every prefix and
terminal route matches; and each paper task demonstrates render to attached
original image, range-to-LaTeX, parseable provenance, and unchanged read-only
workbook hashes.

The 30-task pilot contains the same six canary tasks, so it remains exploratory.
It may start only after the canary passes 18/18. Report the complete 30-task
table and a separate 24-task sensitivity table that excludes canary tasks.
Only after the complete pilot has zero infrastructure/routing/budget failures
may the paired Verified 398 run start.

Primary metrics are end-to-end accuracy and Wilson 95% intervals, Cell/Sheet
strata, completion/error rate, successful model responses, provider tokens,
known HTTP attempts, elapsed time, and tokens per pass. Paired deltas use a
stratified bootstrap; binary paired outcomes use exact McNemar tests with Holm
correction. Inferential statistics are valid only for a complete paired matrix
with no infrastructure errors.

Canonical ASCII-sorted ID-set hashes (one ID per line with a final newline):

- Canary: `3e606b226d0008068b89fc94cd18eef4ca00603911d5b722622d42d0d660eb86`
- Pilot: `9f5e3f0f57d2840d4f531f03ee2febea94c10aadd6842354b18dd168430588d6`
- Pilot in dataset execution order: `c74c9fac6123698e78ff78137ca6d0ca9ccc151a94cc123a6e772162c4b1e023`

The frozen commands are:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id-file benchmarks/protocols/luna-low-three-arm-canary-v4.txt \
  --output benchmarks/results/luna-low-three-arm-canary-v4 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 300 --request-retries 3 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 3 --arm-order-seed 20260811

sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id-file benchmarks/protocols/luna-low-three-arm-pilot-v4.txt \
  --output benchmarks/results/luna-low-three-arm-pilot-v4 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 300 --request-retries 3 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 3 --arm-order-seed 20260811

sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --arm bare --arm paper --arm ours \
  --output benchmarks/results/luna-low-three-arm-full398-v4 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 300 --request-retries 3 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 3 --arm-order-seed 20260811
```
