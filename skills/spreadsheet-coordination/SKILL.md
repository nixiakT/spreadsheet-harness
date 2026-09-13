---
name: spreadsheet-coordination
description: Coordinate structure, formula, manipulation, and verification skills through explicit handoffs and shared postconditions.
---

# Spreadsheet coordination capability

Treat the other spreadsheet capabilities as cooperating specialists. Before editing, create one
short contract containing the workbook/sheet scope, target cells, source dependencies, intended
mutation, and the postcondition that will be checked. Do not let a later specialist silently widen
that scope.

- Structure resolves sheet identity, headers, boundaries, and cross-sheet keys first.
- Formula/financial reasoning then derives the smallest valid value or formula change from that
  structure. Preserve populated historical, anchor, and check cells unless explicitly targeted.
- Manipulation applies only the agreed cells/ranges and preserves styles and neighboring boundaries.
- Verification rereads the changed cells, recalculates when formulas are involved, checks the
  immediate boundary and unchanged anchors, and reports any mismatch back to the responsible
  specialist before submission.

Use explicit handoffs rather than repeating broad workbook discovery. If a tool error or an
ambiguous handoff occurs, stop the dependent step, record the missing evidence, and retry with a
smaller bounded inspection. A successful tool call or saved workbook is not itself proof of
correctness; the final evaluator-facing postcondition is authoritative.
