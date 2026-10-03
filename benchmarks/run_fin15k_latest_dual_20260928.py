#!/usr/bin/env python3
"""Fresh 500-case Fin-1.5K baseline with current harness, basic + financial.

The case list is inherited from the historical 500-case split only by task id;
every input/golden/metadata field is revalidated against the current dataset.
Each arm has an independent output tree, but both arms receive exactly the same
validated task list.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
DATASET_ROOT = REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565"
DATASET = DATASET_ROOT / "Financial_Model/dataset.json"
OLD_SPLIT = REPO / "benchmarks/results/fin15k-scaling-coevolution-20260919/split-manifest.json"
EVALUATOR = REPO / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
BASE_URL = os.environ.get("SPREADSHEET_FIN15K_BASE_URL", "http://47.96.153.159:8010/v1")
MODEL = os.environ.get("SPREADSHEET_FIN15K_SOLVER_MODEL", "dashscope/deepseek-v4-flash-0731")
ARMS = ("spreadsheet-harness-basic", "spreadsheet-harness-financial")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cases() -> list[dict[str, Any]]:
    current = {str(x["id"]): x for x in json.loads(DATASET.read_text(encoding="utf-8"))}
    old = json.loads(OLD_SPLIT.read_text(encoding="utf-8"))["evidence"]
    if len(old) != 500:
        raise RuntimeError(f"Historical split has {len(old)} cases, expected 500")
    selected: list[dict[str, Any]] = []
    families: set[str] = set()
    for index, old_item in enumerate(old):
        item = current.get(str(old_item["id"]))
        if item is None:
            raise RuntimeError(f"Deprecated/missing case: {old_item['id']}")
        if item.get("dataset_role") != "calibration_only":
            raise RuntimeError(f"Case is not calibration_only: {item['id']} ({item.get('dataset_role')})")
        if item.get("category") != "Financial_Model":
            raise RuntimeError(f"Unexpected category: {item['id']} ({item.get('category')})")
        family = str(item.get("source_workbook") or "")
        if not family or family in families:
            raise RuntimeError(f"Duplicate/empty source workbook family: {item['id']} {family}")
        input_path = DATASET_ROOT / "Financial_Model" / str(item["spreadsheet_path"])
        golden_path = DATASET_ROOT / "Financial_Model" / str(item["golden_response_path"])
        if not input_path.is_file() or not golden_path.is_file():
            raise RuntimeError(f"Missing input/golden for current case: {item['id']}")
        selected.append({
            "evidence_index": index,
            "id": str(item["id"]),
            "task_id": f"Financial_Model/{item['id']}",
            "category": "Financial_Model",
            "complexity": str(item["complexity"]),
            "source_workbook": family,
            "dataset_role": str(item["dataset_role"]),
            "input_path": str(input_path.resolve()),
            "golden_response_path": str(golden_path.resolve()),
            "input_sha256": sha256(input_path),
            "golden_sha256": sha256(golden_path),
        })
        families.add(family)
    return selected


def env() -> dict[str, str]:
    out = dict(os.environ)
    out["PYTHONPATH"] = str(REPO / "src")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        out.pop(key, None)
    return out


def run_one(root: Path, case: dict[str, Any], arm: str, attempt: int) -> dict[str, Any]:
    case_dir = root / arm / f"{int(case['evidence_index']):04d}-{case['id']}"
    case_dir.mkdir(parents=True, exist_ok=True)
    preferred = case_dir / ("run" if attempt == 1 else f"run-{attempt}")
    run_dir = preferred
    if run_dir.exists() and any(run_dir.iterdir()):
        suffix = max(2, attempt)
        while (case_dir / f"run-{suffix}").exists():
            suffix += 1
        run_dir = case_dir / f"run-{suffix}"
    command = [
        str(REPO / ".venv/bin/python"), "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
        "--dataset", str(DATASET_ROOT), "--official-evaluator", str(EVALUATOR),
        "--category", "Financial_Model", "--task-id", case["task_id"], "--output", str(run_dir),
        "--arm", arm, "--skill-root", str(REPO / "skills"),
        "--max-model-calls", "50", "--max-turns-per-arm", "50",
        "--max-total-tokens", "unlimited", "--max-output-tokens", "unlimited",
        "--task-timeout", "7200", "--request-timeout", "900", "--litellm-timeout", "600",
        "--request-retries", "5", "--request-interval-seconds", "2",
        "--base-url", BASE_URL, "--api-key-file", str(KEY_FILE), "--model", MODEL,
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--temperature", "0", "--top-p", "1", "--seed", "41", "--enable-thinking",
    ]
    log = case_dir / ("runner.log" if attempt == 1 else f"runner-{attempt}.log")
    with log.open("w", encoding="utf-8") as handle:
        proc = subprocess.run(command, cwd=REPO, env=env(), stdout=handle, stderr=subprocess.STDOUT, timeout=7500, check=False)
    summaries = list(run_dir.glob("summary.json"))
    trajectories = list(run_dir.glob(f"runs/*/*/{arm}/trajectory.jsonl"))
    summary = json.loads(summaries[0].read_text()) if summaries else {}
    evaluated = False
    trajectory_task = None
    provider_failed = False
    if trajectories:
        for line in trajectories[0].read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            payload = row.get("payload") or {}
            if row.get("event") == "spreadsheetbench_v2.evaluated":
                evaluated = str(payload.get("task_id")) == case["task_id"]
            if row.get("event") == "model.failed":
                provider_failed = True
            trajectory_task = trajectory_task or payload.get("task_id")
    study_complete = bool(summary.get("study_complete")) and evaluated and not provider_failed
    return {
        "task_id": case["task_id"], "arm": arm, "attempt": attempt,
        "status": "complete" if study_complete else "retryable_failure",
        "returncode": proc.returncode, "summary": str(summaries[0]) if summaries else None,
        "trajectory": str(trajectories[0]) if trajectories else None,
        "trajectory_task_id": trajectory_task, "provider_failed": provider_failed,
        "finished_at": now(),
    }


def already_complete(root: Path, case: dict[str, Any], arm: str) -> bool:
    case_dir = root / arm / f"{int(case['evidence_index']):04d}-{case['id']}"
    for run_dir in sorted(case_dir.glob("run*")):
        summary_path = run_dir / "summary.json"
        trajectories = list(run_dir.glob(f"runs/*/*/{arm}/trajectory.jsonl"))
        if not summary_path.is_file() or len(trajectories) != 1:
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if not summary.get("study_complete"):
                continue
            rows = [json.loads(line) for line in trajectories[0].read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError):
            continue
        if any(r.get("event") == "model.failed" for r in rows):
            continue
        if any(
            r.get("event") == "spreadsheetbench_v2.evaluated"
            and str((r.get("payload") or {}).get("task_id")) == case["task_id"]
            for r in rows
        ):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=REPO / "benchmarks/results/fin15k-latest-dual-20260928")
    ap.add_argument("--parallelism", type=int, default=10)
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    ap.add_argument("--min-new-valid", type=int, default=0,
                    help="Keep retrying until this many new valid paired cases are available.")
    args = ap.parse_args()
    root = args.root.resolve(); root.mkdir(parents=True, exist_ok=True)
    cases = load_cases()
    selected_arms = tuple(args.arms)
    manifest = {"schema_version": "fin15k-latest-dual-v1", "created_at": now(), "model": MODEL, "arms": ARMS, "requested_arms": selected_arms, "case_count": len(cases), "cases": cases}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending = {
        (arm, case["task_id"]): case
        for arm in selected_arms
        for case in cases
        if not already_complete(root, case, arm)
    }
    results: list[dict[str, Any]] = []
    for attempt in range(1, args.max_attempts + 1):
        if not pending: break
        batch = list(pending.values())
        # Interleave arms so a worker pool never spends an entire wave on one
        # harness while the paired harness waits in the queue.
        jobs = [(case, arm) for case in batch for arm in selected_arms if (arm, case["task_id"]) in pending]
        with futures.ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            futures_map = {pool.submit(run_one, root, case, arm, attempt): (arm, case) for case, arm in jobs}
            next_pending = {}
            for future in futures.as_completed(futures_map):
                arm, case = futures_map[future]; row = future.result(); results.append(row)
                print(json.dumps({"arm": arm, "task_id": case["task_id"], "attempt": attempt, "status": row["status"]}), flush=True)
                if row["status"] != "complete": next_pending[(arm, case["task_id"])] = case
        pending = next_pending
        (root / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if pending:
        (root / "incomplete.json").write_text(json.dumps([{"arm": a, "task_id": t} for a,t in sorted(pending)], indent=2) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
