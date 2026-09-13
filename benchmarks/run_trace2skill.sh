#!/usr/bin/env bash
# Unified Trace2Skill SpreadsheetBench launcher.
#
# API settings are intentionally kept here so experiments can be launched
# without sourcing/exporting an environment file. Edit the values below when
# switching providers.

set -euo pipefail

REPO_ROOT="/data/zju-160/tongzeyuan/spreadsheet-harness"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"

# ---- Edit these three values for your provider ---------------------------
API_BASE_URL="http://10.130.138.46:8010/v1"
API_KEY="${API_KEY:?API_KEY is required (set it in the environment)}"
DEFAULT_MODEL="deepseek-v4-flash"
# ---------------------------------------------------------------------------

VERSION="v1"
DATASET=""
OUTPUT=""
MODEL="$DEFAULT_MODEL"
SKILLS_DIR="$REPO_ROOT/tmp/paper_repos/Trace2Skill/spreadsheet_agent/skills"
# DeepSeek/LiteLLM generation settings.  This is deliberately inline so the
# launcher has one place to edit provider and sampling configuration.
GENERATION_CONFIG='{"max_tokens":8192,"temperature":0.0,"top_p":1.0,"extra_body":{"enable_thinking":true}}'
AGENT="cli_skill_preloaded"
LLM_CLIENT="openai"
SEED="41"
WORKERS="1"
MAX_TURNS="50"
START_IDX="0"
END_IDX=""
INSTANCE_IDS=""
SAMPLE=""
TEMPERATURE="0.0"
SKIP_EVAL=0
VERBOSE=0
MISSING_ONLY=0

usage() {
  cat <<'EOF'
Usage:
  benchmarks/run_trace2skill.sh [options]
  benchmarks/run_trace2skill.sh status [--output PATH] [--eval-progress]

Options:
  --version v1|v2       Benchmark version; selects default dataset (v1)
  --dataset PATH        Dataset directory or JSON/JSONL file
  --output PATH         Experiment root directory (also used by status)
  --model NAME          Model name sent to the provider
  --skills-dir PATH     Trace2Skill skill root
  --generation-config X JSON file or inline JSON generation config
  --agent NAME          cli_skill_preloaded (default) or cli_only
  --llm-client NAME     openai (default) or api_chat
  --seed N              Explicit seed (default: 41)
  --workers N           Parallel workers (default: 1)
  --max-turns N         Maximum turns per workbook (default: 50)
  --start-idx N         First dataset row, inclusive (default: 0)
  --end-idx N           Dataset row, exclusive (default: all)
  --instance-ids IDS    Comma-separated IDs; takes precedence over index range
  --sample N             Run only the first N selected instances
  --temperature X       Sampling temperature (default: 0.0)
  --skip-eval            Do not run the automatic evaluator
  --missing-only         Resume only instances without complete outputs
  --verbose              Print verbose runner output
  -h, --help             Show this help

Status:
  status                  Show progress for the specified/latest experiment
  --output PATH           Experiment root to inspect (default: latest run)

Examples:
  # v1 smoke, first three instructions
  benchmarks/run_trace2skill.sh --version v1 --end-idx 3

  # v2, selected tasks, four workers
  benchmarks/run_trace2skill.sh --version v2 --instance-ids Debugging/01_01,Debugging/01_02 --workers 4

After editing API_BASE_URL/API_KEY at the top, no API-related export is needed.
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }

status_main() {
  local status_output=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output) status_output="${2:?missing value for status --output}"; shift 2 ;;
      --eval-progress) status_eval=1; shift ;;
      -h|--help) usage; return 0 ;;
      *) die "unknown status option: $1" ;;
    esac
  done

  if [[ -z "$status_output" ]]; then
    # Pick the most recently modified experiment, rather than the
    # lexicographically last directory (which may be an older v2 run).
    newest_mtime=0
    while IFS= read -r candidate; do
      candidate_mtime="$(stat -c %Y "$candidate" 2>/dev/null || echo 0)"
      if [[ "$candidate_mtime" -ge "$newest_mtime" ]]; then
        newest_mtime="$candidate_mtime"
        status_output="$candidate"
      fi
    done < <(find "$REPO_ROOT/benchmarks/results" -mindepth 1 -maxdepth 1 \
      -type d -name 'trace2skill-*' -print 2>/dev/null)
  fi
  [[ -n "$status_output" ]] || die "no trace2skill experiment directory found"
  [[ -d "$status_output" ]] || die "experiment directory not found: $status_output"

  "$PYTHON_BIN" - "$status_output" "${status_eval:-0}" "$REPO_ROOT" <<'PY'
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1]).resolve()
run_progress_eval = bool(int(sys.argv[2]))
repo_root = Path(sys.argv[3]).resolve()
outputs = root / "outputs"
runner_file = root / "runner_results.json"
eval_file = root / "eval_official_results.json"
config_file = root / "trace2skill_run_config.json"

