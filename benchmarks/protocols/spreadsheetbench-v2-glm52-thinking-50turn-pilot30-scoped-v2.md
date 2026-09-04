# SpreadsheetBench-v2 GLM-5.2 thinking/50-turn stratified pilot30

Status: frozen run started on 2026-08-29 (Asia/Shanghai).

## Purpose and claim boundary

This paired pilot estimates whether `spreadsheet-harness-basic` improves over
bare GLM-5.2 under the published v2 inference regime before a full 321-task run.
It is not SOTA evidence, even if its partial score exceeds a published row.

## Frozen split

The split contains 30 tasks: 10 each from Debugging, Financial_Model, and
Template. Task IDs and order are bound in the run manifest at:

`benchmarks/results/spreadsheetbench-v2-glm52-thinking-50turn-pilot30-basic-scoped-v2-20260829/manifest.json`

Both `bare` and `spreadsheet-harness-basic` run on every task with deterministic
counterbalanced arm order (`arm_order_seed=20260829`).

## Inference and evaluation

- model alias: laboratory `GLM-5.2`
- thinking: enabled
- maximum interaction turns/model calls per arm: `50 / 50`
- maximum provider-reported tokens per arm: `600000`
- maximum output tokens per call: `8192`
- task deadline: `3600` seconds
- temperature/top-p/seed: `1 / 1 / 41`
- official evaluator SHA-256:
  `04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0`

Provider, deployment, recalculation, timeout, and evaluator failures are
`not_scored`; scored model-execution failures remain separately labeled.

## Frozen harness behavior

The basic arm uses the manifest-bound plugin composition, deterministic workbook
profile, high-confidence debugging warm starts, post-execution repair checkpoint,
and a public-basename Debugging family router. The router limits edits to the
error family stated in public input filenames; it receives no answer positions,
golden workbook content, or evaluator feedback. No implementation change may be
folded into this output directory after the run starts.

The immediately preceding `...basic-latest-v1-20260829` directory contains zero
scored rows and was abandoned before freeze because the family router had not yet
been loaded. It must not be combined with this study.
