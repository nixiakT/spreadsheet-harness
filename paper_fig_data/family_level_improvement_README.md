# Family-level Improvement Distribution — data audit

Audit snapshot: **2026-09-17 23:44 Asia/Shanghai**. This audit searched the current repository and existing experiment artifacts only. It did not launch or resume an experiment and did not modify any experiment result.

> **Family-level held-out comparison currently unavailable.**

`family_level_improvement.csv` is therefore a header-only template. Blank data are not zeros. No development, replay, transfer-gate, regression-gate, validation, canary, pilot, or cross-run values were substituted for formal held-out observations.

## Bottom line

| Required item | Audit result |
|---|---|
| Table 2 formal held-out evaluation actually completed | **No** |
| Formal C0 and CA matched family-level results | **No** |
| Eligible matched families | **0 total** |
| FINWORKFLOW-269 eligible families | **0** |
| FINWORKFLOW-1.5K eligible families | **0** |
| Static-vs-Evolved scatter drawable | **No** |
| Delta-score beeswarm/strip plot drawable | **No** |
| Either suite separately drawable | **No** |

The repository names the requested suites `Fin-269` and `Fin-1.5K`; this README uses the paper-facing names **FINWORKFLOW-269** and **FINWORKFLOW-1.5K** and gives repository aliases where useful.

The missing output is one completed, frozen, formally designated held-out evaluation matrix in which **C0 = H0 + D0** and **CA = H\*_A + D\*_A** are evaluated on exactly the same untouched source-workbook families, with a complete report/raw-record file. Frozen CG and CD results may be added from that same evaluation matrix, but they are not required for the main scatter.

## Why the available artifacts are not CSV rows

### 1. Latest formal paper36 search: endpoints exist, held-out results do not

The best match to Section 4.3 is the 2026-09-17 paper36 search. Its H-only, D-only, and alternating endpoints are frozen. The alternating endpoint accepted an H update and then a D update, so it is the intended CA composition. Nevertheless, every endpoint records `heldout_opened: false`, and none of the four formal search roots contains a `runs/heldout` result artifact.

All four search copies have the same split-manifest SHA-256:

`b8b9a395efbdd8204f83c1f41de8d488a028d88fcf34b9c18fd41bc36a5b034e`

That manifest contains 22 distinct source-workbook families: 4 development, 4 transfer, 2 regression, and 12 labeled held-out. The held-out allocation is six families per suite. Its top-level `dataset_role` is nevertheless `calibration_only`, so these are sealed internal calibration holdouts rather than an independently designated benchmark test split. In any event, they are only a planned set, not 12 usable observations: C0 and CA were not evaluated on them in the formal paper36 run.

This is the aggregate-only/missing-family-result case requested in check A: the search has candidate-level validation aggregates and decisions, but no held-out family scores. Those validation values are deliberately excluded.

### 2. The 36-family held-out pilot has protocol files but no observations

`benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917/` contains only `pilot-split.json` and `protocol.json`. It contains no `report.json`, `raw-records.jsonl`, task run directory, or score output. Its 36 planned families (18 per suite) therefore provide no data.

It is also explicitly labeled an “accelerated preregistered pilot; not the full 269/1565 population,” so it must not silently become a formal Table 2 result even if later completed.

### 3. The 24-family amendment is an in-progress pilot, not a formal result

At the audit snapshot, `benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/` had an active experiment process and partial per-task outputs, but no `report.json`, `raw-records.jsonl`, `tables.md`, or `tables.tex`. Its manifest calls it an “accelerated preregistered 24-family pilot; not the full 269/1565 population,” and every task role is `heldout-pilot`.

Partial pilot cells were not read into the CSV, combined, or treated as failures. In addition to being a pilot, the matrix was incomplete and still changing at the audit snapshot.

### 4. The older two-task run is not the requested alternating comparison

`benchmarks/results/true-coevolution-deepseekpro-fast-20260911/final-report.json` has crossed held-out task results for only two families. It is marked `dataset_role: calibration_only`, used DeepSeek-V4-Pro rather than the paper36 Qwen backbone, and accepted only one **H** round. Its nominal `h1d1` therefore does not represent the requested CA endpoint after alternating H and D evolution. It cannot be joined to paper36 C0 or used as Table 2 evidence.

The multi-plugin and expanded-validation reports are exploratory/canary/validation experiments with different tasks, compositions, runs, or incomplete cells. They are likewise excluded.

## Formal configuration and comparability audit

The intended paper36 held-out protocol is internally consistent on the following settings:

