# ICLR 2027 experiment blueprint

Status: design document. A result may enter the paper only after the corresponding
protocol is frozen, launched once, freshly audited, and linked from the claims ledger.

Working paper title: **Do Not Trust "Done": Semantic Event Lifting for Auditable
Spreadsheet Agents**.

## Research question

Spreadsheet agents often perform a plausible edit and then declare success after an
inspection that is stale, covers the wrong cells, or does not survive recalculation.
The proposed system, provisionally named **SheetLedger**, asks:

> Can harness-computed workbook effects and revision-bound evidence reduce
> accepted-terminal false completion while preserving end-to-end spreadsheet
> accuracy and accepted-terminal coverage?

The central intervention is not another generic temporal-policy language, artifact-identity
scheme, or online gate. Its four defensible mechanisms are: lifting opaque XLSX/Python state
changes into trusted semantic events; prospectively containing staged edits within inspected
declarations; assigning explicit revision semantics to recalculation and rendered-page
evidence; and binding an accepted candidate to the exact postprocessed scoring replica. An
audited noncompletion receives only an observer-generated finalization/lineage integrity
record, not an accepted deliverable.

## Claim discipline

The paper must separate four kinds of evidence.

1. **Historical deployment evidence.** Existing v23--v27 SpreadsheetBench runs test
   predecessor harness configurations. They cannot establish the effect of SheetLedger,
   which was designed later. The active v27 reserve run also incurred the interim-audit
   deviation recorded in `research/protocol_deviations.md`; it is permanently exploratory
   and cannot support confirmatory inference or method selection.
2. **Component-development evidence.** Development and validation tasks may select
   prompts, tools, the researcher-authored fixed contract, and optional procedure artifacts.
   They are never
   reported as a fresh test estimate.
3. **Instance-artifact-unseen external evidence.** SpreadsheetBench 2 Debugging-100 is the
   preferred one-shot main test if its identifiers, task contents, and workbook artifacts
   remain sealed during development. The paper-level taxonomy and published example were
   already read, and model pretraining exposure is unknown; neither is described as fresh.
4. **Mechanism evidence.** A synthetic oracle suite isolates stale evidence, wrong
   scope, opaque code edits, recalculation drift, visual edits, rollback, and collateral
   damage. It supports mechanism claims, not real-world accuracy claims.

No experiment may be described as reproducing or beating a paper unless model checkpoint,
split, backend, tool budget, and protocol match. A service alias without a checkpoint hash
is deployment evidence, not a reproducible model result.

## Method variants

Every runtime arm uses the same model, decoding, call/token/time ceilings, workbook backend,
and initial workbook. A seeded Latin-square schedule balances arm order across time blocks.
Tool descriptions, prompts, and service/checkpoint revisions are hashed or recorded.

| ID | Arm | Purpose |
| --- | --- | --- |
| B0 | Code-only | Original-benchmark-style code interpreter with bounded preview. |
| B1 | Native tools | Spreadsheet-RL-inspired native-tool adapter (no RL-trained checkpoint), no skill or contract. |
| B2 | Multi-format | SpreadsheetAgent-inspired multi-format adapter: B1 plus structure profile and text/image/range views. |
| T2S | Procedure-only baseline | B2 plus a frozen Trace2Skill-inspired procedure; no evidence contract. |
| B5 | Fixed-contract shadow | B2 plus the preregistered researcher-authored fixed contract and visible diagnostics, but no contract-based submission block. |
| B6 | Fixed-contract enforce | Identical to B5 except that an unsatisfied submission is blocked. |
| FULL | SheetLedger | B6 plus exact accepted-terminal, postprocessing, accepted-deliverable-certificate, and scoring-replica lineage enforcement. |
| G0 | No target grounding | FULL with prospective declarations and containment disabled. |
| G1 | Declaration advisory | FULL records declaration/footprint mismatch but does not block staging. |
| G2 | Enforced target grounding | FULL's default: reject staged mutations whose actual footprint escapes an inspected declaration. |

