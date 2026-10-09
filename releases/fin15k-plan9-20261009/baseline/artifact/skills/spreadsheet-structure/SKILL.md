---
name: spreadsheet-structure
description: Ground spreadsheet edits in workbook, sheet, table, header, range, and cross-sheet structure.
---

# Structure capability

Resolve structure before choosing an edit. Treat workbook, sheet, region, table, header, key,
and cross-sheet relations as separate claims that need cell/range evidence.

- Do not assume row 1 is a header. Use labels, types, styles, formulas, merged regions, blank
  separators, and neighboring rows to identify headers and table boundaries.
- Ground every requested A1 coordinate exactly. Preserve its sheet, column, and row; do not turn
  an absolute workbook coordinate into a relative output offset.
- For cross-sheet work, combine column-name similarity with datatype, value overlap, uniqueness,
  and sampled key coverage. A plausible name match alone is not a join relation.
- Inspect the first and last target positions and their immediate boundary before mutation.
  Preserve title, subtotal, total, and adjacent regions unless the task explicitly targets them.
- Keep inspection bounded. Use the deterministic profile as an index, then inspect only the
  smallest ranges needed to confirm ambiguous structure.

Pass a compact structure contract to later capabilities: source sheets/ranges, target
sheets/ranges, header rows, key relations, data boundaries, and unresolved uncertainty.
