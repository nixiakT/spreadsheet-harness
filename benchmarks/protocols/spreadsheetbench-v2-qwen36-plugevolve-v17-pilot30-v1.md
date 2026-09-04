# SpreadsheetBench-v2 Qwen36 PlugEvolve v1.7 pilot30

Status: frozen before execution on 2026-08-21 (Asia/Shanghai).

Development paired pilot using the exact stratified 30-task list from the earlier DeepSeek pilot:
10 Debugging, 10 Financial_Model, and 10 Template tasks in the same interleaved order. This run is
authorized by the audited two-task canary where ours averaged 0.2801 modification versus bare 0,
with regression delta -0.00205.

Primary reporting includes strict whole-task passes, nonzero-modification task count, mean official
modification accuracy, mean regression accuracy, paired deltas, model-execution failures, calls,
and tokens. The target is ours mean modification at least `1.2 *` bare, no lower strict pass count,
and mean regression no more than 0.01 below bare. All 60 arms must be officially scored and the
fresh audit must pass before inference.

Settings: laboratory open-source `qwen36-35b-a3b`; Chat Completions; reasoning none; thinking
disabled; temperature/top-p 1/1; seed 41; presence penalty 2; top-k 40; min-p 0; repetition penalty
1; request timeout 700 seconds; LiteLLM timeout 600 seconds; retries 0; interval 0; 8 calls/turns
(ours plan 1 + execute 7); 120,000 total tokens per arm; 4,096 output tokens; 1,200-second task
timeout; arm-order seed 20260820; `ours=plugevolve-seed`; `policy-ours` 1.7.0.

Fresh output identity:
`benchmarks/results/spreadsheetbench-v2-qwen36-plugevolve-v17-pilot30-20260821`.