B0--B2, T2S, and the direct-prior adapters are whole-system references. They do not enter
the core causal contrasts. The causal effect of online enforcement is B5 versus B6: model,
fixed contract, tool surface, visible diagnostics, decoding, target-grounding setting,
immutable-attempt instrumentation, and arm-level call/token/time ceilings are held fixed, and
only the submission gate changes. The effect of finalization lineage is B6 versus FULL. G2 is
the default FULL configuration, so G2 and FULL denote the same row unless an explicit
grounding ablation is named; B5 and B6 also retain G2 so the enforcement and finalization
contrasts hold grounding fixed. B6 and FULL execute identical deterministic postprocessing,
recalculation, and scorer-copy operations; FULL alone enforces and records the accepted-
terminal-to-final-to-copy lineage. G0 omits declarations, G1 adds elicitation and diagnostics
without containment blocking, and G2 adds that enforcement. Therefore `G1 - G0` estimates
declaration elicitation and `G2 - G1` estimates staged-containment enforcement; `G2 - G0` is
only the combined contrast and is never attributed to one gate. The fixed contract is authored by researchers and
frozen from development data before any external instance is opened; it is not distilled
jointly with a prose procedure.

Online shadow diagnostics are visible to the model and can therefore change its trajectory.
They are not described as a no-intervention counterfactual. Separately, frozen B2
trajectories are replayed through the same fixed monitor offline to measure `would_block` on
an identical trace. Offline replay estimates detection on observed trajectories but cannot
estimate recovery after a block.

Immutable completion-attempt capture is common measurement instrumentation, not a FULL-only
treatment. Every detected submit is frozen before the gate, including rejected, invalid, and
unchanged attempts; counterfactual attempt scoring occurs only after model termination and is
never returned to the model. B6 and FULL share deterministic finalization operations and
observer records. FULL alone enforces exact accepted-candidate lineage and authorizes the
accepted-deliverable certificate. It checks harness-observed candidate-to-final transitions
but does not rerun the model or replay every model-produced witness on the final bytes.

Because the first attempt is captured before the B5/B6 gate can act, first- and any-attempt
false-completion rates are behavior diagnostics, not estimands for the causal effect of
enforcement. The B5/B6 enforcement estimand is accepted-terminal false completion over all
selected tasks. Accepted-terminal coverage, audited noncompletion, Accuracy, and the risk
conditional on an accepted terminal are reported beside it; a conditional accepted-only rate
is never used alone because a reject-all gate can minimize it trivially.

## Direct prior-work adapters

Whole-system comparisons are reported separately from controlled ablations. Each adapter
receives the same observable workbook events and resource budget where its formalism can
express them.

| Prior direction | Adapter |
| --- | --- |
| Spreadsheet-RL | Spreadsheet-RL-inspired native-tool adapter with inspect/modify/verify prompting; no RL-trained checkpoint or RL-result claim. |
| SpreadsheetAgent | SpreadsheetAgent-inspired multi-format adapter with a structural sketch plus localized code, image, and range/LaTeX representations. |
| Trace2Skill | Trace2Skill-inspired procedure-only many-to-one skill distilled from the same quarantined development trajectory pool and frozen before external test. |
| AgentSpec | Per-action trigger/check/enforce rules over typed spreadsheet tool calls. |
| VIGIL | Finite-trace precedence/response rules with artifact identifiers where supported. |
| AgentLTL | Typed finite-trace procedural constraints and block-and-warn submission gate. |

The paper must not imply that a clean-room adapter is an official rerun. Official code is
used only when its license, runtime, model interface, and benchmark protocol permit a fair
reproduction. Otherwise the table labels the row `inspired adapter` and states the gap.
For a representative generic policy engine, cross raw versus SheetLedger-enriched events
with generic versus restricted SheetLedger policy. Publish all policies and event visibility;
an intentionally weak adapter is not evidence that the prior formalism is inexpressive.

The same-policy generic+enriched versus generic+raw contrast is core model-free mechanism
evidence. The containing event/policy factorial is frozen as four explicit cells: generic+raw,
generic+enriched, restricted+raw, and restricted+enriched. All four consume the same sealed
valid-control and tamper traces. `Enriched - raw` within the generic engine identifies the
value of spreadsheet event construction; `restricted - generic` is reported separately and
is not interpreted as a universal language comparison.

## Causal ablations

All ablations start from FULL and change exactly one mechanism.