| Field | Frozen/declared value |
|---|---|
| Solver/backbone | `qwen3.6-plus` |
| Candidate generator during search | `dashscope/glm-5.2` |
| Temperature / top-p / seed | `0.0` / `1.0` / `41` |
| Thinking / reasoning effort | enabled / `medium` |
| Budget | 50 model calls and 50 turns per task; total/output token caps unlimited (`null`) |
| Provider endpoint in evaluation runner | `http://10.130.138.46:8010/v1` |
| Evaluator | unmodified official SpreadsheetBench-2 `process_single_item` |
| Evaluator revision | `83d415ce87b1d6b8e8eafcc26957f5d13d37210f` |
| Evaluator SHA-256 | `04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0` |
| Search split seed/unit | `20260911`; `source_workbook` |
| Formal split | 4 development + 4 transfer + 2 regression + 12 labeled held-out; six held-out families per suite |
| Formal manifest dataset role | `calibration_only` |
| Frozen search run IDs | `paper36-search-h-only-qwen36plus-20260917`; `paper36-search-d-only-qwen36plus-20260917`; `paper36-search-alternating-qwen36plus-20260917` |
| Actual proposal attempts | H-only: 3; D-only: 3; alternating: 1 H proposal + 5 D proposals (not a uniform search budget) |
| Pilot bootstrap declaration | 10,000 family resamples, percentile 95% interval |

The formal C0, CG, CD, and CA skill roots are derived from the same shared baseline, and the held-out runner verifies coordinate-specific skill changes. Thus backbone, budget, evaluator, and evaluation task set are designed to match across arms. However, this establishes protocol comparability only; without a completed formal held-out matrix it does not establish observed matched results.

The three search strategies are independent searches sharing the same split, not arms generated within a single alternating search. CA is from the formal alternating run; CG and CD are from their respective H-only and D-only runs. The endpoint-lock/pilot runner freezes all four compositions before evaluation. The requested condition “same formal alternating run” is therefore satisfied only for C0 versus CA by construction of the intended comparison, not by an existing completed evaluation output.

## Family-balanced evaluation rules found in the repository

No paper `.tex` source or separate Section 4.3 method file was found. The executable rules below are the most specific current implementation, in `benchmarks/run_true_coevolution_20260911.py` and `benchmarks/run_paper36_pilot_20260917.py`; no new aggregation rule was invented for this export.

1. **Family identity and split containment.** A family is the dataset metadata field `source_workbook`. The split validator prevents the same `(suite, source_workbook)` from appearing in different formal roles. Candidate siblings from one source workbook are grouped before selection, so they cannot be split across development/transfer/regression/held-out roles.
2. **Task selection within a family.** The formal split and pilot choose exactly one task from each selected source-workbook family. In the pilot the manifest explicitly enforces `one_task_per_workbook_family: true`. Thus a family score would equal that selected task's official `accuracy`; `n_tasks` would be 1 for every reported family. The runner does not independently weight sibling cases.
3. **Family aggregation.** Suite accuracy is the arithmetic mean of family scores. Because there is one task per family, every family receives equal weight and a family with more available dataset tasks cannot receive more weight.
4. **Scored invalid output.** If the pinned official evaluator returns a valid scored summary, its `accuracy` is authoritative; an incorrect/invalid workbook can therefore receive 0 through the official evaluator.
5. **Missing or unscored output.** A missing/invalid scored summary fails the task attempt and is retried. The report is produced only after the full matrix completes; it does not silently omit the cell and does not impute zero.
6. **Provider/infrastructure failure.** A terminal unrecovered `model.failed` event causes the reporting step to refuse score inference and write an infrastructure-failure audit. A recovered provider failure followed by a valid response may be officially scored. Infrastructure failures are not converted to task failures or dropped from a denominator.

These rules mean that the requested `static_success_rate` and `evolved_success_rate` would equal the family-mean binary Accuracy in this one-task-per-family design. They remain blank because no eligible formal observations exist.

## Leakage, duplication, and held-out-opening audit

### Run-local state

For the latest paper36 search, `baseline-final.json` and all three `search-final.json` files say `heldout_opened: false`. No formal paper36 held-out run artifacts were found. The formal manifest also has no duplicate family and uses source-workbook-level role separation.

### Cross-run contamination risk

The stronger requirement that held-out families were **never seen anywhere in evolution/research history cannot be verified and is false for the formal 12-family set**. The paper36 formal manifest reproduces exactly the same 22 tasks/families and roles as the older `true-coevolution-deepseekpro-fast-20260911` split. All 12 paper36 families labeled held-out have older held-out summary artifacts under that run; two have a full four-arm result and the others at least a C0 summary. Therefore `heldout_opened: false` is only true inside the new paper36 result roots, not globally.

The older run does not show these families in its promotion roles, so this audit found no direct evidence that their scores entered that run's promotion gate. They were nevertheless opened and observed before the latest paper36 search. A clean paper claim that the formal 12 were never seen should not be made.

