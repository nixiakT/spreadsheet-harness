#!/usr/bin/env bash
set -euo pipefail
repo_root="/data/zju-160/tongzeyuan/spreadsheet-harness"
python_bin="$repo_root/.venv/bin/python"
dataset="$repo_root/benchmarks/data/spreadsheetbench_912_v0.1"
output="${1:?output directory required}"
workers="${V1_WORKERS:-8}"
base_url="http://10.130.138.46:8010/v1"
model="DeepSeek-V4-Flash"
mkdir -p "$output/shards" "$output/logs"
"$python_bin" - "$dataset" "$output" "$workers" <<'PY'
import json, sys
from pathlib import Path
dataset, output, workers = sys.argv[1:]
rows = json.loads((Path(dataset) / "dataset.json").read_text())
all_ids = [str(x["id"]) for x in rows]
old = Path("benchmarks/results/spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915/task_ids.txt")
seen = {x.strip() for x in old.read_text().splitlines() if x.strip()}
ids = [x for x in all_ids if x not in seen]
assert len(ids) == 712, len(ids)
root = Path(output)
root.joinpath("task_ids.txt").write_text("\n".join(ids) + "\n")
for i in range(int(workers)):
    root.joinpath("shards", f"worker-{i+1}.ids").write_text("\n".join(ids[i::int(workers)]) + "\n")
PY
run_worker() {
  local i="$1" shard="$output/shards/worker-$1.ids" out="$output/shards/worker-$1"
  local args=()
  while IFS= read -r id; do [[ -n "$id" ]] && args+=(--task-id "$id"); done < "$shard"
  "$python_bin" -m spreadsheet_harness.cli benchmark v1-compare --dataset "$dataset" --output "$out" --arm bare "${args[@]}" \
    --max-model-calls 50 --max-turns-per-arm 50 --max-total-tokens 10000000 --max-output-tokens 8192 --task-timeout 21600 --arm-order-seed 20260915 \
    --base-url "$base_url" --api-key-file /tmp/spreadsheet-harness-litellm.key --model "$model" --api-protocol chat-completions \
    --reasoning-effort medium --seed 41 --temperature 0 --top-p 1 --enable-thinking --request-timeout 1800 --litellm-timeout 1800 --request-retries 5 \
    > "$output/logs/worker-$1.log" 2>&1
}
for i in $(seq 1 "$workers"); do run_worker "$i" & done
wait
