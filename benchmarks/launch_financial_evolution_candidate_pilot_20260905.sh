#!/usr/bin/env zsh
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness
source /data/zju-160/tongzeyuan/.config/litellm/lab.env
unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

root="${RESULT_ROOT:-benchmarks/results/financial-evolution-candidate-qwen36-thinking-20260909}"
skill_root="${SKILL_ROOT:-tmp/evolution-financial-v1/skills}"
dataset="${DATASET:-benchmarks/data/spreadsheetbench-v2}"
base_url="${BASE_URL:-http://47.96.153.159:8010/v1}"
model="${MODEL:-qwen36-35b-a3b}"
reasoning_effort="${REASONING_EFFORT:-medium}"
temperature="${TEMPERATURE:-0}"
top_p="${TOP_P:-1}"
seed="${SEED:-41}"
max_calls="${MAX_MODEL_CALLS:-50}"
max_turns="${MAX_TURNS_PER_ARM:-50}"
max_tokens="${MAX_TOTAL_TOKENS:-500000}"
max_output="${MAX_OUTPUT_TOKENS:-unlimited}"
mkdir -p "$root"

if [[ -n "${TASK_IDS:-}" ]]; then
  task_spec="$TASK_IDS"
elif [[ "$dataset" == *"SpreadsheetBench-v2-enhanced-Financial_Model-1565.tar.gz" ]]; then
  task_spec="Financial_Model/fina_Fina_dam_12ec3ea6c4_fcff2st_c0 Financial_Model/fina_Fina_dam_1810097396_fcfe3st_c0 Financial_Model/fina_Fina_dam_20a0c66af0_riskchecker_c0 Financial_Model/fina_Fina_dam_262360e84d_ddm2st_c0 Financial_Model/fina_Fina_dam_2cc0046ebc_capstru_c0"
else
  task_spec="Financial_Model/01_01 Financial_Model/01_02 Financial_Model/02_02 Financial_Model/04_01 Financial_Model/09_01"
fi
tasks=(${=task_spec})
for task in "${tasks[@]}"; do
  slug="${task//\//_}"
  category="${task%%/*}"
    .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset "$dataset" --category "$category" --task-id "$task" \
    --arm bare --arm spreadsheet-harness-basic --arm spreadsheet-harness-financial \
    --skill-root "$skill_root" --output "$root/$slug" \
    --max-model-calls "$max_calls" --max-turns-per-arm "$max_turns" --max-total-tokens "$max_tokens" \
    --max-output-tokens "$max_output" --task-timeout 1800 --request-timeout 600 \
    --litellm-timeout 600 --request-retries 2 --arm-order-seed 20260904 \
    --base-url "$base_url" \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model "$model" --api-protocol chat-completions \
    --reasoning-effort "$reasoning_effort" --seed "$seed" \
    --temperature "$temperature" --top-p "$top_p" --enable-thinking \
    > "$root/$slug.log" 2>&1
done
echo "financial evolution candidate pilot complete"
