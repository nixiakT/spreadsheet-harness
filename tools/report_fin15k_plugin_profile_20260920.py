#!/usr/bin/env python3
"""Build a plugin-level profile from Fin-1.5K development trajectories.

This is deliberately read-only with respect to source and benchmark artifacts.
It produces a task ledger plus aggregate plugin profile so evolution routing can
use activation, failure, cost, and evidence diversity rather than raw support
count alone.  SpreadsheetBench is not read here and cannot influence routing.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from spreadsheet_harness.plugins import canonical_plugin_name
from spreadsheet_harness.plugin_trace import PluginTraceLedger


GENERAL = {
    "act-code-plus-formula-validation": ("act", "harness"),
    "observe-profile-compact": ("observe", "harness"),
    "control-ours": ("control", "harness"),
    "knowledge-structure": ("knowledge", "harness"),
    "knowledge-formula": ("knowledge", "harness"),
    "knowledge-manipulation": ("knowledge", "harness"),
    "knowledge-analysis": ("knowledge", "harness"),
    "knowledge-visualization": ("knowledge", "harness"),
    "knowledge-verification": ("verify", "harness"),
    "knowledge-memory": ("knowledge", "harness"),
    "verify-formula-runtime": ("verify", "harness"),
    "repair-date-text": ("repair", "harness"),
}
DOMAIN = {"knowledge-financial-model": ("knowledge", "domain")}
PLUGIN_META = {**GENERAL, **DOMAIN}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def event_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def plugin_from_skill(name: str) -> str:
    if name == "visual-review":
        return "knowledge-visualization"
    return canonical_plugin_name(f"skill-{name}")


def numeric(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0


def task_metrics(
    rows: list[dict[str, Any]], cell: Mapping[str, Any], trajectory_path: Path
) -> dict[str, Any]:
    task_id = ""
    family = ""
    category = "Financial_Model"
    active: set[str] = set()
    invoked: Counter[str] = Counter()
    selected: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    failure_categories: Counter[str] = Counter()
    tool_calls = 0
    model_calls = 0
    tokens = 0.0
    elapsed = 0.0
    score: float | None = None
    outcome = "unknown"
    for row in rows:
        event = str(row.get("event", ""))
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        event_counts[event] += 1
        if event == "spreadsheetbench_v2.configured":
            task_id = str(payload.get("task_id", ""))
            category = task_id.split("/", 1)[0] if "/" in task_id else category
        elif event == "harness.composition.resolved":
            composition = payload.get("composition")
            if isinstance(composition, Mapping):
                for item in composition.get("plugins", []):
                    if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                        active.add(canonical_plugin_name(str(item["name"])))
        elif event == "harness.plugin.activated":
            plugin = payload.get("plugin")
            if isinstance(plugin, str):
                active.add(canonical_plugin_name(plugin))
        elif event == "harness.skills.routed":
            for name in payload.get("selected", []) or []:
                if isinstance(name, str):
                    plugin = plugin_from_skill(name)
                    selected[plugin] += 1
                    invoked[plugin] += 1
        elif event == "tool.called":
            tool_calls += 1
            invoked["act-code-plus-formula-validation"] += 1
        elif event == "preprocess.profile":
            invoked["observe-profile-compact"] += 1
        elif event == "agent.started":
            invoked["control-ours"] += 1
        elif event in {
            "agent.formula_runtime_validation_passed",
            "agent.formula_runtime_validation_failed",
            "agent.formula_runtime_validation_incomplete",
        }:
            invoked["verify-formula-runtime"] += 1
        elif event == "postprocess.date_text_repair":
            invoked["repair-date-text"] += 1
        elif event in {"tool.failed", "evaluation.failed", "agent.execution_failed"}:
            category_name = payload.get("error_category") or payload.get("reason") or event
            failure_categories[str(category_name)[:120]] += 1
        elif event in {"model.requested", "model.responded"}:
            if event == "model.requested":
                model_calls += 1
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                tokens += numeric(
                    usage.get("total_tokens", usage.get("input_tokens", 0))
                )
        if event == "spreadsheetbench_v2.evaluated":
            official = payload.get("official_score")
            if isinstance(official, Mapping) and isinstance(official.get("accuracy"), (int, float)):
                score = float(official["accuracy"])
            outcome = str(payload.get("outcome", outcome))
        timestamp = row.get("timestamp")
        if isinstance(timestamp, str):
            # Keep elapsed from explicit payloads when present; timestamps are
            # intentionally not parsed here because provider clocks may differ.
            elapsed = max(elapsed, numeric(payload.get("elapsed_seconds")))
    # Plugin usage is always taken from the native normalizer.  The remaining
    # loop below extracts task-level cost/failure fields, but does not define a
    # second plugin mapping.
    native_profile = PluginTraceLedger.replay_rows(trajectory_path, rows)
    active = set(native_profile["active_plugins"])
    invoked = Counter(
        {
            str(item["plugin"]): int(item.get("invocation_count", 0))
            for item in native_profile["plugins"]
            if int(item.get("invocation_count", 0)) > 0
        }
    )
    selected = Counter(
        {
            str(item["plugin"]): int(item.get("selected_count", 0))
            for item in native_profile["plugins"]
            if int(item.get("selected_count", 0)) > 0
        }
    )
    trace_weight = float(native_profile.get("trace_weight", 0.0) or 0.0)
    attributed_plugins = {
        str(key): int(value)
        for key, value in (native_profile.get("attributed_plugins") or {}).items()
    }
    if not task_id:
        return {}
    task_doc = cell.get("task") if isinstance(cell.get("task"), Mapping) else {}
    family = str(task_doc.get("source_workbook") or task_id)
    if not task_id:
        task_id = str(task_doc.get("task_id", ""))
        category = task_id.split("/", 1)[0] if "/" in task_id else category
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        source = payload.get("source_workbook") or payload.get("workbook_family")
        if isinstance(source, str) and source:
            family = source
            break
    summary_path = cell.get("summary")
    summary = {}
    if isinstance(summary_path, str) and Path(summary_path).is_file():
        try:
            summary = read_json(Path(summary_path))
        except (OSError, json.JSONDecodeError):
            summary = {}
    arm = summary.get("arms", {}).get("spreadsheet-harness-financial", {})
    summary_score = arm.get("scored_accuracy")
    if isinstance(summary_score, (int, float)) and math.isfinite(float(summary_score)):
        score = float(summary_score)
    summary_tokens = arm.get("total_tokens")
    summary_calls = arm.get("model_calls")
    if isinstance(summary_tokens, (int, float)):
        tokens = float(summary_tokens)
    if isinstance(summary_calls, int):
        model_calls = summary_calls
    if score is not None:
        outcome = "pass" if score >= 1.0 else "fail"
    return {
        "task_id": task_id,
        "workbook_family": family,
        "category": category,
        "active_plugins": sorted(active),
        "selected_plugins": sorted(selected),
        "invoked": dict(invoked),
        "selected": dict(selected),
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "tokens": tokens,
        "elapsed_seconds": elapsed,
        "score": score,
        "outcome": outcome,
        "failure_categories": dict(failure_categories),
        "trace_weight": trace_weight,
        "weight_components": native_profile.get("weight_components", {}),
        "failure_reasons": native_profile.get("failure_reasons", {}),
        "attributed_plugins": attributed_plugins,
        "weighted_plugin_contribution": {
            plugin: trace_weight * (1.25 if plugin in attributed_plugins else 1.0)
            for plugin in invoked
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    output = (args.output or root / "plugin-profile").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for cell_path in sorted((root / "baseline-trajectories").glob("*/cell.json")):
        cell = read_json(cell_path)
        if cell.get("status") != "complete":
            continue
        trajectory = Path(str(cell.get("trajectory", "")))
        if not trajectory.is_file():
            continue
        metric = task_metrics(event_rows(trajectory), cell, trajectory)
        if metric:
            rows.append(metric)
    ledger_path = output / "plugin-task-ledger.jsonl"
    with ledger_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    all_plugins = sorted(set(PLUGIN_META) | {p for row in rows for p in row["active_plugins"]})
    profile: list[dict[str, Any]] = []
    task_count = len(rows)
    for plugin in all_plugins:
        invoked_rows = [row for row in rows if row["invoked"].get(plugin, 0) > 0]
        scored_invoked = [row["score"] for row in invoked_rows if row["score"] is not None]
        scored_not = [
            row["score"]
            for row in rows
            if row["invoked"].get(plugin, 0) == 0 and row["score"] is not None
        ]
        support_families = {
            row["workbook_family"] for row in invoked_rows if row["workbook_family"]
        }
        failures = Counter()
        for row in invoked_rows:
            failures.update(row["failure_categories"])
        weighted_invoked = sum(
            float(row.get("weighted_plugin_contribution", {}).get(plugin, 0.0) or 0.0)
            for row in rows
        )
        weighted_attributed = sum(
            float(row.get("trace_weight", 0.0) or 0.0)
            for row in rows
            if plugin in row.get("attributed_plugins", {})
        )
        role, group = PLUGIN_META.get(plugin, ("unknown", "unknown"))
        mean_invoked = sum(scored_invoked) / len(scored_invoked) if scored_invoked else None
        mean_not = sum(scored_not) / len(scored_not) if scored_not else None
        profile.append(
            {
                "plugin": plugin,
                "role": role,
                "group": group,
                "tasks": task_count,
                "invoked_tasks": len(invoked_rows),
                "activation_rate": (len(invoked_rows) / task_count if task_count else None),
                "total_invocations": sum(row["invoked"].get(plugin, 0) for row in rows),
                "avg_invocations_per_task": (
                    sum(row["invoked"].get(plugin, 0) for row in rows) / task_count
                    if task_count
                    else None
                ),
                "evidence_family_count": len(support_families),
                "mean_score_when_invoked": mean_invoked,
                "mean_score_when_not_invoked": mean_not,
                "descriptive_score_delta": (
                    mean_invoked - mean_not
                    if mean_invoked is not None and mean_not is not None
                    else None
                ),
                "mean_tokens_per_invoked_task": (
                    sum(row["tokens"] for row in invoked_rows) / len(invoked_rows)
                    if invoked_rows
                    else None
                ),
                "mean_model_calls_per_invoked_task": (
                    sum(row["model_calls"] for row in invoked_rows) / len(invoked_rows)
                    if invoked_rows
                    else None
                ),
                "mean_elapsed_seconds_per_invoked_task": (
                    sum(row["elapsed_seconds"] for row in invoked_rows) / len(invoked_rows)
                    if invoked_rows
                    else None
                ),
                "failure_categories": dict(failures),
                "weighted_invocation_mass": weighted_invoked,
                "weighted_attribution_mass": weighted_attributed,
            }
        )
    (output / "plugin-profile.json").write_text(
        json.dumps(
            {
                "schema_version": "fin15k-plugin-profile-v1",
                "source_root": str(root),
                "development_only": True,
                "spreadsheetbench_used": False,
                "task_count": task_count,
                "profiles": profile,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fields = [key for key in profile[0] if key != "failure_categories"] if profile else []
    with (output / "plugin-profile.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in profile:
            writer.writerow({key: row.get(key) for key in fields})
    print(json.dumps({"tasks": task_count, "plugins": len(profile), "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
