# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.3 challenger5

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

Development paired challenger on Financial_Model/01_02, Financial_Model/13_05,
Financial_Model/16_04, Financial_Model/05_04, and Template/13_06. The laboratory-recommended
open-source `qwen36-35b-a3b` route replaces DeepSeek because the latter repeatedly returned
DashScope quota errors. Results are compared only within this fresh Qwen paired run.

Acceptance requires all ten arms to receive official scores, mean ours modification accuracy at
least `1.2 *` fresh bare, no per-task strict regression, and mean ours regression accuracy no more
than 0.01 below bare. Failure rejects the composition and does not authorize a larger run.

Fixed settings: Chat Completions; reasoning none; thinking disabled; temperature/top-p 1/1;
seed 41; presence penalty 2; top-k 40; min-p 0; repetition penalty 1; request timeout 700 seconds;
LiteLLM timeout 600 seconds; retries 0; interval 0; 8 calls/turns, 120,000 total tokens, 4,096
output tokens, 1,200-second task timeout, arm-order seed 20260820. Composition is
`ours=plugevolve-seed` with `policy-ours` 1.3.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v13-challenger5-20260820`.
