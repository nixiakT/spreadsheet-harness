---
name: spreadsheet-financial-model
description: Complete and repair financial-model schedules while preserving historical periods, assumptions, selectors, and model checks.
---

# Financial-model capability

Produce executable edits, not an uncertainty essay. Treat blank cells as candidates rather than
automatically missing formulas, but resolve the row role, historical/forecast boundary, and
governing pattern from period headers, nearby formulas, labels, and dependency direction before
emitting an action. If the available evidence does not identify an exact target and formula, omit
that action and give the executor one small exact range to inspect; never emit placeholders,
duplicate fields, comments inside values, or speculative zero fills.

- Preserve populated historical values, imported actuals, assumption inputs, scenario selectors,
  check rows, and anchor formulas unless the instruction explicitly targets them.
- When the task prompt reports instruction-grounded deterministic warm-start targets, treat those
  populated cells as protected completed work. Verify them with one bounded
  `sheet_harness.view_xlsx(...)` batch, inspect only clauses that remain visibly blank, and do not
  restart workbook-wide discovery or rewrite a completed formula with a merely equivalent variant.
- Fill a forecast blank only when adjacent periods establish the same semantic row and a formula
  translates cleanly across the boundary. Do not extend formulas into spacer, label, notes, terminal,
  or sensitivity-table regions merely because they are blank.
- Keep absolute and mixed references stable, especially selector cells and assumption rows. Confirm
  that a translated forecast formula points to the intended period and scenario.
- Follow the workbook's stated sign convention. Inspect nearby notes and existing periods before
  writing expense, deduction, cash-outflow, debt-repayment, or contra-account formulas; if the
  model represents deductions as negative numbers, preserve that sign through every dependent row.
- Reconcile sign semantics across schedules rather than judging an isolated formula. Revenue growth
  normally compounds as `prior * (1 + growth)`; after-tax debt cost uses `cost * (1 - tax rate)`;
  equity value subtracts net debt from enterprise value; and unlevered free cash flow subtracts both
  capex and an increase in net working capital. Apply these identities only when the row labels and
  the workbook's source schedules establish the same convention.
- For totals, ratios, debt schedules, and roll-forwards, inspect the smallest decisive dependencies
  and check the relevant accounting identity or continuity equation after recalculation.
- Cover every independently requested schedule or chart. Group a horizontal forecast into one
  translatable range action only when its top-left formula has been grounded. A formula for a
  multi-cell target must be the real top-left formula with relative references that translate.
- Prefer copying the workbook's adjacent formula pattern over inventing a textbook identity. Use
  a textbook identity only when row labels and the named source cells establish every operand.
- Reject a bulk proposal that mixes blank targets with populated cells. Escalate populated targets
  to exact executor inspection instead of overwriting them from a plan.

After editing, compare the changed cells and their immediate boundaries against the original,
recalculate, and confirm that existing historical/anchor cells remain unchanged and model checks do
not worsen.

## Financial target identity and typed outputs

Identify the requested financial calculation locally before filling: unique sheet and logical
block, complete metric label, sign convention, output type, and period header. Preserve single
letter/category labels and percent markers: A, A%, a category flag and a numeric ratio are not
interchangeable. A YES/NO flag or category must keep its declared text domain; do not substitute
1/0 just because a formula computes. Respect existing expenses/deductions signs and verify the
same convention in their dependent total. Derive quantities from the workbook, not constants
invented to make a check cell pass.

Blank cells outside that contract remain blank. Repeated captions in a second table are ambiguous
until its header and operands establish that it is requested. Never extend a missing financial
formula merely because another calculation can be mechanically translated into the cell.

Parse every instruction-named sheet and clause, including On/in and quoted names. If sheet or metric names have multiple matches, inspect the smallest decisive block; do not pick the first partial-token match. Check denominator support before adding ratios.

Join source and destination periods by their actual fiscal/calendar/year/month/quarter headers, not a fixed column offset. Require an unambiguous period granularity and entity; do not cross terminal, annual-summary or sensitivity boundaries with drag-fill.
