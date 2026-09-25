#!/usr/bin/env python3
"""Augment a plugin ledger with evaluator cell-level failure modes."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


def classify(message: str) -> tuple[str, dict[str, Any]]:
    detail: dict[str, Any] = {"error_message": message}
    match = re.search(
        r"Modification error at ([^!]+)!([A-Z]+\d+): answer=(.*?), output=(.*)$", message
    )
    if match:
        detail.update(sheet=match.group(1), coordinate=match.group(2), answer=match.group(3), output=match.group(4))
        if match.group(4) == "None":
            return "missing-required-cell", detail
        if match.group(3) == "None":
            return "unexpected-write", detail
        return "wrong-value-or-formula", detail
    match = re.search(r"Regression error at ([^!]+)!([A-Z]+\d+): answer=(.*?), output=(.*)$", message)
    if match:
        detail.update(sheet=match.group(1), coordinate=match.group(2), answer=match.group(3), output=match.group(4))
        return "regression-overwrite", detail
    if not message:
        return "unlocalized-scored-failure", detail
    return "other-evaluator-failure", detail


def evaluator_details(root: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for cell_path in root.glob("baseline-trajectories/*/cell.json"):
        try:
            cell = json.loads(cell_path.read_text(encoding="utf-8"))
            trajectory = Path(str(cell["trajectory"]))
            rows = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()]
        except (OSError, KeyError, json.JSONDecodeError):
            continue
        for row in rows:
            if row.get("event") != "spreadsheetbench_v2.evaluated":
                continue
            payload = row.get("payload") or {}
            task_id = str(payload.get("task_id") or "")
            score = payload.get("official_score") or {}
            message = str(score.get("error_message") or "")
            mode, detail = classify(message)
            found[task_id] = {
                "cell_failure_mode": mode,
                "cell_failure_detail": detail,
                "official_accuracy": score.get("accuracy"),
                "official_modification_accuracy": score.get("modification_accuracy"),
                "official_regression_accuracy": score.get("regression_accuracy"),
            }
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--trajectory-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    details = evaluator_details(args.trajectory_root)
    counts: Counter[str] = Counter()
    output: list[dict[str, Any]] = []
    for line in args.ledger.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        extra = details.get(str(row.get("task_id")))
        if extra:
            row.update(extra)
            counts[extra["cell_failure_mode"]] += 1
        output.append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output), encoding="utf-8")
    print(json.dumps({"rows": len(output), "localized": sum(counts.values()), "failure_modes": counts}, ensure_ascii=False, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
