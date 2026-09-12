#!/usr/bin/env python3
"""Aggregate four-arm co-evolution runs without treating missing work as failure.

Each ``--arm`` points at either one ``v2-compare`` output directory or a
directory containing one output directory per task.  The command emits JSON by
default and can also write a small Markdown table for reports.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from spreadsheet_harness.capability_evolution import four_arm_metrics

_METRICS = ("accuracy", "modification_accuracy", "regression_accuracy")
_ALIASES = {
    "pass": "accuracy",
    "exact_pass": "accuracy",
    "modification": "modification_accuracy",
    "regression": "regression_accuracy",
}


def _summary_files(root: Path) -> list[Path]:
    files = sorted(root.rglob("summary.json")) if root.is_dir() else [root]
    # A parent summary may aggregate all children. Prefer task-level summaries
    # when both are present so tasks are not counted twice.
    return [
        path
        for path in files
        if not any(other != path and path.parent in other.parents for other in files)
    ]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc


def _rows_for_summary(summary_path: Path) -> list[dict[str, Any]]:
    result_path = summary_path.with_name("results.json")
    if not result_path.is_file():
        return []
    raw = _read_json(result_path)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _arm_record(summary: dict[str, Any], *, source: Path) -> tuple[str, dict[str, Any]]:
    arms = summary.get("arms")
    if not isinstance(arms, dict) or not arms:
        raise ValueError(f"{source} has no v2 arm summary")
    if len(arms) != 1:
        raise ValueError(
            f"{source} contains multiple arms; provide one output directory per arm"
        )
    name, record = next(iter(arms.items()))
    if not isinstance(record, dict):
        raise ValueError(f"{source} arm {name!r} is not an object")
    return str(name), record


def _aggregate_arm(label: str, root: Path) -> dict[str, Any]:
    if not root.exists():
        raise ValueError(f"Arm path does not exist: {root}")
    summaries = _summary_files(root)
    if not summaries:
        raise ValueError(f"No summary.json found under arm path: {root}")
    totals = Counter()
    outcome_kinds = Counter()
    weighted: dict[str, float] = Counter()
    weighted_denominator = 0
    task_rows: dict[str, dict[str, Any]] = {}
    source_arms: set[str] = set()
    complete = True
    for summary_path in summaries:
        summary = _read_json(summary_path)
        if not isinstance(summary, dict):
            raise ValueError(f"{summary_path} is not an object")
        source_arm, record = _arm_record(summary, source=summary_path)
        source_arms.add(source_arm)
        expected = int(record.get("expected", summary.get("task_count", 0)) or 0)
        completed = int(record.get("completed", 0) or 0)
        totals["expected"] += expected
        totals["completed"] += completed
        totals["passed"] += int(record.get("passed", 0) or 0)
        totals["model_execution_failures"] += int(record.get("model_execution_failures", 0) or 0)
        totals["errors"] += int(record.get("errors", 0) or 0)
        totals["model_calls"] += int(record.get("model_calls", 0) or 0)
        totals["total_tokens"] += int(record.get("total_tokens", 0) or 0)
        complete = complete and expected == completed and bool(summary.get("study_complete", True))
        denominator = max(expected, 1)
        weighted_denominator += denominator
        for metric in _METRICS:
            value = record.get(metric)
            if value is not None:
                weighted[metric] += float(value) * denominator
        for row in _rows_for_summary(summary_path):
            task_id = str(row.get("task_id", "")).strip()
            if task_id:
                task_rows[task_id] = row
            outcome = str(row.get("outcome_kind", "unknown"))
            outcome_kinds[outcome] += 1
            official_score = row.get("official_score")
            if not isinstance(official_score, dict) or official_score.get("accuracy") is None:
                totals["not_scored"] += 1
            agent = row.get("agent")
            if isinstance(agent, dict):
                totals["turns"] += int(agent.get("turns", 0) or 0)
                totals["tool_calls"] += int(agent.get("tool_calls", 0) or 0)
                totals["tool_errors"] += int(agent.get("tool_errors", 0) or 0)
                totals["parallel_tool_batches"] += int(
                    agent.get("parallel_tool_batches", 0) or 0
                )
    totals["not_scored_rate"] = (
        totals["not_scored"] / len(task_rows) if task_rows else None
    )
    if len(source_arms) != 1:
        raise ValueError(f"Arm {label} mixes source arm names: {sorted(source_arms)}")
    metrics = {
        metric: (weighted[metric] / weighted_denominator if weighted_denominator else None)
        for metric in _METRICS
    }
    return {
        "label": label,
        "path": str(root),
        "source_arm": next(iter(source_arms)),
        "task_count": len(task_rows) or totals["expected"],
        "complete": complete,
        "metrics": metrics,
        "counts": dict(totals),
        "outcome_kinds": dict(outcome_kinds),
        "task_rows": task_rows,
    }


def _paired(arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    common = set.intersection(*(set(item["task_rows"]) for item in arms.values()))
    rows: dict[str, dict[str, int]] = {}
    for task_id in sorted(common):
        values = {
            label: bool(arm["task_rows"][task_id].get("passed"))
            for label, arm in arms.items()
        }
        rows[task_id] = {label: int(value) for label, value in values.items()}
    pairwise: dict[str, dict[str, int]] = {}
    labels = list(arms)
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            wins_left = sum(rows[task][left] > rows[task][right] for task in rows)
            wins_right = sum(rows[task][right] > rows[task][left] for task in rows)
            pairwise[f"{left}__vs__{right}"] = {
                "left_wins": wins_left,
                "ties": len(rows) - wins_left - wins_right,
                "right_wins": wins_right,
            }
    return {"common_task_count": len(rows), "pairwise": pairwise}


def build_report(arm_paths: dict[str, Path], *, metric: str) -> dict[str, Any]:
    metric = _ALIASES.get(metric, metric)
    if metric not in _METRICS:
        raise ValueError(f"metric must be one of {', '.join(_METRICS)}")
    if set(arm_paths) != {"h0d0", "h1d0", "h0d1", "h1d1"}:
        raise ValueError("Exactly h0d0, h1d0, h0d1, and h1d1 arms are required")
    arms = {label: _aggregate_arm(label, path) for label, path in arm_paths.items()}
    scores = {label: arms[label]["metrics"][metric] for label in arms}
    complete = all(arm["complete"] for arm in arms.values())
    interaction = None
    if complete and all(value is not None for value in scores.values()):
        interaction = four_arm_metrics(scores)
    for arm in arms.values():
        arm.pop("task_rows", None)
    return {
        "schema_version": "coevolution-four-arm-report-v1",
        "metric": metric,
        "complete": complete,
        "arms": arms,
        "paired": _paired({label: _aggregate_arm(label, path) for label, path in arm_paths.items()}),
        "interaction": interaction,
        "incomplete_reason": None if complete else "one or more arms have missing or incomplete tasks",
    }


def _markdown(report: dict[str, Any]) -> str:
    metric = report["metric"]
    lines = [
        "# Co-evolution four-arm report",
        "",
        f"Metric: `{metric}`; complete: `{report['complete']}`",
        "",
        "| Arm | Score | Expected | Completed | Passed | Tokens | Turns | Tool calls | Tool errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label in ("h0d0", "h1d0", "h0d1", "h1d1"):
        arm = report["arms"][label]
        counts = arm["counts"]
        lines.append(
            f"| `{label}` | {arm['metrics'][metric]} | {counts.get('expected', 0)} | "
            f"{counts.get('completed', 0)} | {counts.get('passed', 0)} | "
            f"{counts.get('total_tokens', 0)} | {counts.get('turns', 0)} | "
            f"{counts.get('tool_calls', 0)} | {counts.get('tool_errors', 0)} |"
        )
    interaction = report.get("interaction")
    if interaction:
        lines.extend(
            [
                "",
                f"`G_H={interaction['G_H']}`, `G_D={interaction['G_D']}`, "
                f"`G_joint={interaction['G_joint']}`, `I={interaction['I']}`",
            ]
        )
    else:
        lines.extend(["", "Interaction gain is unavailable until all four arms are complete."])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="Repeat four times for h0d0, h1d0, h0d1, h1d1",
    )
    parser.add_argument("--metric", default="accuracy", help="Score used for interaction gain")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path")
    parser.add_argument("--markdown", type=Path, help="Write a Markdown report to this path")
    args = parser.parse_args()
    paths: dict[str, Path] = {}
    for item in args.arm:
        if "=" not in item:
            parser.error(f"Invalid --arm {item!r}; expected LABEL=PATH")
        label, raw_path = item.split("=", 1)
        if label in paths:
            parser.error(f"Duplicate arm label: {label}")
        paths[label] = Path(raw_path).expanduser().resolve()
    try:
        report = build_report(paths, metric=args.metric)
    except ValueError as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_markdown(report), encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
