# Domain-plugin co-evolution

## Core idea

Spreadsheet Harness starts with a reusable basic composition (H_0).  For a
downstream task family, we attach a small domain plugin (D_0), such as the
Financial Modeling plugin.  Rather than training a new agent or rewriting the
whole harness, execution experience updates (H) and (D) in alternating,
contract-preserving steps:

```text
basic harness H0
  + downstream plugin D0
  -> paired execution traces
  -> failure attribution: H / D / composition interface / infrastructure
  -> mutate one coordinate while freezing the other
  -> replay + domain transfer + general workbook regression
  -> accept or retire
  -> recompose and repeat
```

We call this **Domain-Plugin Coordinate Evolution**.  “Coordinate” is literal:
one accepted step changes either the general harness or the downstream plugin,
never both at once.  They nevertheless co-evolve because every candidate is
generated from and validated inside the current joint composition
(H_t \oplus D_t), and an accepted change alters the evidence and partner seen
by the next step.

## Why it follows naturally from spreadsheets

Spreadsheet failures often separate cleanly into two layers:

- General execution failures: wrong range grounding, poor workbook context,
  unsafe tool routing, failure to recalculate, or weak change verification.
- Domain failures: a wrong accounting identity, forecast boundary, sign
  convention, roll-forward, valuation rule, or model check.

A financial plugin should own the second layer, while the basic harness should
remain reusable across finance, supply chain, HR, sales, and real estate.  The
workbook artifact gives unusually strong global validation: formulas,
dependencies, cell values, sheet structure, styles, and model checks can all be
tested after a local plugin change.

## Attribution and update routing

For each failed development trajectory, the fixed router assigns one route:

1. **Infrastructure**: provider, timeout, scoring, or recalculation outage.  Do
   not evolve either component.
2. **Domain capability**: the domain plugin was active but supplied an
   incorrect or incomplete business rule.  Evolve (D).
3. **General capability**: the required evidence or operation is not
   domain-specific and fails across workbook families.  Evolve (H).
4. **Composition/interface**: the right plugin exists but was not activated,
   received insufficient context, or its check was not called.  Evolve the
   routing/interface seam in (H), keeping the plugin ABI fixed.
5. **Insufficient evidence**: collect another trace; do not guess a mutation.

The routing decision is auditable and binds evaluator outcomes, trajectory
hashes, activated plugin manifests, and the allowed evolution surface.

## Alternating evolution algorithm

At generation (t):

1. Run the current (H_t \oplus D_t) on a frozen development batch.
2. Remove infrastructure failures and attribute scored failures.
3. Rank eligible targets by repeated attributed failures and available evidence.
4. Choose one target.  When both layers are eligible, alternate after an
   accepted update so one layer cannot absorb all changes.
5. Generate one contract-valid candidate:
   - (H'): observation/profile rule, tool description, routing policy, or
     generic verifier behavior;
   - (D'): domain rule, domain template, domain evidence extraction, or
     domain-specific postcondition.
6. Freeze the partner and validate the candidate in the joint composition:
   - replay the attributed failure;
   - transfer to neighboring domain workbooks;
   - regress on general spreadsheet workflows and historical workbooks.
7. Promote only if the candidate improves the domain objective, passes every
   required context, and stays within the general non-regression bound.
8. Re-run attribution with the promoted composition.  Retire rejected
   candidates; do not weaken the gate.

Selection is lexicographic rather than a fragile scalar reward:

1. no severe workbook or evaluation failure;
2. general regression above its floor;
3. highest paired downstream-task score;
4. lower provider failures, tokens, calls, and elapsed time.

## Financial Modeling instantiation

The seed domain plugin observes compact workbook evidence and contributes:

- historical versus forecast boundary rules;
- relative/absolute reference discipline;
- revenue, margin, debt, cash-flow, valuation, and roll-forward identities;
- workbook-specific sign conventions;
- financial model checks and post-edit reconciliation.

Examples of basic-harness evolution are better evidence selection for long
models, preserving exact cell provenance through context compression, routing a
formula validation call after edits, and exposing a bounded dependency slice to
the domain plugin.  Examples of domain-plugin evolution are learning that a
particular schedule uses negative capex, recognizing a scenario selector, or
adding a debt roll-forward postcondition.  Domain semantics are not copied into
the generic tool runtime.

## Experimental decomposition

Use five frozen arms so “plugin gain” and “co-evolution gain” are distinct:

| Arm | Meaning |
| --- | --- |
| (H_0) | basic Spreadsheet Harness |
| (H_0 \oplus D_0) | add the seed downstream plugin |
| (H_1 \oplus D_0) | evolve only the basic harness |
| (H_0 \oplus D_1) | evolve only the domain plugin |
| (H_1 \oplus D_1) | alternating joint composition after co-evolution |

The first contrast measures modular specialization:

\[
G_{plugin}=S(H_0\oplus D_0)-S(H_0).
\]

The final contrast measures whether adapting the two layers together gives more
than either one-sided update:

\[
G_{co}=S(H_1\oplus D_1)-\max(S(H_1\oplus D_0),S(H_0\oplus D_1)).
\]

An optional interaction statistic is:

\[
I=S(H_1\oplus D_1)-S(H_1\oplus D_0)-S(H_0\oplus D_1)+S(H_0\oplus D_0).
\]

Report these only on common officially scored tasks, together with regression,
token/call cost, and failure rates.

## Data separation

User-supplied financial workbooks, metadata, and structurally deduplicated
synthetic workbooks may provide evolution evidence.  The development split is
grouped by workbook family to prevent near-identical sheets crossing splits.
SpreadsheetBench-v2 held-out Financial Modeling rows and Domain-Spreadsheet
evaluation workbooks/targets cannot be used to generate candidates, write
rules, select generations, or trigger rollback.  A final composition is frozen
before either held-out evaluator is opened.

## Difference from adjacent work

The intended distinction is modest and architectural.  Spreadsheet-RL improves
model weights inside a strong, mostly fixed spreadsheet environment; other
harness work commonly adds a fixed tool, view, workflow, or skill mechanism.
Here the model may stay fixed while a general spreadsheet harness and an
independently versioned downstream module adapt through execution traces under
one ABI.  Updates remain local, but promotion is decided by the composed final
workbook.  The claim is therefore not a new optimization algorithm; it is that
a pluginized spreadsheet harness makes controlled downstream specialization and
composition-aware evolution practical and measurable.

