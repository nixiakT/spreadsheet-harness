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