| ID | Removed or weakened mechanism | Falsified hypothesis |
| --- | --- | --- |
| A1 | Enforcement (same online monitor in shadow mode) | Visible diagnostics without a submission gate are sufficient. |
| A2 | Artifact revision binding | Any earlier verification remains valid. |
| A3 | Workbook-scope binding | Verification of any range is sufficient. |
| A4 | Semantic effect footprint | Tool names/arguments adequately describe edits. |
| A5 | Opaque-code diffing | Code-interpreter mutations need no independent observation. |
| A6 | Stale-evidence invalidation | Later writes cannot invalidate earlier checks. |
| A7 | Recalculation lineage | Pre-recalculation values are enough for formula edits. |
| A8 | Rollback-linked recovery | Any later successful edit clears an earlier failed mutation. |
| A9 | Deliverable certificate | Pre-submit evidence transfers through postprocessing. |
| A10 | Pre-edit target grounding | Post-edit verification alone prevents wrong-target edits. |
| A11 | Budget router | Gains are not due to reserving additional useful evidence calls. |

A1--A10 are evaluated on the sealed synthetic oracle suite and on real tasks where
applicable. A11 receives the same total model-call ceiling and is reported with full
accuracy--budget and reliability--budget curves. Procedure/contract complementarity is not a
main ablation because the SheetLedger intervention uses a fixed contract and no paired
procedure.

## Optional secondary: joint artifact distillation

Joint procedure/contract distillation is not part of the main SheetLedger intervention and
must not delay or alter the fixed-contract matrix. If compute remains after the main arms,
one separately labeled secondary study may use disjoint `D_evolve`, `D_contract_unit`, and
`D_select` layers to propose a prose procedure and a restricted-DSL contract. Candidate and
query counts, deterministic tie-breaking, and a Pareto promotion rule are frozen in advance;
the sealed external test and final oracle remain untouched. The promoted pair is compared
with the already-frozen fixed contract without editing either artifact.

This optional study can test whether trajectory-local lessons are useful, but it cannot
support the main enforcement, grounding, lineage, or cross-model claims. Model-generated
rules never become executable Python: a candidate remains inside the closed DSL and must
pass independent valid/invalid trace checks. If the study is not adequately powered or its
selection layers overlap, C6/C7 remain blocked and no quantitative claim enters the paper.

## Datasets

### SpreadsheetBench-Verified

- Trace2Skill's nominal 400-example verified collection uses indices 0:200 for evolution and
  200:400 for held-out evaluation. In the pinned local copy, documented exclusions leave 198
  usable held-out examples; never describe all 400 as held out.
- Historical v27 exhausts the remaining 79 locally fresh examples and must never be rerun.
- Use v27 only as a one-shot comparison of the predecessor `bare` and `ours` arms.
- Any SheetLedger development study uses already quarantined development examples and is
  labeled accordingly.
- The original benchmark's sibling-replay Soft/Hard (`solution_once_apply_n`) protocol and
  this project's `agent_per_workbook` protocol use different evaluation units. Never pool
  them, compare their scores as one leaderboard setting, or silently translate between them.

### SpreadsheetBench 2 Debugging-100

- Seal all 100 Debugging task IDs as the primary external test before inspecting task
  identifiers, contents, or workbooks. Disclose prior exposure to paper-level aggregates and
  check whether the published example belongs to the split.
- Preserve the benchmark's native `bash`, `view_xlsx`, `submit`, 50-turn scaffold as a
  baseline, then run resource-matched adapters.
- Report benchmark Modification and Accuracy plus the reliability metrics below.
- Do not use these tasks to generate or select skills/contracts. If any task is exposed,
  quarantine it and replace the primary dataset rather than silently changing the split.
- Run Visualization-24 as a mandatory secondary set for visual claims; preregister use of the
  remaining workflows before opening them.

### Synthetic evidence-oracle suite

Build templated workbooks with exact semantic and presentation oracles. Each template has
multiple randomized instances and paired positive/negative traces.

| Family | Required coverage |
| --- | --- |
| Value | scalar/range writes, blanks, dates, duplicates, boundary expansion. |
| Formula | relative references, fill translation, shared/array ranges, cached errors, recalculation drift. |
| Style | number format, font/fill/border/alignment, conditional formatting. |
| Structure | row/column deletion, merges, tables, sheet creation/deletion, defined names. |
| Visual | chart data ranges, chart type/title/style, rendered page and manifest identity. |
| Opaque code | direct library edits, valid ZIP-byte rewrites, malformed cell plumbing, calculation-property changes, empty custom-properties plumbing, opaque-part changes, unknown effects. |
| Recovery | failed mutation, scoped rollback, unrelated later edit, repeated repair. |
| Lineage | stale read, wrong sheet/range/page, immutable-attempt/terminal mismatch, post-submit recalc, tampered final/certificate/scoring copy. |
| Noncompletion | rejected/invalid/no-op attempts, budget failure, no accepted candidate, tampered failure reason or attempt ledger. |

