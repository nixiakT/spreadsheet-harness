from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _launcher():
    path = Path(__file__).resolve().parents[1] / "benchmarks/run_v1_isolated_canary_20260918.py"
    spec = importlib.util.spec_from_file_location("v1_canary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canary_does_not_promote_partial_or_provider_failed_results(tmp_path: Path) -> None:
    launcher = _launcher()
    task = tmp_path / "tasks" / "a"
    task.mkdir(parents=True)
    rows = [
        {"task_id": "a", "arm": "bare", "status": "not_scored", "soft": None,
         "hard": None, "error_type": "ProviderError"},
        {"task_id": "a", "arm": "spreadsheet-harness-basic", "status": "completed",
         "soft": 1.0, "hard": 1, "case_results": [{"passed": True}]},
    ]
    (task / "results.json").write_text(json.dumps(rows))
    result = launcher.summarize(tmp_path, {"tasks": [{"task_id": "a"}, {"task_id": "b"}]})
    assert result["promote_to_default"] is False
    assert result["eligible_for_quality_comparison"] is False
    assert result["complete"] is False
    assert result["arms"]["bare"]["errors"][0]["error_type"] == "ProviderError"
    assert result["arms"]["spreadsheet-harness-basic"]["hard_fixed_denominator_pct"] == 50


def test_complete_canary_still_requires_larger_confirmation(tmp_path: Path) -> None:
    launcher = _launcher()
    task = tmp_path / "tasks" / "a"
    task.mkdir(parents=True)
    (task / "results.json").write_text(json.dumps([
        {"task_id": "a", "arm": arm, "status": "completed", "soft": 1.0, "hard": 1}
        for arm in launcher.ARMS
    ]))
    result = launcher.summarize(tmp_path, {"tasks": [{"task_id": "a"}]})
    assert result["eligible_for_quality_comparison"] is True
    assert result["promote_to_default"] is False