The separate 36-family pilot manifest explicitly excludes families found in several named historical trajectory roots and is disjoint from the latest paper36 formal non-held-out families. A broader top-level ledger scan still found that 9 of its 36 task/family IDs had appeared in the older incomplete `true-coevolution-deepseekpro-20260911` manifest: two as development, two as transfer, and five as held-out. Additional Fin-269 debugging tasks appear in older coevolution batch ledgers. The 36/24-family pilot therefore also lacks a repository-wide never-seen guarantee, independent of its pilot status.

No duplicate family occurs within the formal 22-family manifest, the 36-family pilot manifest, or the 24-family pilot manifest. FINWORKFLOW-269 and FINWORKFLOW-1.5K are separable in all manifests and could be plotted as separate panels once a valid matched evaluation exists; currently neither panel has eligible rows.

## Requested statistics and confidence intervals

With zero eligible matched families, all requested statistics are **not estimable**:

- total families;
- counts/proportions with CA > C0, CA = C0, and CA < C0;
- median, mean, minimum, and maximum family delta;
- paired overall-improvement confidence interval.

The existing held-out pilot protocol declares a paired family bootstrap: form the per-family delta `CA - C0`, sample the same number of families with replacement, take the mean delta, repeat 10,000 times, and report the 2.5th and 97.5th percentiles. The implementation uses deterministic seeds derived from `20260917` plus the suite/comparison label. This audit did **not** execute that bootstrap because there is no eligible complete matched vector.

## Anomaly checklist

| Check | Result |
|---|---|
| A. Aggregate only, no family scores | Yes for formal paper36 search: validation aggregates exist, held-out family scores do not. |
| B. Static/Evolved on identical held-out tasks | Intended and lock-checked, but not observed in a completed formal output. Partial pilots do not qualify. |
| C. Same backbone/budget/evaluator | Intended paper36 protocol says yes; no completed formal matrix exists to validate every cell. |
| D. Same formal alternating run | C0/CA endpoint construction yes; no completed held-out evaluation. CG/CD come from separate frozen searches. |
| E. `heldout_opened` true | No in formal paper36 artifacts; it remains false. Historical reuse means global secrecy is already broken. |
| F. Pilot/canary mistaken as formal | Avoided. No pilot/canary rows were exported. |
| G. Duplicate family/data leakage | No within-manifest duplicates; material cross-run prior exposure exists as described above. |
| H. Suites separately drawable | Structurally separable, but neither is drawable from eligible data. |

## Source paths

Primary formal search evidence:

- `benchmarks/results/paper36-shared-baseline-qwen36plus-20260917/baseline-final.json`
- `benchmarks/results/paper36-shared-baseline-qwen36plus-20260917/split-manifest.json`
- `benchmarks/results/paper36-search-h-only-qwen36plus-20260917/search-final.json`
- `benchmarks/results/paper36-search-d-only-qwen36plus-20260917/search-final.json`
- `benchmarks/results/paper36-search-alternating-qwen36plus-20260917/search-final.json`
- `benchmarks/results/paper36-search-alternating-qwen36plus-20260917/recurrent-gate-protocol.json`
- `benchmarks/results/paper36-search-*/decisions/*.json` (validation/promotion audit only; not held-out data)

Held-out pilot protocol/status evidence:

- `benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917/protocol.json`
- `benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917/pilot-split.json`
- `benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/protocol.json`
- `benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/pilot-split.json`
- `benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/endpoint-lock.json`
- `benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917/run-status.tsv` (in progress at audit snapshot)

Implementation/method evidence:

- `benchmarks/run_true_coevolution_20260911.py`
- `benchmarks/run_paper36_pilot_20260917.py`
- `benchmarks/run_paper24_pilot_20260917.py`
- `DOMAIN_COEVOLUTION.md`
- `tests/test_paper36_pilot.py`
- `tests/test_true_coevolution_controller.py`

Excluded historical evidence used to diagnose contamination or non-comparability:

- `benchmarks/results/true-coevolution-deepseekpro-fast-20260911/split-manifest.json`
- `benchmarks/results/true-coevolution-deepseekpro-fast-20260911/final-report.json`
- `benchmarks/results/true-coevolution-deepseekpro-fast-20260911/runs/heldout/`
- `benchmarks/results/true-coevolution-deepseekpro-20260911/split-manifest.json`
- `benchmarks/results/multi-plugin-coevolution-deepseekflash-20260913/report.json`
- `benchmarks/results/expanded-multi-plugin-validation-*/report.json`

When an eligible formal result becomes available, populate one row per `(suite, source_workbook)` only after verifying a complete matched C0/CA pair from the same frozen evaluation matrix. Convert official scores to percentages, calculate deltas in percentage points, and retain the direct raw-record/report path in `source_file`.
