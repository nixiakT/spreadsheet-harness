# Qwen service-alias local post-optimization evaluation v24

This protocol freezes a one-shot, 16-task SpreadsheetBench-Verified evaluation
of the same served model under two resource-matched agent configurations:

- `bare`: a bounded deterministic workbook preview plus only the code
  interpreter, without native spreadsheet tools or harness-only skills.
- `ours`: the same served model and resource budget plus the complete
  spreadsheet harness and its frozen skills.

The primary estimand is paired end-to-end accuracy `ours - bare` on these 16
tasks. This is a local post-optimization evaluation of the literal service
alias `qwen36-35b-a3b`, not a reproduction of a SpreadsheetBench,
SpreadsheetAgent, Spreadsheet-RL, or Trace2Skill leaderboard. Without an
upstream checkpoint revision and serving recipe, the alias must not be reported
as an exact Trace2Skill checkpoint.

## Development quarantine and non-replay boundary

All 16 IDs in
`qwen35-trace2skill-local-unattempted-pilot16-v2.json` entered development
quarantine when that manifest was frozen, including IDs for which no request
was ever made:

```text
33157 35747 37229 46121 53383 53449 54474 56419
56599 58723 55977 56563 57117 57262 57612 59511
```

None is eligible for this evaluation or a later confirmation cohort. In the
v23 run, `33157::ours` has an unknown request-delivery outcome. That arm-task
must never be replayed, inferred as a pass or failure, or replaced. The v23
run spec and result directory are historical, read-only audit inputs:

- never launch, resume, or seal with the v23 run spec;
- never use `--resume` on its result directory;
- never merge its known rows into the v24 cohort; and
- never use a new output directory to replay any v23 arm-task.

The runner also maintains a code-owned protected-task ledger for the complete
v23 and v24 cohorts. Direct `--task-id`, task-file, ordinary benchmark, or
unregistered-split selection cannot bypass these run-spec boundaries.

## Cohort freeze

The new cohort was selected before any inference on its IDs. Its immutable
manifest is `qwen35-trace2skill-local-postopt16-v1.json`. The derivation is:

1. Start from the attested 143-task parent
   `qwen35-trace2skill-local-unattempted-v2.json`.
2. Exclude the complete old 16-task development pilot by exact task ID, leaving
   127 candidates in parent-manifest order.
3. For every candidate compute
   `SHA256(UTF8("20260812:" + task_id))`, sort by ascending digest with task ID
   as the tie-breaker, and take the first 16.
4. Serialize those 16 in their original parent-manifest order.

The frozen task order is:

```text
36191 37456 39190 45944 50631 51354 52050 52532
58147 42902 43657 55260 55421 55976 59160 59358
```

The integrity bindings are:

- 127-candidate ordered-ID SHA-256:
  `71c14c013bb98a1fe8d0219a5be4a784fc9aa13dcbce2419d2ec963c7457d6b7`.
- 16-task selected ordered-ID SHA-256:
  `a4a485d5543710352a20be947d3ac3dc251ca8fbaa32b9a0dfe571d0506b6f7a`.
- Selected-manifest raw-byte SHA-256:
  `de82b9a5f17aaaf66e112f4d38938abbe9651ceab1a784ba815c82d171569c1b`.
- Remaining 111-task ordered-ID SHA-256:
  `2b62d8104fe5fd65abe4fccea90f392af4fba8479a290b4fa518a9feded38a59`.

The code-anchored run spec is
`qwen35-trace2skill-local-postopt16-run-spec-v1.json`, with raw-byte SHA-256
`a7e335c81cd86ec1edb81f223b103bafabec19e983a669d56f3ded6965151644`.
It binds comparison protocol `resource_matched_multi_arm_v24`, comparison
manifest schema 13, the split, endpoint, model alias, decoding, budgets, arms,
skills, and output path.

This run spec is fresh-only: it may launch exactly once into its absent, frozen
output path and must reject `--resume` even for its own directory. If a process
interruption, ambiguous in-flight request, circuit breaker, or missing row
prevents a complete matrix, do not resume or relaunch the cohort. Preserve the
directory for audit and classify the one-shot evaluation as incomplete.

## Frozen online configuration

The model, endpoint, protocol, sampling, and budgets are unchanged from v23:

- endpoint `http://101.37.174.109:8010/v1`;
- model alias `qwen36-35b-a3b` over Chat Completions, with thinking disabled;
- seed 41, temperature 1, top-p 1, presence penalty 2, top-k 40, min-p 0,
  repetition penalty 1;
- zero request retries and zero request interval, 700-second client timeout,
  and 600-second LiteLLM upstream timeout;
- at most 8 model calls and 8 model turns per arm-task, 120,000 total tokens per
  arm-task, and 4,096 output tokens per call;
- 1,200-second task timeout, LibreOffice recalculation enabled, circuit breaker
  threshold 3, and arm-order seed 20260812.

Both arms receive the same task order and budgets. The API credential is read
only from `/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key`; it must
not be copied into a command, manifest, trajectory, result, report, or commit.

## Publication gate

No provider or model request for the new 16 may occur until the complete v24
implementation and all three frozen protocol artifacts pass tests, are
committed, are pushed to `origin/main`, and the live GitHub SHA equals local
`HEAD`. From the repository root, execute this sequence:

