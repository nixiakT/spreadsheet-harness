#!/usr/bin/env python3
"""Evaluate one frozen Fin-1.5K evolution candidate on SpreadsheetBench-v2.

This is intentionally a post-freeze evaluator: no V2 score is available to
the Fin-1.5K proposer or promotion gate.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

import run_fin15k_scaling_evolution_20260919 as experiment


REPO = Path(__file__).resolve().parents[1]
SEED = experiment.SEED


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def tasks(limit: int | None) -> list[dict[str, str]]:
    all_tasks = experiment._nonvisual_v2_tasks()
    if limit is None:
        return all_tasks
    by_category: dict[str, list[dict[str, str]]] = defaultdict(list)
    for task in all_tasks:
        by_category[task["category"]].append(task)
    for category, values in by_category.items():
        random.Random(f"{SEED}:profiled-v2:{category}").shuffle(values)
    chosen: list[dict[str, str]] = []
    while len(chosen) < limit:
        for category in ("Debugging", "Financial_Model", "Template"):
            if by_category[category] and len(chosen) < limit:
                chosen.append(by_category[category].pop())
    return chosen


def variants(root: Path, variant: str) -> dict[str, Path]:
    cell_path = root / "cells" / f"{variant}.json"
    if not cell_path.is_file():
        raise RuntimeError(f"Frozen profiled evolution cell is missing: {cell_path}")
    cell = load(cell_path)
    workspace = Path(cell["workspace"])
    initial = workspace / "revisions" / str(cell["initial_revision_sha256"])
    frozen = workspace / "revisions" / str(cell["frozen_revision_sha256"])
    for revision in (initial, frozen):
        if not (revision / "artifact").is_dir() or not (revision / "composition.json").is_file():
            raise RuntimeError(f"Frozen revision is incomplete: {revision}")
    return {"baseline": initial, variant: frozen}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--task-limit", type=int, default=6)
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument("--evaluation-name")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    selected = variants(root, args.variant)
    selected_tasks = tasks(args.task_limit)
    evaluation_name = args.evaluation_name or f"profiled-{args.variant}-{args.task_limit}"
    jobs = [
        (name, revision, task)
        for name, revision in selected.items()
        for task in selected_tasks
    ]
    random.Random(SEED).shuffle(jobs)
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    retry = jobs
    for attempt in range(1, 4):
        if not retry:
            break
        with futures.ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            pending = {
                pool.submit(
                    experiment._v2_cell,
                    root,
                    name,
                    revision,
                    task,
                    evaluation_name=evaluation_name,
                ): (name, task["task_id"])
                for name, revision, task in retry
            }
            for future in futures.as_completed(pending):
                rows[pending[future]] = future.result()
        retry = [
            item
            for item in jobs
            if rows.get((item[0], item[2]["task_id"]), {}).get("status") != "scored"
        ]
    report = experiment.summarize_v2(
        root,
        variants=tuple(selected),
        expected_tasks=len(selected_tasks),
        evaluation_name=evaluation_name,
    )
    output = root / f"v2-report-{evaluation_name}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
