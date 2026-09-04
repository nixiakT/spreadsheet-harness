---
name: spreadsheet-memory
description: Reuse only validated spreadsheet procedures, backend workarounds, and transferable templates.
---

# Experience and memory capability

Use memory as advisory, reusable evidence rather than as workbook-specific truth. A remembered
procedure must still be grounded in the current workbook.

- Prefer procedures supported by repeated evaluator outcomes across more than one workbook.
- Bind backend workarounds to their engine and version assumptions. For example, a LibreOffice or
  openpyxl workaround must not be presented as universal Excel behavior.
- Keep templates parameterized by semantic inputs, target ranges, and postconditions; remove task
  IDs, hidden answers, exact held-out coordinates, and one-off workbook content.
- When memory conflicts with observed structure or runtime evidence, current evidence wins.
- Record usage and marginal utility. Merge redundant procedures, refine rules with repeated mixed
  outcomes, roll back regressions, and retire entries that remain unused or non-transferable.

Only validation-gated candidates enter persistent memory. Ephemeral candidates may be replayed on
the failure case, neighboring tasks, and historical regression workbooks but must not silently
replace persistent procedures.
