# Domain-Spreadsheet harness ablation v1

Status: integration protocol; execution is blocked until the released dataset
artifacts and an Excel-compatible official reward service are pinned locally.

## Source boundary

- Paper: Spreadsheet-RL, arXiv `2605.22642`
- Official repository commit inspected:
  `389be8c29fed059e3b2072741c8231e86d1dee8b`
- Dataset: `Spreadsheet-RL/Spreadsheet-RL`
- Paper evaluation count: 1,660 rollouts
- Current released `test_domain_hermes.parquet`: 1,662 rows

The two-row release/paper difference must be resolved from pinned task IDs and
artifact checksums before comparison.  Results cannot silently switch between
the paper's 1,660-task log set and the current 1,662-row release.

## Intended arms

1. `spreadsheet-harness-basic`
2. `spreadsheet-harness-financial`
3. the frozen co-evolved Financial Modeling composition

All use the same model and execution limits.  Report overall Pass@1 and the
paper's domain slices: finance by difficulty, supply chain, human resources,
sales, and real estate.  The financial plugin's primary transfer endpoint is
the finance slice; non-finance slices are mandatory regression contexts.

## Evaluator compatibility

Domain-Spreadsheet's source of truth is Microsoft Excel.  The official released
reward service recalculates a submitted workbook in Excel and compares target
ranges against `target.xlsx`, using tolerant numeric equality and exact text;
formula cells may be checked through canonicalized formulas and/or evaluated
values.  LibreOffice-only scores must be labeled a separate proxy and cannot be
compared directly with the paper's reported 8.4%/17.2% Pass@1.

Each released task directory is expected to contain `instruction.json`,
`input.xlsx`, `output.xlsx`, and `target.xlsx`.  The adapter must bind the
parquet row, task directory, instruction and workbook SHA-256 values in one
manifest and stage each rollout in an isolated workspace.

## Leakage policy

Domain-Spreadsheet evaluation workbooks and targets are held out from candidate
generation, plugin editing, routing thresholds, promotion, rollback and merge
decisions.  User-supplied financial workbooks and the synthetic workbook corpus
may be used for evolution only after deduplication against evaluation workbook
hashes and near-duplicate structural fingerprints.

## Required launch gates

1. Pin the Hugging Face dataset commit and all required file hashes.
2. Reconcile the 1,660 versus 1,662 task set.
3. Self-check the official reward service with untouched input and oracle target.
4. Freeze a small stratified pilot without reading target workbook content.
5. Run all arms paired and record provider/infrastructure failures as
   `not_scored`.
6. Freshly audit workbook hashes, evaluator responses and paired summaries.

