# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.4 canary2

Status: frozen before execution on 2026-08-20 (Asia/Shanghai).

Paired canary on `Financial_Model/13_05` and `Template/13_06`. These tasks had fresh v1.3
bare modification scores of approximately 0.4849 and 1.0 respectively, while v1.3 ours scored
zero. This run tests the new read-only YAML planner plus fresh-context executor architecture.

Acceptance requires all four arms to receive official scores, mean ours modification accuracy at
least `1.2 *` fresh bare, no per-task strict regression, and mean ours regression accuracy no more
than 0.01 below bare. Failure rejects v1.4 and does not authorize the five-task challenger.

Fixed settings: Chat Completions; reasoning none; thinking disabled; temperature/top-p 1/1;
seed 41; presence penalty 2; top-k 40; min-p 0; repetition penalty 1; request timeout 700 seconds;
LiteLLM timeout 600 seconds; retries 0; interval 0; 8 calls/turns split as planner 5 plus executor
3 for ours; 120,000 total tokens; 4,096 output tokens; 1,200-second task timeout; arm-order seed
20260820. Model is laboratory open-source `qwen36-35b-a3b`; composition is
`ours=plugevolve-seed` with `policy-ours` 1.4.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v14-canary2-20260820-run2`.

The initially reserved identity without `-run2` stopped during local preflight because an explicit
skills path duplicated the default registry; it made no model requests and is not a study run.
