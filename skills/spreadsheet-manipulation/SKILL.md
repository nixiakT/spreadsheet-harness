---
name: spreadsheet-manipulation
description: Perform bounded spreadsheet filtering, sorting, cleaning, formatting, and structural mutations safely.
---

# Manipulation capability

Change the smallest cell/range surface that satisfies the task. Snapshot all source rows before
sorting, filtering, compacting, deleting, or rewriting a destination that overlaps the source.

- Treat planner-proposed writes as hypotheses until their exact targets are inspected. Preserve
  populated cells by default, and reject a bulk target that mixes blank and nonblank cells.
- Do not use an empty string as a clearing operation unless the instruction explicitly requests
  clearing and the before/after boundary is verified; use the workbook API's real blank value.
- Do not insert or delete worksheet rows/columns unless the instruction requires a structural
  change. For partial compaction, rewrite only the target columns and keep unrelated cells fixed.
- Clear leftover tail cells explicitly; assigning `None` through an API that skips existing values
  is not a verified clear.
- Preserve styles, number formats, merged regions, hidden state, tables, and stable ordering unless
  a requested operation changes them.
- Copy only the requested formatting properties. Avoid replacing a whole style for a local fill,
  border, alignment, or number-format edit.
- Verify complete intended coverage, no residual matches after removal/replacement, stable tie
  order for sorts, and no changes beyond the target boundary.

Record before/after range hashes or bounded cell diffs so a manipulation repair can distinguish a
wrong operation from a correct operation applied to the wrong boundary.
