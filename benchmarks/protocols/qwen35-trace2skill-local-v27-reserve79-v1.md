# Qwen service-alias local v27 exhaustive fresh-reserve evaluation

This protocol freezes a one-shot, 79-task SpreadsheetBench-Verified comparison
of the literal LiteLLM service alias `qwen36-35b-a3b` under two
resource-matched configurations, producing 158 expected arm-task rows:

- `bare`: bounded deterministic preview plus the isolated code interpreter;
- `ours`: the same model, decoding settings, and arm-task budget plus a compact
  deterministic profile, six spreadsheet work tools, and frozen spreadsheet
  skills.

The primary estimand is paired end-to-end accuracy `ours - bare`. This remains
a local agent comparison rather than a reproduction of any paper leaderboard:
the service does not expose a checkpoint revision or serving recipe, the
harness uses LibreOffice and an `agent_per_workbook` protocol, and the cohort
is the exhaustive locally fresh reserve rather than the paper's exact split.

## Historical quarantine

The v23 pilot, v24 post-optimization evaluation, v25 confirmation, and v26
confirmation are permanently read-only. Never launch, resume, replay,
substitute, or merge an arm-task from those cohorts.

The v26 run produced an audit-valid complete 16-pair matrix: `ours` passed
11/16 and `bare` passed 8/16, for a paired difference of +18.75 percentage
points. Its 32 arm-task outcomes and all 16 task IDs are now historical and
remain excluded from v27.

The 198-task held-out pool contains 55 task IDs exposed before the local
unattempted parent was frozen. Exact removal leaves 143 locally unattempted
IDs. The four frozen 16-task cohorts consume 64 of those IDs, leaving exactly
79 genuinely fresh IDs. Padding this study to 100 would require 21 exposed or
development tasks and is forbidden for the primary comparison.

## Exhaustive fresh cohort

The immutable split is
`qwen35-trace2skill-local-v27-reserve79-v1.json`. It takes the exact ordered
set difference between the 143-task local-unattempted parent and all four
earlier frozen cohorts, selects every remaining candidate exactly once, and
serializes the selected tasks in parent-manifest order. Selection is based
only on task identity and exposure history, not task instructions, workbooks,
results, or predicted difficulty.

The selected task order is:

```text
32612 32789 32902 35742 36097 36764 37086 37378
40757 40892 41265 41348 41420 41589 41978 42181
42216 42354 42515 42930 43589 44296 45738 46897
48527 49857 49945 50250 50486 50811 50971 51249
51680 52220 52233 52305 52964 53117 53161 53647
54274 54638 54667 54925 55060 55085 55427 55965
55979 56378 56920 56921 57113 57590 57743 58484
58701 59595 43213 44017 44913 45707 45937 46646
47827 55468 55708 56786 56953 57354 58109 58499
58687 58942 59129 59224 59734 59794 59902
```

Integrity bindings:

- 143-task parent-manifest raw-byte SHA-256:
  `aa12a17a65e8e60cc7678257e63d5a58f5760935ee3df1d27135b982b4de09cd`;
- 143-task parent ordered-ID SHA-256:
  `7b76ebca59be0e97964108b5e2d0552ea6a9c0f11eb51d15c10552b82efd3386`;
- selected/candidate 79-task ordered-ID SHA-256:
  `40e4491074477ddb2bd11a0e4dc7e5513447b1e1efb90b2a169b4026fc839e7b`;
