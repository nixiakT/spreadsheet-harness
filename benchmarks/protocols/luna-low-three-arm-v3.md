# Luna low three-arm SpreadsheetBench protocol v3

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

## Why v3 exists

V1 and v2 established that Relay behavior depended strongly on tool routing.
Equivalent `auto` requests twice produced no headers within 180 seconds, while
a named request completed in 152 seconds. Later named requests ranged from a
few seconds to an explicit `server_is_overloaded` failure and a 321-second
transport failure. Request size and workbook tool output do not deterministically
explain those outcomes.

Two single-attempt reconstructions using `tool_choice: required` returned HTTP
200 headers within 1.0 second and completed in 5.9 and 5.7 seconds. The second
observed exactly one `code_interpreter` call. V3 therefore uses a common,
auditable routing and termination interface. Each frozen prefix response
exposes only its prescribed operational tool and sends `tool_choice: required`.
After the prefix, the stage exposes its normal operational tools plus the
shared `submit_result` control tool, again under `required`; the model calls
that control tool when the stage is complete. `parallel_tool_calls` remains
false, and a response with zero or multiple calls fails closed. The submission
response counts against model-response and token budgets but is not a workbook
tool invocation. Paper reconciliation has no tools and returns text directly.

This is a Relay compatibility mitigation, not an identified provider root
cause. It changes termination semantics, so v1/v2 rows cannot be combined with
v3. A v2 canary accidentally started during a read-only review was terminated
after exactly one completed paper row; that row is retained only as an aborted
diagnostic and is never resumed or imported.

## Frozen settings

- Dataset: `KAKA22/SpreadsheetBench@ab0b742b0fc95b946f212d80ac7771b5531272e4`
- Model: `gpt-5.6-luna`
- Reasoning effort: `low`
- Maximum provider responses per arm-task: 20, including `submit_result`
- Stage response caps: bare solve `20`; paper
  extract/vision/LaTeX/reconcile/solve `6+3+3+1+7=20`; ours solve `20`
- Maximum provider-reported tokens per arm-task: 100,000
- Maximum output tokens per response: 4,096
- End-to-end arm-task wall time: 900 seconds
- Per-attempt request timeout/retries: 300 seconds / one transient retry
- Explicit overload retry backoff: at least 15 seconds before that one retry
- Per-attempt audit: status, headers latency, first SSE latency, terminal event
  and latency, transport exception class, retryability, retry-after, and
  requested/actual backoff; a deadline-expired request that never starts is not
  counted as an HTTP attempt
- Semantic task retries: zero
- Circuit breaker: three exhausted transient arm-tasks or three required-routing
  protocol failures; any fatal provider error stops immediately
- Arm-order seed: `20260811`, with deterministic cyclic counterbalancing
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
vision deliberately reserves exactly three responses: render, attached original
image view, and `submit_result` with evidence YAML.

A `submit_result` response consumes one stage turn, one shared arm-task model
response, its provider-reported tokens, elapsed time, and HTTP attempt. It is
not included in `agent.tool_calls` or `tool_trace`; those fields record
operational workbook-tool attempts. Terminal submissions and total function
calls are audited separately. For paper extraction, vision, and LaTeX stages,
`submit_result.result` becomes the stage final response and undergoes the same
bounded YAML parsing, non-empty provenance, normalization, and read-only
workbook-integrity checks as a direct evidence response.

Outcome differences are attributable to each complete arm policy; this study
does not isolate a tools-, vision-, routing-, or skills-only causal effect.

## Differences from the released paper system

The `paper` arm is a fixed single pass through extraction, vision, LaTeX,
reconciliation, and solving. It does not implement the paper's per-sheet
extraction or up-to-three-round verifier feedback loop into the extractor. It
uses the same Luna small model for every role rather than the released
Qwen3-Coder-480B extractor/solver plus GLM-4.5V vision model. For fairness, it
also withholds evaluator-only `instruction_type`, `answer_position`, and the
golden workbook from the solver. The released code's solver constraints and
evaluator metadata are not reproduced.

The paper reports an original 912-task soft/hard-restriction setting, whereas
this study uses Verified 398, a corrected value scorer, and LibreOffice. Its
absolute scores must not be compared directly with the paper's tables.

## Gates and reporting

The six-task canary covers formula repair, merged cells, a large range,
ordinary sheet editing, multi-sheet reasoning, and an explicitly
color-dependent task. It passes only when all 18 arm-tasks finish without an
infrastructure error, every output opens, every prefix and terminal route
matches, no budget is exhausted, and each paper task demonstrates render to
attached original image, range-to-LaTeX, parseable provenance, and unchanged
read-only workbook hashes.

The 30-task pilot contains the same six canary tasks, so it remains exploratory.
Report the complete 30-task table and a separate 24-task sensitivity table that
excludes canary tasks. Primary metrics are end-to-end accuracy and Wilson 95%
intervals, Cell/Sheet strata, completion/error rate, model responses, provider
tokens, known HTTP attempts, elapsed time, and tokens per pass. Paired deltas
use a stratified bootstrap; binary paired outcomes use exact McNemar tests with
Holm correction. Inferential statistics are valid only for a complete paired
matrix with no infrastructure errors.

Canonical ASCII-sorted ID-set hashes (one ID per line with a final newline):

- Canary: `3e606b226d0008068b89fc94cd18eef4ca00603911d5b722622d42d0d660eb86`
- Pilot: `9f5e3f0f57d2840d4f531f03ee2febea94c10aadd6842354b18dd168430588d6`
- Pilot in dataset execution order: `c74c9fac6123698e78ff78137ca6d0ca9ccc151a94cc123a6e772162c4b1e023`
