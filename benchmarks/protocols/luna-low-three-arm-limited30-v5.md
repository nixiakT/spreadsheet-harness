# Luna-low limited 30-task three-arm protocol v5

This protocol pre-registers a finite, paired engineering study. It is not a
SpreadsheetAgent reproduction, a leaderboard submission, or an unbiased
estimate of the full SpreadsheetBench distribution. It replaces the retired
v4 transport policy; no v1-v4 row may be resumed, imported, or resampled into
this run.

## Question and arms

Every selected task uses `gpt-5.6-luna` at `low` effort with the same input
workbook, deterministic preview, LibreOffice backend, value scorer, successful
response cap, token cap, output-token cap, and arm-task deadline.

1. `bare`: the small model with an isolated local Python interpreter.
2. `paper` (displayed as `paper-inspired`): a clean-room, resource-matched
   adaptation of task-independent structural extraction plus complementary
   original-image and LaTeX verification before a Python solver.
3. `ours`: the same model with the full local spreadsheet tools, original PNG
   vision path, and frozen spreadsheet skills.

The primary descriptive contrast is `ours - paper-inspired`; `ours - bare` and
`paper-inspired - bare` are secondary. Any difference is attributable to the
complete arm policy, not to an isolated tool, vision, routing, or skill effect.

## Paper comparison boundary

The reference is arXiv:2604.12282v1 and released repository commit
`b4ded1ebdb73ab66acfa8439ad2af54470e317e3`. The released system evaluates 912
instructions and 2,729 workbook cases, supplies evaluator-only answer metadata
to its solver, uses Qwen3-Coder-480B plus GLM-4.5V, and performs per-sheet
extraction with up to three verifier-refinement rounds. Its Soft and Hard
metrics aggregate up to three cases per instruction.

This study instead uses the corrected Verified 398 set, which has one
init/golden pair per task. It withholds `instruction_type`, `answer_position`,
`answer_sheet`, and the golden workbook from every model. The paper-inspired
arm uses Luna for every role and one fixed
extract -> vision -> LaTeX -> reconcile -> solve pass. The released repository
has no explicit license, so no third-party implementation code is copied.
Absolute scores from this study must not be compared with the paper's tables;
Verified single-case Soft and Hard scores would be identical.

## Frozen sample and interpretation

The task file contains 30 tasks: 21 Cell-Level and 9 Sheet-Level. It reuses the
capability-balanced v1-v4 engineering list, which was frozen before any
three-arm outcome was produced but subsequently participated in harness
development. The complete 30-task result is therefore exploratory. Report a
separate 24-task sensitivity table excluding the six historical canary IDs
`38703`, `41691`, `423-16`, `493-18`, `54196`, and `84-40`.

The list was limited to workbooks no larger than 250 KiB, at most 30,000
non-empty cells, at most 750 corrected-scorer cells, and at most 100,000
aggregate used-range cells, then capability-balanced across formula repair,
text/date work, formatting/merge/visual cues, large ranges, reshaping,
ordinary edits, multi-sheet reasoning, and sorting. The two dataset rows marked
excluded were never eligible.

Canonical ASCII-sorted ID-set SHA-256 (one ID per line, final newline):
`9f5e3f0f57d2840d4f531f03ee2febea94c10aadd6842354b18dd168430588d6`.

The 30-task sample has low power. Confidence intervals spanning zero are
reported as uncertain, never as evidence of equivalence or no effect.

## Frozen model and resource limits

- Dataset: `KAKA22/SpreadsheetBench@ab0b742b0fc95b946f212d80ac7771b5531272e4`
- Model / effort: `gpt-5.6-luna` / `low`
- Successful provider responses per arm-task: at most 20
- Provider-reported tokens per arm-task: at most 100,000
- Output tokens per response: at most 4,096
- Arm-task wall time: 1,800 seconds
- Per-attempt request timeout: 360 seconds
- Configured request retries: one, but only the safe delivery allowlist below
- Semantic task or arm-task resampling: zero
- Circuit breaker: one transient provider or routing-protocol arm-task failure;
  any fatal provider failure stops immediately
- Arm-order seed: `20260811`, with deterministic cyclic position balancing
- Strict Linux Bubblewrap isolation with no unsandboxed fallback
- Response storage disabled

The comparison stage caps, required-tool routes, terminal `submit_result`
protocol, preview envelope, context bounds, paper evidence validation, and
read-only workbook-integrity checks are the frozen implementation recorded in
the run manifest.

## Relay delivery and pacing policy

Every physical HTTP attempt receives a logical request ID, a unique client
attempt ID, and a canonical payload SHA-256. Only allowlisted, redacted response
headers are persisted.

Automatic replay is permitted only for delivery states known safe:

- `ConnectTimeout`, `ConnectError`, or `PoolTimeout`, after at least 30 seconds;
- explicit HTTP 425, 429, or 503 rejection; or
- an exact structured overload rejection.

Capacity rejection delay is `max(valid Retry-After, 15 seconds)`, capped at 60
seconds and the remaining arm deadline. `x-should-retry:false` vetoes replay;
`x-should-retry:true` never expands the allowlist.

Read/write timeout or error, HTTP 408, other 5xx, remote protocol error, stream
EOF, missing terminal event, and completed-response protocol errors all fail
closed without replay because upstream delivery may already have occurred.
Provider transience remains separate from safe replay authorization.

One runner-level pacer is shared across client, stage, arm, and task boundaries.
It admits every physical attempt, including a safe retry, no faster than 20
seconds start-to-start, with no jitter and the first request immediate. Pacing
wait counts against the arm-task deadline but not the per-attempt network
timeout. The formal run uses one consumer process and one Relay key.

This pacing is a conservative mitigation motivated by the observed request and
token burst; it is not a proven Relay root-cause fix. The pre-run exact replay
must succeed after at least 60 quiet minutes. A repeated approximately 321
second empty HTTP 408 blocks the experiment and requires Relay-side queue,
gateway, quota, and upstream-log repair.

## Gate, execution, and reporting

After a successful cooled exact replay, run one fresh, non-scored three-arm
smoke on task `41691`. It passes only if all three arm-tasks complete, every
workbook reopens, all forced and terminal routes match, and the paper arm
records original-image attachment, LaTeX conversion, valid provenance, and
unchanged hashes for every read-only stage.

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id 41691 \
  --output benchmarks/results/luna-low-three-arm-smoke-41691-v5 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 360 --request-retries 1 \
  --request-interval-seconds 20 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 1 --arm-order-seed 20260811
```

Only then may the limited run start in a completely new directory:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id-file benchmarks/protocols/luna-low-three-arm-limited30-v5.txt \
  --output benchmarks/results/luna-low-three-arm-limited30-v5 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 360 --request-retries 1 \
  --request-interval-seconds 20 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 1 --arm-order-seed 20260811
```

Do not rerun an errored arm-task. Resume is allowed only for rows never started
after a clean process interruption and only when the manifest matches exactly.
One ambiguous-delivery failure invalidates inferential statistics and stops the
run.

Report end-to-end accuracy on all pre-registered tasks, Wilson 95% intervals,
Cell/Sheet strata, the 24-task sensitivity result, completion/error categories,
successful responses, provider tokens, physical attempts, elapsed time, and
tokens per pass. Paired deltas use a task-stratified bootstrap; binary paired
outcomes use exact McNemar tests with Holm correction. Inferential fields are
valid only for a complete paired matrix without provider, routing, or budget
failures. Style is not scored and must be reported as `style_checked: false`.

No full-398 run is authorized by this limited protocol.
