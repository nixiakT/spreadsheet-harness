from __future__ import annotations

import json
from pathlib import Path

from spreadsheet_harness.plugin_trace import PluginTraceLedger
from spreadsheet_harness.trajectory import TrajectoryRecorder, read_trajectory


def _stable(profile: dict) -> dict:
    return {
        key: value
        for key, value in profile.items()
        if key not in {"trajectory", "run_id", "updated_at"}
    }


def test_replay_uses_the_same_normalizer_as_live_sidecar(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(trajectory, "same-run")
    recorder.record(
        "harness.composition.resolved",
        {
            "composition_sha256": "c",
            "composition": {
                "plugins": [{"name": "skill-spreadsheet-formula", "version": "1.0.0"}],
                "providers": {"knowledge.spreadsheet-formula": "skill-spreadsheet-formula"},
            },
        },
    )
    recorder.record("harness.skills.routed", {"selected": ["spreadsheet-formula"]})
    recorder.record(
        "spreadsheetbench_v2.evaluated",
        {"passed": True, "outcome_kind": "scored", "official_score": {"accuracy": 1.0}},
    )

    live = json.loads((tmp_path / "plugin-profile.json").read_text())
    replay = PluginTraceLedger.replay_rows(trajectory, read_trajectory(trajectory))
    assert _stable(live) == _stable(replay)


def test_model_failure_is_provider_not_plugin_execution_failure(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(trajectory, "provider-run")
    recorder.record(
        "harness.composition.resolved",
        {
            "composition_sha256": "c",
            "composition": {
                "plugins": [{"name": "knowledge-formula", "version": "1.0.0"}],
                "providers": {"knowledge.spreadsheet-formula": "knowledge-formula"},
            },
        },
    )
    recorder.record("model.failed", {"provider_error": {"status_code": 429}})
    profile = PluginTraceLedger.replay_rows(trajectory, read_trajectory(trajectory))
    assert profile["failure_reasons"]["provider_failure"] == 1
    assert "execution_failure" not in profile["failure_reasons"]