output_files = sorted(outputs.rglob("*_output.xlsx")) if outputs.exists() else []

def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

runner = load_json(runner_file) if runner_file.exists() else None
evaluation = load_json(eval_file) if eval_file.exists() else None
progress_evaluation_file = root / "eval_progress_results.json"
progress_evaluation = load_json(progress_evaluation_file) if progress_evaluation_file.exists() else None
config = load_json(config_file) or {}
if not config:
    # Backward-compatible fallback for runs started before launch metadata was
    # added: recover the essential fields from the live runner command line.
    try:
        ps = subprocess.run(["pgrep", "-af", "run_spreadsheetbench.py"], capture_output=True, text=True)
        line = next((x for x in ps.stdout.splitlines() if str(root) in x or root.name in x), "")
        import shlex
        argv = shlex.split(line.split(" ", 1)[1]) if " " in line else []
        def arg(name, default=None):
            return argv[argv.index(name) + 1] if name in argv else default
        config = {"dataset": arg("--data_path"), "workers": int(arg("--workers", "1")),
                  "start_idx": int(arg("--start_idx", "0")),
                  "end_idx": (int(arg("--end_idx")) if arg("--end_idx") else None)}
    except Exception:
        config = {}

process_running = False
try:
    ps = subprocess.run(["pgrep", "-af", "run_spreadsheetbench.py"], capture_output=True, text=True)
    process_running = any(
        "run_spreadsheetbench.py" in line
        and (str(root) in line or root.name in line)
        for line in ps.stdout.splitlines()
    )
except OSError:
    pass

mtime_paths = [p for p in root.rglob("*") if p.is_file()]
latest = max((p.stat().st_mtime for p in mtime_paths), default=root.stat().st_mtime)
latest_text = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S")
idle_seconds = max(0, int(datetime.now().timestamp() - latest))

if process_running:
    run_state = "RUNNING"
elif evaluation is not None:
    run_state = "COMPLETED"
elif runner is not None:
    run_state = "RUNNER_FINISHED（评分未完成）"
else:
    run_state = "STOPPED/INTERRUPTED（进程已退出且 runner_results 未生成）"

print(f"运行状态: {run_state}")
print(f"实验: {root.name}（模型: {config.get('model', 'unknown')}）")

# Per-worker table. Trace2Skill's runner distributes the selected instances
# round-robin, so worker N owns positions N, N+workers, ... . A task is
# considered complete when every available input case has an output workbook.
workers = int(config.get("workers") or 1)
selected_ids = []
dataset = config.get("dataset")
if dataset:
    try:
        dataset_file = Path(dataset)
        if dataset_file.is_dir():
            dataset_file = dataset_file / "dataset.json"
        rows = json.loads(dataset_file.read_text(encoding="utf-8"))
        start = int(config.get("start_idx") or 0)
        end = config.get("end_idx")
        selected_ids = [str(row["id"]) for row in rows[start:(int(end) if end is not None else None)]]
    except Exception:
        selected_ids = []

task_done = {}
task_success = {}
task_case_progress = {}
if selected_ids:
    dataset_root = Path(dataset)
    if dataset_root.is_file():
        dataset_root = dataset_root.parent
    for task_id in selected_ids:
        expected_cases = len(list((dataset_root / "spreadsheet" / task_id).glob("*_input.xlsx")))
        if expected_cases == 0:
            expected_cases = len(list((dataset_root / task_id).glob("*_input.xlsx")))
        started_cases = len(list((root / "work").glob(f"{task_id}_*/input.xlsx")))
        final_output_dir = root / "outputs" / "spreadsheet" / task_id
        case_outputs = len(list(final_output_dir.glob("*_output.xlsx")))
        # Each sibling case gets its own work directory. A task is execution-
        # successful only when every expected case has an output workbook.
        # Do not infer this from the markdown log: the runner reuses the same
        # instance_id log filename for sibling cases, so the last log would
        # otherwise hide earlier failures.
        task_done[task_id] = expected_cases > 0 and case_outputs >= expected_cases
        task_success[task_id] = task_done[task_id]
        task_case_progress[task_id] = (min(case_outputs, expected_cases), expected_cases, started_cases)
    completed_tasks = sum(task_done.values())
    total_cases = sum(item[1] for item in task_case_progress.values())
    produced_cases = sum(item[0] for item in task_case_progress.values())
    print(f"任务: {completed_tasks}/{len(selected_ids)} 已完成，剩余 {len(selected_ids) - completed_tasks}")
    print(f"case: {produced_cases}/{total_cases} 已产出，剩余 {total_cases - produced_cases}")
