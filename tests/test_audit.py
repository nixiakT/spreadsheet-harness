from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook

from spreadsheet_harness.audit import audit_comparison
from spreadsheet_harness.benchmark import SpreadsheetTask, compare_workbooks
from spreadsheet_harness.comparison import (
    COMPARISON_PROTOCOL_VERSION,
    ComparisonBenchmarkRunner,
)
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.skills import SkillRegistry


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
    runner = ComparisonBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            reasoning_effort="none",
        ),
        results,
        skill_registry=SkillRegistry([]),
        arms=("bare",),
        max_model_calls=3,
        max_turns_per_arm=3,
        max_total_tokens=100,
        max_output_tokens=64,
        task_timeout_seconds=30,
        recalculate=False,
    )
    runner._prepare_manifest([task])
    manifest = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = _sha256(runner.manifest_path)
    run_dir = results / "runs" / task.task_id / "bare"
    output = run_dir / "artifacts" / "output.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(task.golden_path, output)
    comparison = compare_workbooks(
        task.golden_path,
        output,
        task.answer_position,
        answer_sheet=task.answer_sheet,
    )
    attempt = {"api_protocol": "responses", "endpoint": "/responses"}
    timings = [
        {
            "turn": turn,
            "attempts": 1,
            "attempt_history": [attempt],
        }
        for turn in range(1, 4)
    ]
    stage_agent = {
        "turns": 3,
        "request_timings": timings,
    }
    stage = {
        "name": "solve",
        "max_turns": 3,
        "allowed_tools": ["code_interpreter"],
        "first_tool_choice": "code_interpreter",
        "observed_first_tool": "code_interpreter",
        "forced_tool_prefix": ["code_interpreter", "code_interpreter"],
        "observed_forced_tool_prefix": ["code_interpreter", "code_interpreter"],
        "terminal_tool": "submit_result",
        "observed_terminal_tool": "submit_result",
        "agent": stage_agent,
    }
    row = {
        "task_id": task.task_id,
        "arm": "bare",
        "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
        "comparison_manifest_sha256": manifest_sha256,
        "instruction_type": task.instruction_type,
        "model": "test-model",
        "api_protocol": "responses",
        "requested_reasoning_effort": "none",
        "reasoning_effort": "none",
        "request_interval_seconds": 0.0,
        "litellm_timeout_seconds": None,
        "generation": manifest["configuration"]["generation"],
        "max_model_calls": 3,
        "max_turns_per_arm": 3,
        "stage_turn_caps": {"solve": 3},
        "calculation_backend": "not_recalculated",
        "status": "completed",
        "passed": comparison.passed,
        "comparison": comparison.to_dict(),
        "agent": {
            "arm": "bare",
            "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
            "request_timings": timings,
            "stages": [stage],
            "budget": {
                "limit": {
                    "model_calls": 3,
                    "total_tokens": 100,
                    "elapsed_seconds": 30,
                },
                "used": {
                    "model_calls": 3,
                    "total_tokens": 10,
                    "elapsed_seconds": 0.9,
                },
                "termination": None,
            },
        },
        "budget": {
            "limit": {
                "model_calls": 3,
                "total_tokens": 100,
                "elapsed_seconds": 30,
            },
            "used": {"model_calls": 3, "total_tokens": 10, "elapsed_seconds": 1.0},
            "termination": None,
        },
        "run_dir": str(run_dir),
        "output_workbook": str(output),
        "output_sha256": _sha256(output),
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


@pytest.mark.parametrize(
    ("target", "field", "value", "expected_reason"),
    [
        (
            "manifest",
            "schema_version",
            9,
            "comparison_manifest_schema_mismatch",
        ),
        (
            "manifest",
            "comparison_protocol_version",
            "resource_matched_multi_arm_v19",
            "comparison_manifest_protocol_mismatch",
        ),
        (
            "row",
            "comparison_protocol_version",
            "resource_matched_multi_arm_v19",
            "result_protocol_mismatch:1",
        ),
    ],
)
def test_audit_rejects_protocol_provenance_mismatch(
    tmp_path: Path,
    target: str,
    field: str,
    value: Any,
    expected_reason: str,
) -> None:
    results, task, row = _fixture(tmp_path)
    path = (
        results / "comparison-manifest.json"
        if target == "manifest"
        else results / "results.jsonl"
    )
    if target == "manifest":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[field] = value
    else:
        payload = dict(row)
        payload[field] = value
    path.write_text(json.dumps(payload) + ("\n" if target == "row" else ""), encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert expected_reason in summary["reasons"]


@pytest.mark.parametrize(
    ("target", "field", "value", "expected_reason"),
    [
        ("row", "comparison_manifest_sha256", "0" * 64, "manifest_sha256_binding_mismatch"),
        ("row", "model", "different-model", "row_manifest_mismatch:model"),
        ("row", "generation", {"temperature": 0.5}, "row_manifest_mismatch:generation"),
        ("row", "stage_turn_caps", {"solve": 99}, "row_manifest_mismatch:stage_turn_caps"),
        ("row", "calculation_backend", "libreoffice", "row_manifest_mismatch:calculation_backend"),
        ("agent", "arm", "ours", "agent_arm_mismatch"),
        ("stage", "observed_forced_tool_prefix", [], "agent_observed_prefix_mismatch:solve"),
        ("stage", "observed_terminal_tool", "unknown", "agent_observed_terminal_invalid:solve"),
        ("timing", "attempt_history", [], "request_attempt_audit_inexact"),
    ],
)
def test_audit_rejects_tampered_resource_and_routing_evidence(
    tmp_path: Path,
    target: str,
    field: str,
    value: Any,
    expected_reason: str,
) -> None:
    results, task, row = _fixture(tmp_path)
    if target == "row":
        row[field] = value
    elif target == "agent":
        row["agent"][field] = value
    elif target == "stage":
        row["agent"]["stages"][0][field] = value
    else:
        row["agent"]["request_timings"][0][field] = value
        row["agent"]["stages"][0]["agent"]["request_timings"][0][field] = value
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert _row_reason(summary, expected_reason)


@pytest.mark.parametrize("field", ["configuration", "harness_source", "runtime"])
def test_audit_requires_frozen_manifest_provenance(
    tmp_path: Path,
    field: str,
) -> None:
    results, task, _ = _fixture(tmp_path)
    path = results / "comparison-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop(field)
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    expected_fragment = "source_fingerprint" if field == "harness_source" else field
    assert any(expected_fragment in reason for reason in summary["reasons"])


def test_audit_requires_manifest_source_to_match_active_checkout(
    tmp_path: Path,
) -> None:
    results, task, row = _fixture(tmp_path)
    path = results / "comparison-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["harness_source"]["files"][0]["sha256"] = "0" * 64
    combined = hashlib.sha256()
    for entry in manifest["harness_source"]["files"]:
        combined.update(entry["path"].encode("utf-8"))
        combined.update(b"\0")
        combined.update(entry["sha256"].encode("ascii"))
        combined.update(b"\n")
    manifest["harness_source"]["sha256"] = combined.hexdigest()
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    row["comparison_manifest_sha256"] = _sha256(path)
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert "comparison_manifest_source_checkout_mismatch" in summary["reasons"]


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
