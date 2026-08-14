# Qwen service-alias local v26 confirmation

This protocol freezes a one-shot, 16-task SpreadsheetBench-Verified comparison
of the literal LiteLLM service alias `qwen36-35b-a3b` under two
resource-matched configurations:

- `bare`: bounded deterministic preview plus the isolated code interpreter;
- `ours`: the same model, decoding settings, and arm-task budget plus a compact
  deterministic profile, six spreadsheet work tools, and frozen spreadsheet
  skills.

The primary estimand is paired end-to-end accuracy `ours - bare`. This remains
a local agent comparison rather than a reproduction of any paper leaderboard:
the service does not expose a checkpoint revision or serving recipe, the
harness uses LibreOffice and an `agent_per_workbook` protocol, and the cohort
contains only 16 tasks.

## Historical quarantine

The v23 pilot, v24 post-optimization evaluation, and v25 confirmation are
permanently read-only. Never launch, resume, replay, substitute, or merge an
arm-task from those cohorts.

The v25 run observed all 32 arm-tasks, but three `ours` final submissions ended
with HTTP 200 and `finish_reason=length`. Under its frozen v25 contract those
rows are provider errors, so the 29-valid-row result is descriptive only:
`ours` passed 4/16 and `bare` passed 3/16. The v25 traces supplied only general
development lessons; none of the v26 task outcomes was inspected before this
split and run spec were frozen.

## Fresh cohort

The immutable split is
`qwen35-trace2skill-local-v26-confirm16-v1.json`. It continues the same
ascending `SHA256(UTF8("20260812:" + task_id))` ranking after exact exclusion
of all three earlier cohorts, takes original candidate ranks 33 through 48,
and serializes the selected tasks in parent-manifest order.

The selected task order is:

```text
34210 37462 37554 44628 45372 50051 50521 52541
54085 54513 55817 57033 57445 57558 57693 59639
```

Integrity bindings:

- prior 95-candidate ordered-ID SHA-256:
  `ab2d825f7dba9f2706325251bd55eaf1d433043e9c5b1614677239a6bb9b20aa`;
- selected ordered-ID SHA-256:
  `f735283a19d2d464f46b10387764cc600598bb15f00a767ff4df17d154629d27`;
- remaining 79-task ordered-ID SHA-256:
  `40e4491074477ddb2bd11a0e4dc7e5513447b1e1efb90b2a169b4026fc839e7b`;
- split raw-byte SHA-256:
  `5471aadfa319948fd60e97048f5f07aa39418195770c589085d7da63ed27cb61`;
- run-spec raw-byte SHA-256:
  `4bca7fe452c9ba2dadc31c374f29abcda575cb243e5f960789f2f50b4191884a`;
- executable-source contract SHA-256:
  `10ead91dc5e40b5f065b09e2c0b132342350cc7afa6edd3d8d38d2edc6f4a1d3`.

The code-owned run spec is
`qwen35-trace2skill-local-v26-confirm16-run-spec-v1.json`. It binds v26/schema
15, the exact split/output identity, provider and decoding settings, budgets,
arms, source contract, and skill hashes. It is launchable only as a fresh run
and rejects resume.

## v26 method changes

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

Only `max_model_calls` and `max_total_tokens` exhaustion may justify an
auditable completed-false `budget_exhausted` outcome. Other recognized
completed-false model outcomes are `workbook_unchanged`,
`edit_recovery_exhausted`, `terminal_submission_invalid`, and the exact
v26-only `terminal_submission_truncated` route. Timeout, provider, routing,
sandbox, and harness failures remain errors. Token-overage and timeout
precedence remain fail-closed.

## Publication and preflight gate

Before any selected-task request, run all tests and lint, validate the skill,
commit every code/skill/protocol file, push `main`, verify live GitHub `main`
equals local `HEAD`, and require a clean worktree. Any later executable,
prompt, skill, split, or run-spec change requires a new output identity and a
repeat of this gate.

Then verify the split, require the literal model alias from `/v1/models`, and
run the synthetic forced-tool canary. These checks must not include a selected
workbook or task. Read the credential only from
`/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key`; never print or
persist it.

```bash
.venv/bin/sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --verify benchmarks/protocols/qwen35-trace2skill-local-v26-confirm16-v1.json

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

If preflight fails, do not switch the endpoint, model, protocol, decoding, or
budget and do not issue a selected-task request.

## One-shot launch

Require the frozen output path to be absent and launch exactly once without
`--resume`. Do not inspect task-level outputs, scores, traces, or progress to
make decisions while the command is running.

```bash
test ! -e benchmarks/results/qwen36-local-v26-confirm-eval16-v1-bare-ours-seed41

.venv/bin/sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-local-v26-confirm16-v1.json \
  --run-spec benchmarks/protocols/qwen35-trace2skill-local-v26-confirm16-run-spec-v1.json \
  --output benchmarks/results/qwen36-local-v26-confirm-eval16-v1-bare-ours-seed41 \
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
```

Any interruption, ambiguous in-flight request, circuit breaker, or missing row
ends this one-shot study. Preserve the directory and do not resume or relaunch.

## Audit and reporting

After natural command exit, run a read-only fresh-rescore audit:

```bash
.venv/bin/sheet-harness benchmark audit \
  benchmarks/results/qwen36-local-v26-confirm-eval16-v1-bare-ours-seed41 \
  --dataset benchmarks/data/spreadsheetbench_verified_400
```

The preregistered primary inference requires an audit-valid complete 16-pair
matrix. Report arm pass counts, completion/error taxonomy, end-to-end accuracy,
paired delta, discordant pairs, exact McNemar p-value, paired stratified
bootstrap interval, calls, tokens, and elapsed time. Completed-false model
outcomes remain in the denominator.

For a deliberately limited numerical context check, 6/16 is 37.5% and exceeds
the best reported Trace2Skill Qwen3.5-35B Verified figure of 36.67%. This is not
an inferential paper-beating claim because the model endpoint, cohort size,
seeds, tools, and execution protocol differ. A defensible paper-level claim
still requires the fixed 198-task held-out split (or the paper's exact 200-task
definition) and preferably seeds 41/42/43.

Once any v26 outcome is inspected, all 16 IDs enter development quarantine. If
`ours` remains below 6/16 or the audit is invalid, use only general failure
lessons to revise the method, freeze a new disjoint cohort from the remaining
79 tasks, commit and push the new protocol, and repeat once. Never rerun this
cohort until a desired result appears.
