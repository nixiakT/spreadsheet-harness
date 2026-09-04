# PlugEvolve v1

PlugEvolve evolves independently versioned harness plugins while keeping the
benchmark, scorer, sandbox, session log, artifact checks, and plugin ABI fixed.
It is a contract-constrained extension of the existing spreadsheet harness, not
a migration to another agent runtime.

The composition model is clean-room and informed by DeepSeek Harness's public
"everything is a plugin" architecture at commit
`141eb6fef83422698aef7a981029e843e8161534`: consumers depend on named services
rather than concrete implementations, required services determine activation,
and a composition is an ordered plugin tree. PlugEvolve deliberately differs at
the research boundary. The benchmark, hidden evaluator inputs, scorer, sandbox,
budget and promotion gate remain a privileged fixed kernel so an optimizer
cannot improve its score by changing the experiment.

## Trust boundary

The fixed kernel owns:

- dataset selection, hidden evaluator inputs, scoring, and split provenance;
- provider requests, budgets, sandboxing, and artifact integrity;
- trajectory recording, candidate validation, promotion, and rollback;
- allowed capabilities, hooks, permissions, and configuration schemas.

The optimizer cannot edit these components. It may propose one change to one
registered plugin, or the code-owned composition search may change one
capability slot.

## Plugin shape

Every contract has a stable name/version, implementation identity, provided and
required services, hooks, permissions, bounded config schema, spreadsheet
capability attribution, and a plugin-specific evolution strategy. This mirrors
three capability-seam roles:

- the contract defines the service and lifecycle boundary;
- a selected plugin instance provides that service with hash-bound config; and
- policies/workflows consume the named service without importing a provider.

Resolution rejects missing services, duplicate providers, conflicts, unknown
hooks, invalid config and dependency cycles before the first model request.
Activation is recorded as `harness.plugin.activated`; a run manifest binds the
complete composition and every plugin contract/config hash. Unlike a dynamic
interactive plugin, an ephemeral evolution candidate is never restored or
promoted automatically.

## Runtime plugin plane

The first registry maps the current harness into these plugin families:

| Family | Current providers | Evolvable surface |
| --- | --- | --- |
| Observe | full and compact deterministic profiles | implementation, bounded config |
| Act | code interpreter and native spreadsheet tools | implementation, description |
| Control | bare, profile, native, and ours solve policies | prompt, middleware implementation |
| Verify | formula-runtime validation | bounded config, implementation |
| Knowledge | independently selectable spreadsheet capability skills | prompt/skill content |
| Repair | date-text repair | implementation |
| Workflow | legacy paper workflow | frozen in v1 |

Consumers require capabilities such as `context.workbook-profile`; they do not
name or import another provider. A composition may contain at most one provider
for each capability. The registry rejects missing requirements, duplicate
providers, unknown hooks, invalid configuration, and capability cycles before a
model request is made.

Run-time and manifest identities are SHA-256-bound and recorded in the
trajectory as `harness.composition.resolved`.

## Spreadsheet capability bank

The research seed exposes a spreadsheet-specific bank rather than treating one
large prompt as the unit of evolution:

| Capability | Plugin | Main evidence | Update operators |
| --- | --- | --- | --- |
| Structure | `skill-spreadsheet-structure` | headers, boundaries, cross-sheet relations, range grounding | semantic-edge and structure rules |
| Formula | `skill-spreadsheet-formula` | failed formula, expected value, dependency graph, reference AST | formula templates and reference repair |
| Manipulation | `skill-spreadsheet-manipulation` | workbook diff, boundaries, format metadata | mutation procedures and boundary/format rules |
| Analysis | `skill-spreadsheet-analysis` | grouping keys, source/output pairs, expected aggregation | analysis templates and aggregation rules |
| Visualization | `skill-spreadsheet-visualization` | rendered pages, chart metadata, visual diff | chart type/range and visual rules |
| Verification | `skill-spreadsheet-verification` plus `verifier-formula-runtime` | false positives/negatives and execution postconditions | postconditions and verifier implementation |
| Memory | `skill-spreadsheet-memory` | repeated successes, backend workarounds, transfer evidence | experience rules, merge and redundancy pruning |

The runtime and control plugins remain separate seams. They can be implicated
by composition evidence, but the router prefers the narrowest spreadsheet
capability provider over rewriting a generic tool runtime after every failure.

## Failure attribution and evolution routing

The deterministic route is:

```text
Spreadsheet execution trace + evaluator outcome
  -> infrastructure failure?          -> retry/repair infrastructure; no evolution
  -> selected capability not active?  -> composition failure; repair routing
  -> no selected specialist?          -> capability gap; enable a candidate plugin
  -> active specialist failed?        -> capability failure; evolve that plugin
  -> insufficient evidence            -> collect evidence; do not guess a target
```

`sheet-harness plugins attribute` hashes the trajectory, requires an explicit
evaluator outcome, infers only the fixed spreadsheet taxonomy, and returns the
target plugin's allowed surfaces, evidence contract, operators, and global
validation contexts. Agent completion or a saved workbook is not correctness.

```bash
sheet-harness plugins attribute runs/TASK/trajectory.jsonl \
  --composition plugevolve-seed \
  --task-type 'Cell-Level Manipulation: formula lookup'
```

## Two evolution levels

### Plugin-local evolution

`PluginMutation` targets exactly one immutable plugin manifest and one declared
surface: `config`, `implementation`, `prompt`, or `description`. It carries
trajectory evidence hashes and candidate artifact hashes, but has no field for
changing hooks, capabilities, permissions, or contracts. A config mutation is
validated against a code-owned scalar schema.

The intended plugin-local proposal routes are:

