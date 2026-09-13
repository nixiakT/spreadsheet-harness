#!/usr/bin/env bash
set -euo pipefail

cd /data/zju-160/tongzeyuan/spreadsheet-harness

run_root="$(realpath benchmarks/results/deepseek-v4-flash-official-dsh-full-v2-20260913)"
runner="$(realpath benchmarks/run_deepseek_harness_spreadsheetbench_v2.py)"
log_root="$run_root/retry-supervisor"
mkdir -p "$log_root"

# The first full pass was already started before this supervisor.  Wait for
# that exact runner, rather than launching a duplicate full pass.
initial_pid="${INITIAL_DSH_RUNNER_PID:-896628}"
if [[ -d "/proc/$initial_pid" ]]; then
  while [[ -d "/proc/$initial_pid" ]]; do
    sleep 30
  done
fi

pass=1
while :; do
  pass_log="$log_root/pass-${pass}.log"
  printf '%s starting retry pass %d\n' "$(date -Is)" "$pass" >> "$pass_log"
  .venv/bin/python "$runner" \
    --dataset benchmarks/data/spreadsheetbench-v2 \
    --run-root "$run_root" \
    --parallelism 6 \
    --max-turns 50 \
    --task-timeout 5400 \
    --model DeepSeek-V4-Flash \
    --base-url http://10.130.138.46:8010/v1 \
    --api-key-file /tmp/spreadsheet-harness-litellm.key \
    --skill skills/spreadsheet-core/SKILL.md \
    --recalculate-before-evaluation >> "$pass_log" 2>&1 || true

  if .venv/bin/python - "$run_root" "$pass_log" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
log = Path(sys.argv[2])
statuses = []
for path in (root / "tasks").glob("*/status.json"):
    try:
        statuses.append(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        continue
retryable = [
    item for item in statuses
    if item.get("status") in {"failed", "timeout"}
    and int(item.get("model_requests") or 0) < int(item.get("max_turns") or 50)
]
terminal = [item for item in statuses if item.get("status") in {"completed", "turn_limit", "failed", "timeout"}]
summary = {
    "status_files": len(statuses),
    "terminal": len(terminal),
    "retryable": len(retryable),
    "completed": sum(item.get("status") == "completed" for item in statuses),
    "turn_limit": sum(item.get("status") == "turn_limit" for item in statuses),
    "failed": sum(item.get("status") == "failed" for item in statuses),
    "timeout": sum(item.get("status") == "timeout" for item in statuses),
}
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
if len(statuses) >= 321 and not retryable:
    raise SystemExit(0)
raise SystemExit(1)
PY
  then
    rc=0
  else
    rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    printf '%s retry supervisor complete\n' "$(date -Is)" >> "$pass_log"
    exit 0
  fi
  pass=$((pass + 1))
done
