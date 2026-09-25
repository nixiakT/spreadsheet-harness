"""Run one repaired V1 arm/model on the frozen 200-task set."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "benchmarks/results/v1-isolated-direct-200-deepseek-20260919"
DATASET = REPO / "benchmarks/data/spreadsheetbench_912_v0.1"
BASE_URL = "http://10.130.138.46:8010/v1"


def run_task(root: Path, task_id: str, *, model: str, arm: str) -> int:
    out = root / "tasks" / task_id
    log = root / "logs" / f"{task_id}.log"
    result_path = out / "results.json"
    if result_path.exists():
        try:
            rows = json.loads(result_path.read_text())
            if rows and all(r.get("status") == "completed" and r.get("soft") is not None for r in rows):
                return 0
        except (OSError, json.JSONDecodeError):
            pass
    command = [
        sys.executable, "-m", "spreadsheet_harness.cli", "benchmark", "v1-compare",
        "--dataset", str(DATASET), "--output", str(out), "--task-id", task_id,
        "--v1-execution-mode", "direct", "--arm", arm,
        "--base-url", BASE_URL, "--model", model,
        "--api-key-file", "/tmp/spreadsheet-harness-litellm.key",
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--enable-thinking", "--arm-order-seed", "20260918", "--seed", "41",
        "--temperature", "0", "--top-p", "1", "--max-turns-per-arm", "50",
        "--max-model-calls", "50", "--max-output-tokens", "12288",
        "--max-total-tokens", "10000000", "--task-timeout", "1800",
        "--request-timeout", "300", "--litellm-timeout", "300",
        "--request-retries", "2",
    ]
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    with log.open("x") as stream:
        completed = subprocess.run(command, cwd=REPO, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, check=False)
    return completed.returncode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--arm", required=True,
                        choices=("spreadsheet-harness-basic", "spreadsheet-harness-financial"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    manifest = json.loads((SOURCE / "experiment.json").read_text())
    task_ids = [str(item["task_id"]) for item in manifest["tasks"]]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "tasks").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root / "interrupted-backups" / stamp
    for task_id in task_ids:
        task_dir = root / "tasks" / task_id
        result_path = task_dir / "results.json"
        log_path = root / "logs" / f"{task_id}.log"
        if result_path.exists():
            try:
                rows = json.loads(result_path.read_text())
                if rows and all(r.get("status") == "completed" and r.get("soft") is not None for r in rows):
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        if task_dir.exists() or log_path.exists():
            archive.mkdir(parents=True, exist_ok=True)
            destination = archive / task_id
            destination.mkdir()
            if task_dir.exists():
                shutil.move(str(task_dir), str(destination / "task"))
            if log_path.exists():
                shutil.move(str(log_path), str(destination / "task.log"))
    (root / "experiment.json").write_text(json.dumps({
        "schema": "v1-repaired-200-v1", "source_experiment": str(SOURCE),
        "model": args.model, "base_url": BASE_URL, "execution_mode": "direct",
        "arm": args.arm, "tasks": task_ids, "max_turns": 50, "temperature": 0,
        "top_p": 1, "thinking": True, "max_output_tokens": 12288,
        "workers": args.workers,
    }, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_task, root, task_id, model=args.model, arm=args.arm): task_id
            for task_id in task_ids
        }
        for future in as_completed(futures):
            print(json.dumps({"task_id": futures[future], "returncode": future.result()}), flush=True)


if __name__ == "__main__":
    main()
