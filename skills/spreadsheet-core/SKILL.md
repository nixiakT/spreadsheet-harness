---
name: spreadsheet-core
description: Safe inspect-edit-verify workflow for realistic spreadsheet tasks.
---

# Spreadsheet core workflow

Work on the smallest range that satisfies the instruction. Start with `list_sheets`, then inspect the stated range and nearby headers. Search before assuming where a label or formula lives.

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
- The code interpreter is valid for analysis, bulk computations, and direct workbook edits when it is the available or most reliable execution path. When editing with Python, load `SHEET_WORKBOOK`, save back to that exact path, reopen it, and verify the changed range.
- Never access paths outside the run workspace or embed credentials in code, cells, logs, or responses.