- empty remaining-reserve ordered-ID SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`;
- split raw-byte SHA-256:
  `28e7f5ecc4549077a8c966d2704c46fca6bc36dbad53cb2692acaf51e536105b`;
- run-spec raw-byte SHA-256:
  `748fd0458e9b2c20adf5161fc9471e4f29421faecd5b4e02bdfa6b32b9342371`;
- executable-source contract SHA-256:
  `ab359f5c45ab797ec1b88ae1cfa54e50c9aba7fd44d6fddeb28e0a5df1448328`;
- `spreadsheet-core` skill raw-byte SHA-256:
  `be7760742aa7c09f282143a497d6786bc58cf8dc8b8af471acb10d5ac7bffc2f`;
- `visual-review` skill raw-byte SHA-256:
  `d41ebedc305b9c5de53b5df7701180730a50ae47c96fe94012a6988474f91213`.

The code-owned run spec is
`qwen35-trace2skill-local-v27-reserve79-run-spec-v1.json`. It binds v27/schema
16, the exact split/output identity, provider and decoding settings, budgets,
arms, source contract, and skill hashes. It is launchable only as a fresh run
and rejects resume.

## Frozen v26 method retained unchanged

V27 changes only the experiment identity, exhaustive fresh split, run-spec
anchor, and executable source registration needed to preserve v26 as a
historical contract. Its agent behavior and resource policy are identical to
v26:

- The final `submit_result` route exposes only an empty acknowledgement schema
  with an internal 128-token output cap; the harness supplies final prose.
- Tool-bearing stages reject text-only completion. Early prose is reprompted,
  and the final or last-budget call is reserved for `submit_result` only after
  the forced tool prefix is complete; otherwise routing fails closed before a
  final non-prefix request is issued.
- A final-route HTTP-200 output-limit response becomes the auditable
  completed-false reason `terminal_submission_truncated`. Its delivered call,
  usage, timing, and opaque discarded-message digest are retained; partial
  text or tool arguments are never parsed, exposed, or executed.
- Forced edit recovery occurs no later than the penultimate call. The final
  call remains reserved for submission, so a last-call edit cannot be accepted
  without a subsequent terminal decision.
- `ours` exposes only `code_interpreter`, `inspect_range`, `fill_formula`,
  `recalculate_and_read`, `render_workbook`, and `view_image`.
- The deterministic profile is capped at 12,000 rendered characters while
  retaining source/backend identity, representative boundary samples,
  formulas, formats, and provenance.
- The trajectory-local spreadsheet skill requires exact target coverage,
  first/middle/last and blank-boundary checks, reference-translation checks,
  LibreOffice recalculation, formula-error/blank blocking, and representative
  hand checks for last-N, date, blank, and duplicate-lookup logic.

## Frozen execution contract

- endpoint `http://101.37.174.109:8010/v1`;
- model `qwen36-35b-a3b`, Chat Completions, thinking disabled;
- seed 41, temperature 1, top-p 1, presence penalty 2, top-k 40,
  min-p 0, repetition penalty 1;
- zero retries and request interval, 700-second client timeout, 600-second
  LiteLLM timeout;
- 12 model calls and turns, 180,000 total tokens, and 4,096 output tokens per
  arm-task;
- 1,800-second task timeout, LibreOffice recalculation, circuit breaker 3,
  and arm-order seed 20260812.

Across 158 arm-tasks, the frozen per-row caps imply study-wide hard ceilings
of 1,896 model calls and 28,440,000 total tokens. These are ceilings, not
targets, and may not be reallocated between tasks or arms.

Only `max_model_calls` and `max_total_tokens` exhaustion may justify an
auditable completed-false `budget_exhausted` outcome. Other recognized
completed-false model outcomes are `workbook_unchanged`,
`edit_recovery_exhausted`, `terminal_submission_invalid`, and
`terminal_submission_truncated`. Timeout, provider, routing, sandbox, and
harness failures remain errors. Token-overage and timeout precedence remain
fail-closed.

## Publication and preflight gate

Before any selected-task request, replace every temporary hash placeholder,
run all tests and lint, validate both skills, commit every
code/skill/protocol file, push `main`, verify live GitHub `main` equals local
`HEAD`, and require a clean worktree. Any later executable, prompt, skill,
split, or run-spec change requires a new output identity and a repeat of this
gate.

Then verify the split, require the literal model alias from `/v1/models`, run
the synthetic forced-tool canary, and run a separate synthetic final-route
probe that returns exactly one `submit_result({})` call with no assistant text.
These checks must not include a selected workbook or task. Read the credential
only from `/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key`; never
print or persist it.

