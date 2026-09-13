#!/usr/bin/env bash
set -uo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

run_root="${RUN_ROOT:-benchmarks/results/deepseek-v4-flash-harness-financial-thinking-full-r16-20260913}"
task_root="$run_root/tasks"
log_root="$run_root/logs"
failed_root="$run_root/failed-attempts"
mkdir -p "$task_root" "$log_root" "$failed_root"

run_one() {
  local task="$1"
  local slug="${task//\//__}"
  local output="$task_root/$slug"
  local log="$log_root/$slug.log"

  source /data/zju-160/tongzeyuan/.config/litellm/lab.env
  unset OPENAI_API_KEY HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

  # Use the explicit DashScope model group. The unqualified alias is
  # intermittently rate-limited by LiteLLM even when this deployment is
  # healthy; keeping the provider prefix makes routing deterministic.
  .venv/bin/python -m spreadsheet_harness.cli benchmark v2-compare \
    --dataset benchmarks/data/spreadsheetbench-v2 \
    --category Financial_Model \
    --task-id "$task" \
    --arm spreadsheet-harness-financial \
    --output "$output" \
    --max-model-calls 50 \
    --max-turns-per-arm 50 \
    --max-total-tokens 10000000 \
    --max-output-tokens 32768 \
    --task-timeout 21600 \
    --arm-order-seed 20260908 \
    --base-url http://47.96.153.159:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --model dashscope/deepseek-v4-flash \
    --api-protocol chat-completions \
    --reasoning-effort medium \
    --temperature 0 \
    --top-p 1 \
    --request-timeout 700 \
    --litellm-timeout 600 \
    --request-retries 5 \
    --enable-thinking >"$log" 2>&1
}

if [[ "${1:-}" == "--one" ]]; then
  task="$2"
  slug="${task//\//__}"
  for attempt in 1 2 3; do
    run_one "$task"
    if .venv/bin/python - "$task_root/$slug/results.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    rows = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
valid = (
    len(rows) == 1
    and rows[0].get("outcome_kind") == "scored"
    and isinstance(rows[0].get("official_score"), dict)
)
raise SystemExit(0 if valid else 1)
PY
    then
      exit 0
    fi
    if [[ -d "$task_root/$slug" ]]; then
      mv "$task_root/$slug" "$failed_root/${slug}__attempt_${attempt}__pid_$$"
    fi
    if [[ -f "$log_root/$slug.log" ]]; then
      mv "$log_root/$slug.log" "$failed_root/${slug}__attempt_${attempt}__pid_$$.log"
    fi
  done
  exit 1
fi

.venv/bin/python - "$task_root" "${SKIP_TASKS:-}" <<'PY' | xargs -P "${PARALLELISM:-8}" -n 1 env RUN_ROOT="${RUN_ROOT:-benchmarks/results/deepseek-v4-flash-harness-financial-thinking-full-r16-20260913}" bash benchmarks/run_financial_latest_r16_20260913.sh --one
import json
import sys
from pathlib import Path

task_root = Path(sys.argv[1])
skip_tasks = {item.strip() for item in sys.argv[2].split(",") if item.strip()}
for item in json.loads(Path("benchmarks/data/spreadsheetbench-v2/Financial_Model/dataset.json").read_text()):
    task_id = "Financial_Model/" + item["id"]
    if task_id in skip_tasks:
        continue
    result = task_root / task_id.replace("/", "__") / "results.json"
    try:
        rows = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rows = []
    valid = (
        len(rows) == 1
        and rows[0].get("outcome_kind") == "scored"
        and isinstance(rows[0].get("official_score"), dict)
    )
    if not valid:
        print(task_id)
PY