else:
    print("任务: 无法读取数据集进度")
    print(f"case: 已生成 {len(output_files)} 个 workbook")

score_result = evaluation or progress_evaluation
if isinstance(score_result, dict):
    summary = score_result.get("summary", score_result)
    scored_tasks = int(summary.get("total_instances", 0))
    print(f"正确率（已评分 {scored_tasks} 个 task）: "
          f"Hard {summary.get('avg_hard_score', 0):.1%}，"
          f"Soft {summary.get('avg_soft_score', 0):.1%}，"
          f"case {summary.get('test_case_accuracy', 0):.1%}")
else:
    print("正确率: 暂无阶段性评分")

if run_progress_eval:
    progress_file = root / "eval_progress_results.json"
    cmd = [sys.executable, str(repo_root / "tmp/paper_repos/Trace2Skill/evaluate_with_official.py"),
           "--data_path", str(dataset), "--output_dir", str(outputs),
           "--start_idx", str(config.get("start_idx") or 0), "--completed-only",
           "--results_file", str(progress_file)]
    if config.get("end_idx") is not None:
        cmd += ["--end_idx", str(config["end_idx"])]
    print("\n增量官方评分（仅完整 task，不含未完成 task）:")
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            print(f"  评分失败: {detail[-1] if detail else 'unknown error'}")
        else:
            progress = load_json(progress_file) or {}
            summary = progress.get("summary", {})
            total = int(summary.get("total_instances", 0))
            correct = int(summary.get("fully_correct_instances", 0))
            cases = int(summary.get("total_test_cases", 0))
            passed_cases = int(summary.get("passed_test_cases", 0))
            print(f"  已评分 task: {total}，Hard 正确: {correct}/{total} "
                  f"({summary.get('avg_hard_score', 0):.1%})")
            print(f"  test case: {passed_cases}/{cases} "
                  f"({summary.get('test_case_accuracy', 0):.1%})")
            print(f"  Soft score: {summary.get('avg_soft_score', 0):.1%}")
            print(f"  结果文件: {progress_file}")
            print("  注：这是当前完整 task 的阶段性结果，样本仍较少，最终结果会变化。")
    except Exception as exc:
        print(f"  评分失败: {exc}")
PY
}

# The status subcommand does not need API credentials or a dataset.
if [[ "${1:-}" == "status" ]]; then
  shift
  status_main "$@"
  exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:?missing value for --version}"; shift 2 ;;
    --dataset) DATASET="${2:?missing value for --dataset}"; shift 2 ;;
    --output) OUTPUT="${2:?missing value for --output}"; shift 2 ;;
    --model) MODEL="${2:?missing value for --model}"; shift 2 ;;
    --skills-dir) SKILLS_DIR="${2:?missing value for --skills-dir}"; shift 2 ;;
    --generation-config) GENERATION_CONFIG="${2:?missing value for --generation-config}"; shift 2 ;;
    --agent) AGENT="${2:?missing value for --agent}"; shift 2 ;;
    --llm-client) LLM_CLIENT="${2:?missing value for --llm-client}"; shift 2 ;;
    --seed) SEED="${2:?missing value for --seed}"; shift 2 ;;
    --workers) WORKERS="${2:?missing value for --workers}"; shift 2 ;;
    --max-turns) MAX_TURNS="${2:?missing value for --max-turns}"; shift 2 ;;
    --start-idx) START_IDX="${2:?missing value for --start-idx}"; shift 2 ;;
    --end-idx) END_IDX="${2:?missing value for --end-idx}"; shift 2 ;;
    --instance-ids) INSTANCE_IDS="${2:?missing value for --instance-ids}"; shift 2 ;;
    --sample) SAMPLE="${2:?missing value for --sample}"; shift 2 ;;
    --temperature) TEMPERATURE="${2:?missing value for --temperature}"; shift 2 ;;
    --skip-eval) SKIP_EVAL=1; shift ;;
    --missing-only) MISSING_ONLY=1; shift ;;
    --verbose) VERBOSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

