#!/usr/bin/env python3
"""Evaluate any completed Fin-1.5K evolution cell on V2 immediately.

The frozen scaling protocol intentionally requires all nine cells before its
publication run.  This helper provides an early paired canary without changing
that frozen runner: baseline and one completed candidate use identical V2
tasks, solver settings, and scoring code.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

import run_fin15k_scaling_evolution_20260919 as experiment


def frozen_pair(root: Path, variant: str) -> dict[str, Path]:
    cell_path = root / "cells" / f"{variant}.json"
    if not cell_path.is_file():
        raise RuntimeError(f"Evolution cell is not complete: {variant}")
    cell = experiment.load(cell_path)
    workspace = Path(cell["workspace"])
    initial = workspace / "revisions" / str(cell["initial_revision_sha256"])
    candidate = workspace / "revisions" / str(cell["frozen_revision_sha256"])
    for revision in (initial, candidate):
        if not (revision / "artifact").is_dir() or not (
            revision / "composition.json"
        ).is_file():
            raise RuntimeError(f"Frozen revision is incomplete: {revision}")
    return {"baseline": initial, variant: candidate}


def select_tasks(limit: int | None) -> list[dict[str, str]]:
    tasks = experiment._nonvisual_v2_tasks()
    if limit is None:
        return tasks
    if limit < 1 or limit > len(tasks):
        raise ValueError("task limit must be between 1 and 297")
    by_category: dict[str, list[dict[str, str]]] = defaultdict(list)
    for task in tasks:
        by_category[task["category"]].append(task)
    for category, values in by_category.items():
        random.Random(f"{experiment.SEED}:v2:{category}").shuffle(values)
    chosen: list[dict[str, str]] = []
    while len(chosen) < limit:
        for category in ("Debugging", "Financial_Model", "Template"):
            if by_category[category] and len(chosen) < limit:
                chosen.append(by_category[category].pop())
    return chosen


def run(
    root: Path,
    *,
    variant: str,
    limit: int | None,
    parallelism: int,
    evaluation_name: str,
) -> dict[str, Any]:
    experiment.verify_protocol(root)
    variants = frozen_pair(root, variant)
    tasks = select_tasks(limit)
    jobs = [
        (name, revision, task)
        for name, revision in variants.items()
        for task in tasks
    ]
    random.Random(experiment.SEED).shuffle(jobs)
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    retry_jobs = jobs
    experiment.append_event(
        root,
        "v2.incremental.started",
        variant=variant,
        tasks=len(tasks),
        jobs=len(jobs),
        evaluation_name=evaluation_name,
    )
    for attempt in range(1, 4):
        if not retry_jobs:
            break
        with futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
            pending = {
                pool.submit(
                    experiment._v2_cell,
                    root,
                    name,
                    revision,
                    task,
                    evaluation_name=evaluation_name,
                ): (name, task["task_id"])
                for name, revision, task in retry_jobs
            }
            for future in futures.as_completed(pending):
                key = pending[future]
                try:
                    record = future.result()
                except Exception as exc:  # keep one stalled provider task from aborting the canary
                    record = {
                        "status": "infrastructure",
                        "returncode": None,
                        "metrics": {},
                        "weighted_score": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                rows[key] = record
                experiment.append_event(
                    root,
                    "v2.incremental.cell.finished",
                    variant=key[0],
                    task_id=key[1],
                    status=record["status"],
                    attempt=attempt,
                )
        retry_jobs = [
            item
            for item in jobs
            if rows.get((item[0], item[2]["task_id"]), {}).get("status") != "scored"
        ]
    report = experiment.summarize_v2(
        root,
        variants=tuple(variants),
        expected_tasks=len(tasks),
        evaluation_name=evaluation_name,
    )
    experiment.append_event(
        root,
        "v2.incremental.completed",
        variant=variant,
        report=str(root / f"v2-report-{evaluation_name}.json"),
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--task-limit", type=int, default=30)
    parser.add_argument("--parallelism", type=int, default=8)
    parser.add_argument("--evaluation-name")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    name = args.evaluation_name or f"incremental-{args.variant}-{args.task_limit}"
    run(
        root,
        variant=args.variant,
        limit=args.task_limit,
        parallelism=args.parallelism,
        evaluation_name=name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
