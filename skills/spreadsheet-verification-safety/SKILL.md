---
name: spreadsheet-verification-safety
description: Gate spreadsheet mutations with a minimal-patch, rollback-safe verification loop.
---

# Minimal-patch verification gate

Use this capability as a safety gate around every workbook mutation. The goal is to
increase exact success without sacrificing requested coverage: make a supported edit,
then prove it locally before doing anything else.

## Before editing

- Translate the instruction into an explicit set of target cells/ranges and expected value
  types. Do not infer extra targets from visually blank cells.
- Read the target, its immediate boundary, and the smallest precedent/source range. Treat
  populated historical values, assumptions, selectors, checks, anchors, and unrelated
  formulas as immutable unless the instruction names them.
- Record a compact before-snapshot of every target, its boundary, and the non-target cells
  that the planned formula references. If the target or expected operation remains
  ambiguous after this bounded inspection, do not guess; leave the workbook unchanged and
  report the exact missing evidence.

## Minimal mutation

- Apply the smallest edit that satisfies the explicit target set. Prefer translating a
  verified neighboring formula or writing a validated materialized value; never rewrite a
  whole sheet or a broad rectangle to solve a local issue.
- Keep independent sheets or schedules in separate edit/verify blocks. Do not continue to
  another block after a failed tool call or an uncertain read-back.
- Never overwrite a populated cell merely to make a pattern look uniform. Never fill
  spacer, label, terminal, sensitivity, or protected historical regions.

## Immediate gate

- Save once, recalculate when formulas are involved, and immediately read every target plus
  its boundary. Confirm exact address, value type, formula references, and non-blank/blank
  behavior against the predeclared postcondition.
- Check direct dependents for `#REF!`, `#VALUE!`, `#DIV/0!`, `#NAME?`, `#N/A`, `#NUM!`,
  unexpected blanks, or newly populated cells. Confirm protected anchors and non-target
  snapshot cells are unchanged.
- If any gate check fails, stop. Restore the last known-good workbook (or reload the
  untouched input and submit that unchanged state when restoration is unavailable); do not
  stack another speculative fix on top of a failed edit.
- A successful save or a syntactically valid formula is not evidence of success. The final
  evaluator-facing target values and regression checks are authoritative.

## Submission rule

Submit only after the full explicit target set has passed the gate. If evidence is
insufficient, a conservative unchanged workbook is preferable to an unverified broad edit.
Keep the failure coordinates and reason in the final result so the next evolution round can
attribute the issue to the correct plugin.
