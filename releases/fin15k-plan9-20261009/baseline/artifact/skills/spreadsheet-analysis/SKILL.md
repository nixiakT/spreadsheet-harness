---
name: spreadsheet-analysis
description: Build auditable spreadsheet aggregations, group-bys, pivots, statistics, and cross-table analyses.
---

# Analysis capability

Define the analytical contract before writing output: source ranges, grouping keys, measures,
filters, missing-value policy, duplicate-key policy, ordering, and destination range.

- Validate grouping and join keys with structure evidence; do not aggregate on a guessed header or
  a name-only cross-sheet match.
- Compute a small representative result independently before filling the full output.
- Preserve zeros, blanks, errors, dates, and text as distinct states. Do not silently coerce a
  missing value into zero unless the task specifies that policy.
- For pivot-like output, verify expected fields, row/column labels, aggregation function, output
  range, totals, and at least one hand-calculated group.
- When a native pivot object is unsupported by the backend, produce a clearly materialized summary
  only if it satisfies the requested artifact semantics; do not claim that a static table is a
  native pivot object.

Verification should compare source/output pairs and boundary totals, not merely check that a new
sheet or table exists.
