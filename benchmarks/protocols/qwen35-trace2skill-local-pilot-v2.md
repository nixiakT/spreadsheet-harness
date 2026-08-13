# Qwen service-alias local exploratory pilot v2

This protocol freezes a 16-task development pilot for a paired comparison of
the same served model with two agent configurations:

- `bare`: deterministic workbook preview plus the local code interpreter.
- `ours`: the same served model plus the complete spreadsheet harness.

It does not reproduce any paper leaderboard. The current evaluation uses one
SpreadsheetBench-Verified workbook per agent, LibreOffice recalculation, and the
harness scorer. Original SpreadsheetBench used `solution_once_apply_n`, replayed
one generated Python program over sibling cases, recalculated with Excel COM,
and reported instruction-level Soft and Hard scores.

## Model and literature boundary

Trace2Skill reports `Qwen/Qwen3.5-35B-A3B` and
`Qwen/Qwen3.5-122B-A10B` as its main skill-author/skill-user checkpoints. The
relay name `qwen36-35b-a3b` is only a service alias unless the operator supplies
the upstream checkpoint revision, tokenizer, chat template, quantization,
serving revision, and generation configuration. Results must therefore be
reported under the literal service alias, not as a Trace2Skill checkpoint
reproduction.

Relevant method components are treated as design inspiration, not score
targets:

- SpreadsheetAgent (arXiv:2604.12282): localized structural exploration plus
  code, images, LaTeX, and YAML sketches.
- Spreadsheet-RL (arXiv:2605.22642): a code interpreter, explicit
  recalculation/readback, spreadsheet-native tools, and execution-grounded
  rewards.
- Trace2Skill (arXiv:2603.25158): trajectory-local lessons distilled into
  reusable skills and evaluated with a separate task split.

## Local exposure freeze

The v1 parent enumerates 198 usable tasks from pinned raw rows `[200, 400)`
after honoring the two release exclusions. Trace2Skill describes a nominal
200/200 split; this repository's pinned release and exclusion rule yield 198
usable tasks in the second half, so those counts are not interchangeable.

At repository revision `7af635617e8f78de34cd3cdbff9fec7e373f8ba5` and cutoff
`2026-08-13T15:50:24Z`, local substantive exposure was defined as either:

1. a task ID appearing in a result row or task-specific run directory; or
2. an otherwise unrun task ID named in a task-specific protocol list.

The first category contains 51 IDs and the second contains 4; their union of 55
is removed in parent order. The remaining 143 are labeled only
`locally_unattempted_and_not_substantively_selected_as_of_freeze`. Full-split
administrative enumeration is deliberately not substantive selection. This
label never means globally unseen, training-uncontaminated, or untouched.

The exposure calculation is backed by
`qwen35-trace2skill-local-exposure-evidence-v1.json`. It records the source
revision/tree, exact scan policy, hashes and counts for 17 ignored result
journals, a complete 10-file tracked protocol candidate inventory, the 6
matching tracked sources, and every derived ordered ID list. It is a committed
local attestation: a fresh clone can verify the snapshot and tracked protocol
files, but cannot independently recover the ignored raw result artifacts.

The immutable manifests are:

- `qwen35-trace2skill-heldout-v1.json`: 198-task pinned parent.
- `qwen35-trace2skill-local-unattempted-v2.json`: attested 143-task local pool.
- `qwen35-trace2skill-local-unattempted-pilot16-v2.json`: fixed 16-task pilot.

The pilot order was fixed before any request against these 16 IDs. Freezing the
pilot moves all 16 into development/quarantine immediately. Any timeout,
provider failure, or partial response still consumes that selected task and
does not permit substitution from the remaining 127 tasks. Once results are
observed, optimization may use only quarantined development tasks. The 127 are
only a locally attested, not-yet-selected reserve; they are not globally unseen
or guaranteed unprocessed. Any later confirmatory claim needs a separately
justified cohort and a new derivative manifest frozen before inference.

Verify and run in manifest order:

```bash
sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --verify benchmarks/protocols/qwen35-trace2skill-local-unattempted-pilot16-v2.json

sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-local-unattempted-pilot16-v2.json \
  --run-spec benchmarks/protocols/qwen35-trace2skill-local-pilot16-run-spec-v1.json \
  --output benchmarks/results/qwen36-local-pilot16-v2-bare-ours-v23-seed41 \
  --api-key-file /path/to/owner-only-benchmark.key \
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

The verifier pins sibling parent paths and bytes, rejects malformed or duplicate
JSON keys, rebuilds every recognized manifest from code-side anchors, and
returns the already-verified task order to the runner. The comparison manifest,
result rows, and fresh audit bind the verified split schema, manifest hash,
task count/order hash, and dataset hash. A split manifest cannot be combined
with task IDs, a task-ID file, offset, or limit.

The code-anchored run spec is the launch gate for this pilot. It freezes the
split provenance, endpoint, literal model alias, arms, decoding, budgets,
pacing, recalculation policy, skill hashes, and output path before any model
request. The credential path and credential remain host-local. A fresh launch
refuses an existing output path; a continuation requires explicit `--resume`
and exact manifest, run-spec, result-row, and source bindings.

Before each arm-task, the runner durably writes an in-flight marker and removes
it only after the terminal result row is fsynced. A surviving marker represents
a potentially delivered request with an unknowable outcome. It permanently
forbids replay of that arm-task, but does not require abandoning the comparison
directory: seal the affected arm-task first, then resume only the remaining
work.

## Recorded interruption and recovery

The initial formal v23 launch touched only `33157::ours`. Two model responses
completed and a third request was sent, but its delivery outcome cannot be
established. There is no terminal `results.jsonl` row for the arm-task, while
`.inflight-arm-task.json` durably identifies it. Consequently, `33157::ours`
must never be replayed or assigned a synthetic pass/fail result.

After the implementation revision has passed its tests, been committed and
pushed, and the live `origin/main` SHA has been verified equal to `HEAD`, seal
the interruption from that clean checkout with the frozen provider contract:

```bash
sheet-harness benchmark seal-interrupted \
  benchmarks/results/qwen36-local-pilot16-v2-bare-ours-v23-seed41 \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-local-unattempted-pilot16-v2.json \
  --run-spec benchmarks/protocols/qwen35-trace2skill-local-pilot16-run-spec-v1.json \
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

This command validates the frozen contract and source provenance but must not
make a provider/model request. It atomically records `33157::ours` as
`interrupted_unknown_outcome` with `passed = null` and replay forbidden, then
removes the live marker. After a successful seal and audit, rerun the exact
frozen `benchmark compare` command above with `--resume`; the same directory
may execute the other 31 arm-tasks, while `33157::ours` remains permanently
skipped.

The interruption makes the preregistered 16-pair primary analysis permanently
incomplete. Even if every remaining arm-task finishes, there can be at most 15
complete `bare`/`ours` pairs. Any analysis of those known outcomes is
descriptive/sensitivity analysis only, never the preregistered primary result;
the unknown outcome is counted as neither a failure nor a missing task.

## Frozen online configuration

Use Chat Completions with thinking disabled and record the endpoint only after
canonicalization by the harness. Use seed 41, temperature 1, top-p 1, presence
penalty 2, top-k 40, min-p 0, repetition penalty 1, no retries, at most 8 model
calls/turns per arm, 120,000 total tokens per arm, 4,096 output tokens per call,
1,200 seconds per task, a 700-second client timeout, a 600-second relay timeout,
and arm-order seed 20260812. Both arms receive the same task order and resource
limits. The API credential must be loaded from the private key file and must
not appear in commands, manifests, trajectories, logs, or reports.

Before the pilot, query model availability and run a synthetic forced-tool
round-trip canary. If either fails, do not silently switch model, protocol, or
generation settings.

## Reporting

Fresh-rescore the completed directory and report completion, end-to-end and
completed-only accuracy, model calls, input/output/total tokens, wall time,
provider/tool errors, per-pass resource use, paired discordance, exact McNemar
p-value, and the paired bootstrap interval. The estimand is `ours - bare` for
this exact served model and protocol. Cross-paper absolute scores are context,
not thresholds that this experiment can validly claim to exceed.
