#!/usr/bin/env python3
"""Extract auditable runtime plugin usage from four existing V2 runs.

This script reads existing trajectories only.  It deliberately ignores
``harness.plugin.activated``, which records composition loading rather than use.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from spreadsheet_harness.plugins import canonical_plugin_name

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "paper_fig_data/plugin_usage_heatmap.csv"
QWEN_BASIC_SNAPSHOT_CUTOFF = "2026-09-17T15:57:58+00:00"

RUNS = (
    {
        "backbone": "DeepSeek-V4-Flash",
        "method": "SHEETHARNESS-BASIC",
        "run": "deepseek-v4-flash-harness-v26-basic-v2-p6-20260915",
        "trace_glob": "tasks/*/runs/*/*/ours/trajectory.jsonl",
    },
    {
        "backbone": "DeepSeek-V4-Flash",
        "method": "SHEETHARNESS-FINANCIAL",
        "run": "deepseek-v4-flash-harness-v26-all-v2-p6-20260915",
        "trace_glob": "tasks/*/runs/*/*/ours/trajectory.jsonl",
    },
    {
        "backbone": "Qwen3-Coder-480B",
        "method": "SHEETHARNESS-BASIC",
        "run": "ours-basic-qwen-v2-20260917",
        "trace_glob": "**/trajectory.jsonl",
        # This continuation was still running during the audit.  Freeze the
        # exported snapshot so rerunning this script cannot silently add tasks.
        "finished_at_cutoff": QWEN_BASIC_SNAPSHOT_CUTOFF,
    },
    {
        "backbone": "Qwen3-Coder-480B",
        "method": "SHEETHARNESS-FINANCIAL",
        "run": "ours-qwen-v2-run2-20260916",
        "trace_glob": "**/trajectory.jsonl",
    },
)

CATEGORY_NAMES = {
    "Template": "Template",
    "Financial_Model": "Financial Modeling",
    "Debugging": "Debugging",
    "Visualization": "Visualization",
}

# Exact registry names from src/spreadsheet_harness/plugins.py.
PLUGIN_META = {
    "act-code-plus-formula-validation": ("Act", "H"),
    "observe-profile-compact": ("Observe", "H"),
    "control-ours": ("Control", "H"),
    "knowledge-structure": ("Knowledge", "H"),
    "knowledge-formula": ("Knowledge", "H"),
    "knowledge-manipulation": ("Knowledge", "H"),
    "knowledge-analysis": ("Knowledge", "H"),
    "knowledge-visualization": ("Knowledge", "H"),
    "knowledge-verification": ("Knowledge", "H"),
    "knowledge-memory": ("Knowledge", "H"),
    "verify-formula-runtime": ("Verify", "H"),
    "repair-date-text": ("Repair", "H"),
    "knowledge-financial-model": ("Knowledge", "D"),
}

VALIDATION_EVENTS = {
    "agent.formula_runtime_validation_passed",
    "agent.formula_runtime_validation_failed",
    "agent.formula_runtime_validation_incomplete",
}


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def skill_plugin(name: str) -> str:
    if name == "visual-review":
        return "knowledge-visualization"
    return canonical_plugin_name(f"skill-{name}")


def result_record(path: Path) -> dict:
    record = read_json(path)
    if isinstance(record, list):
        record = record[0] if record else {}
    return record if isinstance(record, dict) else {}


def task_score(path: Path) -> float | None:
    record = result_record(path)
    score = (record.get("official_score") or {}).get("accuracy")
    return float(score) if isinstance(score, (int, float)) else None


def trace_identity(
    trace: Path, run_root: Path
) -> tuple[str, str, Path, str, str] | None:
    """Return task id/category/result path/start time for a materialized attempt."""

    task_id = category = None
    with trace.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("event") == "spreadsheetbench_v2.configured":
                payload = event.get("payload") or {}
                task_id = payload.get("task_id")
                category = str(task_id).split("/", 1)[0] if task_id else None
                break
    if not task_id or category not in CATEGORY_NAMES:
        return None
    result_path = None
    for parent in trace.parents:
        candidate = parent / "results.json"
        if candidate.is_file():
            result_path = candidate
            break
        if parent == run_root:
            break
    if result_path is None:
        return None
    record = result_record(result_path)
    return (
        str(task_id),
        category,
        result_path,
        str(record.get("started_at") or ""),
        str(record.get("finished_at") or ""),
    )


def select_task_traces(
    run_root: Path, pattern: str, finished_at_cutoff: str | None = None
) -> tuple[dict[str, tuple[Path, Path]], int]:
    """Choose the chronologically latest materialized attempt per task, never best-of."""

    candidates: dict[str, list[tuple[str, Path, Path]]] = defaultdict(list)
    materialized_attempts = 0
    for trace in sorted(run_root.glob(pattern)):
        try:
            identity = trace_identity(trace, run_root)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if identity is None:
            continue
        task_id, _, result_path, started_at, finished_at = identity
        if finished_at_cutoff and (not finished_at or finished_at > finished_at_cutoff):
            continue
        candidates[task_id].append((started_at, trace, result_path))
        materialized_attempts += 1
    selected = {}
    for task_id, task_attempts in candidates.items():
        ordered = sorted(task_attempts, key=lambda item: (item[0], str(item[1])))
        if ordered:
            selected[task_id] = (ordered[-1][1], ordered[-1][2])
    return selected, materialized_attempts


def extract_trace(path: Path) -> Counter[str]:
    calls: Counter[str] = Counter()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            name = str(event.get("event", ""))
            payload = event.get("payload") or {}
            if name == "harness.skills.routed":
                for selected in payload.get("selected", []):
                    plugin = skill_plugin(str(selected))
                    if plugin in PLUGIN_META:
                        calls[plugin] += 1
            elif name == "tool.called":
                calls["act-code-plus-formula-validation"] += 1
            elif name == "preprocess.profile":
                calls["observe-profile-compact"] += 1
            elif name == "agent.started":
                calls["control-ours"] += 1
            elif name in VALIDATION_EVENTS:
                calls["verify-formula-runtime"] += 1
            elif name == "postprocess.date_text_repair":
                # The event exists only when at least one cell was changed.
                calls["repair-date-text"] += 1
            elif name == "harness.financial_domain_runtime.warm_started":
                # This event is guarded by plugin_plan.financial_model_runtime.
                calls["knowledge-financial-model"] += 1
    return calls


def main() -> None:
    fields = [
        "backbone",
        "method",
        "benchmark",
        "category",
        "plugin_name",
        "plugin_role",
        "plugin_group",
        "task_activation_rate",
        "avg_calls_per_task",
        "total_calls",
        "call_share",
        "n_tasks",
        "n_scored_tasks",
        "source_file",
    ]
    output_rows = []
    failure_audit: dict[str, dict[str, dict[str, list[int]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    coverage = []
    for spec in RUNS:
        backbone = spec["backbone"]
        method = spec["method"]
        run_name = spec["run"]
        run_root = ROOT / "benchmarks/results" / run_name
        selected_traces, materialized_attempts = select_task_traces(
            run_root, spec["trace_glob"], spec.get("finished_at_cutoff")
        )
        for raw_category, category in CATEGORY_NAMES.items():
            traces = []
            for task_id, (trace, result_path) in selected_traces.items():
                if task_id.split("/", 1)[0] == raw_category:
                    traces.append((task_id, trace, result_path))
            per_task = []
            parse_failures = []
            for task_id, trace, result_path in traces:
                try:
                    calls = extract_trace(trace)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    parse_failures.append(f"{trace}: {exc}")
                    continue
                per_task.append((task_id, calls, task_score(result_path)))
            coverage.append(
                (
                    backbone,
                    method,
                    category,
                    len(traces),
                    len(per_task),
                    parse_failures,
                    materialized_attempts,
                )
            )
            category_total_calls = sum(sum(calls.values()) for _, calls, _ in per_task)
            n_tasks = {"Template": 97, "Financial_Model": 100, "Debugging": 100, "Visualization": 24}[raw_category]
            n_readable = len(per_task)
            for plugin, (role, group) in PLUGIN_META.items():
                total_calls = sum(calls[plugin] for _, calls, _ in per_task)
                activated = sum(calls[plugin] > 0 for _, calls, _ in per_task)
                output_rows.append(
                    {
                        "backbone": backbone,
                        "method": method,
                        "benchmark": "SpreadsheetBench v2",
                        "category": category,
                        "plugin_name": plugin,
                        "plugin_role": role,
                        "plugin_group": group,
                        "task_activation_rate": (
                            f"{activated / n_readable * 100:.2f}" if n_readable else ""
                        ),
                        "avg_calls_per_task": (
                            f"{total_calls / n_readable:.4f}" if n_readable else ""
                        ),
                        "total_calls": total_calls if n_readable else "",
                        "call_share": (
                            f"{total_calls / category_total_calls * 100:.2f}"
                            if category_total_calls
                            else ""
                        ),
                        "n_tasks": n_tasks,
                        # Requested column name; here it means readable traces,
                        # not official evaluator successes.  README discloses this.
                        "n_scored_tasks": n_readable,
                        "source_file": (
                            f"benchmarks/results/{run_name}/{spec['trace_glob']}"
                        ),
                    }
                )
                for _, calls, exact in per_task:
                    if exact is not None:
                        outcome = "pass" if exact == 1.0 else "fail"
                        failure_audit[f"{backbone}|{method}"][plugin][outcome].append(calls[plugin])

    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)

    for backbone, method, category, found, readable, errors, attempts in coverage:
        print(
            f"coverage,{backbone},{method},{category},{found},{readable},"
            f"{len(errors)},run_materialized_attempts={attempts}"
        )
    for spec in RUNS:
        method_key = f"{spec['backbone']}|{spec['method']}"
        for plugin in (
            "knowledge-verification",
            "verify-formula-runtime",
            "repair-date-text",
            "control-ours",
        ):
            values = failure_audit[method_key][plugin]
            passed = values.get("pass", [])
            failed = values.get("fail", [])
            pass_mean = sum(passed) / len(passed) if passed else None
            fail_mean = sum(failed) / len(failed) if failed else None
            print(
                "failure-audit",
                method_key,
                plugin,
                f"pass_n={len(passed)}",
                f"pass_mean={pass_mean}",
                f"fail_n={len(failed)}",
                f"fail_mean={fail_mean}",
            )


if __name__ == "__main__":
    main()