- skill content;
- profile implementation or bounded parameters;
- tool description or helper implementation;
- recovery policy prompt or middleware;
- verifier strategy or bounded parameters.

Each spreadsheet plugin fixes *how* it may evolve. Formula evolution consumes
reference/dependency and expected-output evidence; Structure evolution changes
semantic relation rules; Verification evolution adds or repairs postconditions.
The proposal model cannot substitute a generic prompt rewrite for those
plugin-specific evidence requirements.

### Composition evolution

`enumerate_single_plugin_candidates()` performs code-controlled `enable`,
`disable`, `replace`, and `configure` mutations. Every candidate must resolve to
an executable composition. Replacement is a change to one capability slot even
though one provider name leaves and another enters.

`ConstrainedCompositionRouter` optionally maps an allowlisted benchmark task
type to a prevalidated composition. It sees only the declared task type, uses a
fixed fallback for unmapped allowed types, rejects unknown types, and hashes the
complete route manifest. It never routes from task text or model output.

`select_composition()` accepts a candidate only when it:

- has scores for exactly the baseline contexts;
- has no failed contexts;
- exceeds the required mean margin; and
- stays within the allowed regression bound in every context.

Mean score is the first ranking criterion. Lower context variance, lower cost,
and the composition hash are deterministic tie-breakers. This rewards portable
marginal utility instead of a plugin that works only with one fixed partner.

## Built-in compositions

The current arm names resolve through immutable compositions. Their active
behavior is preserved: the optimized code-only `ours` does not load skills and
does not require formula-runtime validation. Historical v26-v29 manifests keep
their original six-tool `ours` contract and are audited under that version.

`plugevolve-seed` is the explicit research seed. It combines the seven independently
selectable spreadsheet capability skills and `verifier-formula-runtime` with the
`ours` policy. Because runtime formula verification depends on the scope-aware
`recalculate_and_read` service, dependency resolution selects the minimal
`runtime-code-plus-formula-validation` provider. It exposes code interpretation and
formula recalculation without widening the model-facing tool surface to every native
spreadsheet tool. The seed must be evaluated under a new protocol and output identity;
it must not be substituted into historical `ours` results.

Inspect the catalog and code-controlled candidates with:

```bash
sheet-harness plugins list
sheet-harness plugins resolve ours
sheet-harness plugins candidates plugevolve-seed
```

The literal registered seed identifier is `plugevolve-seed`.

The official SpreadsheetBench adapter can execute a compatible built-in
composition without changing the historical arm identifier. The override is
recorded and independently audited from the manifest:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id TASK_ID --arm ours \
  --composition ours=plugevolve-seed \
  --output benchmarks/results/plugevolve-seed-smoke \
  --model MODEL --reasoning-effort low
sheet-harness benchmark audit benchmarks/results/plugevolve-seed-smoke \
  --dataset benchmarks/data/spreadsheetbench_verified_400
```

Composition overrides are forbidden under an older frozen run spec. A candidate
needs a new output identity and preregistered protocol; it cannot overwrite or
resume a historical `ours` result.

SpreadsheetBench-v2 has its own paired entry point and scorer. It always runs
`bare` and `ours` for the same selected tasks and records the concrete
composition and hash for each arm:

```bash
sheet-harness benchmark v2-compare \
  --dataset benchmarks/data/spreadsheetbench-v2 \
  --category Template --task-id Template/06_05 \
  --arm bare --arm ours \
  --composition ours=plugevolve-seed \
  --output benchmarks/results/v2-paired-smoke \
  --api-key-file /path/to/provider.key \
  --base-url PROVIDER_BASE_URL --model MODEL
sheet-harness benchmark v2-audit benchmarks/results/v2-paired-smoke \
  --dataset benchmarks/data/spreadsheetbench-v2
```

The v2 official evaluator treats a non-Visualization task as passed only when
both modification and regression checks pass. The current Linux path does not
claim Visualization coverage because the official workflow requires Windows
Excel/WPS COM and a VLM. A single-task smoke is development evidence only; it is
not a full 321-task benchmark or a leaderboard result. Provider timeouts remain
`not_scored` and must not be converted into model failures or zero accuracy.

## Lifecycle

A new plugin mutation begins `ephemeral`. Its validation report must hash-match
the candidate and include all three context kinds:

1. `replay` on the attributed failure;
2. `transfer` on neighboring tasks/workbooks; and
3. `regression` on historical workbook-level workflows.

Promotion requires a mean improvement strictly above the configured margin, no
failed/severe context, and no per-context regression beyond tolerance. Failure
retires the ephemeral candidate rather than weakening the gate. The command is
decision-only; deployment remains a separate explicit operation:

```bash
sheet-harness plugins lifecycle mutation.json validation.json \
  --min-delta 0.01 --max-context-regression 0
```

Persistent-bank telemetry chooses among retain, refine, merge, rollback and
retire from usage, marginal utility, redundancy and severe regressions:

```bash
sheet-harness plugins maintain \
  --usage-count 12 --marginal-utility 0.04 --redundancy 0.15
```

This yields the intended story: local plugin evolution, global workbook
validation, then a bounded long-lived capability ecosystem rather than an
ever-growing monolithic harness prompt.

## Experimental sequence

1. Confirm that every historical arm composition passes the existing arm tests.
2. Freeze a PlugEvolve development split and validation contexts.
3. Generate plugin-local candidates from development trajectories only.
4. Evaluate each accepted plugin version independently.
5. Enumerate composition candidates from accepted versions and bounded configs.
6. Promote a composition only through cross-context non-regression validation.
7. Freeze the final composition before the unseen evaluation split is opened.

The unseen split must never be used for candidate generation, routing thresholds,
configuration selection, or rollback decisions.
