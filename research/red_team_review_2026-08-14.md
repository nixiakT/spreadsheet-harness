# Independent red-team review: 2026-08-14

Scope: manuscript, experiment blueprint, claims ledger, evidence-contract implementation,
and ICLR 2027 compliance. The reviewer did not inspect frozen benchmark instances or v27
task outputs and did not call an evaluated model. Initial recommendation was weak reject /
reject if only the result placeholders were filled. This ledger records required corrections;
`implemented` never substitutes for a fresh empirical result.

## P0 findings and disposition

| ID | Finding | Disposition | Remaining gate |
| --- | --- | --- | --- |
| R1 | Novelty overstated generic monitors and understated VIGIL/AgentSpec/AgentLTL. | Manuscript now disclaims novelty for temporal languages, artifact identity, and online gates. It narrows the mechanism to opaque XLSX/Python state-to-event lifting, prospective staged containment, recalc/render revision semantics, and accepted-candidate-to-exact-scoring-replica lineage. Generic engines are credited with artifact/semantic expressiveness. | Run the core same-policy raw/enriched contrast and containing generic/restricted-policy factorial; expand verified related work. |
| R2 | The earlier main comparison changed enforcement, distillation, and finalization together. | The causal core now uses one researcher-authored fixed contract: B5/B6 differ only in the submission gate; B6/FULL use equivalent deterministic postprocessing and differ in exact accepted-terminal/finalization lineage enforcement. Immutable-attempt capture is common measurement instrumentation. Trace2Skill is procedure-only, and joint distillation is optional secondary work. | Freeze exact arm manifests, grounding settings, instrumentation, and budgets before external test; run the core model-free paired finalization challenge. |
| R3 | Static artifact equality was inconsistent with recalculation and cumulative coverage. | Method now uses byte-publication revisions and stage machines with dynamic artifact selectors; an accepted scoring replica (or noncompletion diagnostic copy) is not a new revision. | Independent replay review, hash-cycle tests, and code/paper conformance audit. |
| R4 | Hash chains were described as signatures/authentication. | Wording now says content-addressed consistency under a trusted host/storage and explicitly excludes coordinated rewriting of artifacts, records, and timestamps. | Terminology scan; an external append-only anchor or transparency log, authenticated signatures, and trusted clock are required before any hostile-host claim. |
| R5 | Post-edit evidence could not address the motivating wrong-target failure. | Added prospective target grounding: inspect, declare, stage, semantic-diff containment, then publish. G1--G0 identifies declaration elicitation; G2--G1 identifies containment enforcement; G2--G0 is only a total contrast. | Transactional native/opaque runtime integration and the two separately interpreted contrasts. |
| R6 | Multi-seed inference risked treating task-seed rows, or two selected model families, as independent. | Task is now the cluster; model families are unpooled fixed replication strata; single-seed Wilson/McNemar are restricted; Modification uses paired bootstrap; four primary-checkpoint real-task tests plus one model-free oracle test form the Holm family. | Freeze code, power/precision analysis, and decision rules before launch. |
| R7 | Contract selection and final oracle shared attack families, risking circular validation. | The core contract is researcher-authored and frozen on development data; the sealed tamper corpus is not used to author it. A standard-library ZIP/XML oracle now shares no parser with the runtime comparator and covers shared/array formulas, `calcPr`/manual-calculation changes, custom-properties plumbing, opaque/lossy parts, exact `ExcelA1` producer churn, and paired negative variants. Optional distillation remains quarantined from the core. | Seal template code/seeds; add producer-diverse workbooks, held-out handcrafted corruptions, property/fuzz tests, and Excel/LibreOffice differential cases. |
| R8 | Structural claims lacked machine-enforced evidence gates. | Claims ledger now separates empirical C1--C9 from structural I1--I8, including immutable attempts, terminal binding, accepted-only deliverable claims, and an observer-only finalization integrity record for audited noncompletion. Release checks cannot promote a pending implementation claim into an empirical result. | Link every structural claim to independently reviewed code, adversarial tests, and fresh replay before release. |
| R9 | The enforcement contrast used false first completion even though the first attempt is frozen before the gate; attempt coverage also failed to expose reject-all behavior. | C1 now uses accepted-terminal false completion over all tasks, with Accuracy and accepted-terminal coverage as non-inferiority guardrails. First/any-attempt rates are diagnostic. | Freeze practically justified margins and the one-sided clustered precision analysis; verify result code distinguishes attempt, accepted-terminal, and scoring-delivery coverage. |

## P1 gates

