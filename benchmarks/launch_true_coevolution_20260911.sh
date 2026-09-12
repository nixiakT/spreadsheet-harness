#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

result_root="${RESULT_ROOT:-benchmarks/results/true-coevolution-deepseekpro-fast-20260911}"
base_url="${BASE_URL:-http://10.130.138.46:8010/v1}"
api_key_file="${API_KEY_FILE:-/tmp/spreadsheet-harness-litellm.key}"
model="${MODEL:-DeepSeek-V4-Pro}"
parallelism="${PARALLELISM:-10}"

extra_args=()
if [[ "${FAST_GATE_AFTER_FIRST:-1}" == 1 ]]; then
  extra_args+=(--fast-gate)
fi
if [[ "${PREWARM_BASELINE_HELDOUT:-0}" == 1 ]]; then
  extra_args+=(--prewarm-baseline-heldout)
fi

[[ -r "$api_key_file" ]] || {
  echo "Missing readable API key file: $api_key_file" >&2
  exit 2
}

exec .venv/bin/python benchmarks/run_true_coevolution_20260911.py \
  --result-root "$result_root" \
  --base-url "$base_url" \
  --api-key-file "$api_key_file" \
  --model "$model" \
  --parallelism "$parallelism" \
  --task-attempts "${TASK_ATTEMPTS:-2}" \
  --candidate-attempts "${CANDIDATE_ATTEMPTS:-2}" \
  --gate-model-calls "${GATE_MODEL_CALLS:-50}" \
  --target-rounds "${TARGET_ROUNDS:-4}" \
  --heldout-limit "${HELDOUT_LIMIT:-0}" \
  --task-timeout "${TASK_TIMEOUT_SECONDS:-3600}" \
  --request-interval "${REQUEST_INTERVAL_SECONDS:-0.5}" \
  "${extra_args[@]}"
