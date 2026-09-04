---
name: spreadsheet-formula
description: Synthesize, translate, and repair spreadsheet formulas with explicit reference and runtime checks.
---

# Formula capability

Prefer a verified adjacent formula pattern over inventing a new expression. Translate relative
references in the fill direction while preserving absolute rows and columns containing `$`.

- In period-based models, distinguish historical values, assumptions/selectors, forecast blanks,
  totals, and spacer rows before filling. A blank alone is not evidence that a formula is missing.
- Do not extend an adjacent formula through a historical/forecast boundary until headers,
  dependency direction, and at least one neighboring period confirm the intended translation.
- Store real Excel formulas beginning with `=`, using English function names and comma separators.
- Confirm that lookup tables and return ranges contain the requested key and return field before
  changing an index or match expression.
- Use materialized values for one-time transformations when live formulas are not requested and a
  new long dynamic-array or cross-sheet formula would be brittle.
- Preserve typed dates and number formats. For date-format requests, prefer typed date values plus
  `number_format` over converting dates to display text with `TEXT`.
- After formula changes, reopen with `data_only=False`, verify representative cells have formula
  type, run the pending-formula LibreOffice validation, and inspect cached results for errors or
  unexpected blanks.
- Compare populated historical and anchor cells before/after. Formula completion is not permission
  to replace an existing value or create a new calculation section outside the requested target.

A failed formula is evidence, not merely a prompt-writing problem. Retain the failed expression,
expected value, source dependencies, reference shape, backend, and boundary cells for repair.