Development and sealed-test template generators, oracle checks, seeds, and expected event
chains are versioned separately. The current test oracle uses only the Python standard
library to parse ZIP/XML and shares no parsing path with the runtime comparator. It covers
values, styles, merges, tables, names, charts, shared/array formulas, lossy extensions,
opaque XML/binary parts, `calcPr`/manual-calculation changes, atomic custom-properties
part/relationship/content-type plumbing, exact LibreOffice `ExcelA1` producer churn paired
with near-neighbor negative variants, and byte-only no-ops.
Final ground truth additionally requires held-out handcrafted corruptions, property/fuzz
checks, producer-diverse workbooks, and LibreOffice/Excel differential cases. Valid controls
ensure that a reject-all monitor scores poorly.

The sealed suite also includes a core model-free paired finalization challenge. Each exact
frozen candidate
is passed through B6 and FULL under a preregistered benign/no-op transition, a stale-lineage
substitution, and a supported or unknown semantic postprocessing mutation. Candidate bytes,
proposed final bytes, and transition assignment are otherwise identical; for benign cases
accepted by both arms, scoring inputs must also be identical. This model-free challenge tests
the lineage mechanism under known interventions; naturally occurring postprocessing incidence
and failures remain a separate real-task estimate.

## Models and seeds

Minimum paper matrix:

- a pinned open Qwen3.5-35B-A3B checkpoint, matching one Trace2Skill model scale;
- a pinned checkpoint from a second model family (a same-family scale change does not satisfy
  this cross-family requirement);
- seeds 41, 42, and 43 for stochastic rollout studies;
- the service alias run reported separately with an explicit unpinned-checkpoint warning.

Before launch, record a no-outcome availability probe for both Trace2Skill author/user
checkpoints, Qwen3.5-35B-A3B and Qwen3.5-122B-A10B. If the 122B checkpoint is reproducibly
deployable within the frozen budget, run a separately labeled B2/T2S/B5/B6/FULL
protocol-alignment extension; otherwise report its preregistered unavailability. This extension
does not replace the second-family causal matrix and is never called an official Trace2Skill
rerun unless its code, skill, split, backend, and serving protocol also match.

If compute prevents the full factorial, prioritize: B5 versus B6, B6 versus FULL, G1 versus
G0, G2 versus G1, and same-policy raw versus enriched events on Debugging-100 or the sealed
oracle as specified; run all cheap mechanism ablations and B2/B5/B6/FULL/G0/G1/G2 on the
second model family. T2S is a procedure-only whole-system row, not a substitute for any of
these causal contrasts.
Do not replace missing repetitions with repeated calls after looking at outcomes.

## Outcomes

### Primary

`Accuracy`: fraction of all selected tasks that pass the official end-to-end evaluator.
Errors, timeouts, rejected submissions, and budget exhaustion remain failures.

### Reliability

- `false_first_completion`: the first immutable completion-attempt snapshot fails the
  official evaluator;
- `any_false_completion`: at least one immutable completion-attempt snapshot fails within a
  task; attempts are never treated as independent samples;
- `accepted_false_completion`: the exact gate-accepted terminal attempt fails its
  post-termination official evaluation; the primary denominator is all selected tasks, and
  risk conditional on an accepted terminal is diagnostic;
- `attempt_coverage`: at least one immutable completion attempt is captured for the task;
- `accepted_terminal_coverage`: execution ends with a gate-accepted terminal attempt;
- `scoring_delivery_coverage`: an accepted candidate's final output reaches the official
  scorer as its scoring replica; an audited noncompletion's diagnostic scorer copy does not
  count as accepted delivery;
- `contract_rejection`: a completion is blocked because evidence obligations remain;
- `successful_recovery`: a blocked completion is followed by valid evidence/repair and a pass;
- `grounding_rejection` / `grounding_recovery`: a staged mutation is blocked for escaping its
  inspected declaration, and is then either repaired within scope or redeclared from a fresh
  observation;
- `audited_noncompletion`: execution terminates without an accepted candidate and its
  observer-only finalization integrity record, failure reason, and complete immutable-attempt
  ledger pass fresh audit;
