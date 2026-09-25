from __future__ import annotations

import json
from pathlib import Path

from spreadsheet_harness.plugin_trace import PluginTraceLedger
from spreadsheet_harness.trajectory import TrajectoryRecorder, read_trajectory


def _run(tmp_path: Path, name: str, *, passed: bool, attributed: bool = False) -> dict:
    path = tmp_path / f"{name}.jsonl"
    recorder = TrajectoryRecorder(path, name)
    recorder.record(
        "harness.composition.resolved",
        {
            "composition": {
                "plugins": [{"name": "skill-spreadsheet-formula"}],
                "providers": {"knowledge.spreadsheet-formula": "skill-spreadsheet-formula"},
            }
        },
    )
    recorder.record("harness.skills.routed", {"selected": ["spreadsheet-formula"]})
    if attributed:
        recorder.record(
            "harness.failure.attributed",
            {"target_plugins": [{"plugin": "skill-spreadsheet-formula"}]},
        )
    recorder.record(
        "spreadsheetbench_v2.evaluated",
        {"passed": passed, "outcome_kind": "scored", "official_score": {"accuracy": float(passed)}},
    )
    return json.loads((tmp_path / "plugin-profile.json").read_text())


def test_failed_attributed_trace_gets_more_weight_than_success(tmp_path: Path) -> None:
    success = _run(tmp_path, "success", passed=True)
    failure = _run(tmp_path, "failure", passed=False, attributed=True)
    assert success["trace_weight"] == 1.0
    assert failure["trace_weight"] > success["trace_weight"]
    plugin = next(item for item in failure["plugins"] if item["plugin"] == "knowledge-formula")
    assert plugin["attribution_count"] == 1
