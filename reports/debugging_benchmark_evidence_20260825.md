# SpreadsheetBench-v2 Debugging frozen evidence (2026-08-25)

## Primary claim: untouched frozen cohort

The Embedded Hardcode repair policy was developed on `Debugging/02_02`. The frozen
cohort below is `Debugging/06_02` through `Debugging/10_02`; its golden workbooks
were not used to develop the policy. All rows use the official SpreadsheetBench-v2
paired evaluator, model `qwen36-35b-a3b`, policy `policy-ours 1.23.0`, and have a
successful independent fresh audit.

| Task | Bare modification | Ours modification | Bare regression | Ours regression |
|---|---:|---:|---:|---:|
| `Debugging/06_02` | 0.0000 | 0.0000 | 1.0000 | 1.0000 |
| `Debugging/07_02` | 0.0000 | 0.0000 | 1.0000 | 1.0000 |
| `Debugging/08_02` | 0.0000 | 0.0667 | 1.0000 | 1.0000 |
| `Debugging/09_02` | 0.0000 | 0.3750 | 1.0000 | 1.0000 |
| `Debugging/10_02` | 0.7021 | 0.7021 | 1.0000 | 1.0000 |
| **Mean** | **0.14042** | **0.22876** | **1.0000** | **1.0000** |

- Absolute modification gain: **+0.08834**
- Relative modification gain: **+62.91%**
- Regression change: **0.0000**
- Valid scored pairs: **5/5**

Evidence directories:

- `benchmarks/results/frozen-policy-v123-embedded-heldout5-paired-20260825`
- `benchmarks/results/frozen-policy-v123-embedded-08_02-paired-retry1`
- `benchmarks/results/frozen-policy-v123-embedded-10_02-paired-retry1`

Each directory has an `audit.json` with `audit_valid: true` and no audit reasons.
The single-task retry manifests have the same bare and ours composition hashes as
the five-task manifest. Provider failures in the first five-task attempt for the
bare arms of `08_02` and `10_02` were not scored; the table uses their valid fresh
paired retries.

## Six strict paired wins obtained during optimization

The project also reached the separate operational target of six cases with ours
strictly above bare and no regression decrease. These rows are spread across
successive policy versions, so they are supporting optimization evidence rather
than the untouched-cohort primary claim above.

| Task | Bare modification | Ours modification | Regression (bare / ours) | Fresh audit |
|---|---:|---:|---:|---|
| `Debugging/08_02` | 0.0000 | 0.0667 | 1.0000 / 1.0000 | valid |
| `Debugging/09_02` | 0.0000 | 0.3750 | 1.0000 / 1.0000 | valid |
| `Debugging/08_07` | 0.0000 | 0.0385 | 1.0000 / 1.0000 | valid |
| `Debugging/08_10` | 0.0000 | 0.4800 | 1.0000 / 1.0000 | valid |
| `Debugging/09_07` | 0.6667 | 0.9271 | 1.0000 / 1.0000 | valid |
| `Debugging/08_01` | 0.0000 | 0.8933 | 1.0000 / 1.0000 | valid |

Across these six rows, bare mean is **0.11112**, ours mean is **0.46343**, and the
relative gain is **+317.07%**. Fresh-audit evidence is in:

- `benchmarks/results/frozen-policy-v123-embedded-08_02-paired-retry1`
- `benchmarks/results/frozen-policy-v123-embedded-heldout5-paired-20260825`
- `benchmarks/results/frozen-policy-v124-target6-paired-20260825`
- `benchmarks/results/frozen-policy-v1242-repair2-paired-20260825`

## Verification notes

- Official evaluator SHA-256 in the audits:
  `04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0`.
- Provider errors and code-isolation failures are excluded from scoring, never
  converted to zero.
- Relevant non-integration tests pass, and Ruff passes. A full test invocation in
  the current managed host reports only environment failures: bubblewrap cannot
  create a loopback NETLINK socket, and LibreOffice cannot write the read-only
  user cache. These failures are outside the repair logic and did not affect the
  already completed fresh audits.
