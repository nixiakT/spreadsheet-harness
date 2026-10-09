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
