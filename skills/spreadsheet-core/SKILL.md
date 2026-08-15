---
name: spreadsheet-core
description: Inspect, edit, and verify realistic spreadsheet workbooks safely, especially for formula fills, recalculation checks, layout-sensitive changes, and bounded workbook mutations.
---

# Spreadsheet core workflow

Work on the smallest range that satisfies the instruction. Use the supplied profile and preview to target the first inspection. If sheet identity or row role remains ambiguous, resolve it with a bounded `code_interpreter` inspection. Inspect the stated range and nearby structure, and search before assuming where a label, header, or formula lives.

## Budget discipline

- The run has a hard call and token budget. Inspect, edit, verify, and submit; do not keep exploring once the requested range is understood.
- Keep diagnostic output bounded. Print representative rows, formulas, and formats rather than whole sheets or long cell-by-cell dumps.
- Make the first saved edit by the second tool call whenever the target can be identified safely. If a script fails, fix the specific exception instead of restarting broad inspection.
- After a successful save, verify only the changed range and its immediate boundary, then submit. Reserve the final model turn for `submit_result`, not another exploratory tool call.

## Preserve intent

- Treat formulas, number formats, borders, fills, merged cells, hidden rows/columns, and tables as part of the answer.
- Do not rebuild a sheet to make one local change. Avoid deleting rows or columns unless the instruction explicitly requires structural deletion.
- Match nearby formatting by inspecting it first. Change only the requested properties with `code_interpreter`; do not replace an entire style when a narrower update is sufficient.

## Choose values or formulas

- Use formulas only when the instruction or a verified adjacent pattern requires values to remain live or updateable, and the exact formula behavior is supported by LibreOffice.
- Otherwise, prefer materialized values for one-time cleaning, sorting, mapping, or aggregation. Preserve each cell's style, number format, and intended data type unless the instruction explicitly changes them.
- Extend a verified adjacent formula with `fill_formula`; it translates relative references more reliably than manually composing each formula. For an uncertain function or dynamic-array behavior, validate the smallest decisive sample with LibreOffice before a broad fill.
- If recalculation exposes an unsupported formula, error, or unexpected blank, immediately use a Calc-compatible formula or, when live formulas are not required, replace it with validated materialized values.

## Boundary contract

- Never assume row 1 is a header. Establish headers and data boundaries from the instruction, labels, formulas, styles, types, merged regions, and neighboring rows; inspect when the evidence conflicts.
- Before editing, identify the exact target rows and columns. Inspect the first and last target positions and the immediately adjacent positions on each relevant side.
- Preserve title, header, section, subtotal, and total anchors unless the instruction explicitly targets them. Treat a style change or blank separator as possible boundary evidence, not as disposable data.
- After editing, verify complete intended coverage, no residual matches for requested removal or replacement, and no changes beyond the target boundary.
- Preserve date and datetime values as typed dates with their number formats; do not serialize them to strings. Use stable sorting and preserve original relative order within equal-key groups unless another tie-breaker is requested.

## Verify

After every mutation, inspect the exact changed range and its immediate boundary. For formulas, recalculate with LibreOffice and inspect cached results. If the task is visual—formatting, charts, merged headers, or layout—render and view the relevant sheet image.

LibreOffice is the declared calculation backend. Do not silently substitute an Excel-only function when Calc semantics differ. A known invalid live formula blocks submission: repair it with a compatible formula, or use validated materialized values only when the instruction allows a non-live result.

### Formula verification gate

Complete every applicable check before submitting a formula edit:

- Define the exact expected target cells before editing. After saving, assert that every target contains the intended formula and that the cells immediately outside the target were not filled accidentally.
- Inspect formulas and cached results at the first, middle, and last target positions, plus any blank-to-data or data-to-blank boundary. For a two-dimensional fill, sample first/middle/last positions on both the horizontal and vertical axes.
- Compare translated formulas, not just their displayed values. Relative row and column references must move in the fill direction; absolute rows, absolute columns, and fully absolute references containing `$` must remain fixed.
- Recalculate the exact target and its dependency boundary with `recalculate_and_read`. Assert that cells expected to produce values are nonblank, that any blank matches the intended blank-input behavior, and that results contain none of `#REF!`, `#VALUE!`, `#DIV/0!`, `#NAME?`, `#N/A`, `#NUM!`, `#NULL!`, `#SPILL!`, or `#CALC!`. If a formula text is returned where an intended cached blank is ambiguous, reopen the recalculated workbook with `data_only=True` in `code_interpreter`; for cross-sheet formulas, also inspect the smallest decisive source range.
- Hand-check representative inputs for last-N, date-filtered, blank-aware, or lookup logic. Include the cutoff or last included item, a blank when one exists, and a duplicate key when one exists; confirm whether the intended lookup chooses the first, last, or all duplicates.

Any missing target, reference drift, Calc error, unexpected blank, or failed hand-check blocks submission. Correct it, recalculate, and repeat the failed checks; never report success with a known formula or recalculation error.

## Tool discipline

- In the `ours` comparison arm, use only its fixed tool set: `code_interpreter`, `inspect_range`, `fill_formula`, `recalculate_and_read`, `render_workbook`, and `view_image`.
- Use the managed code interpreter as the primary inspection and mutation path; `sheet_harness.save_workbook` records managed saves. Use `inspect_range` only for a bounded target check and `fill_formula` only for an already-verified adjacent pattern.
- Use `recalculate_and_read` only when formula results matter. Use `render_workbook` followed by `view_image` only when the answer depends on layout or visual formatting.
- Never access paths outside the run workspace or embed credentials in code, cells, logs, or responses.