```bash
.venv/bin/pytest -q tests/test_agent.py tests/test_arms.py tests/test_code_interpreter.py
.venv/bin/pytest -q tests/test_benchmark.py tests/test_comparison.py tests/test_audit.py tests/test_cli.py
.venv/bin/ruff check .
git diff --check
.venv/bin/pytest -q

git status --short
git add -u
git add benchmarks/protocols/qwen35-trace2skill-local-postopt16-v1.json \
  benchmarks/protocols/qwen35-trace2skill-local-postopt16-run-spec-v1.json \
  benchmarks/protocols/qwen35-trace2skill-local-postopt-v1.md
git diff --cached --check
git commit -m "Add v24 recovery and frozen postopt evaluation"
GIT_SSH_COMMAND='ssh -i /home/tongzeyuan/.ssh/id_ed25519_nixiakt -o IdentitiesOnly=yes' \
  git push origin main

published_local_sha="$(git rev-parse HEAD)"
published_remote_sha="$(GIT_SSH_COMMAND='ssh -i /home/tongzeyuan/.ssh/id_ed25519_nixiakt -o IdentitiesOnly=yes' \
  git ls-remote origin refs/heads/main | awk '{print $1}')"
test "$published_local_sha" = "$published_remote_sha"
test -z "$(git status --porcelain)"
```

Any code, prompt, skill, run-spec, or split change after this gate requires a
new tested commit, push, and live-SHA equality check before launch. Do not amend
or silently replace the published run-spec bytes.

## Preflight and one-shot launch

After the publication gate, verify the frozen split locally:

```bash
.venv/bin/sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --verify benchmarks/protocols/qwen35-trace2skill-local-postopt16-v1.json
```

Then query `/v1/models` without printing the credential and require the literal
model alias to be listed:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path

import httpx

base_url = "http://101.37.174.109:8010/v1"
model = "qwen36-35b-a3b"
key_path = Path("/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key")
key = key_path.read_text(encoding="utf-8").strip()
response = httpx.get(
    f"{base_url}/models",
    headers={"Authorization": f"Bearer {key}"},
    timeout=60,
)
response.raise_for_status()
available = {
    item.get("id")
    for item in response.json().get("data", [])
    if isinstance(item, dict)
}
assert model in available, f"frozen model alias unavailable: {model}"
print(model)
PY
```

Run the synthetic forced-tool round trip with the same provider and sampling
contract. This sends no SpreadsheetBench task and opens no workbook:

```bash
.venv/bin/sheet-harness doctor --online --tools \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --base-url http://101.37.174.109:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions \
  --reasoning-effort none \
  --request-timeout 700 \
  --request-retries 0 \
  --request-interval-seconds 0 \
  --litellm-timeout 600 \
  --temperature 1 \
  --top-p 1 \
  --seed 41 \
  --presence-penalty 2 \
  --top-k 40 \
  --min-p 0 \
  --repetition-penalty 1 \
  --disable-thinking
```

If availability, split verification, or the tool canary fails, do not switch
the model, endpoint, API protocol, decoding, or budget. Make no request for a
selected task. Diagnose the preflight separately and repeat the publication
gate after any code change.

Require the output path to be absent, then launch exactly once without
`--resume`:

The launcher claims the absent output without replacement. It uses
`renameat2(RENAME_NOREPLACE)` where supported and an exclusive `mkdir` claim
on filesystems that reject that flag; neither path can replace an existing run.

```bash
test ! -e benchmarks/results/qwen36-local-postopt-eval16-v3-bare-ours-v24-seed41

.venv/bin/sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-local-postopt16-v1.json \
  --run-spec benchmarks/protocols/qwen35-trace2skill-local-postopt16-run-spec-v1.json \
  --output benchmarks/results/qwen36-local-postopt-eval16-v3-bare-ours-v24-seed41 \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --arm bare --arm ours \
  --base-url http://101.37.174.109:8010/v1 \
  --model qwen36-35b-a3b \
  --api-protocol chat-completions \
  --reasoning-effort none \
  --request-timeout 700 \
  --request-retries 0 \
  --request-interval-seconds 0 \
  --litellm-timeout 600 \
  --temperature 1 \
  --top-p 1 \
  --seed 41 \
  --presence-penalty 2 \
  --top-k 40 \
  --min-p 0 \
  --repetition-penalty 1 \
  --disable-thinking \
  --max-model-calls 8 \
  --max-turns-per-arm 8 \
  --max-total-tokens 120000 \
  --max-output-tokens 4096 \
  --task-timeout 1200 \
  --arm-order-seed 20260812 \
  --circuit-breaker 3
```

## Audit, primary result, and cohort lifecycle

Run the read-only fresh-rescore audit after the one-shot command exits:

```bash
.venv/bin/sheet-harness benchmark audit \
  benchmarks/results/qwen36-local-postopt-eval16-v3-bare-ours-v24-seed41 \
  --dataset benchmarks/data/spreadsheetbench_verified_400
```

The primary result is the paired `ours - bare` end-to-end accuracy delta over
the complete 16-pair matrix. A known `model_execution_failure` is a completed
failure and remains in the denominator. An unknown delivery outcome, missing
arm-task, invalid audit, or incomplete matrix invalidates the preregistered
primary inference; it must not be converted into a known failure or removed
from the denominator. Also report each arm's pass count, completion and
end-to-end accuracy, completed-only accuracy, calls, tokens, elapsed time,
error taxonomy, paired discordance, exact McNemar p-value, and paired
stratified-bootstrap interval.

Inspecting the result for reporting does not authorize a rerun. If any observed
score, workbook, trace, failure, or trajectory from these 16 informs a prompt,
skill, tool, recovery, routing, or code change, the entire cohort immediately
becomes development/quarantine. It may still be reported as exploratory
development evidence, but it can never again confirm the changed method.

Any subsequent confirmation must freeze a new, disjoint cohort from the
remaining 111 tasks, in a new derivative manifest and run spec, before making
any inference request for those IDs. The new selection must preserve the
111-task reserve hash above and state a new deterministic selection rule. No
task substitution, selective rerun, or repeated testing of this cohort may be
used to obtain `ours > bare`.
