#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

root="${RESULT_ROOT:?RESULT_ROOT is required}"
dataset="${DATASET:?DATASET is required}"
skill_base="${SKILL_ROOT_BASE:?SKILL_ROOT_BASE is required}"
base_url="${BASE_URL:-http://10.130.138.46:8010/v1}"
model="${MODEL:-DeepSeek-V4-Flash}"
task_spec="${TASK_IDS:?TASK_IDS is required}"
api_key_file="${API_KEY_FILE:-/tmp/spreadsheet-harness-litellm.key}"
mkdir -p "$root" "$root/.locks"
status_file="$root/batch-status.tsv"
if [[ ! -f "$status_file" ]]; then
  printf 'arm\ttask\tstatus\texit_code\n' > "$status_file"
fi
[[ -r "$api_key_file" ]] || {
  printf 'Missing readable API key file: %s\n' "$api_key_file" >&2
  exit 2
}

record_status() {
  local variant="$1" task="$2" status="$3" code="$4"
  (
    flock 9
    printf '%s\t%s\t%s\t%s\n' "$variant" "$task" "$status" "$code" >> "$status_file"
  ) 9>"$status_file.lock"
}

summary_complete() {
  local summary="$1"
  [[ -f "$summary" ]] || return 1
  .venv/bin/python - "$summary" <<'PY'
import json
import sys

try:
    document = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if document.get("study_complete") else 1)
PY
}

run_one() {
  local variant="$1" task="$2"
  local slug="${task//\//_}" category="${task%%/*}"
  local out="$root/$variant/$slug"
  mkdir -p "$root/$variant"
  exec {lock_fd}>"$root/.locks/${variant}-${slug}.lock"
  if ! flock -n "$lock_fd"; then
    record_status "$variant" "$task" locked 0
    return
  fi
  if summary_complete "$out/summary.json"; then
    record_status "$variant" "$task" skipped-complete 0
    return
  fi
  if [[ -e "$out" ]]; then
    local interrupted="${out}.interrupted.$(date +%Y%m%dT%H%M%S)"
    mv "$out" "$interrupted"
    if [[ -f "$out.log" ]]; then
      mv "$out.log" "${interrupted}.log"
    fi
  fi
  local skill_args=()
  if [[ "$variant" != h0d0 ]]; then
    skill_args=(--skill-root "$skill_base/$variant")
  fi
  if .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
      --dataset "$dataset" --category "$category" --task-id "$task" \
      --arm spreadsheet-harness-financial "${skill_args[@]}" \
      --output "$out" --max-model-calls 50 --max-turns-per-arm 50 \
      --max-total-tokens "${MAX_TOTAL_TOKENS:-unlimited}" \
      --max-output-tokens "${MAX_OUTPUT_TOKENS:-unlimited}" \
      --task-timeout "${TASK_TIMEOUT_SECONDS:-3600}" \
      --request-timeout "${REQUEST_TIMEOUT_SECONDS:-1800}" \
      --litellm-timeout "${LITELLM_TIMEOUT_SECONDS:-1800}" --request-retries 3 \
      --arm-order-seed 20260910 --base-url "$base_url" \
      --api-key-file "$api_key_file" --model "$model" \
      --api-protocol chat-completions --reasoning-effort medium --seed 41 \
      --temperature 0 --top-p 1 --enable-thinking \
      > "$out.log" 2>&1; then
    record_status "$variant" "$task" ok 0
  else
    local code=$?
    record_status "$variant" "$task" failed "$code"
  fi
}

read -r -a tasks <<< "$task_spec"
read -r -a arms <<< "${ARM_FILTER:-h0d0 h1d0 h0d1 h1d1}"
for variant in "${arms[@]}"; do
  for task in "${tasks[@]}"; do
    run_one "$variant" "$task"
  done
done
printf 'matrix complete: %s\n' "$status_file"
