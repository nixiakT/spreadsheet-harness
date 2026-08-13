# Qwen service-alias local confirmation v25

This protocol freezes a one-shot, 16-task SpreadsheetBench-Verified comparison
of the literal LiteLLM service alias `qwen36-35b-a3b` under two
resource-matched configurations:

- `bare`: bounded deterministic preview plus the isolated code interpreter;
- `ours`: the same model, generation settings, and arm-task budget plus the
  deterministic profile, spreadsheet tools, rendering/recalculation, and
  frozen spreadsheet skills.

The primary estimand is paired end-to-end accuracy `ours - bare`. This is a
local agent comparison, not a reproduction of a paper leaderboard: the service
does not expose a checkpoint revision or serving recipe, this harness uses
LibreOffice and an `agent_per_workbook` protocol, and the cohort has only 16
tasks. Paper values may be shown as explicitly non-equivalent context only.

## Historical quarantine

The v23 pilot and v24 post-optimization evaluation are permanently read-only.
Their complete cohorts remain protected even where no request was made. Never
launch, resume, replay, substitute, or merge an arm-task from either cohort.

The v24 run recorded all 32 arm-tasks (29 completed and 3 errored) and
descriptively scored `ours` 6/16 and `bare` 4/16. It is development evidence,
not confirmation: two `ours` rows
crossed the token cap and one `bare` terminal submission was truncated, while
the v24 contract classified all three as infrastructure errors. The v24
artifacts also informed the bounded-profile, budget-terminal, and terminal-JSON
changes tested by v25. Its run spec is therefore non-launchable.

## Fresh cohort

The immutable split is
`qwen35-trace2skill-local-confirm16-v1.json`. It was selected without using any
outcome from its tasks:

1. Start from the same 127 candidates used by the frozen v24 ranking.
2. Continue ascending `SHA256(UTF8("20260812:" + task_id))` after the first 16.
3. Take original ranks 17 through 32 and serialize them in parent-manifest
   order.

The selected task order is:

```text
35739 36277 40959 43436 44266 45063 50683 51556
54717 55049 57232 59196 56427 57989 58904 59884
```

Integrity bindings:

- split raw-byte SHA-256:
  `c5b878de7fef5367f1e2e771f413c6724e5d4ea0c9079e9c0e99fe6feab3dc22`;
- selected ordered-ID SHA-256:
  `41fef0069fb4b5c7c0e14f5ce06e8dcb504685c33c00fe620675e5669250ee11`;
- prior 111-candidate ordered-ID SHA-256:
  `2b62d8104fe5fd65abe4fccea90f392af4fba8479a290b4fa518a9feded38a59`;
- remaining 95-task ordered-ID SHA-256:
  `ab2d825f7dba9f2706325251bd55eaf1d433043e9c5b1614677239a6bb9b20aa`;
- run-spec raw-byte SHA-256:
  `61ec4d37d0548e1be63ebf8619feb591d98ca78d7dce4d9d573886498ca74984`.

The code-owned run spec is
`qwen35-trace2skill-local-confirm16-run-spec-v1.json`. It binds v25/schema 14,
the split and output path, model/provider settings, budgets, arms, and exact
skill hashes and an executable-source fingerprint. It is fresh-only and cannot
resume.

## Frozen execution contract

- endpoint `http://101.37.174.109:8010/v1`;
- model `qwen36-35b-a3b`, Chat Completions, thinking disabled;
- seed 41, temperature 1, top-p 1, presence penalty 2, top-k 40,
  min-p 0, repetition penalty 1;
- zero retries and request interval, 700-second client timeout, 600-second
  LiteLLM timeout;
- 8 model calls and turns, 120,000 total tokens, and 4,096 output tokens per
  arm-task;
- 1,200-second task timeout, LibreOffice recalculation, circuit breaker 3,
  and arm-order seed 20260812.

