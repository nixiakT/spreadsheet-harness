#!/usr/bin/env python3
"""Report four-arm co-evolution results by task complexity and workbook family.

This intentionally tolerates an incomplete matrix: missing tasks remain missing,
while completed tasks are aggregated with explicit denominators.  It is useful
for monitoring long Harbor runs and for separating C1 from the hard C2/C3
financial-model strata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ARMS = ("h0d0", "h1d0", "h0d1", "h1d1")
METRICS = ("accuracy", "modification_accuracy", "regression_accuracy")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_rows(root: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for summary_path in sorted(root.rglob("summary.json")):
        try:
            summary = _read(summary_path)
        except (OSError, json.JSONDecodeError):
            continue
        # Interrupted attempts are archived beside the live output.  Their
        # partial results must not be treated as scored observations.
        if not isinstance(summary, dict) or not summary.get("study_complete"):
            continue
        result_path = summary_path.with_name("results.json")
        if not result_path.is_file():
            continue
        raw = _read(result_path)
        if not isinstance(raw, list):
            continue
        for row in raw:
            if isinstance(row, dict) and row.get("task_id"):
                rows[str(row["task_id"])] = row
    return rows


def _arm_records(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    rows = _task_rows(root)
    totals = Counter()
    complete = True
    for summary_path in sorted(root.rglob("summary.json")):
        summary = _read(summary_path)
        arms = summary.get("arms") if isinstance(summary, dict) else None
        if not isinstance(arms, dict) or len(arms) != 1:
            continue
        record = next(iter(arms.values()))
        expected = int(record.get("expected", 0) or 0)
        completed = int(record.get("completed", 0) or 0)
        totals["expected"] += expected
        totals["completed"] += completed
        totals["errors"] += int(record.get("errors", 0) or 0)
        totals["model_execution_failures"] += int(record.get("model_execution_failures", 0) or 0)
        totals["model_calls"] += int(record.get("model_calls", 0) or 0)
        totals["total_tokens"] += int(record.get("total_tokens", 0) or 0)
        complete = complete and bool(summary.get("study_complete"))
    complete = complete and bool(rows) and totals["expected"] == totals["completed"]
    return {"complete": complete, "counts": dict(totals), "task_count": len(rows)}, rows


def _weighted(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"task_count": len(rows), "scored_count": 0, "not_scored": 0}
    for metric in METRICS:
        values = []
        for row in rows:
            score = row.get("official_score")
            if isinstance(score, dict) and score.get(metric) is not None:
                values.append(float(score[metric]))
        out[metric] = sum(values) / len(values) if values else None
        if metric == "accuracy":
            out["scored_count"] = len(values)
    out["not_scored"] = len(rows) - out["scored_count"]
    out["not_scored_rate"] = out["not_scored"] / len(rows) if rows else None
    out["passed"] = sum(bool(row.get("passed")) for row in rows)
    out["model_failures"] = sum(
        1 for row in rows if row.get("outcome_kind") in {"model_execution_failure", "error"}
    )
    out["tokens"] = sum(
        int((row.get("agent") or {}).get("usage", {}).get("total_tokens", 0) or 0)
        for row in rows
    )
    out["turns"] = sum(int((row.get("agent") or {}).get("turns", 0) or 0) for row in rows)
    return out


def _paired(arm_rows: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    common = set.intersection(*(set(rows) for rows in arm_rows.values())) if arm_rows else set()
    pairs: dict[str, Any] = {}
    for i, left in enumerate(ARMS):
        for right in ARMS[i + 1 :]:
            lw = rw = ties = 0
            for task in common:
                lp = bool(arm_rows[left][task].get("passed"))
                rp = bool(arm_rows[right][task].get("passed"))
                if lp and not rp:
                    lw += 1
                elif rp and not lp:
                    rw += 1
                else:
                    ties += 1
            pairs[f"{left}__vs__{right}"] = {"left_wins": lw, "ties": ties, "right_wins": rw}
    return {"common_task_count": len(common), "pairwise": pairs}


def build(args: argparse.Namespace) -> dict[str, Any]:
    dataset = Path(args.dataset)
    metadata = {str(item["id"]): item for item in _read(dataset / "Financial_Model" / "dataset.json")}
    arm_rows: dict[str, dict[str, dict[str, Any]]] = {}
    arm_info: dict[str, Any] = {}
    for label, path in args.arm.items():
        info, rows = _arm_records(path)
        arm_info[label] = info
        arm_rows[label] = rows
    strata: dict[str, dict[str, Any]] = {}
    families: dict[str, dict[str, Any]] = {}
    for complexity in ("C1", "C2", "C3", "unknown"):
        strata[complexity] = {}
        for arm in ARMS:
            selected = [
                row for task, row in arm_rows[arm].items()
                if metadata.get(task.split("/", 1)[-1], {}).get("complexity", "unknown") == complexity
            ]
            strata[complexity][arm] = _weighted(selected)
    for task_id, item in metadata.items():
        family = str(item.get("source_workbook", "unknown"))
        families.setdefault(family, {"complexity": item.get("complexity", "unknown"), "arms": {}})
        for arm in ARMS:
            row = arm_rows[arm].get("Financial_Model/" + task_id)
            if row:
                families[family]["arms"].setdefault(arm, []).append(row)
        families[family]["task_id_count"] = families[family].get("task_id_count", 0) + 1
    for family, info in families.items():
        info["metrics"] = {arm: _weighted(info["arms"].get(arm, [])) for arm in ARMS}
        info.pop("arms", None)
    source = _read(dataset / "harbor_source.json") if (dataset / "harbor_source.json").is_file() else {}
    dataset_hash = hashlib.sha256((dataset / "Financial_Model" / "dataset.json").read_bytes()).hexdigest()
    return {
        "schema_version": "coevolution-stratified-report-v1",
        "dataset": str(dataset),
        "dataset_source": source,
        "normalized_dataset_sha256": dataset_hash,
        "arm_paths": {k: str(v) for k, v in args.arm.items()},
        "arms": arm_info,
        "strata": strata,
        "source_workbook_families": families,
        "paired": {"overall": _paired(arm_rows), "by_complexity": {
            c: _paired({a: {t: r for t, r in arm_rows[a].items()
                            if metadata.get(t.split("/", 1)[-1], {}).get("complexity", "unknown") == c}
                         for a in ARMS}) for c in ("C1", "C2", "C3")
        }},
    }


def markdown(report: dict[str, Any]) -> str:
    lines = ["# Stratified co-evolution report", "", f"Dataset: `{report['dataset']}`", "",
             "| Complexity | Arm | Tasks | Accuracy | Modification | Regression | Passed | Not scored | Tokens |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for complexity in ("C1", "C2", "C3", "unknown"):
        for arm in ARMS:
            x = report["strata"][complexity][arm]
            lines.append(f"| {complexity} | {arm} | {x['task_count']} | {x['accuracy']} | {x['modification_accuracy']} | {x['regression_accuracy']} | {x['passed']} | {x['not_scored']} | {x['tokens']} |")
    lines += ["", "## Paired outcomes by complexity", ""]
    for c, pair in report["paired"]["by_complexity"].items():
        lines.append(f"- `{c}` common tasks: {pair['common_task_count']}; " + ", ".join(f"{k}={v['left_wins']}/{v['ties']}/{v['right_wins']}" for k, v in pair['pairwise'].items()))
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--arm", action="append", required=True, metavar="LABEL=PATH")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--markdown", type=Path)
    ns = p.parse_args()
    arms = {}
    for value in ns.arm:
        label, path = value.split("=", 1)
        if label not in ARMS:
            raise SystemExit(f"invalid arm {label}")
        arms[label] = Path(path)
    if set(arms) != set(ARMS):
        raise SystemExit("all four arms are required")
    ns.arm = arms
    report = build(ns)
    ns.output.parent.mkdir(parents=True, exist_ok=True)
    ns.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if ns.markdown:
        ns.markdown.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"output": str(ns.output), "dataset": str(ns.dataset), "complete": all(v["complete"] for v in report["arms"].values())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
