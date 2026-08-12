# Luna low three-arm SpreadsheetBench protocol

This is a fixed, paired, directional pilot rather than an unbiased estimate of
the full Verified 398 set. Every selected task is run with the same model,
reasoning effort, workbook input, LibreOffice backend, corrected value scorer,
five-row preview policy, and end-to-end limits.

The arms are:

1. `bare`: model plus an isolated local Python interpreter.
2. `paper`: a clean-room, small-model-only adaptation inspired by
   SpreadsheetAgent's task-independent structural extraction and complementary
   visual/LaTeX verification. It is not the paper's original implementation or
   an absolute-score reproduction.
3. `ours`: the local spreadsheet tools, original-image vision path, and frozen
   spreadsheet skills.

Frozen settings:

- Dataset: `KAKA22/SpreadsheetBench@ab0b742b0fc95b946f212d80ac7771b5531272e4`
- Model: `gpt-5.6-luna`
- Reasoning effort: `low`
- Maximum provider responses per arm-task: 20
- Maximum provider-reported tokens per arm-task: 100,000, with a stop before
  another request once the recorded total reaches the limit; one final response
  can cross the threshold because its usage is only known after receipt
- Maximum output tokens per response: 4,096
- End-to-end arm-task wall time: 900 seconds
- Per-attempt request timeout/retries: 90 seconds / one transient retry. Luna-low canary
  requests normally produce their first event within seconds; this bounded
  timeout prevents a stalled Relay stream from consuming a third of the shared
  arm-task deadline before the single retry.
- Semantic task retries: zero
- Circuit breaker: three exhausted transient arm-tasks
- Arm-order seed: `20260811`, with deterministic cyclic counterbalancing
- Shared five-row preview serialization: bounded flat records with canonical
  escaping, per-cell coordinates/values/formulas/types, and sheet/range/merge/
  table/truncation metadata. In pre-run diagnostics, nested JSON/YAML summary
  conditions stalled while an equivalent flat TSV summary completed; separate
  fresh non-trivial generation and tool calls also completed. This is an
  observed compatibility difference, not an identified Relay root cause.
- Deterministic first-tool routing, followed by `auto`: `code_interpreter` for
  bare and paper solve, `list_sheets` for ours and paper extraction,
  `render_workbook` for paper vision, and `range_to_latex` for paper LaTeX.
  Requested and observed routing are recorded and mismatches fail closed. Any
  outcome difference is attributable to the complete arm policy, including
  this routing; the study does not isolate a tools-, vision-, or skills-only
  causal effect.

The six-task canary covers formula repair, merged cells, a large range, ordinary
sheet editing, multi-sheet reasoning, and an explicitly color-dependent task.
It must finish all 18 arm-tasks without infrastructure errors; every output must
open; budget exhaustion must stay below 10%; and the paper arm must demonstrate
successful render/view-image and range-to-LaTeX calls in its recorded trace.
These six tasks are also members of the 30-task pilot and may expose harness
failures during capability-gate debugging. The pilot is therefore exploratory;
report a separate 24-task sensitivity result excluding all canary tasks rather
than describing the full 30-task table as untouched or confirmatory.

The 30-task pilot contains 21 Cell-Level and 9 Sheet-Level tasks. Selection was
frozen before any three-arm result was produced. Files were limited to 250 KiB,
30,000 non-empty cells, 750 corrected-scorer cells, and 100,000 aggregate used-
range cells, then manually stratified across formula, text/date, formatting/
merge/visual, large-range, reshaping, ordinary edit, multi-sheet, and sorting
capabilities. The two dataset rows marked excluded were not eligible.

Canonical ID-set hashes use ASCII-sorted IDs, one per line, including the final
newline:

- Canary: `3e606b226d0008068b89fc94cd18eef4ca00603911d5b722622d42d0d660eb86`
- Pilot: `9f5e3f0f57d2840d4f531f03ee2febea94c10aadd6842354b18dd168430588d6`
- Pilot in dataset execution order: `c74c9fac6123698e78ff78137ca6d0ca9ccc151a94cc123a6e772162c4b1e023`

Primary reporting uses end-to-end accuracy and Wilson 95% intervals, plus
Cell/Sheet strata, completion and error rates, provider tokens, model calls,
elapsed time, and tokens per pass. Paired deltas use a stratified bootstrap;
paired binary outcomes use exact McNemar tests with Holm correction. Inferential
statistics are only treated as valid when the full paired result matrix is
complete and free of infrastructure errors.