[[ "$VERSION" == "v1" || "$VERSION" == "v2" ]] || die "--version must be v1 or v2"
[[ -x "$PYTHON_BIN" ]] || die "Python environment not found: $PYTHON_BIN"
[[ "$API_KEY" != "REPLACE_WITH_YOUR_API_KEY" ]] || die "edit API_KEY at the top of this script first"

if [[ -z "$DATASET" ]]; then
  if [[ "$VERSION" == "v1" ]]; then
    DATASET="$REPO_ROOT/benchmarks/data/spreadsheetbench_912_v0.1"
  else
    DATASET="$REPO_ROOT/tmp/trace2skill_spreadsheetbench_v2_90"
  fi
fi

if [[ -z "$OUTPUT" ]]; then
  timestamp="$(date +%Y%m%d_%H%M%S)"
  OUTPUT="$REPO_ROOT/benchmarks/results/trace2skill-${VERSION}-${timestamp}"
fi

[[ -e "$DATASET" ]] || die "dataset not found: $DATASET"
[[ -d "$SKILLS_DIR" ]] || die "skills directory not found: $SKILLS_DIR"
mkdir -p "$OUTPUT" "$OUTPUT/outputs" "$OUTPUT/work" "$OUTPUT/logs"

# Save launch metadata for status reporting and later reproducibility.
"$PYTHON_BIN" - "$OUTPUT/trace2skill_run_config.json" <<PY
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
  "version": "$VERSION", "dataset": "$DATASET", "output": "$OUTPUT",
  "model": "$MODEL", "max_turns": int("$MAX_TURNS"),
  "temperature": float("$TEMPERATURE"), "generation_config": json.loads('''$GENERATION_CONFIG'''),
  "workers": int("$WORKERS"), "start_idx": int("$START_IDX" or 0),
  "end_idx": int("$END_IDX") if "$END_IDX" else None,
  "instance_ids": "$INSTANCE_IDS", "sample": int("$SAMPLE") if "$SAMPLE" else None,
}, indent=2), encoding="utf-8")
PY

# These variables are scoped to this script and its child processes only.
export OPENAI_API_KEY="$API_KEY"
export OPENAI_BASE_URL="$API_BASE_URL"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

cmd=(
  "$PYTHON_BIN" "$REPO_ROOT/tmp/paper_repos/Trace2Skill/run_spreadsheetbench.py"
  --data_path "$DATASET"
  --output_dir "$OUTPUT/outputs"
  --working_dir "$OUTPUT/work"
  --agent "$AGENT"
  --skills_dir "$SKILLS_DIR"
  --model "$MODEL"
  --llm_client "$LLM_CLIENT"
  --max_turns "$MAX_TURNS"
  --temperature "$TEMPERATURE"
  --seeds "$SEED"
  --workers "$WORKERS"
  --generation_config "$GENERATION_CONFIG"
  --log_dir "$OUTPUT/logs"
  --log_format markdown
  --results_file "$OUTPUT/runner_results.json"
)
if [[ -n "$START_IDX" ]]; then cmd+=(--start_idx "$START_IDX"); fi
if [[ -n "$END_IDX" ]]; then cmd+=(--end_idx "$END_IDX"); fi
if [[ -n "$INSTANCE_IDS" ]]; then cmd+=(--instance_ids "$INSTANCE_IDS"); fi
if [[ -n "$SAMPLE" ]]; then cmd+=(--sample "$SAMPLE"); fi
if (( MISSING_ONLY )); then cmd+=(--missing_only); fi
if (( VERBOSE )); then cmd+=(--verbose); fi

echo "[Trace2Skill] version=$VERSION model=$MODEL dataset=$DATASET"
echo "[Trace2Skill] output=$OUTPUT"
"${cmd[@]}"

if (( ! SKIP_EVAL )); then
  if [[ -n "$INSTANCE_IDS" ]]; then
    echo "[Trace2Skill] skipping automatic evaluation because --instance-ids is selected;"
    echo "             evaluate those outputs separately or use --skip-eval."
  else
    eval_cmd=(
      "$PYTHON_BIN" "$REPO_ROOT/tmp/paper_repos/Trace2Skill/evaluate_with_official.py"
      --data_path "$DATASET"
      --output_dir "$OUTPUT/outputs"
      --start_idx "$START_IDX"
      --results_file "$OUTPUT/eval_official_results.json"
      --verbose
    )
    if [[ -n "$END_IDX" ]]; then eval_cmd+=(--end_idx "$END_IDX"); fi
    "${eval_cmd[@]}"
    echo "[Trace2Skill] evaluation: $OUTPUT/eval_official_results.json"
  fi
fi

echo "[Trace2Skill] runner results: $OUTPUT/runner_results.json"
