#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
current_root="benchmarks/results/expanded-multi-plugin-validation-qwen36-attributed-glm52-attributed-20260915"
source_root="$current_root"
for round in 2 3; do
  while [[ ! -f "$current_root/report.json" ]]; do sleep 120; done
  if .venv/bin/python - "$current_root/report.json" <<'PY'
import json, math, sys
x = json.load(open(sys.argv[1], encoding="utf-8"))
arms = x.get("arms", {})
b = arms.get("h0d0", {})
be = b.get("exact", {}).get("mean", float("nan"))
bm = b.get("modification", {}).get("mean", float("nan"))
br = b.get("regression", {}).get("mean", float("nan"))
for name, value in arms.items():
    if name == "h0d0": continue
    e = value.get("exact", {}).get("mean", float("nan"))
    m = value.get("modification", {}).get("mean", float("nan"))
    r = value.get("regression", {}).get("mean", float("nan"))
    if all(math.isfinite(z) for z in (be, bm, br, e, m, r)) and e > be and m >= bm - 0.02 and r >= br - 0.02:
        raise SystemExit(0)
raise SystemExit(1)
PY
  then echo "qualifying improvement found in $current_root"; exit 0; fi
  cand="benchmarks/results/gpt56-sol-candidates-glm52-attributed-20260915-round${round}"
  next="benchmarks/results/expanded-multi-plugin-validation-qwen36-attributed-glm52-attributed-20260915-round${round}"
  env -u OPENAI_API_KEY CODEX_HOME=/tmp/codex-isolated-spreadsheet-20260915 .venv/bin/python benchmarks/run_gpt56_sol_candidate_generation_20260913.py --source "$source_root" --output "$cand" --api-key-file /tmp/spreadsheet-harness-litellm.key --base-url http://47.96.153.159:8010/v1 --model dashscope/glm-5.2 --limit 6 --balanced --request-timeout 600 >> "benchmarks/logs/gpt56-sol-candidates-glm52-attributed-20260915-round${round}.log" 2>&1
  SPREADSHEET_EVOLUTION_ROUTE_STRUCTURE=1 .venv/bin/python benchmarks/run_expanded_multi_plugin_validation_20260913.py --result-root "$next" --candidate-root "$cand" --model qwen36-35b-a3b --base-url http://47.96.153.159:8010/v1 --api-key-file /tmp/spreadsheet-harness-litellm.key --parallelism 4 --task-timeout 21600 --request-timeout 600 --request-retries 3 --request-interval 1.1 >> "benchmarks/logs/expanded-multi-plugin-validation-qwen36-attributed-20260915-round${round}.log" 2>&1
  source_root="$next"; current_root="$next"
done
echo "automatic rounds exhausted without a qualifying improvement"