- `wrong_scope_acceptance`: an adversarial wrong-range witness is accepted;
- `stale_acceptance`: a witness predating the latest relevant transition is accepted;
- `collateral_damage`: non-target cells or workbook structures differ from the input/golden
  contract when the task does not authorize the change;
- `target_precision_recall`: actual changed cells versus the gold target mask, used only by
  evaluation and never exposed at runtime;
- `finalization_record_validity`: a fresh auditor validates every row's candidate outcome,
  event chain, revision, observed finalization transitions, final scan, final artifact hash,
  and scorer-copy lineage. Only an accepted-candidate row additionally has
  `accepted_deliverable_certificate_validity` and calls the copy a scoring replica.
- `tamper_acceptance` / `valid_control_rejection`: invalid corrupted records accepted and
  semantically valid controls rejected in the sealed tamper corpus.

False attempts are reported at task level with both denominators: all tasks and tasks with at
least one attempted submission. Accepted-terminal false completion is reported over all tasks
and conditionally on an accepted terminal. Attempt, accepted-terminal, and scoring-delivery
coverage, pass rate, audited noncompletion, rejection, and recovery are reported beside them.

### Cost and behavior

Report model input/output/total tokens per task, logical calls, HTTP attempts, tool calls by
phase, wall time per task, and separate diff/recalculation/render/finalization-record CPU time.
Incremental tokens per additional pass is reported only when its denominator is positive.
Report attempt snapshots/task, posthoc-evaluation time, candidate-to-final transitions, and
peak temporary storage. Accuracy-versus-budget and false-completion-versus-budget curves use
a frozen event classifier with an `unclassified` category. On the frozen backend subset,
report formula-value, page-render, and official-score disagreement with paired intervals.

## Statistical analysis

- Tasks, not task-seed rows, are the independent clusters. For repeated seeds, first average
  within task and then across tasks while preserving each complete arm-by-seed vector.
- Model families are fixed replication strata, not independent draws from a population of
  models. The four real-task confirmatory tests use preregistered Qwen3.5-35B-A3B; the
  second family repeats those estimands with separate intervals and decisions, without
  cross-family pooling. The sealed-oracle event contrast is model-free. Any broader
  model-generalization statement remains descriptive.
- Wilson intervals and exact two-sided McNemar tests are restricted to clearly labeled
  single-seed binary task pairs. Modification and multi-seed outcomes use paired task-cluster
  bootstrap/permutation intervals, with percentile and BCa sensitivity analyses.
- Use Holm correction for the preregistered family of primary pairwise claims. Ablations are
  secondary and report intervals rather than significance stars alone.
- Failure-taxonomy labels are masked to arm identity where tool shape permits. Report sample
  size, Cohen's kappa or Krippendorff's alpha, annotator role, AI assistance, and adjudication;
  automated labels remain separate.
- Report effect sizes even when a small sample is underpowered. A non-significant favorable
  point estimate is not described as an improvement.
- Before launch, freeze practically justified non-inferiority margins for Accuracy and
  accepted-terminal coverage. Use one-sided task-cluster confidence bounds as an intersection
  guardrail for C1, with the exact bootstrap/permutation procedure and random seeds frozen.
  The margins come from application requirements and development data, never Debugging-100
  outcomes. A blinded design-stage simulation spans a conservative grid of paired discordance
  and intra-task seed correlation; if 100 tasks cannot resolve the chosen margins, the study
  may report utility descriptively but cannot claim preservation.

Primary claim family (items 1--4 use Qwen3.5-35B-A3B and repeat separately on the second
family; item 5 is model-free):

1. B6 versus B5: task-level accepted-terminal false-completion rate over all selected tasks;
   Accuracy and accepted-terminal coverage are preregistered non-inferiority guardrails, not
   extra hidden hypothesis tests. First/any-attempt false completion is diagnostic because
   those attempts are recorded before enforcement can act.
2. FULL versus B6: postprocess-stale acceptance rate; final Accuracy is a guardrail.
3. G1 versus G0: off-target-change rate from declaration elicitation against the gold-only
   mask; Accuracy is a guardrail.
4. G2 versus G1: off-target-change rate from staged-containment enforcement against the
   gold-only mask; Accuracy is a guardrail. G2 versus G0 is a descriptive total contrast.
5. Enriched versus raw events under the same representative generic policy engine on sealed
   oracle invalid-attack acceptance.

