#!/usr/bin/env python3
"""Repeat the most informative multi-plugin arms to estimate service variance.

The first probe is complete, but its two financial tasks are stochastic at the
provider even with temperature=0.  This runner reuses the frozen generated
skills and compositions from that probe and repeats baseline, strict H1+D1,
and H1+D1+coordination twice on both tasks.  It is resumable and keeps every
trajectory in an isolated output directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv/bin/python"
SOURCE_ROOT = REPO / "benchmarks/results/multi-plugin-coevolution-deepseekflash-20260913"
DEFAULT_ROOT = REPO / "benchmarks/results/multi-plugin-repeats-deepseekflash-20260913"
DATASETS = {
    "v06": REPO / "benchmarks/data/normalized-harbor/v06-financial-269",
    "enhanced-v2": REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565",
}
TASKS = (
    ("v06", "Financial_Model/fina_Fina_18_RoadAssets_c0"),
    ("enhanced-v2", "Financial_Model/fina_Fina_she_76be0f6c16_118661d1313_c0"),
)
ARMS = ("h0d0", "h1d1", "h1d1-coordination")


def load_arms() -> dict[str, dict[str, str]]:
    payload = json.loads((SOURCE_ROOT / "arms.json").read_text(encoding="utf-8"))
    return {item["name"]: item for item in payload if item["name"] in ARMS}


def run_one(
    root: Path,
    arm: dict[str, str],
    repeat: int,
    dataset: str,
    task_id: str,
    *,
    model: str,
    base_url: str,
    api_key_file: Path,
    max_calls: int,
    timeout: float,
) -> dict[str, Any]:
    output = root / "runs" / f"repeat-{repeat:02d}" / arm["name"] / dataset / task_id.replace("/", "_")
    summary = output / "summary.json"
    if summary.is_file():
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
            if payload.get("study_complete"):
                return {"repeat": repeat, "arm": arm["name"], "dataset": dataset, "task_id": task_id, "status": "skipped", "summary": payload}
        except (OSError, json.JSONDecodeError):
            pass
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
        "--dataset", str(DATASETS[dataset]), "--category", "Financial_Model", "--task-id", task_id,
        "--arm", "ours", "--composition-file", f"ours={arm['composition_file']}",
        "--skill-root", arm["skill_root"], "--output", str(output),
        "--max-model-calls", str(max_calls), "--max-turns-per-arm", str(max_calls),
        "--max-total-tokens", "unlimited", "--max-output-tokens", "unlimited",
        "--task-timeout", str(timeout), "--arm-order-seed", "20260913",
        "--base-url", base_url, "--api-key-file", str(api_key_file), "--model", model,
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--seed", "41", "--temperature", "0", "--top-p", "1", "--enable-thinking",
    ]
    with output.with_suffix(".log").open("w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=REPO, stdout=handle, stderr=subprocess.STDOUT, check=False)
    payload: dict[str, Any] = {}
    if summary.is_file():
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "repeat": repeat, "arm": arm["name"], "dataset": dataset, "task_id": task_id,
        "status": "scored" if payload.get("study_complete") else "failed",
        "returncode": completed.returncode, "summary": payload,
    }


def metric(row: dict[str, Any], name: str) -> float:
    first = next(iter((row.get("summary") or {}).get("arms", {}).values()), {})
    value = first.get(name)
    return float(value) if value is not None else math.nan


def summarize(root: Path, rows: list[dict[str, Any]], model: str) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["arm"], []).append(row)
    aggregate: dict[str, Any] = {}
    for arm, values in sorted(grouped.items()):
        aggregate[arm] = {"n": len(values), "cells": []}
        for row in sorted(values, key=lambda item: (item["repeat"], item["dataset"])):
            aggregate[arm]["cells"].append({
                "repeat": row["repeat"], "dataset": row["dataset"],
                "exact": metric(row, "accuracy"),
                "modification": metric(row, "modification_accuracy"),
                "regression": metric(row, "regression_accuracy"),
                "model_calls": metric(row, "model_calls"),
            })
        for name, key in (("exact", "accuracy"), ("modification", "modification_accuracy"), ("regression", "regression_accuracy")):
            vals = [metric(row, key) for row in values]
            aggregate[arm][name] = {
                "mean": statistics.fmean(vals),
                "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            }
    document = {"schema_version": "multi-plugin-repeat-report-v1", "model": model, "arms": aggregate, "rows": rows}
    (root / "report.json").write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Multi-plugin repeat report", "", "| Arm | Exact mean±sd | Modification mean±sd | Regression mean±sd |", "|---|---:|---:|---:|"]
    for arm, values in aggregate.items():
        lines.append(
            f"| {arm} | {values['exact']['mean']:.4f}±{values['exact']['stdev']:.4f} | "
            f"{values['modification']['mean']:.4f}±{values['modification']['stdev']:.4f} | "
            f"{values['regression']['mean']:.4f}±{values['regression']['stdev']:.4f} |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    parser.add_argument("--parallelism", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-model-calls", type=int, default=50)
    parser.add_argument("--task-timeout", type=float, default=3600)
    args = parser.parse_args()
    if not args.api_key_file.is_file():
        raise RuntimeError(f"Missing API key file: {args.api_key_file}")
    arms = load_arms()
    jobs = [(arms[name], repeat, dataset, task_id) for repeat in range(1, args.repeats + 1) for name in ARMS for dataset, task_id in TASKS]
    rows: list[dict[str, Any]] = []
    args.result_root.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallelism) as executor:
        futures = {
            executor.submit(run_one, args.result_root, arm, repeat, dataset, task_id,
                             model=args.model, base_url=args.base_url, api_key_file=args.api_key_file,
                             max_calls=args.max_model_calls, timeout=args.task_timeout): (arm["name"], repeat, dataset)
            for arm, repeat, dataset, task_id in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps({"arm": row["arm"], "repeat": row["repeat"], "dataset": row["dataset"], "status": row["status"]}, ensure_ascii=False), flush=True)
    summarize(args.result_root, rows, args.model)
    print(json.dumps({"status": "complete", "report": str(args.result_root / "report.md")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
