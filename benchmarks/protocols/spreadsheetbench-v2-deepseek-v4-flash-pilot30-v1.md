# SpreadsheetBench-v2 DeepSeek-V4-Flash paired pilot30 v1

Status: frozen before launch on 2026-08-20 (Asia/Shanghai).

## Question and estimand

Compare the same `DeepSeek-V4-Flash` provider under the `bare` composition and
the `ours=plugevolve-seed` composition on a fixed 30-task SpreadsheetBench-v2
development pilot. The primary estimand is the paired difference `ours - bare`
in official task accuracy. Modification accuracy, regression accuracy, model
calls, total tokens, completion, and failure classes are secondary outcomes.

This pilot is not the full 321-task benchmark and is not a leaderboard result.
Visualization is excluded because the official workflow requires Windows
Excel/WPS COM and a VLM.

## Fixed sources

- Dataset: `KAKA22/SpreadsheetBench-v2`
- Dataset revision: `9dea60025792fbac5928ce9f44812362dccbeecd`
- Dataset archive SHA-256: `17147ef9578cd57ce76c9a719d19da7821f3e5cb0d8f776c820f699fdcdb761c`
- Evaluator repository: `RUCKBReasoning/SpreadsheetBench-2`
- Evaluator revision: `83d415ce87b1d6b8e8eafcc26957f5d13d37210f`
- `evaluation.py` SHA-256: `04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0`
- Harness protocol: `paired_official_evaluator_v2`

## Sampling and execution order

For each of Debugging, Financial_Model, and Template, rank all task IDs by
`SHA256("20260820:" + CATEGORY + "/" + ID)` and take the first ten. The pilot
does not contain the earlier smoke task `Template/06_05`. Execution is
round-robin by category and then by within-category sample rank.

First-arm position is determined independently by ranking the 30 selected IDs
by `SHA256("arm-order:20260820:" + TASK_ID)`: the first 15 are bare-first and
the other 15 ours-first. Each task's two arms remain adjacent.

