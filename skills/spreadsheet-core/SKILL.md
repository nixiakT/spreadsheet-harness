---
name: spreadsheet-core
description: Safe inspect-edit-verify workflow for realistic spreadsheet tasks.
---

# Spreadsheet core workflow

Work on the smallest range that satisfies the instruction. Use the supplied profile and preview to target the first inspection; call `list_sheets` only when sheet identity is still ambiguous. Inspect the stated range and nearby headers, and search before assuming where a label or formula lives.

## Budget discipline

- The run has a hard call and token budget. Inspect, edit, verify, and submit; do not keep exploring once the requested range is understood.
- Keep diagnostic output bounded. Print representative rows, formulas, and formats rather than whole sheets or long cell-by-cell dumps.
- Make the first saved edit by the second tool call whenever the target can be identified safely. If a script fails, fix the specific exception instead of restarting broad inspection.
- After a successful save, verify only the changed range and its immediate boundary, then submit. Reserve the final model turn for `submit_result`, not another exploratory tool call.

## Preserve intent

- Treat formulas, number formats, borders, fills, merged cells, hidden rows/columns, and tables as part of the answer.
- Prefer formulas for values that should update when inputs change. Write literal values only for constants or when explicitly requested.
- Do not rebuild a sheet to make one local change. Avoid deleting rows or columns unless the instruction explicitly requires structural deletion.
- Extend an adjacent formula with `fill_formula`; it translates relative references more reliably than manually composing each formula.
- Match nearby formatting by inspecting it first. Use `format_range` only for requested properties.

## Verify

After every mutation, inspect the exact changed range and its immediate boundary. For formulas, recalculate with LibreOffice and inspect cached results. If the task is visual—formatting, charts, merged headers, or layout—render and view the relevant sheet image.

LibreOffice is the declared calculation backend. Do not silently substitute an Excel-only function when Calc semantics differ. If a task needs unsupported Excel behavior, preserve the formula where possible and state the backend limitation in the run summary.

## Tool discipline

- Spreadsheet mutation tools are transactional and create snapshots. If verification shows an unintended change, use `undo_last`.
- In the `ours` comparison arm, follow the arm instruction to use the managed code interpreter as the primary inspection and mutation path; `sheet_harness.save_workbook` records managed saves. Use native mutation tools for narrow gaps or when the arm instruction explicitly routes to them.
- Never access paths outside the run workspace or embed credentials in code, cells, logs, or responses.
