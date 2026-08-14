# Qwen service-alias local v26 confirmation result

This file records the immutable outcome of the one-shot run specified by
`qwen35-trace2skill-local-v26-confirm-v1.md`. The local result directory remains
ignored because it contains workbooks and detailed trajectories; the hashes
below bind this report to those artifacts.

## Run identity

- published source commit:
  `9adefad0fe1fa346f1ab3ee42ab4e9e21c422cee`;
- run-spec SHA-256:
  `4bca7fe452c9ba2dadc31c374f29abcda575cb243e5f960789f2f50b4191884a`;
- split SHA-256:
  `5471aadfa319948fd60e97048f5f07aa39418195770c589085d7da63ed27cb61`;
- comparison manifest SHA-256:
  `bd714dd5f53b7411f2456a2fdbd42b4625e5651dc6e456c3012fe0a69aafccfa`;
- results JSONL SHA-256:
  `e1a1cbb0ecf9686dd8e11ce3171a121950c4293cf0113958aee973049d670f43`;
- runner summary SHA-256:
  `dadd686b6ed62b0576ba3c50c21cf1cc5ba46757ccece71c5784aced89a3a81e`;
- fresh-audit JSON SHA-256:
  `efa086639d821ffb7c09bdd902d26bf82b16be97f533e0abe98d99ea222d6b4d`;
- UTC span: `2026-08-14T03:42:55.741047+00:00` through
  `2026-08-14T05:05:22.364340+00:00` (4,946.623 seconds).

Before launch, the split verified, the literal `qwen36-35b-a3b` alias was
available, the online two-request tool compatibility probe passed, and an
additional synthetic final-route probe returned exactly `submit_result({})`.
The clean-source gate bound local `HEAD`, `origin/main`, and live GitHub
`main` to the published source commit. The comparison then ran exactly once,
without `--resume`, and exited naturally with status 0.

## Audit decision

The fresh audit accepted all 32 expected arm-task rows:

- `audit_valid`: true;
- `journal_integrity_valid`: true;
- `study_complete`: true;
- `inference_valid`: true;
- observed/valid rows: 32/32;
- missing, interrupted, provider-error, routing-error, and harness-error rows:
  zero.

All completed-false model outcomes remain in their arm denominators. The run
therefore supports the preregistered paired local comparison.

## Primary outcome

| Arm | Passed | End-to-end accuracy | Completed | Errors | Model execution failures | Tokens | Model calls | HTTP attempts | Elapsed sum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bare` | 8/16 | 50.00% | 16/16 | 0 | 1 | 898,916 | 120 | 120 | 2,668.730 s |
| `ours` | 11/16 | 68.75% | 16/16 | 0 | 2 | 1,461,911 | 133 | 133 | 2,277.353 s |

The paired end-to-end difference is `+18.75` percentage points, or three
additional passed workbooks for `ours`.

## Paired inference

- `ours`-only passes: 3;
- `bare`-only passes: 0;
- exact McNemar p-value: 0.25;
- Holm-adjusted p-value: 0.25;
- paired stratified bootstrap 95% interval for `ours - bare`:
  `[0.00, 37.50]` percentage points.

The point estimate favors the harness, but the small 16-task confirmation is
not powered to establish a conventional 5% significance claim.

## Completion taxonomy

The `bare` arm had one auditable `budget_exhausted` outcome caused by
`max_total_tokens`. The `ours` arm had one auditable `budget_exhausted`
outcome caused by `max_total_tokens` and one `edit_recovery_exhausted`
outcome. These three rows were observed, audit-valid completed-false model
outcomes rather than infrastructure errors. No retry, missing-row,
output-limit, timeout, provider, routing, sandbox, or harness failure occurred.

## Interpretation and stop decision

The preregistered local stop threshold was `ours >= 6/16`; `ours` achieved
`11/16`. Numerically, 68.75% also exceeds the cited 36.67% Trace2Skill
Qwen3.5-35B SpreadsheetBench-Verified figure, so no further development cohort
is opened and this v26 cohort must never be rerun or resumed.

This is not a protocol-equivalent paper-leaderboard or state-of-the-art claim.
The service checkpoint and serving recipe are not pinned, this cohort has only
16 tasks, and the tools, task slice, seeds, LibreOffice backend, and execution
protocol differ from the paper. A paper-level comparison still requires the
fixed full held-out split and preferably multiple preregistered seeds.
