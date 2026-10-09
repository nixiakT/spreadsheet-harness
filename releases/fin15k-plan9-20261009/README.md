# Fin15k plugin-plan9 release

This release contains the frozen baseline and nine **unevaluated** candidates:
`50/200/500 × h-only/d-only/joint`. The candidate source is distributed as a
baseline plus exact, hash-checked overlays so it does not depend on the
publisher's absolute paths. `CANDIDATES.md` describes the proposed mutations.

The bundle has no API keys, provider logs, benchmark answers, development
traces, or Python bytecode. The candidate status is `materialized-not-evaluated`;
offline validation is not a benchmark score and does not establish an
improvement.

## 1. Clone and install

```bash
git clone --branch release/fin15k-plan9-20261009 \
  git@github.com:nixiakT/spreadsheet-harness.git
cd spreadsheet-harness
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

The SpreadsheetBench v2 dataset and pinned evaluator are intentionally not
included in this release. Supply the same dataset directory with
`Debugging/`, `Financial_Model/`, and `Template/` (100/100/97 tasks), and the
pinned evaluator file:

```bash
export DATASET=/path/to/spreadsheetbench-v2
export EVALUATOR="$PWD/benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
```

## 2. Materialize and verify

Materialization is local and makes no model calls. Its output must be outside
the release directory.

```bash
python releases/fin15k-plan9-20261009/run.py materialize \
  --output /tmp/fin15k-plan9-materialized
python releases/fin15k-plan9-20261009/run.py verify \
  --bundle /tmp/fin15k-plan9-materialized --write
```

The verifier must report `offline-checks-passed-not-scored` and 9 candidates.

## 3. Provider key and optional canary

Put one provider key in an owner-only file; never put it in the command line or
repository:

```bash
umask 077
printf '%s\n' "$LITELLM_API_KEY" > /tmp/litellm.key
chmod 600 /tmp/litellm.key
```

Run the artificial two-cell canary first. It is not a SpreadsheetBench score:

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u all_proxy -u ALL_PROXY NO_PROXY='*' no_proxy='*' \
python releases/fin15k-plan9-20261009/run.py canary \
  --bundle /tmp/fin15k-plan9-materialized \
  --output /tmp/fin15k-plan9-canary \
  --model DeepSeek-V4-Flash \
  --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/litellm.key \
  --request-timeout 1800 --litellm-timeout 1800 --task-timeout 21600 \
  --request-retries 1 --max-model-calls 50 --max-output-tokens 32768 \
  --enable-thinking
```

## 4. Full paired run (20 workers)

The scheduler runs baseline plus all nine candidates: 297 tasks × 10 arms =
2970 cells. It uses 20 workers, a 6-hour task limit, 1800-second request and
LiteLLM limits, and at most one retry. Results must be written outside the
frozen bundle.

```bash
mkdir -p /tmp/fin15k-plan9-results
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u all_proxy -u ALL_PROXY NO_PROXY='*' no_proxy='*' \
python releases/fin15k-plan9-20261009/run.py run \
  --bundle /tmp/fin15k-plan9-materialized \
  --results /tmp/fin15k-plan9-results \
  --dataset "$DATASET" --official-evaluator "$EVALUATOR" \
  --python "$PWD/.venv/bin/python" \
  --model DeepSeek-V4-Flash \
  --base-url http://47.96.153.159:8010/v1 \
  --api-key-file /tmp/litellm.key \
  --workers 20 --task-timeout 21600 \
  --request-timeout 1800 --litellm-timeout 1800 \
  --request-retries 1 --max-model-calls 50 --max-turns 50 \
  --max-output-tokens 32768 --temperature 0 --top-p 1 \
  --enable-thinking
```

Use a new results directory for a new provider/model/configuration. To resume
the exact same binding after an interruption, add `--resume`; to inspect it:

```bash
python releases/fin15k-plan9-20261009/run.py status \
  --results /tmp/fin15k-plan9-results
```

Interpretation is deliberately strict: only
`status=completed` **and** `outcome_kind=scored` is `normal`. A finite score
after a provider failure is `provider_salvage`, not a normal completion;
`algorithm_failure` is not retried. Do not compare candidates or claim an
improvement until the baseline and candidate have full normal paired coverage.

To evaluate the 500-trace candidates first, add `--evidence-size 500`.
This selects baseline + 500-h-only/d-only/joint (1188 cells). `--workers 4`
is useful when checking a provider at reduced concurrency. This selects arms
without modifying any frozen artifact, composition, or evaluator.
