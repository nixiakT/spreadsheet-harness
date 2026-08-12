# Luna low three-arm SpreadsheetBench protocol v2

This is a fixed, paired, directional harness study rather than an exact
reproduction of SpreadsheetAgent or an unbiased estimate from its canary and
pilot subsets. Every selected task uses the same model, effort, workbook input,
LibreOffice backend, corrected value scorer, preview policy, and end-to-end
resource limits.

The arms are:

1. `bare`: the small model plus an isolated local Python interpreter.
2. `paper` (reported as `paper-inspired`): a clean-room, small-model-only
   adaptation of task-independent structural extraction and complementary
   visual/LaTeX verification. It is not the paper's released implementation or
   an absolute-score reproduction.
3. `ours`: the local spreadsheet tools, original-image vision path, and frozen
   spreadsheet skills.

## Why v2 exists

The v1 capability gate is permanently retained as a failed infrastructure
diagnostic. It completed `493-18/paper` and `493-18/ours`, then
`493-18/bare` turn 2 exhausted two 90-second read attempts before the Relay
produced a terminal response. The v1 client did not retain enough timing detail
to distinguish waiting for headers from waiting for a later SSE event. Those
rows are not reused or resampled in v2.

Separately instrumented probes reconstructed the same approximately 5 KiB
turn-2 condition. Both explicit `tool_choice: auto` and omitted `tool_choice`
produced no response headers or SSE events within 180 seconds. Changing the
choice to the named `code_interpreter` tool returned HTTP 200 headers at 146.8
seconds and a `response.completed` event at 152.0 seconds. V2 therefore
pre-registers a deterministic named-tool prefix and a 300-second request
timeout. These are protocol changes, not post-hoc retries of a benchmark row.
The redacted observations and reconstruction limitations are recorded in
`benchmarks/diagnostics/relay-493-18-turn2.md`.

This is a Relay compatibility mitigation supported by the observed condition,
not an identified internal root cause or a guarantee of completion.

## Frozen settings

- Dataset: `KAKA22/SpreadsheetBench@ab0b742b0fc95b946f212d80ac7771b5531272e4`
- Model: `gpt-5.6-luna`
- Reasoning effort: `low`
- Maximum provider responses per arm-task: 20
- Stage response caps: bare solve `20`; paper
  extract/vision/LaTeX/reconcile/solve `6+3+3+1+7=20`; ours solve `20`
- Maximum provider-reported tokens per arm-task: 100,000
- Maximum output tokens per response: 4,096
- End-to-end arm-task wall time: 900 seconds
- Per-attempt request timeout/retries: 300 seconds / one transient retry
- Explicit overload retry backoff: at least 15 seconds before that one retry
- Semantic task retries: zero
- Circuit breaker: three exhausted transient arm-tasks
- Arm-order seed: `20260811`, with deterministic cyclic counterbalancing
- Shared preview: bounded `flat-workbook-preview-v1` records with canonical
  escaping, coordinates, values, formulas, types, ranges, merges, tables,
  truncation metadata, and a content hash
- Named-tool prefixes, followed by `auto`:
  - bare solve: `code_interpreter`, `code_interpreter`
  - paper extract: `list_sheets`, `inspect_range`
  - paper vision: `render_workbook`, `view_image`
  - paper LaTeX: `range_to_latex`
  - paper solve: `code_interpreter`, `code_interpreter`
  - ours solve: `list_sheets`, `inspect_range`

Paper vision deliberately reserves exactly three responses: render, attached
image view, and final evidence YAML. Prefix routing therefore leaves one final
response in every stage but is intentionally strict.

Requested and observed prefix tools are recorded for every stage and any
mismatch fails closed. Outcome differences are attributable to each complete
arm policy; this study does not isolate a tools-, vision-, routing-, or
skills-only causal effect.

## Differences from the released paper system

The `paper` arm is a fixed single pass through
extract to vision to LaTeX to reconcile to solve. It does not implement the
paper's per-sheet extraction or the up-to-three-round verifier feedback loop
back into the extractor. It uses the same Luna small model for every role rather
than the released Qwen3-Coder-480B extractor/solver plus GLM-4.5V vision model.
For fairness, it also withholds evaluator-only `instruction_type`,
`answer_position`, and the golden workbook from the solver. The released code's
solver constraints and evaluator metadata are therefore not reproduced.

The paper reports an original 912-task soft/hard-restriction setting, whereas
this study uses Verified 398, a corrected value scorer, and LibreOffice. Its
absolute scores must not be compared directly with the paper's tables.

## Gates and reporting

The six-task canary covers formula repair, merged cells, a large range,
ordinary sheet editing, multi-sheet reasoning, and an explicitly
color-dependent task. It passes only when all 18 arm-tasks finish without an
infrastructure error, every output opens, every routing prefix matches, no
budget is exhausted, and each paper task demonstrates render to attached image,
range-to-LaTeX, parseable provenance, and unchanged read-only workbook hashes.

The 30-task pilot contains the same six canary tasks, so it remains exploratory.
Report the complete 30-task table and a separate 24-task sensitivity table that
excludes canary tasks. Primary metrics are end-to-end accuracy and Wilson 95%
intervals, Cell/Sheet strata, completion/error rate, model responses, provider
tokens, known HTTP attempts, elapsed time, and tokens per pass. The summary
marks whether request-attempt auditing is complete; for an error row without
successful-turn timings, `known_http_attempts_sum` is a lower bound. Paired
deltas use a stratified bootstrap; binary paired outcomes use exact McNemar
tests with Holm correction. Inferential statistics are valid only for a
complete paired matrix with no infrastructure errors.

Canonical ASCII-sorted ID-set hashes (one ID per line with a final newline):

- Canary: `3e606b226d0008068b89fc94cd18eef4ca00603911d5b722622d42d0d660eb86`
- Pilot: `9f5e3f0f57d2840d4f531f03ee2febea94c10aadd6842354b18dd168430588d6`
- Pilot in dataset execution order: `c74c9fac6123698e78ff78137ca6d0ca9ccc151a94cc123a6e772162c4b1e023`
