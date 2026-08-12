# Luna-low limited 30-task three-arm protocol v6

This is the credential- and transport-isolated successor to v5. It incorporates
the scientific design in `luna-low-three-arm-limited30-v5.md` (SHA-256
`82e6e51ca6643081197d7e32df1b3c530df923805ddb5934d7e55cd9f351bc15`)
except where this amendment is stricter. The v6 task file has the same frozen
30 IDs and SHA-256
`9f5e3f0f57d2840d4f531f03ee2febea94c10aadd6842354b18dd168430588d6`.
No v1-v5 result row may be resumed, imported, or resampled into v6.

## Why v5 was retired

The v5 gate attempted only `41691::bare`. Its first physical request received
an empty HTTP 408 after 328.881 seconds, with no SSE event or terminal event.
Delivery was ambiguous, so the client correctly suppressed replay and the
circuit breaker stopped the remaining arms. The v5 smoke is permanently a
failed diagnostic and has no benchmark score.

The same Relay key was also used by the interactive Codex session and the
server runner. Read-only key statistics continued to increase after the server
runner stopped, proving that v5 did not have the single-consumer condition it
claimed. The key was unbound to a dedicated OpenAI account and had no enforced
per-key concurrency or rate limit. That transport confound must not be repaired
by merely waiting and trying the same arm again.

## Blocking transport and credential gate

All conditions below are mandatory before any v6 model request:

1. The old Relay key and the SSH private key exposed in chat are revoked and
   replaced. A replacement secret must never be pasted into chat, committed,
   written to diagnostics, or passed as a command-line value.
2. The Relay is reached through a certificate-verified HTTPS URL. Plain HTTP,
   `--insecure`, and silent HTTP fallback are forbidden. The intended URL is
   `https://home.aaron-family.top/openai/v1`; it must pass TLS verification from
   `zju-57` before it is frozen into a run manifest.
3. Every reverse proxy, tunnel, gateway, and the Relay upstream timeout exceeds
   the client's 360-second request deadline. The operator must remove the
   observed roughly 300-second idle gateway boundary (recommended proxy
   read/send timeout: at least 700 seconds) and confirm the two empty 408s in
   gateway and upstream logs.
4. A new benchmark-only Relay key is stored as one UTF-8 line in
   `/home/tongzeyuan/.config/spreadsheet-harness/benchmark.key`, mode `0600`,
   owned by the benchmark user, and supplied only with `--api-key-file`.
5. A preflight compares the benchmark key with the interactive Codex credential
   in memory and rejects equality. The `--api-key-file` loader enforces this
   against both `OPENAI_API_KEY` and file-backed Codex auth before isolation,
   manifest creation, or any model request. An operator-side HTTPS self-stats
   check records only the non-secret Relay key ID, never a key value or
   reversible credential material.
6. Relay self-statistics must show the benchmark key active, bound to a
   dedicated OpenAI account, and limited to one concurrent request. The bound
   account and key have no other consumer for the entire gate and formal run.
7. The benchmark key and its bound account are quiet for at least 60 minutes.
   The only process allowed to make subsequent model requests is one comparison
   runner on `zju-57`. A side-channel increase not attributable to that runner
   invalidates the gate and stops collection.

These are operator-side preconditions. Client timeout increases, automatic
replay of HTTP 408, or repeated canary sampling are not substitutes.

## Harness integrity changes

The v6 source must include and test all of the following before deployment:

- owner-only `--api-key-file` input without secret-bearing argv or manifests;
- refusal to run the single-arm summary command on a comparison directory;
- fail-closed resume on duplicate arm-task rows;
- inference invalidation for duplicate, unknown-task, unknown-arm, or
  unexpected-arm rows;
- request-attempt audit marked complete only when every expected row exists;
- explicit `style_checked: false` and calculation-backend accounting.

The local and server source fingerprints, full test suite, Ruff, and
`git diff --check` must match before the gate.

## Fresh v6 gate

After the 60-minute quiet period, first run one transport-only, non-scored bare
canary on historical canary `493-18` in a fresh v6 directory. It must complete
with one unambiguous terminal response chain, certificate-verified HTTPS, no
unsafe retry, and a reopenable workbook. This is transport qualification, not
a benchmark observation.

Then run the three-arm smoke on `41691` in another fresh directory. It passes
only if all three arm-tasks complete and the routing, paper evidence, original
image attachment, LaTeX, read-only hashes, pacing, terminal tools, workbook
reopen, and fresh rescore checks all pass. Any HTTP 408, ambiguous delivery,
provider/routing/budget error, external key activity, or missing row retires
the entire v6 gate without replay.

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id 493-18 --arm bare \
  --output benchmarks/results/luna-low-transport-canary-493-18-v6 \
  --base-url https://home.aaron-family.top/openai/v1 \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 360 --request-retries 1 \
  --request-interval-seconds 20 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 1 --arm-order-seed 20260811

sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id 41691 \
  --output benchmarks/results/luna-low-three-arm-smoke-41691-v6 \
  --base-url https://home.aaron-family.top/openai/v1 \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 360 --request-retries 1 \
  --request-interval-seconds 20 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 1 --arm-order-seed 20260811
```

After a passing smoke, enforce another 60-minute quiet period before formal
collection. The smoke is not imported into the formal result.

## Formal limited run

Only a passing v6 gate authorizes this new 90-arm-task run:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id-file benchmarks/protocols/luna-low-three-arm-limited30-v6.txt \
  --output benchmarks/results/luna-low-three-arm-limited30-v6 \
  --base-url https://home.aaron-family.top/openai/v1 \
  --api-key-file /home/tongzeyuan/.config/spreadsheet-harness/benchmark.key \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 360 --request-retries 1 \
  --request-interval-seconds 20 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 1 --arm-order-seed 20260811
```

Run once, in one process, with no resume after an arm-task starts. The formal
report follows the v5 estimands and reports the complete paired 30-task matrix
plus the predeclared 24-task sensitivity analysis excluding `38703`, `41691`,
`423-16`, `493-18`, `54196`, and `84-40`. Inferential statistics are valid only
after a read-only audit reopens and freshly rescores every workbook and verifies
all manifest, route, artifact, hash, pacing, request, and row-integrity fields.
The 24-task summary must be computed with the full 30 tasks supplied as
`collection_tasks`; first require the full summary to be valid, then call
`comparison_summary(results, sensitivity_tasks, collection_tasks=all_30_tasks)`
so the six preregistered exclusions are not misclassified as unknown rows.

No full-398 run is authorized.