- Run Visualization-24 before making visual generalization claims; preregister any broader
  SpreadsheetBench 2 secondary suite before opening instances.
- Describe Debugging-100 as instance-artifact-unseen to this study, not fresh to the model;
  disclose prior exposure to paper-level aggregates and the published case study.
- Prefer official prior-work code; otherwise publish adapter policies and visible event fields.
  Label B1 `Spreadsheet-RL-inspired native-tool adapter (no RL-trained checkpoint)`, B2
  `SpreadsheetAgent-inspired multi-format adapter`, and T2S as procedure-only inspired; do
  not claim official reproduction.
- Keep Trace2Skill's nominal 400 examples, official 0:200 evolution / 200:400 held-out split,
  and the locally usable 198 held-out examples distinct. Keep SpreadsheetBench sibling-replay
  Soft/Hard (`solution_once_apply_n`) results separate from `agent_per_workbook` results.
- Use an independent standard-library ZIP/XML oracle for OOXML effects and include shared/array
  formulas, lossy round-trip, opaque-part, exact LibreOffice `ExcelA1` churn versus near-
  neighbor negatives, fuzz/property checks, real-workbook stress, and Excel/LibreOffice
  differential cases.
- Report attempt, accepted-terminal, and scoring-delivery coverage, accepted error, submission and grounding rejection/recovery, target
  precision/recall, collateral damage, Accuracy, Modification, and component-level cost
  together.
- Balance arm order with a seeded Latin square and time blocks; report infrastructure
  incidence and service/checkpoint revision.
- Treat annotation as masked where possible, not perfectly blind; report leakage, sample
  size, agreement, adjudication, author role, and AI assistance.
- Build the anonymous supplement anew, without Git history, identity, endpoints, workbook
  author metadata, visitor tracking, or restricted raw workbooks.

## Structural-claim audit

No row below is an empirical result. `Pending` means the manuscript may state the scoped
invariant only after the named implementation, adversarial tests, and independent replay agree.

| Claim | Red-team interpretation | Required falsification before release | State |
| --- | --- | --- | --- |
| I1 | Semantic footprints must come from harness-observed OOXML effects, including opaque-code paths, and fail closed on absent/incomplete/opaque effects. | Independent standard-library ZIP/XML oracle; shared/array formulas; manual-calculation and custom-properties plumbing; lossy/opaque parts; producer-churn positive/negative controls. | Pending |
| I2 | Reads and renders cannot publish revisions or carry same-stage coverage across a byte transition. | Hash-cycle, recalculation, read-only event, and replay attacks. | Pending |
| I3 | A target declaration is pre-edit, source-revision-bound, observation-covered, one-use, and contains the full staged footprint. | Wrong-source, replayed declaration, partial coverage, and transactional rollback attacks for native and opaque edits. | Pending |
| I4 | Formula evidence identifies both recalculation input/output and the final readback selected by the contract. | Cached-value, shared/array-formula, dynamic-selector, and finalization-replay attacks. | Pending |
| I5 | Visual evidence transfers only when sheet/page identity and rendered hashes remain equivalent. | Wrong-page, layout churn, backend differential, and pixel-hash corruption attacks. | Pending |
| I6 | For an accepted candidate, the scorer reads a byte-identical replica of the certified final artifact without trusted-score mutation; noncompletion has only diagnostic-copy lineage. | Final/replica substitution, accepted/noncompletion branch checks, plus before/after scorer digest tests. | Pending |
| I7 | All-row finalization records and accepted-deliverable certificates check canonical field/content consistency under a trusted host/storage; neither stops coordinated rewriting or supplies signatures. | Terminology scan, wholesale-ledger-rewrite threat-model review, and explicit external-anchor/signature/trusted-clock boundary. | Pending |
| I8 | Every submit attempt is frozen before gating; accepted output is terminal-bound, while audited noncompletion has a failure reason, complete attempt ledger, and observer-only finalization integrity record but no accepted candidate or deliverable. | Omitted/reordered attempt, terminal mismatch, tampered reason/ledger, and fresh noncompletion replay attacks. | Pending |

## Required next review

After runtime integration and before opening any external instance, a second reviewer must
attempt to falsify: transactional staging, exact revision replay, target-declaration
containment, recalc output binding, visual page equivalence, immutable-attempt and terminal
binding, accepted/noncompletion branching, final/replica equality, and the one-factor arm
matrix. That review must also verify that finalization checks observed transitions without
rerunning the model or claiming to replay all model-produced evidence. A third review occurs
only after audited aggregates populate the paper.
