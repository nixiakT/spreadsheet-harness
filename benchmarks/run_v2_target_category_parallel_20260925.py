#!/usr/bin/env python3
"""Run one SpreadsheetBench-v2 category/arm as isolated official cells in parallel."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
DATASET = ROOT / "benchmarks/data/spreadsheetbench-v2"
EVALUATOR = ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"


def run_cell(args: argparse.Namespace, task_id: str) -> dict[str, object]:
    item_id = task_id.split("/", 1)[1]
    cell = args.output / args.model_slug / args.arm / args.category / item_id
    completed_runs = sorted(cell.glob("run*/summary.json"))
    for candidate in reversed(completed_runs):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            arm_data = next(iter(payload.get("arms", {}).values()))
            if arm_data.get("completed") == 1 and arm_data.get("errors", 0) == 0:
                return {"task": task_id, "status": "reused", "output": str(candidate.parent)}
        except (OSError, ValueError, StopIteration, TypeError):
            continue
    cell.mkdir(parents=True, exist_ok=True)
    output = cell / "run"
    attempt = 2
    while output.exists():
        output = cell / f"run-{attempt}"
        attempt += 1
    summary = output / "summary.json"
    command = [
        str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
        "--dataset", str(DATASET), "--official-evaluator", str(EVALUATOR),
        "--category", args.category, "--task-id", task_id,
        "--output", str(output), "--arm", args.arm,
        "--max-model-calls", "50", "--max-turns-per-arm", "50",
        "--max-total-tokens", "unlimited", "--max-output-tokens", "unlimited",
        "--task-timeout", "3600", "--request-timeout", "600",
        "--request-retries", "5", "--request-interval-seconds", "1.1",
        "--litellm-timeout", "600", "--base-url", args.base_url,
        "--api-key-file", str(args.api_key_file), "--model", args.model,
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--temperature", "0", "--top-p", "1", "--enable-thinking",
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    with (cell / "runner.log").open("a", encoding="utf-8") as log:
        completed = subprocess.run(
            command, cwd=ROOT, env=environment, stdout=log,
            stderr=subprocess.STDOUT, check=False,
        )
    return {
        "task": task_id,
        "status": "completed" if completed.returncode == 0 and summary.is_file() else "failed",
        "returncode": completed.returncode,
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True, choices=("Template", "Debugging", "Financial_Model"))
    parser.add_argument("--arm", required=True, choices=("spreadsheet-harness-basic", "spreadsheet-harness-financial"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-slug", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument(
        "--task-ids", nargs="*", default=None,
        help="Optional item ids for a focused smoke run; omit for the full category.",
    )
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    args = parser.parse_args()
    args.output = args.output.resolve()
    data = json.loads((DATASET / args.category / "dataset.json").read_text(encoding="utf-8"))
    selected = set(args.task_ids or [])
    tasks = [
        f"{args.category}/{item['id']}" for item in data
        if not selected or item["id"] in selected
    ]
    if not tasks:
        parser.error("--task-ids did not match any item in the selected category")
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallelism) as pool:
        pending = {pool.submit(run_cell, args, task): task for task in tasks}
        for index, future in enumerate(concurrent.futures.as_completed(pending), 1):
            row = future.result()
            rows.append(row)
            print(json.dumps({"completed": index, "total": len(tasks), **row}, ensure_ascii=False), flush=True)
    status = args.output / f"{args.model_slug}-{args.arm}-{args.category}-launcher.json"
    status.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row["status"] in {"completed", "reused"} for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
