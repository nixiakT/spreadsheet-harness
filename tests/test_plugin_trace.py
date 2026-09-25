from __future__ import annotations

import json
from pathlib import Path

from spreadsheet_harness.profile_evolution import ProfileGuidedEvolutionPlanner
from spreadsheet_harness.trajectory import TrajectoryRecorder


def test_plugin_trace_normalizes_usage_and_outcome(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(trajectory, "run-1")
    recorder.record(
        "harness.composition.resolved",
        {
            "composition_sha256": "abc",
            "composition": {
                "plugins": [
                    {"name": "runtime-code-plus-formula-validation", "version": "1.0.0"},
                    {"name": "skill-spreadsheet-financial-model", "version": "1.3.0"},
                ],
                "providers": {
                    "action.spreadsheet": "runtime-code-plus-formula-validation",
                    "knowledge.spreadsheet-financial-model": "skill-spreadsheet-financial-model",
                },
            },
        },
    )
    recorder.record("harness.skills.routed", {"selected": ["spreadsheet-financial-model"]})
    recorder.record("tool.called", {"name": "code_interpreter", "operation": "write_formula"})
    recorder.record("tool.returned", {"name": "code_interpreter", "result": {"ok": True}})
    recorder.record(
        "spreadsheetbench_v2.evaluated",
        {"passed": False, "outcome_kind": "scored", "official_score": {"accuracy": 0.0}},
    )

    profile = json.loads((tmp_path / "plugin-profile.json").read_text())
    plugins = {item["plugin"]: item for item in profile["plugins"]}
    financial = plugins["knowledge-financial-model"]
    runtime = plugins["act-code-plus-formula-validation"]
    assert financial["selected_count"] == 1
    assert financial["outcome_count"] == 1
    assert financial["failure_count"] == 1
    assert runtime["invocation_count"] == 1
    assert runtime["success_count"] == 1
    assert profile["outcome"] == "failure"

    trace = [json.loads(line) for line in (tmp_path / "plugin-trace.jsonl").read_text().splitlines()]
    assert any(row["kind"] == "selected" and row["plugin"] == "knowledge-financial-model" for row in trace)
    assert any(row["kind"] == "outcome" and row["outcome_kind"] == "failure" for row in trace)


def test_profile_planner_exposes_three_mechanisms() -> None:
    planner = ProfileGuidedEvolutionPlanner(
        {
            "profiles": [
                {"plugin": "knowledge-formula", "group": "harness", "invoked_tasks": 10, "evidence_family_count": 8},
                {"plugin": "knowledge-financial-model", "group": "domain", "invoked_tasks": 10, "evidence_family_count": 8},
            ]
        }
    )
    assert planner.plan("h-only")[0].mechanism == "h-only"
    assert planner.plan("d-only")[0].mechanism == "d-only"
    joint = planner.plan("joint")
    assert any(item.operation == "joint" for item in joint)
