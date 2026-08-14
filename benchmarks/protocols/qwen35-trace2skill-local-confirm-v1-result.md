# Qwen service-alias local confirmation v25 result

This file records the immutable outcome of the one-shot run specified by
`qwen35-trace2skill-local-confirm-v1.md`. The local result directory remains
ignored because it contains workbooks and detailed trajectories; the hashes
below bind this report to those artifacts.

## Run identity

- publication commit: `4dd6ec58a5c58b10b275718d7d87795bc70495e4`
- run-spec SHA-256: `61ec4d37d0548e1be63ebf8619feb591d98ca78d7dce4d9d573886498ca74984`
- split SHA-256: `c5b878de7fef5367f1e2e771f413c6724e5d4ea0c9079e9c0e99fe6feab3dc22`
- comparison manifest SHA-256: `20acb63592ba15c87cd20188d43679d599f11d8725b8c3d19afe9726fb163707`
- results JSONL SHA-256: `32de97ae418cfc5f960355b339ab0348826142d1557f3f69290027a519353fba`
- runner summary SHA-256: `8c4aa3a6713b18126baa4c02c6f0d05ca7bd22385f09abaf6829e28c83c50bda`
- fresh-audit JSON SHA-256: `147d416bdf5938146166d287daedbbda7b9892cbfa9a67db2bcbebc3c2f995d9`
- UTC span: `2026-08-13T23:53:03.245585+00:00` through
  `2026-08-14T01:40:36.788708+00:00` (6,453.543 seconds)

The model availability check and synthetic forced-tool doctor passed before
launch. The compare command ran once without `--resume`, reached all 32
arm-tasks without tripping the circuit breaker, and exited with status 2.

## Audit decision

The preregistered primary inference is invalid. The fresh audit observed all
32 expected rows but accepted only 29:

```text
50683::ours:status_not_completed
57232::ours:status_not_completed
57989::ours:status_not_completed
```

All three rows are non-retryable `provider_task` errors. On the eighth and
final reserved submission call, the Relay returned HTTP 200 with
`finish_reason="length"` where a complete `submit_result` tool call required
`finish_reason="tool_calls"`. The fail-closed client did not execute partial
tool arguments. There are no missing or interrupted rows, but provider errors
make `audit_valid`, `study_complete`, `journal_integrity_valid`, and
`inference_valid` false under the frozen v25 contract.

This cohort is now development-quarantined. It must not be resumed, replayed,
or rerun after observing the outcome.

## Descriptive outcome

These values describe the recorded 16-task matrix; they are not a valid
confirmation estimate:

| Arm | Passed | Completed | Errors | End-to-end accuracy | Tokens | Logical calls | HTTP attempts | Elapsed sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bare` | 3/16 | 16 | 0 | 18.75% | 938,078 | 122 | 122 | 2,970.663 s |
| `ours` | 4/16 | 13 | 3 | 25.00% | 1,316,894 | 120 | 123 | 3,482.347 s |

The descriptive end-to-end difference is `+6.25` percentage points. Among the
13 completed pairs, there were zero `bare`-only passes and one `ours`-only
pass. Exact McNemar and bootstrap results are deliberately null because three
pairs contain provider errors. The four `bare` and one `ours` completed-false
model outcomes were all `edit_recovery_exhausted` and remain in their arm
denominators.

As a postmortem only, temporary copies of the three error artifacts were
recalculated and rescored; none passed. This observation does not repair the
invalid matrix and is not substituted into the frozen result.

## Interpretation

The only defensible performance statement is that this run descriptively
favored the harness by one additional task while consuming more tokens, but it
did not produce a valid confirmation estimate. It is not evidence that the
harness beats Trace2Skill, SpreadsheetAgent, or Spreadsheet-RL because their
models, dataset slices, seeds, backends, tool budgets, and execution protocols
are not equivalent.
