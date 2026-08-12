from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook

from spreadsheet_harness.audit import audit_comparison
from spreadsheet_harness.benchmark import SpreadsheetTask, compare_workbooks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _book(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.active.title = "Sheet1"
    workbook.active["A1"] = value
    workbook.save(path)
    workbook.close()


def _scoring_metadata_sha256(task: SpreadsheetTask) -> str:
    encoded = json.dumps(
        {
            "answer_position": task.answer_position,
            "answer_sheet": task.answer_sheet,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_sha256(encoded)


def _fixture(tmp_path: Path) -> tuple[Path, SpreadsheetTask, dict[str, Any]]:
    dataset = tmp_path / "dataset"
    initial = dataset / "initial.xlsx"
    golden = dataset / "golden.xlsx"
    _book(initial, 0)
    _book(golden, 42)
    task = SpreadsheetTask(
        task_id="task-1",
        instruction="Set A1 to 42.",
        input_path=initial,
        golden_path=golden,
        instruction_type="Cell-Level Manipulation",
        answer_position="A1",
        answer_sheet="Sheet1",
    )

    results = tmp_path / "comparison"
    run_dir = results / "runs" / task.task_id / "bare"
    output = run_dir / "artifacts" / "output.xlsx"
    _book(output, 42)
    comparison = compare_workbooks(
        task.golden_path,
        output,
        task.answer_position,
        answer_sheet=task.answer_sheet,
    )
    manifest = {
        "schema_version": 8,
        "task_count": 1,
        "task_ids": [task.task_id],
        "arms": ["bare"],
        "tasks": [
            {
                "task_id": task.task_id,
                "instruction_sha256": _text_sha256(task.instruction),
                "input_sha256": _sha256(task.input_path),
                "golden_sha256": _sha256(task.golden_path),
                "scoring_metadata_sha256": _scoring_metadata_sha256(task),
            }
        ],
    }
    results.mkdir(parents=True, exist_ok=True)
    (results / "comparison-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    row = {
        "task_id": task.task_id,
        "arm": "bare",
        "status": "completed",
        "passed": comparison.passed,
        "comparison": comparison.to_dict(),
        "run_dir": str(run_dir),
        "output_workbook": str(output),
        "recalculation": {"output_sha256": _sha256(output)},
    }
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return results, task, row


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _row_reason(summary: dict[str, Any], reason: str) -> bool:
    return any(reason in row["reasons"] for row in summary["rows"])


def test_audit_comparison_valid_and_read_only(tmp_path: Path) -> None:
    results, task, _ = _fixture(tmp_path)
    before = _tree_hashes(tmp_path)

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is True
    assert summary["reasons"] == []
    assert summary["expected_rows"] == summary["observed_rows"] == 1
    assert summary["valid_rows"] == 1
    assert summary["rows"][0]["audit_valid"] is True
    assert summary["rows"][0]["fresh_comparison"]["passed"] is True
    assert summary["rows"][0]["output_sha256"] == summary["rows"][0][
        "expected_output_sha256"
    ]
    assert _tree_hashes(tmp_path) == before


def test_audit_rejects_tampered_stored_passed(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    row["passed"] = False
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert _row_reason(summary, "stored_passed_mismatch")


def test_audit_rejects_tampered_workbook(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    output = Path(row["output_workbook"])
    workbook = load_workbook(output)
    workbook["Sheet1"]["A1"] = 99
    workbook.save(output)
    workbook.close()

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert _row_reason(summary, "artifact_hash_mismatch")
    assert _row_reason(summary, "stored_passed_mismatch")
    assert _row_reason(summary, "stored_comparison_mismatch")


@pytest.mark.parametrize("path_field", ["run_dir", "output_workbook"])
def test_audit_rejects_artifact_path_outside_managed_arm(
    tmp_path: Path, path_field: str
) -> None:
    results, task, row = _fixture(tmp_path)
    outside_run = tmp_path / "outside"
    outside_output = outside_run / "artifacts" / "output.xlsx"
    _book(outside_output, 42)
    if path_field == "run_dir":
        row[path_field] = str(outside_run)
    else:
        row[path_field] = str(outside_output)
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    expected = (
        "run_dir_outside_expected_arm"
        if path_field == "run_dir"
        else "output_path_not_managed_artifact"
    )
    assert _row_reason(summary, expected)


def test_audit_rejects_duplicate_result_row(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    (results / "results.jsonl").write_text(
        json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8"
    )

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert summary["observed_rows"] == 2
    assert _row_reason(summary, "duplicate_result_rows")


def test_audit_rejects_missing_result_row(tmp_path: Path) -> None:
    results, task, _ = _fixture(tmp_path)
    (results / "results.jsonl").write_text("", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert summary["expected_rows"] == 1
    assert summary["observed_rows"] == 0
    assert _row_reason(summary, "missing_result_row")
