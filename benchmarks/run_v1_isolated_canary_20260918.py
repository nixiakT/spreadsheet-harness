"""Freeze and run a small V1-only paired canary, never a paper-result replacement."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARMS = ("bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial")


def hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def prepare(root: Path, per_type: int, *, model: str, base_url: str) -> dict:
    from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1

    excluded = set((REPO / "benchmarks/results/spreadsheetbench-v1-representative-200-seed42/task_ids.txt")
                   .read_text().split())
    tasks = load_spreadsheetbench_v1(REPO / "benchmarks/data/spreadsheetbench_912_v0.1")
    groups: dict[str, list] = {}
    for task in tasks:
        if task.task_id in excluded or not task.instruction.strip():
            continue
        if any(c.input_path is None or c.golden_path is None for c in task.cases):
            continue
        groups.setdefault(task.instruction_type, []).append(task)
    selected = []
    for category, candidates in sorted(groups.items()):
        candidates.sort(key=lambda t: hashlib.sha256(f"v1-isolated-canary-20260918:{t.task_id}".encode()).hexdigest())
        selected.extend({"task_id": t.task_id, "instruction_type": category} for t in candidates[:per_type])
    root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(REPO / "src/spreadsheet_harness", root / "source/spreadsheet_harness",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(REPO / "skills", root / "skills", ignore=shutil.ignore_patterns("__pycache__"))
    manifest = {
        "schema": "v1-isolated-paired-canary-v1", "mode": "direct",
        "selection": "hash-ranked within instruction_type, excluding prior representative 200; no score filtering",
        "tasks": selected, "arms": list(ARMS), "model": model,
        "base_url": base_url, "seed": 41, "temperature": 0, "top_p": 1,
        "max_turns": 50, "max_model_calls": 50, "max_output_tokens": 8192,
        "max_total_tokens": 10000000, "task_timeout": 1800, "request_timeout": 300,
        "request_retries": 2, "workers": 2,
        "source_sha256": hashes(root / "source"), "skills_sha256": hashes(root / "skills"),
        "status": "prepared", "interpretation": "development canary, not held-out or full-benchmark evidence",
    }
    (root / "experiment.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def run_job(root: Path, manifest: dict, task: dict) -> dict:
    tid = task["task_id"]
    out = root / "tasks" / tid
    command = [
        sys.executable, "-m", "spreadsheet_harness.cli", "benchmark", "v1-compare",
        "--dataset", str(REPO / "benchmarks/data/spreadsheetbench_912_v0.1"),
        "--output", str(out), "--task-id", tid, "--v1-execution-mode", manifest["mode"],
        "--base-url", manifest["base_url"], "--model", manifest["model"],
        "--api-key-file", "/tmp/spreadsheet-harness-litellm.key", "--api-protocol", "chat-completions",
        "--reasoning-effort", "medium", "--enable-thinking", "--arm-order-seed", "20260918",
    ]
    for arm in ARMS:
        command.extend(("--arm", arm))
    for flag, key in (("seed", "seed"), ("temperature", "temperature"), ("top-p", "top_p"),
                      ("max-turns-per-arm", "max_turns"), ("max-model-calls", "max_model_calls"),
                      ("max-output-tokens", "max_output_tokens"), ("max-total-tokens", "max_total_tokens"),
                      ("task-timeout", "task_timeout"), ("request-timeout", "request_timeout"),
                      ("litellm-timeout", "request_timeout"), ("request-retries", "request_retries")):
        command.extend(("--" + flag, str(manifest[key])))
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(root / "source")
    with (root / "logs" / f"{tid}.log").open("x") as log:
        completed = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=3 * manifest["task_timeout"] + 300, check=False)
    result = {"task_id": tid, "returncode": completed.returncode}
    if (out / "results.json").exists():
        rows = json.loads((out / "results.json").read_text())
        result["rows"] = [{k: r.get(k) for k in ("arm", "status", "soft", "hard", "error_type", "budget")}
                          for r in rows]
    return result


def summarize(root: Path, manifest: dict) -> dict:
    rows = [r for p in sorted((root / "tasks").glob("*/results.json")) for r in json.loads(p.read_text())]
    n = len(manifest["tasks"])
    summary = {"planned_tasks_per_arm": n, "recorded": len(rows), "arms": {}}
    for arm in ARMS:
        records = [r for r in rows if r["arm"] == arm]
        scored = [r for r in records if r.get("status") == "completed" and r.get("soft") is not None]
        summary["arms"][arm] = {
            "recorded": len(records), "scored": len(scored), "missing_or_not_scored": n - len(scored),
            "soft_fixed_denominator_pct": 100 * sum(r["soft"] for r in scored) / n,
            "hard_fixed_denominator_pct": 100 * sum(r["hard"] for r in scored) / n,
            "case1_pass": sum(bool(r.get("case_results", [{}])[0].get("passed")) for r in scored),
            "model_calls": sum(r.get("budget", {}).get("used", {}).get("model_calls", 0) for r in records),
            "total_tokens": sum(r.get("budget", {}).get("used", {}).get("total_tokens", 0) for r in records),
            "errors": [{"task_id": r["task_id"], "error_type": r.get("error_type"),
                        "outcome_kind": r.get("outcome_kind")}
                       for r in records if r not in scored],
        }
    summary["complete"] = len(rows) == n * len(ARMS)
    summary["all_scored"] = all(v["scored"] == n for v in summary["arms"].values())
    summary["eligible_for_quality_comparison"] = summary["complete"] and summary["all_scored"]
    summary["promote_to_default"] = False
    summary["promotion_reason"] = "Canary only; require larger paired confirmation and V2 isolation audit."
    summary["note"] = "Diagnostic only; missing/not_scored counted as failure in fixed denominator; report coverage separately."
    (root / "canary_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def prepare_resume(root: Path, manifest: dict) -> list[dict]:
    """Keep finished tasks and recoverably archive interrupted task directories."""

    pending = []
    archive_root = root / "interrupted-backups" / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    for task in manifest["tasks"]:
        task_id = task["task_id"]
        task_dir = root / "tasks" / task_id
        result_path = task_dir / "results.json"
        rows = []
        if result_path.exists():
            try:
                rows = json.loads(result_path.read_text())
            except (json.JSONDecodeError, OSError):
                rows = []
        if isinstance(rows, list) and len(rows) == len(ARMS):
            continue
        pending.append(task)
        log_path = root / "logs" / f"{task_id}.log"
        if task_dir.exists() or log_path.exists():
            archive_root.mkdir(parents=True, exist_ok=True)
            destination = archive_root / task_id
            destination.mkdir()
            if task_dir.exists():
                shutil.move(str(task_dir), str(destination / "task"))
            if log_path.exists():
                shutil.move(str(log_path), str(destination / "task.log"))
    return pending


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-type", type=int, default=2)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--run-prepared", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    args = parser.parse_args()
    if args.per_type < 1:
        parser.error("--per-type must be positive")
    root = args.output.resolve()
    manifest = (json.loads((root / "experiment.json").read_text()) if args.run_prepared or args.summarize
                else prepare(root, args.per_type, model=args.model, base_url=args.base_url))
    if args.summarize:
        print(json.dumps(summarize(root, manifest), indent=2))
        return
    if args.prepare_only:
        print(json.dumps({"output": str(root), "tasks": manifest["tasks"]}, indent=2))
        return
    if hashes(root / "source") != manifest["source_sha256"] or hashes(root / "skills") != manifest["skills_sha256"]:
        raise ValueError("Frozen source/skill hashes changed")
    (root / "logs").mkdir(exist_ok=args.run_prepared)
    (root / "tasks").mkdir(exist_ok=args.run_prepared)
    tasks_to_run = prepare_resume(root, manifest) if args.run_prepared else manifest["tasks"]
    print(json.dumps({"event": "launcher.started", "pending_tasks": len(tasks_to_run),
                      "completed_tasks": len(manifest["tasks"]) - len(tasks_to_run)}), flush=True)
    with ThreadPoolExecutor(max_workers=manifest["workers"]) as pool:
        futures = [pool.submit(run_job, root, manifest, task) for task in tasks_to_run]
        for future in as_completed(futures):
            print(json.dumps(future.result()), flush=True)
            summarize(root, manifest)
    print(json.dumps(summarize(root, manifest), indent=2), flush=True)


if __name__ == "__main__":
    main()
