---
name: spreadsheet-verification
description: Verify workbook edits against user intent with capability-specific postconditions.
---

# Verification capability

Treat agent completion and a saved workbook as execution facts, not correctness. Verify the exact
user intent against before/after workbook state.

- Structure: confirm sheet/range identity, target coverage, boundaries, and absence of unintended
  row, column, table, or merged-region changes.
- Formula: confirm stored formulas, translated references, dependency ranges, clean LibreOffice
  cached values, and no unexpected errors or blanks.
- Manipulation: compare bounded before/after values and formats, residual matches, stable ordering,
  and untouched neighboring cells.
- Regression: compare populated non-target cells before/after, with special attention to historical
  periods, assumptions, selectors, subtotal anchors, and cells bordering a formula fill.
- Analysis: check fields, keys, aggregation semantics, representative groups, totals, and output
  placement.
- Visualization: check chart existence, type, source range, series, axis, legend, placement, and a
  rendered image when layout matters.

Return `pass` only when every applicable postcondition has evidence. Otherwise return a failure
type, exact coordinates/ranges, and the missing or contradictory evidence. Preserve false-positive
and false-negative traces so the verifier itself can evolve.

Task completion does not authorize inventing a new calculation section or filling unrelated blank
regions. Treat either change as a verification failure unless it is explicitly required.

## Bounded independent completion audit

A clean save and a passing formula-runtime check are separate from semantic completion.
Review the already edited workbook read-only first. Use the current instruction's clause ledger,
not an automatically broadened list of all neighbouring holes. A concrete gap means a missing
requested output, wrong output type, wrong entity/period/units, or a documented erroneous target;
it does not mean an optional extra total could be calculated. Preserve unrequested blanks.

For cross-sheet outputs, compare both row role and actual period headers; column letters alone
are not a join key. A scalar terminal/summary column is not the next forecast period. After any
necessary repair, reopen and recalculate affected formulas, then repeat these same postconditions.
If evidence or budget is insufficient, report an unresolved verification failure; do not certify
success or fill unknowns with constants. Hook checks precede benchmark final recalculation.
