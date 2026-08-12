from __future__ import annotations

from pathlib import Path

from spreadsheet_harness.trajectory import TrajectoryRecorder, read_trajectory


def test_recorder_redacts_secrets_and_image_data(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    recorder.record(
        "event",
        {
            "api_key": "secret-value",
            "text": "Bearer cr_abcdefghijklmnopqrstuvwxyz123456",
            "image": "data:image/png;base64,aGVsbG8=",
        },
    )
    raw = path.read_text(encoding="utf-8")
    assert "secret-value" not in raw
    assert "cr_abcdefghijklmnopqrstuvwxyz123456" not in raw
    assert "aGVsbG8=" not in raw
    row = read_trajectory(path)[0]
    assert row["payload"]["api_key"] == "[REDACTED]"
    assert "IMAGE_DATA_URL" in row["payload"]["image"]
