"""Frozen full SpreadsheetBench V1 run for all instructions and three arms."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "benchmarks/data/spreadsheetbench_912_v0.1"
BASE_URL = "http://10.130.138.46:8010/v1"
ARMS = ("bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial")


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def complete_task(path: Path, arms: tuple[str, ...] = ARMS) -> bool:
    result = path / "results.json"
    if not result.exists():
        return False
    try:
        rows = json.loads(result.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(rows, list)
        and len(rows) == len(arms)
        and {row.get("arm") for row in rows} == set(arms)
        and all(row.get("status") == "completed" and row.get("soft") is not None for row in rows)
    )


def run_task(
    root: Path,
    task_id: str,
    *,
    model: str,
    base_url: str,
    api_key_file: str,
    frozen_source: Path,
    arms: tuple[str, ...],
    v1_execution_mode: str,
    request_retries: int,
    request_interval_seconds: float,
) -> int:
    output = root / "tasks" / task_id
    log = root / "logs" / f"{task_id}.log"
    if complete_task(output, arms):
        return 0
    command = [
        sys.executable, "-m", "spreadsheet_harness.cli", "benchmark", "v1-compare",
        "--dataset", str(DATASET), "--output", str(output), "--task-id", task_id,
        "--v1-execution-mode", v1_execution_mode,
        "--base-url", base_url, "--model", model,
        "--api-key-file", api_key_file,
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--enable-thinking", "--arm-order-seed", "20260918", "--seed", "41",
        "--temperature", "0", "--top-p", "1", "--max-turns-per-arm", "50",
        "--max-model-calls", "50", "--max-output-tokens", "12288",
        "--max-total-tokens", "10000000", "--task-timeout", "21600",
        "--request-timeout", "1800", "--litellm-timeout", "1800",
        "--request-retries", str(request_retries),
        "--request-interval-seconds", str(request_interval_seconds),
    ]
    for arm in arms:
        command.extend(("--arm", arm))
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "10.130.138.46,127.0.0.1,localhost"
    env["no_proxy"] = env["NO_PROXY"]
    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(frozen_source) + (os.pathsep + old_pythonpath if old_pythonpath else "")
    with log.open("x") as stream:
        return subprocess.run(command, cwd=REPO, env=env, stdout=stream,
                              stderr=subprocess.STDOUT, check=False).returncode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="dashscope/deepseek-v4-flash-0731")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--api-key-file", default="/tmp/spreadsheet-harness-litellm.key")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--arm",
        action="append",
        choices=ARMS,
        dest="selected_arms",
        help="Arm to run; repeat for multiple arms. Defaults to all three.",
    )
    parser.add_argument(
        "--v1-execution-mode",
        choices=("legacy", "repaired", "direct"),
        default="repaired",
        help="V1 path; repaired enables the V1 planner/YAML fixes.",
    )
    parser.add_argument(
        "--request-retries",
        type=int,
        choices=range(0, 6),
        default=4,
        help="Retries for transient provider failures per arm",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=1.0,
        help="Minimum start-to-start interval between provider attempts",
    )
    parser.add_argument("--task-file", type=Path,
                        help="Optional frozen task-id file; defaults to all 912 instructions")
    args = parser.parse_args()
    selected_arms = tuple(args.selected_arms or ARMS)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "tasks").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    frozen_source = root / "frozen-source"
    if not frozen_source.exists():
        (frozen_source / "spreadsheet_harness").parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / "src/spreadsheet_harness", frozen_source / "spreadsheet_harness")
        shutil.copytree(REPO / "skills", root / "skills")
    sys.path.insert(0, str(frozen_source))
    from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1

    tasks = load_spreadsheetbench_v1(DATASET)
    available = {task.task_id for task in tasks}
    if args.task_file is None:
        task_ids = [task.task_id for task in tasks]
    else:
        task_ids = [line.strip() for line in args.task_file.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        if len(task_ids) != len(set(task_ids)):
            raise SystemExit("task file contains duplicate task IDs")
        missing = [task_id for task_id in task_ids if task_id not in available]
        if missing:
            raise SystemExit("task file contains unknown task IDs: " + ", ".join(missing))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root / "interrupted-backups" / stamp
    for task_id in task_ids:
        task_dir = root / "tasks" / task_id
        log_path = root / "logs" / f"{task_id}.log"
        if complete_task(task_dir, selected_arms):
            continue
        if task_dir.exists() or log_path.exists():
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / task_id
            target.mkdir()
            if task_dir.exists():
                shutil.move(str(task_dir), str(target / "task"))
            if log_path.exists():
                shutil.move(str(log_path), str(target / "task.log"))
    manifest = {
        "schema": "spreadsheetbench-v1-frozen-taskset-20260921",
        "dataset": str(DATASET), "task_count": len(task_ids), "task_ids": task_ids,
        "arms": list(selected_arms), "model": args.model, "base_url": args.base_url,
        "execution_mode": args.v1_execution_mode, "max_turns": 50, "temperature": 0,
        "top_p": 1, "thinking": True, "max_output_tokens": 12288,
        "workers": args.workers, "request_retries": args.request_retries,
        "request_interval_seconds": args.request_interval_seconds,
        "source_sha256": tree_hash(frozen_source),
        "skills_sha256": tree_hash(root / "skills"),
        "evaluator_patch": "tolerant-v1-answer-position-ranges-v1",
    }
    (root / "experiment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                run_task,
                root,
                task_id,
                model=args.model,
                base_url=args.base_url,
                api_key_file=args.api_key_file,
                frozen_source=frozen_source,
                arms=selected_arms,
                v1_execution_mode=args.v1_execution_mode,
                request_retries=args.request_retries,
                request_interval_seconds=args.request_interval_seconds,
            ): task_id
            for task_id in task_ids
        }
        for future in as_completed(futures):
            print(json.dumps({"task_id": futures[future], "returncode": future.result()}), flush=True)


if __name__ == "__main__":
    main()
