# SpreadsheetBench-v2 paper-method reproduction protocol

This protocol fixes the comparison split to the 30 task IDs in
`benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json`
(10 Debugging, 10 Financial_Model, 10 Template).  All runs use the same
official-compatible evaluator, seed 41, and Linux/LibreOffice recalculation.
The visualization split is excluded because the papers' Excel/VLM pipelines
require Windows Excel/COM.

## Reproduction identity

| Method | Execution identity | Official status |
|---|---|---|
| SpreadsheetAgent (arXiv:2604.12282) | Existing `paper-vision` arm; extraction/verification/LaTeX stages | Clean-room proxy on Linux; official Excel-to-image requires Windows COM and Qwen3-Coder-480B + GLM-4.5V |
| Spreadsheet-RL (arXiv:2605.22642) | Official `Spreadsheet-RL-4B` checkpoint when local endpoint is used; `spreadsheet-rl-native` for the tool loop | We have the official 4B weights; the official Excel reward service still requires a Windows Excel host, so Linux scores are native-tool proxy scores |
| Trace2Skill (arXiv:2603.25158) | Public runner `run_spreadsheetbench.py`, `cli_skill_preloaded`, released `xlsx-35B` skills | Official code/skills, but lab model is `qwen36-35b-a3b` rather than paper Qwen3.5-35B; label as model-substitution reproduction |
| SheetCompass (arXiv:2608.14452) | Graph/memory proxy to be evaluated through the harness native inspection and verification tools | No public implementation/checkpoint located; clean-room proxy only |

## Current artifacts

- Trace2Skill source: `tmp/paper_repos/Trace2Skill/`.
- Spreadsheet-RL source: `tmp/paper_repos/Spreadsheet-RL/`.
- SpreadsheetAgent source: `tmp/paper_repos/SpreadsheetAgent/`.
- Trace2Skill staging data (30 symlinked cases): `tmp/trace2skill_spreadsheetbench_v2_30/`.
- Official Spreadsheet-RL-4B checkpoint: `models/Spreadsheet-RL-4B-f594c782331194bf38356107041e309ce7e0b65a/` (8,822,894,520 bytes; safetensors header validated).

## Run commands

Trace2Skill's released-skill 30-case run is launched by
`benchmarks/launch_trace2skill_v2_30_qwen36_20260905.sh`.  The official
Spreadsheet-RL checkpoint server is started on GPU 7 at `127.0.0.1:8625` using
the environment documented in that launch log.  Any score reported from this
Linux host must retain the `Linux/LibreOffice` qualifier.

The original Trace2Skill qwen36 run exposed a tool-environment defect
(`/bin/sh: python: not found`).  It is retained as a diagnostic run; the clean
model-substitution run is `benchmarks/launch_trace2skill_v2_30_qwen36_envfixed_20260905.sh`,
which prepends the project virtualenv to `PATH` before invoking the same
official runner and released skills.

The follow-up compatibility run is
`benchmarks/launch_trace2skill_v2_30_qwen36_compatfix_20260905.sh`: it uses the
same qwen36 endpoint with thinking disabled and accepts the public runner's
occasionally nested bash-action arguments.  Its workbooks are rescored by
`tools/score_trace2skill_outputs.py` with the pinned evaluator; these rows remain
separate from the original and qwen3-coder-plus model-substitution results.

Because the reference CLI is case-serial and individual calls can approach the
1800-second task deadline, pending cases are also dispatched in a separate,
two-worker continuation tree by
`benchmarks/launch_paper_reproduction_parallel_pending_20260905.sh`.  These
trees are merged by task ID only after completion; an original successful row
is preferred over an error row, and continuation rows are never presented as a
different method or checkpoint.

## Reporting rules

Report completion rate, hard pass accuracy, modification accuracy, regression
accuracy, model-execution failures, timeout count, model name, and evaluator
hash.  Never combine proxy and official results into one leaderboard row, and
never call the SpreadsheetAgent or SheetCompass rows official reproductions.