Only `max_model_calls` and `max_total_tokens` exhaustion may justify an
auditable completed-false `budget_exhausted` outcome. A token overage is valid
only when the recorded prior responses were within the cap and the final
provider response caused the overage. Other auditable completed-false model
outcomes include `workbook_unchanged`, `edit_recovery_exhausted`, and
`terminal_submission_invalid`. Elapsed-time, provider, routing, sandbox, and
harness failures remain errors. An incomplete or inconsistent provider
`finish_reason`, including `length`, remains a provider error; only a completed
tool call whose submitted JSON or result is invalid is
`terminal_submission_invalid`.

## Publication and preflight gate

Before any selected-task request, run the full tests and lint, commit all
code/skill/protocol files, push `main`, verify live GitHub `main` equals local
`HEAD`, and require a clean worktree. Any later code, prompt, skill, split, or
run-spec change repeats this gate and requires a new output identity.

Then verify the split, require the model alias from `/v1/models`, and run the
synthetic forced-tool canary. These checks must not include a selected workbook
or task. Read the credential only from
`/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key` and never print or
persist it.

```bash
.venv/bin/sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --verify benchmarks/protocols/qwen35-trace2skill-local-confirm16-v1.json

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

If a preflight fails, do not switch the endpoint, model, protocol, decoding, or
budget and do not issue a selected-task request.

## One-shot launch

Require the frozen output path to be absent and launch exactly once without
`--resume`. Do not inspect task-level outputs, scores, traces, or progress to
make decisions while the command is running.

```bash
test ! -e benchmarks/results/qwen36-local-confirm-eval16-v1-bare-ours-v25-seed41

.venv/bin/sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-local-confirm16-v1.json \
  --run-spec benchmarks/protocols/qwen35-trace2skill-local-confirm16-run-spec-v1.json \
  --output benchmarks/results/qwen36-local-confirm-eval16-v1-bare-ours-v25-seed41 \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --arm bare --arm ours \
  --base-url http://101.37.174.109:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions --reasoning-effort none \
  --request-timeout 700 --request-retries 0 \
  --request-interval-seconds 0 --litellm-timeout 600 \
  --temperature 1 --top-p 1 --seed 41 --presence-penalty 2 \
  --top-k 40 --min-p 0 --repetition-penalty 1 --disable-thinking \
  --max-model-calls 8 --max-turns-per-arm 8 \
  --max-total-tokens 120000 --max-output-tokens 4096 \
  --task-timeout 1200 --arm-order-seed 20260812 --circuit-breaker 3
```

Any interruption, ambiguous in-flight request, circuit breaker, or missing row
ends this one-shot study. Preserve the directory and do not resume or relaunch.

## Audit and reporting

After natural command exit, run a read-only fresh-rescore audit:

```bash
.venv/bin/sheet-harness benchmark audit \
  benchmarks/results/qwen36-local-confirm-eval16-v1-bare-ours-v25-seed41 \
  --dataset benchmarks/data/spreadsheetbench_verified_400
```

The preregistered primary inference requires an audit-valid complete 16-pair
matrix. Report arm pass counts, completion/error taxonomy, end-to-end accuracy,
paired delta, discordant pairs, exact McNemar p-value, paired stratified
bootstrap interval, calls, tokens, and elapsed time. Completed-false model
outcomes remain in the denominator.

For paper context, distinguish protocols explicitly. Trace2Skill reports its
Qwen3.5-35B user on a 200-task held-out Vrf split averaged over seeds 41/42/43;
SpreadsheetAgent reports Soft/Hard on full SpreadsheetBench with other models;
Spreadsheet-RL reports Pass@1 with a trained Qwen3-4B agent in Spreadsheet Gym.
Neither their absolute values nor gains are an inferential threshold for this
16-task LibreOffice study.

Once any confirmation outcome is inspected, all 16 IDs enter development
quarantine. If the method changes afterward, a later claim requires a new,
disjoint, pre-frozen cohort from the remaining 95 tasks. Never rerun this cohort
until a desired result appears.