## Promotion and success criteria

These are engineering gates, not guaranteed paper conclusions.

- No fresh-test launch until all selected unit, integration, mutation, audit, and secret scans
  pass from a clean published commit.
- Mechanism gate: preregistered rejection of invalid oracle traces and acceptance of valid
  controls; any target threshold is chosen from development data and not called a test result.
  Field-corruption checks establish internal consistency under trusted storage, not security
  against wholesale ledger replacement.
- Real-task gate: accuracy is analyzed jointly with accepted-terminal false completion,
  accepted-terminal and scoring-delivery coverage, and audited noncompletion. Non-inferiority
  margins are justified
  by an a priori precision/power calculation; a 2-point margin is not used if 100 independent
  tasks cannot resolve it.
- Artifact gate: every reported row has a reproducible manifest and observer-only
  finalization integrity record. Every accepted final workbook hash equals its accepted-
  deliverable certificate and byte-identical scoring replica; every audited noncompletion has
  no accepted candidate or deliverable, records only diagnostic-copy lineage, and passes a
  fresh ledger audit.
- Portability gate: the same preregistered researcher-authored fixed contract is evaluated unchanged on both
  model families. Optional model-authored procedure or contract artifacts are not a submission
  gate and, if studied, are reported separately.

If a gate fails, report the failure and revise only on development/validation data. Never
rerun an observed fresh cohort under a new label.

## Threats to validity

- Evidence compliance is not task correctness. Target grounding proves inspected declaration
  and footprint containment, not that the declaration matches natural-language intent.
- Harness-computed diffs and recalculation engines form the trusted computing base and may be
  incomplete for macros, external links, dynamic arrays, and vendor-specific objects.
- LibreOffice and Excel have different semantics. Run a preregistered Excel-COM subset and
  report backend disagreement rather than combining scores.
- A contract can induce conservative abstention. Report rejection and recovery, not only the
  accepted-submission error rate.
- SpreadsheetBench 2 has 100 debugging tasks; uncertainty remains substantial. Multi-seed
  estimates do not create more independent tasks.
- Closed or changing service aliases limit reproducibility and may confound comparisons.
- Finalization integrity assumes a trusted host, controller, storage, scorer, and auditor. A
  coordinating compromise can rewrite artifacts, records, and timestamps consistently; that
  threat requires an external append-only anchor or transparency log, authenticated
  signatures, and a trusted clock, none of which SheetLedger supplies.

## Required artifacts before submission

- anonymous source archive generated from an explicit allowlist (never a repository tarball),
  with no git history/remote, usernames, host/home paths,
  credentials/endpoints, identifying result trajectories, tracking analytics, or Office core
  properties that disclose workbook authors;
- immutable split/run manifests and environment lockfile;
- contract grammar, the researcher-authored fixed contract, freeze/authorship provenance, and, only if the
  optional study is run, separate distillation provenance;
- synthetic oracle generators and negative-control traces;
- de-identified task-by-arm-by-seed result rows, fresh-audit reports, and table/figure
  generation scripts;
- an exact paper build command using the unmodified ICLR 2027 style;
- a disclosure ledger for every AI-assisted research, coding, analysis, and writing activity.

## Official ICLR 2027 submission gates

Verified against the official Call for Papers, Author Guidelines, and AI Policy for Authors on
2026-08-14; re-check the live pages immediately before abstract registration and upload.

- Register a genuine abstract by 2026-09-18 AoE and the paper plus supplementary material by
  2026-09-25 AoE. No author may be added or removed after the abstract deadline; author order
  may change only until the full-paper deadline.
- Keep both the paper and supplementary material double-blind. Identity disclosure in either is
  a desk-reject condition; self-citations remain in third person.
- Keep submission main text at nine pages or fewer. References are unlimited and appendices
  follow the bibliography, but reviewers are not required to read them.
- Use the unmodified official ICLR 2027 style archive and verify the pinned style hashes. The
  release build must fail on an over-limit main-text label, style drift, unresolved references,
  undefined citations, or overfull boxes.
- Include the mandatory AI-use statement outside the page limit and mirror its disclosure in the
  submission form. The statement must cover AI-assisted research design, implementation,
  analysis, figures, and writing, and assign responsibility to the authors.
- Keep the recommended reproducibility statement and any relevant ethics statement before the
  references. Anonymous artifact links must not expose identity or track reviewer visits.
