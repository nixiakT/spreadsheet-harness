from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "tools" / "report_coevolution_metrics.py"
_SPEC = importlib.util.spec_from_file_location("report_coevolution_metrics", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_report = _MODULE.build_report


def _write_arm(root, *, passed: bool, score: float, task_id: str = "Financial_Model/t1"):
    root.mkdir()
    (root / "summary.json").write_text(
        json.dumps(
            {
                "study_complete": True,
                "task_count": 1,
                "arms": {
                    "spreadsheet-harness-financial": {
                        "expected": 1,
                        "completed": 1,
                        "passed": int(passed),
                        "errors": 0,
                        "model_execution_failures": 0,
                        "accuracy": score,
                        "modification_accuracy": score,
                        "regression_accuracy": 1.0,
                        "total_tokens": 10,
                        "model_calls": 2,
                    }
                },
            }
        )
    )
    (root / "results.json").write_text(
        json.dumps([{"task_id": task_id, "passed": passed}])
    )


def test_build_report_computes_difference_in_differences(tmp_path):
    scores = {"h0d0": 0.5, "h1d0": 0.6, "h0d1": 0.65, "h1d1": 0.8}
    paths = {}
    for label, score in scores.items():
        path = tmp_path / label
        _write_arm(path, passed=score == 1.0, score=score)
        paths[label] = path

    report = build_report(paths, metric="accuracy")

    assert report["complete"] is True
    assert report["interaction"]["G_H"] == pytest.approx(0.1)
    assert report["interaction"]["G_D"] == pytest.approx(0.15)
    assert report["interaction"]["G_joint"] == pytest.approx(0.3)
    assert report["interaction"]["I"] == pytest.approx(0.05)
    assert report["paired"]["common_task_count"] == 1


def test_build_report_with_incomplete_arm_does_not_compute_interaction(tmp_path):
    paths = {}
    for label in ("h0d0", "h1d0", "h0d1"):
        path = tmp_path / label
        _write_arm(path, passed=False, score=0.0)
        paths[label] = path
    incomplete = tmp_path / "h1d1"
    incomplete.mkdir()
    (incomplete / "summary.json").write_text(
        json.dumps(
            {
                "study_complete": False,
                "task_count": 1,
                "arms": {
                    "spreadsheet-harness-financial": {
                        "expected": 1,
                        "completed": 0,
                        "passed": 0,
                        "accuracy": None,
                    }
                },
            }
        )
    )
    paths["h1d1"] = incomplete

    report = build_report(paths, metric="accuracy")

    assert report["complete"] is False
    assert report["interaction"] is None
    assert "incomplete" in report["incomplete_reason"]