```bash
.venv/bin/sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --verify benchmarks/protocols/qwen35-trace2skill-local-v27-reserve79-v1.json

.venv/bin/python - <<'PY'
from pathlib import Path

import httpx

base_url = "http://101.37.174.109:8010/v1"
model = "qwen36-35b-a3b"
key = Path("/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key").read_text(
    encoding="utf-8"
).strip()
response = httpx.get(
    f"{base_url}/models",
    headers={"Authorization": f"Bearer {key}"},
    timeout=30.0,
)
response.raise_for_status()
available = {
    item.get("id")
    for item in response.json().get("data", [])
    if isinstance(item, dict)
}
if model not in available:
    raise SystemExit(f"required model alias is unavailable: {model}")
print(f"required model alias is available: {model}")
PY

.venv/bin/sheet-harness doctor --online --tools \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --base-url http://101.37.174.109:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions \
  --reasoning-effort none \
  --request-timeout 700 --request-retries 0 \
  --request-interval-seconds 0 --litellm-timeout 600 \
  --temperature 1 --top-p 1 --seed 41 --presence-penalty 2 \
  --top-k 40 --min-p 0 --repetition-penalty 1 --disable-thinking
```

If any preflight check fails, do not switch the endpoint, model, protocol,
decoding, or budget and do not issue a selected-task request.

## One-shot launch

Require the frozen output path to be absent and launch exactly once without
`--resume`. Run it under `nohup` so a controlling-shell disconnect cannot send
`SIGHUP`; redirect stdout and stderr outside the output directory, record only
the wrapper PID and its eventual exit status, and do not start a second wrapper.
While the command is running, inspect only process liveness; do not inspect the
log, task-level outputs, scores, traces, workbook contents, or progress to make
decisions.

```bash
test ! -e benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41

nohup sh -c '
.venv/bin/sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-local-v27-reserve79-v1.json \
  --run-spec benchmarks/protocols/qwen35-trace2skill-local-v27-reserve79-run-spec-v1.json \
  --output benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41 \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --arm bare --arm ours \
  --base-url http://101.37.174.109:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions --reasoning-effort none \
  --request-timeout 700 --request-retries 0 \
  --request-interval-seconds 0 --litellm-timeout 600 \
  --temperature 1 --top-p 1 --seed 41 --presence-penalty 2 \
  --top-k 40 --min-p 0 --repetition-penalty 1 --disable-thinking \
  --max-model-calls 12 --max-turns-per-arm 12 \
  --max-total-tokens 180000 --max-output-tokens 4096 \
  --task-timeout 1800 --arm-order-seed 20260812 --circuit-breaker 3
run_status=$?
printf "%s\n" "$run_status" > \
  benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41.launch.exit
exit "$run_status"
' > benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41.launch.log \
  2>&1 < /dev/null &
printf "%s\n" "$!" > \
  benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41.launch.pid
```

Any interruption, ambiguous in-flight request, circuit breaker, or missing row
ends this one-shot study. Preserve all artifacts and do not resume, relaunch,
or fill missing rows from another invocation.

## Audit and reporting

After natural command exit, run a read-only fresh-rescore audit:

```bash
.venv/bin/sheet-harness benchmark audit \
  benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41 \
  --dataset benchmarks/data/spreadsheetbench_verified_400
```

The preregistered primary inference requires an audit-valid complete 79-pair,
158-row matrix. Report arm pass counts, completion/error taxonomy, end-to-end
accuracy, paired delta, discordant pairs, exact McNemar p-value, paired
stratified bootstrap interval, calls, tokens, HTTP attempts, and elapsed time.
Completed-false model outcomes remain in the denominator.

For a deliberately limited numerical context check, 29/79 is 36.71% and is
the first whole-task pass count above the cited 36.67% Trace2Skill Qwen3.5-35B
SpreadsheetBench-Verified figure. This is not an inferential paper-beating
claim because the model endpoint, exact split, tools, seed, serving recipe,
LibreOffice backend, and execution protocol differ.

V27 exhausts the locally fresh reserve. Once any outcome is inspected, all 79
IDs enter development quarantine. Regardless of success, failure, or audit
validity, never rerun or resume this cohort and never pad it with an exposed
task. After reporting, make the v27 anchor non-launchable, pin its historical
source contract, re-audit the immutable result, then commit and push that
closure so another checkout cannot relaunch the study.