| # | Task | Selection hash | First arm |
|---:|---|---|---|
| 1 | `Debugging/01_04` | `046846e9301ffa25aa34eb731622184aed3436c5f16c63c1fc2ac1b7a1e07808` | ours |
| 2 | `Financial_Model/05_04` | `02bc1a37e9c17602a39a64222259aac758566182afd430a73aa284d9a3c3be6c` | ours |
| 3 | `Template/01_04` | `00af266883eec6f56a0c0ca1832ab1e8ab43bc451eecf4617c23bc71c7f322c2` | ours |
| 4 | `Debugging/09_01` | `049259c1ace4d69856cbc2ba2ee613df5a07f89e78e9fc395d564a729e51ec8c` | ours |
| 5 | `Financial_Model/13_05` | `052dde20cb322e7a1c007dbd36711c8272437574214b9540572fdc75cca35e48` | ours |
| 6 | `Template/08_03` | `023417ee758dd742804850131e60f97ab0be2d5e9d4b169e7238173095ece164` | bare |
| 7 | `Debugging/04_06` | `05f2361dc21b96b7872f9c3957059241e6de4f75aebe3ddb287fabf8f29caabc` | bare |
| 8 | `Financial_Model/04_01` | `055963bb943dd5c0436ef2b7fe2d778089f876a32a5507cf6667b58e2c6205fd` | bare |
| 9 | `Template/02_03` | `0ac75e29f501733dc31d46aeee89f69f814ded3676caca45e6b92a1ef48d152a` | ours |
| 10 | `Debugging/09_06` | `07294960e70e4f323ab04d00d88107bf86f85cdbfec7041fcc48b2e9fb3adbde` | ours |
| 11 | `Financial_Model/01_01` | `0b71c98204ad5e2f68872b85e8a221a16366e550c338eb6e98b43de65d56611a` | ours |
| 12 | `Template/15_02` | `0b35a2873cc83e6d99fa6a767034137f6d2be378ea5aa16752d8df3732d617fd` | ours |
| 13 | `Debugging/08_05` | `079fdcec350eeea12ffc0dfc5dd1d230acae011172566756c7f005c8975153f1` | ours |
| 14 | `Financial_Model/20_02` | `0d85b0bf81ca86b91249a10fea13d9398561cd4feec3d9c401f79a437efe9102` | bare |
| 15 | `Template/06_09` | `13a9031a4b1ff028f9478b0cc2332d05871466bc3472f47fd7b0a8b7aca3ba73` | bare |
| 16 | `Debugging/08_08` | `0b927d1cc9c8b5a87162d146f0a6b3e16058350a0a5d84bbdaa02c60d0553b3f` | bare |
| 17 | `Financial_Model/01_02` | `1260ae092e0baa7e8899574dfa3093bb69c971ca9d80e722563f90c77c393e4b` | bare |
| 18 | `Template/02_05` | `166ed77db1d16305789c65cbacd8c8c10ea9582716f3961ca3912138cae1b7f8` | bare |
| 19 | `Debugging/06_01` | `0ce9e1563bc9877aea6320520cef739bfcb5b1ccaf92789eac0b2a1a21fb1069` | bare |
| 20 | `Financial_Model/09_01` | `1443bd7b3a455141be5ac4eb44963a60d982d3b7c52f6e56823670655eb7f58e` | bare |
| 21 | `Template/13_06` | `19be9d63fe9fe0e84b116cb950f6fd0a12d407a1cf2db9ad9001e85273c37ab8` | bare |
| 22 | `Debugging/05_03` | `0f414697f272dd2d2242b2694b57a30dd5e5533cfd10c21d9786d0dd97bb982d` | bare |
| 23 | `Financial_Model/16_04` | `171da52b0412676923407d0c8c824c3fce5f4e1a16d3d4df21ff942a6567facc` | ours |
| 24 | `Template/14_07` | `1c61fcdb942f2bc4476796db753c519bd42e13b127d91c00e0294e845224fc43` | ours |
| 25 | `Debugging/10_08` | `10beb894bcfdda7b3f18969f9b8f8a30655fd55a89b0e25fedd315ab380ec2dd` | ours |
| 26 | `Financial_Model/07_01` | `17c574c78b943d300600835e636da04f4ee9607c2caa00e8627251f4b4d2c5d7` | ours |
| 27 | `Template/06_02` | `20e9f9fdd9718735edbbdd1a0dc2d67aaeca3ba507ae944867203f236ca290ad` | bare |
| 28 | `Debugging/02_08` | `192577a1a2631be61d90d4ebe921f32948554c81289d16c08d848ea48a506935` | bare |
| 29 | `Financial_Model/02_02` | `1923df54ce58888b10d4d6ad5463dc9a7e60197b883f4020bf59241ab62ee6b3` | bare |
| 30 | `Template/02_01` | `22ebc691fedb51404cb53267345161bcaa46d0ecdffeb8eb622fdcaaddec06c4` | ours |

## Provider and budgets

- Open-source model: `DeepSeek-V4-Flash`
- API protocol: Chat Completions through the laboratory LiteLLM relay
- Temperature/top-p: `1/1`
- Seed: `41`
- Presence penalty: `2`
- top-k/min-p/repetition penalty: `40/0/1`
- Thinking: disabled
- Per-request timeout/retries: `180 seconds / 0`
- Start-to-start request interval: `5.5 seconds`
- Per-arm limits: 20 calls, 20 turns, 200,000 total tokens
- Per-call output limit: 4,096 tokens
- Per-arm elapsed limit: 1,800 seconds
- Arm-order seed: `20260820`

No OpenAI or Anthropic model is authorized by this protocol.

## Completion and failure rules

- No selected task may be replaced, resampled, or silently retried under this result identity.
- Provider/infrastructure failures are `not_scored`, never numerical zeroes.
- Primary arm accuracy and paired deltas are reported only if all 60 arm-tasks are scored.
- Completed-case metrics and completion rates may be shown for diagnosis but are not substitutes for the primary estimand.
- Three consecutive failures with the same provider/infrastructure cause the launch to stop for diagnosis; already written rows remain evidence.
- After completion, run `v2-audit` to hash-check outputs and fresh-rescore all 60 rows with the pinned official evaluator.

Output identity:

`benchmarks/results/spreadsheetbench-v2-deepseek-v4-flash-pilot30-paired-plugevolve-v1-20260820-run1`
