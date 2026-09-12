#!/usr/bin/env python3
"""Score Trace2Skill-produced workbooks with the pinned SpreadsheetBench-v2 evaluator.

Trace2Skill's public runner reports agent success but does not invoke the
benchmark evaluator.  This adapter only discovers its output workbooks and
uses the same immutable evaluator as the harness; it does not alter workbooks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from spreadsheet_harness.spreadsheetbench_v2 import (
    DEFAULT_V2_EVALUATOR,
    _load_official_evaluator,
    _official_score,
    load_spreadsheetbench_v2_tasks,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="Trace2Skill result root")
    ap.add_argument("--dataset", type=Path, default=Path("benchmarks/data/spreadsheetbench-v2"))
    ap.add_argument("--manifest", type=Path, default=Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json"))
    args = ap.parse_args()
    root = args.root.resolve()
    manifest = json.loads(args.manifest.read_text())
    wanted = [str(x["task_id"]) for x in manifest["tasks"]]
    all_tasks = {t.task_id: t for t in load_spreadsheetbench_v2_tasks(args.dataset, categories=("Debugging", "Financial_Model", "Template"))}
    evaluator, evaluator_path, evaluator_sha = _load_official_evaluator(DEFAULT_V2_EVALUATOR)
    runner_payload = {}
    direct = root / "runner_results.json"
    if direct.is_file():
        try:
            runner_payload = json.loads(direct.read_text())
        except Exception:
            runner_payload = {}
    runner_rows = runner_payload.get("results", []) if isinstance(runner_payload, dict) else runner_payload
    by_task = {str(r.get("id")): r for r in runner_rows if isinstance(r, dict) and r.get("id")}
    rows = []
    staging = root / "official_outputs"
    for task_id in wanted:
        task = all_tasks[task_id]
        slug = task_id.replace("/", "_")
        output_root = root / "outputs"
        work_root = root / "work"
        candidates = [p for p in output_root.glob(f"**/{slug}*output.xlsx") if p.is_file()]
        # Trace2Skill uses either the task slug directory or the original
        # workbook stem; fall back to an exact item-id search if necessary.
        if not candidates:
            candidates = [p for p in output_root.glob(f"**/*{task.item_id}*output.xlsx") if p.is_file()]
        # A task that reaches ACTION: TASK_COMPLETE but fails the public
        # runner's bookkeeping can still leave a valid work/output.xlsx.
        if not candidates:
            candidates = [p for p in work_root.glob(f"**/output.xlsx") if p.is_file() and slug in str(p.parent)]
        rec = by_task.get(task_id, {})
        row = {"task_id": task_id, "category": task.category, "item_id": task.item_id,
               "model": rec.get("model"), "status": "error", "passed": False,
               "evaluator": str(evaluator_path), "official_evaluator_sha256": evaluator_sha}
        if not candidates:
            row.update({"error_type": "MissingOutput", "error": "Trace2Skill produced no output workbook", "model_failure_reason": "trace2skill_task_unsuccessful"})
            rows.append(row)
            continue
        output = max(candidates, key=lambda p: p.stat().st_mtime_ns)
        try:
            score = _official_score(evaluator, task, output, staging / task.category, model_calls=0)
            row.update({"status": "completed", "passed": score.get("accuracy") == 1.0,
                        "outcome_kind": "scored", "official_score": score,
                        "output_workbook": str(output)})
        except Exception as exc:
            row.update({"error_type": type(exc).__name__, "error": str(exc), "output_workbook": str(output),
                        "model_failure_reason": "trace2skill_evaluator_failure"})
        rows.append(row)
    out = root / "results.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {out}: {sum(r['status']=='completed' for r in rows)}/{len(rows)} scored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
