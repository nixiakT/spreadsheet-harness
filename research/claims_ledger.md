# Claims ledger

Every quantitative or novelty claim in the manuscript must have a status and an evidence
anchor. `blocked` means the statement must remain absent from the paper, not softened into
an unsupported claim. `optional-blocked` means the study is outside the core contribution and
the statement remains absent unless its separately preregistered evidence is completed.

| Claim ID | Proposed statement | Required evidence | Status |
| --- | --- | --- | --- |
| C1 | Online enforcement reduces unsafe accepted delivery without an unacceptable Accuracy or accepted-terminal coverage loss. | One B6 online source execution with B5 durably frozen at the first gate-eligible submit before rejection feedback; no second rollout or post-outcome reconstruction; family-atomic B5/B6/FULL publication; task-level unsafe accepted delivery over all tasks plus preregistered Accuracy/coverage guardrails on the primary checkpoint and a separately reported second-family replication. | pending-principal |
| C2 | Declaration elicitation and staged-containment enforcement have separately identified effects on wrong-target/off-target outcomes. | Secondary G1 vs G0 and G2 vs G1 paired effects on the artifact-unseen Debugging stratum, with gold-only masks, staged-containment oracle, grounding rejection/recovery, and a separately reported second-family replication. G2 vs G0 is descriptive total change, not one gate's effect. | pending-secondary |
| C3 | Raw spreadsheet traces are observationally insufficient for completion validity, while workbook-enriched events separate the sealed indistinguishable pairs. | Exact valid/invalid pairs with byte-identical raw projection and independent semantic labels; per-pair raw-only lower bound; principal same-policy enriched-vs-raw invalid-attack-acceptance gate with valid-control-rejection guardrail. The full event/policy $2\times2$ is secondary. | pending-principal |
| C4 | Revision and scope binding are causally necessary for stale/wrong-scope rejection. | Oracle and applicable real-task A2/A3 ablations with valid controls. | pending |
| C5 | For an accepted candidate, finalization lineage binds the exact scoring replica under a trusted host and storage. | Secondary FULL vs B6 sharing the same source terminal and factual finalization assessment, plus the model-free paired sealed finalization challenge, replay audit, field-corruption tests, accepted final/replica hashes, and all-row observer records passing fresh audit. | pending-secondary |
| C6 | Optional jointly distilled procedure/contract artifacts add value beyond the frozen researcher-authored fixed contract. | Separately preregistered study with disjoint evolution, contract-unit, and selection layers; frozen candidate/query budgets; paired comparison against the unchanged fixed contract. | optional-blocked |
| C7 | Optional distilled artifacts transfer across models. | Frozen author-model artifacts evaluated unchanged on another family/scale, reported separately from fixed-contract portability. | optional-blocked |
| C8 | Runtime overhead is practical. | Tokens/task, calls, wall time, component CPU time, and accuracy-budget curves with intervals. | pending |
| C9 | Historical predecessor v27 deployment outcomes may be described only as exploratory context. | Complete 158-row fresh audit, frozen result report, and the disclosed interim-audit deviation in `research/protocol_deviations.md`; no confirmatory test, method-selection claim, or SheetLedger attribution is permitted. | exploratory-only |

The headline mechanism-and-utility claim is conjunctive: both principal claims C3 and C1
must pass their frozen paired test and guardrail. Passing one does not compensate for failing
or leaving the other unresolved. C2 and C5 remain secondary even when their intervals are
favorable.

## Allowed structural claim

The following is a conditional invariant that can be established by implementation tests and
a proof over the monitor transition rules:

> Assuming trusted event instrumentation and collision-resistant artifact hashes, every
> accepted submission has a contract-authorized witness after its triggering mutation, on
> the required artifact lineage, covering the required workbook scope, with the configured
> predicate satisfied.

For protected mutations, the target-grounding extension additionally establishes that a
same-source, pre-edit observation set covered the declared target and that the complete
staged semantic footprint stayed inside it. Neither statement proves that the declaration is
the target intended by the user.

## Implementation claims

Structural statements in the paper require code, adversarial tests, and an independent replay
anchor even when they contain no benchmark number.

| Claim ID | Structural statement | Required anchor | Status |
| --- | --- | --- | --- |
| I1 | Native and opaque-code mutations use a harness-computed semantic footprint and fail closed when it is absent, incomplete, or opaque. | `workbook_diff.py`, session/code adapters, missing/unknown/chart/layout tests, and an independent standard-library ZIP/XML oracle covering shared/array formulas, `calcPr`/manual-calculation changes, atomic custom-properties plumbing, opaque/lossy parts, and producer-churn controls. | pending-review |
| I2 | Read/render/view events cannot publish an artifact revision, and same-stage coverage never crosses a byte transition. | monitor transition tests including hash-cycle/recalc cases and replay audit. | pending-review |
| I3 | A prospective declaration is revision-bound, observation-covered, one-use, and contains the staged footprint. | target-grounding state machine, transactional tool integration, opaque-code staging tests. | pending |
| I4 | Formula witnesses bind recalculation source/output and final readback. | portable metadata validation, dynamic selector tests, finalization replay. | pending-review |
| I5 | Visual evidence transfers through finalization only under sheet/page and pixel-hash equivalence. | final render-equivalence and page/hash corruption tests. | pending-review |
| I6 | For an accepted candidate, the scoring input is a byte-identical replica of the certified final artifact and remains unchanged during trusted scoring; a noncompletion has only diagnostic-copy lineage. | deliverable/audit integration, accepted/noncompletion branch tests, read-only/COW scorer test, before/after digest. | pending-review |
| I7 | All-row finalization records, and accepted-deliverable certificates, provide canonical content consistency only under a trusted host/storage; they are not digital signatures and cannot stop coordinated record/artifact/timestamp rewriting. | terminology scan, threat-model text, wholesale-rewrite negative statement, and explicit external-anchor/signature/trusted-clock requirement. | pending-review |
| I8 | Every detected submit is frozen before the gate; acceptance is terminal-bound to its exact attempt, while termination without acceptance records a failure reason, complete immutable-attempt ledger, and observer-only finalization integrity record but claims no accepted candidate or deliverable. | completion-attempt, terminal-binding, audited-noncompletion, tampered-reason/ledger, and fresh-replay tests. | pending-review |

This is **not** a correctness proof. It does not establish that the target scope is the one
intended by the user, that a predicate captures full spreadsheet semantics, or that the
recalculation backend agrees with Excel.

## Prohibited wording

- `100% guaranteed acceptance`, `proof of correctness`, or `formally verified spreadsheet`;
- `state of the art` without a protocol-equivalent, fresh, reproducible comparison;
- `beats Trace2Skill/SpreadsheetAgent/Spreadsheet-RL` based on a different split, model,
  backend, or harness;
- `100-task SpreadsheetBench held-out` by padding the exhausted 79-task reserve with exposed
  examples;
- `400-example held-out` for Trace2Skill's nominal collection: the official split uses 0:200
  for evolution and 200:400 for evaluation, and documented local exclusions leave 198 usable
  held-out examples;
- `open model` for an opaque service alias whose checkpoint weights and revision are unknown.
