# SpreadsheetBench-v2 Financial Modeling plugin paired pilot10 v1

Status: frozen before execution on 2026-08-28 (Asia/Shanghai).

## Question

Does one business-domain plugin improve Financial Modeling performance when the
general spreadsheet harness, model, tools, policy, profile, verifier, repair,
budget, task order, and evaluator are held fixed?

## Arms

- `spreadsheet-harness-basic`
- `spreadsheet-harness-financial`

The financial arm is exactly the basic composition plus
`skill-spreadsheet-financial-model`.  The resolved composition hashes must be
recorded in the run manifest.  No other composition override is allowed.

## Tasks

This development pilot reuses the ten Financial Modeling rows in the frozen
stratified pilot, in their prior order:

1. `Financial_Model/05_04`
2. `Financial_Model/13_05`
3. `Financial_Model/04_01`
4. `Financial_Model/01_01`
5. `Financial_Model/20_02`
6. `Financial_Model/01_02`
7. `Financial_Model/09_01`
8. `Financial_Model/16_04`
9. `Financial_Model/07_01`
10. `Financial_Model/02_02`

These rows may gate a later full-category evaluation but cannot establish SOTA.

## Fixed execution settings

- Dataset revision: `9dea60025792fbac5928ce9f44812362dccbeecd`
- Official evaluator SHA-256:
  `04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0`
- Model: laboratory open-source `qwen36-35b-a3b`
- API: Chat Completions, thinking disabled
- Temperature/top-p/seed: `1 / 1 / 41`
- Maximum model calls/turns per arm: `8 / 8`
- Maximum provider-reported tokens per arm: `120000`
- Maximum output tokens per call: `4096`
- Task deadline: `1200` seconds
- Request/LiteLLM deadline: `700 / 600` seconds
- Arm-order seed: `20260828`

Provider/infrastructure failures are `not_scored`, never zero.  Interrupted
ambiguous requests are sealed and never replayed.  Results from a different
composition hash or execution setting cannot be merged into this study.

## Pilot acceptance

The pilot authorizes the full 100-row Financial Modeling run only if:

1. both arms have at least eight common officially scored tasks;
2. financial mean modification accuracy on common tasks exceeds basic;
3. financial wins more tasks than it loses;
4. mean regression accuracy decreases by no more than `0.005` and no severe
   workbook corruption is observed; and
5. token, call, timeout, and model-failure rates are reported alongside scores.

The full-category claim requires all eligible rows, a fresh audit, and a
preregistered held-out protocol.  This pilot is development evidence only.

