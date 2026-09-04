# Spreadsheet Harness research evidence contract

This file turns the paper narrative into falsifiable deliverables.  A claim is
`complete` only when its implementation, frozen protocol, raw results, fresh
audit, and paper table all exist.  Development canaries and incomplete provider
rows are never promoted to leaderboard or SOTA evidence.

## Claim and acceptance matrix

| ID | Intended claim | Required evidence | Current state |
| --- | --- | --- | --- |
| C1 | Prior spreadsheet methods expose useful capabilities but lack one common, evolvable harness abstraction. | Related-work capability/interface table based on cited public artifacts; no unsupported universality claim. | Partial: clean-room Spreadsheet-RL minimal/native and screenshot workflow adapters exist; literature table pending. |
| C2 | Spreadsheet Harness provides a plugin ABI that combines robust general spreadsheet behavior with domain flexibility. | Contract resolver, capability/service dependencies, composition hashes, activation traces, isolated domain-plugin ablation, and tests. | Implemented: fixed kernel and plugin plane exist. `spreadsheet-harness-basic` and `spreadsheet-harness-financial` now differ by exactly one domain plugin. Full benchmark validation pending. |
| C2-E | Local spreadsheet plugins can self-evolve while preserving global workbook behavior. | Frozen development/evaluation separation; failure attribution; one-plugin mutations; replay/transfer/regression validation; promotion/rollback record; held-out improvement. | Mechanism implemented in PlugEvolve; end-to-end held-out evolution experiment pending. |
| C3-V1 | The final frozen harness reaches SOTA on SpreadsheetBench v1. | Full comparable soft, hard, and verified metrics; dataset revision, model, inference budget, competitor source, raw outputs, and audit. | Not established. Existing verified pilots are development evidence only. |
| C3-V2 | The final frozen harness reaches SOTA on SpreadsheetBench v2. | Template, Financial Modeling, Debugging, Visualization, and overall under the official protocol; thinking mode, up to 50 interaction turns, all 321 tasks or the official eligible count, and a fresh audit. | Not established. Existing 8-turn/no-thinking runs are cost-ablation evidence only. The first 50-turn/thinking canary completed and the latest Debugging canary plus a stratified 30-task paired pilot are running/queued; Linux does not provide the official Windows Excel/WPS + VLM Visualization evaluator. |
| C4 | A Financial Modeling plugin improves v2 Financial Modeling over the basic harness. | Paired run using identical model/runtime/budget and compositions that differ only by `skill-spreadsheet-financial-model`; category score, task deltas, cost, failures, and non-regression. | Composition pair implemented; frozen paired experiment pending. |
| C5 | Co-evolving the basic harness and Financial Modeling plugin improves again. | Development-only domain corpus, attributed mutations at one seam per generation, composition-aware validation, frozen promoted pair, held-out v2 Financial Modeling gain over C4, and regression suite. | Lifecycle machinery exists; domain data and experiment pending. |
| C6 | Gains transfer to Spreadsheet-RL Domain-Spreadsheet. | Pinned dataset/task split and evaluator; basic/domain/evolved-domain ablation under a common model and budget. | Pending dataset/evaluator integration. |

## Frozen composition ablation

The business-plugin experiment uses two built-in compositions:

- `spreadsheet-harness-basic`: the general runtime, compact workbook profile,
  solve policy, generic spreadsheet capability plugins, formula runtime verifier,
  and date-text repair.
- `spreadsheet-harness-financial`: exactly the basic composition plus
  `skill-spreadsheet-financial-model`.

No tool, model, prompt policy, profile, verifier, repair, budget, dataset row, or
evaluator may differ between the two arms.  The run manifest must bind both
composition hashes.  This pair measures domain specialization, not RL training.

## Self-evolution experiment

The evolution unit is a versioned plugin or one composition slot, never the
benchmark kernel.  Each candidate follows:

```text
development trajectory
  -> infrastructure filtering
  -> failure attribution
  -> capability/composition routing
  -> one-plugin candidate
  -> replay + neighboring transfer + workbook regression
  -> promote or retire
  -> freeze
  -> held-out evaluation
```

Financial data supplied later may be used for candidate generation and transfer
validation, but held-out SpreadsheetBench-v2 Financial Modeling and
Domain-Spreadsheet evaluation rows must not be used to author, select, merge, or
rollback candidates.

## Reporting rules

1. Report `not_scored` separately from zero and from scored model failures.
2. Report paired common-task deltas in addition to arm-wise means when provider
   completion differs.
3. Report calls, tokens, elapsed time, and failure rates with task accuracy.
4. Label clean-room method proxies explicitly; do not report them as reproduced
   post-trained Spreadsheet-RL checkpoints.
5. Use “SOTA” only after the full comparable protocol and audit pass.  Until
   then use “pilot”, “development”, or “partial-category result”.
6. Do not combine results with different dataset/evaluator/model/generation,
   resource, or composition hashes into one paired study.
7. SpreadsheetBench-v2 SOTA comparisons must match the paper's thinking-mode,
   50-interaction-turn setting.  Lower-turn or thinking-disabled runs are
   explicitly budget ablations, regardless of task count.

## Development evidence ledger (2026-08-29)

- The first GLM-5.2 thinking/50-turn canary completed all six arms in
  `spreadsheetbench-v2-glm52-thinking-50turn-canary3-basic-v2-20260829`.
  `Template/02_01` was an exact basic-harness win (`1` versus bare `0`), while
  the pre-detector `Debugging/02_01` arms tied at exact `0` and the Financial
  arms were both non-exact. This is a protocol/service canary, not SOTA evidence.
- A frozen deterministic repair pass on `Debugging/02_01`, followed by the same
  LibreOffice recalculation and pinned official evaluator used by benchmark
  arms, scored regression `1.0`, modification `1.0`, exact `1.0`, with zero
  model calls. The four recorded cells are `DCF!R32`, `WACC!D10`,
  `Merger Model!E29`, and `Merger Model!E57`.
- Frozen transfer checks on other Double Counting workbooks did not establish
  additional exact wins: `05_01` reg/mod/exact = `0.9829/0/0`, `07_01` =
  `0.9972/0.9884/0`, `08_01` = `1/0.8933/0`, and `10_01` = `1/0.5556/0`.
  These results are diagnostic only and were not used to retune the frozen
  detector. A prior no-recalculation attempt was invalid because openpyxl had
  cleared formula caches and is excluded from evidence.
- The 8-turn/no-thinking pilot was stopped at 31/60 preserved arms because it
  is not comparable to the published SOTA protocol and was competing for the
  same provider capacity. It remains a partial cost-ablation artifact only.
- The first latest-code 30-task attempt was stopped before producing any score
  after an executor scope defect was found. The frozen replacement is
  `spreadsheetbench-v2-glm52-thinking-50turn-pilot30-basic-scoped-v2-20260829`;
  its public-basename family router and protocol are fixed for the whole run.
- The frozen replacement's first pair (`Debugging/01_04`, Inconsistent Color
  Coding) is non-exact for both arms. Basic scored reg/mod/exact
  `1.0/0.4969/0` in 6 turns versus bare `0.8301/0/0` in 12 turns. The basic arm
  substantially improves cell-level correctness but this is not a task-accuracy
  win. Failure attribution found Excel What-If Data Table and circular cached
  values that LibreOffice does not reproduce; Linux exact evaluation for this
  task is therefore platform-limited, not established.
