#!/usr/bin/env zsh
set -euo pipefail
cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

root="${RESULT_ROOT:-benchmarks/results/coevolution-validation-minimax-m27-highspeed-thinking-20260909}"
skill_base="${SKILL_ROOT_BASE:-tmp/coevolution-roots}"
dataset="${DATASET:-benchmarks/data/spreadsheetbench-v2}"
base_url="${BASE_URL:-http://47.96.153.159:8010/v1}"
model="${MODEL:-MiniMax-M2.7-highspeed}"
reasoning_effort="${REASONING_EFFORT:-medium}"
temperature="${TEMPERATURE:-0}"
top_p="${TOP_P:-1}"
seed="${SEED:-41}"
max_calls="${MAX_MODEL_CALLS:-50}"
max_turns="${MAX_TURNS_PER_ARM:-50}"
max_tokens="${MAX_TOTAL_TOKENS:-unlimited}"
max_output="${MAX_OUTPUT_TOKENS:-unlimited}"
task_timeout="${TASK_TIMEOUT_SECONDS:-3600}"
request_timeout="${REQUEST_TIMEOUT_SECONDS:-1800}"
litellm_timeout="${LITELLM_TIMEOUT_SECONDS:-1800}"
mkdir -p "$root"
status_file="$root/batch-status.tsv"
print -r -- $'arm\ttask\tstatus\texit_code' > "$status_file"
failure_count=0
if [[ -n "${TASK_IDS:-}" ]]; then
  task_spec="$TASK_IDS"
elif [[ "$dataset" == *"SpreadsheetBench-v2-enhanced-Financial_Model-1565.tar.gz" ]]; then
  task_spec="Financial_Model/fina_Fina_dam_12ec3ea6c4_fcff2st_c0 Financial_Model/fina_Fina_dam_1810097396_fcfe3st_c0 Financial_Model/fina_Fina_dam_20a0c66af0_riskchecker_c0 Financial_Model/fina_Fina_dam_262360e84d_ddm2st_c0 Financial_Model/fina_Fina_dam_2cc0046ebc_capstru_c0 Financial_Model/fina_Fina_dam_315efb8f32_fcffneg_c0"
elif [[ "$dataset" == *"SpreadsheetBench-v0.6-Financial_Model-269.tar.gz" ]]; then
  task_spec="Financial_Model/fina_Debu_01_c0 Financial_Model/fina_Debu_02_c0 Financial_Model/fina_Debu_03_c0 Financial_Model/fina_Debu_03_c1 Financial_Model/fina_Debu_04_c0 Financial_Model/fina_Debu_06_c1"
else
  task_spec="Financial_Model/01_01 Financial_Model/01_02 Financial_Model/02_02 Financial_Model/05_04 Financial_Model/13_05 Financial_Model/16_04"
fi
tasks=(${=task_spec})

for spec in h0d0:h0d0 h1d0:h1d0 h0d1:h0d1 h1d1:h1d1; do
  variant="${spec%%:*}"; label="${spec#*:}"
  skill_root="$skill_base/$variant"
  skill_args=()
  if [[ "$variant" != h0d0 ]]; then
    skill_args=(--skill-root "$skill_root")
  fi
  mkdir -p "$root/$label"
  for task in "${tasks[@]}"; do
    slug="${task//\//_}"; category="${task%%/*}"
    if .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
        --dataset "$dataset" --category "$category" --task-id "$task" \
        --arm spreadsheet-harness-financial "${skill_args[@]}" \
        --output "$root/$label/$slug" --max-model-calls "$max_calls" --max-turns-per-arm "$max_turns" \
        --max-total-tokens "$max_tokens" --max-output-tokens "$max_output" --task-timeout "$task_timeout" \
        --request-timeout "$request_timeout" --litellm-timeout "$litellm_timeout" --request-retries 2 \
        --arm-order-seed 20260904 --base-url "$base_url" \
        --api-key-file /tmp/spreadsheet-harness-litellm.key --model "$model" \
        --api-protocol chat-completions --reasoning-effort "$reasoning_effort" --seed "$seed" \
        --temperature "$temperature" --top-p "$top_p" --enable-thinking \
        > "$root/$label/$slug.log" 2>&1; then
      print -r -- "${label}"$'\t'"${task}"$'\t'ok$'\t'0 >> "$status_file"
    else
      exit_code=$?
      failure_count=$((failure_count + 1))
      print -r -- "${label}"$'\t'"${task}"$'\t'failed$'\t'"${exit_code}" >> "$status_file"
    fi
  done
done
echo "coevolution validation complete: $failure_count task runs failed; see $status_file"
exit "$((failure_count > 0))"
