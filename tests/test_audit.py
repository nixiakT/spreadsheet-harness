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
    CONTINUATION_SOURCE_FILENAME,
    INFLIGHT_FILENAME,
    INTERRUPTED_SEALS_FILENAME,
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


def _interrupted_seal(results: Path, task: SpreadsheetTask) -> dict[str, Any]:
    manifest_path = results / "comparison-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "arm": "bare",
        "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
        "comparison_manifest_sha256": _sha256(manifest_path),
        "split_provenance": manifest.get("split_provenance"),
        "run_spec_provenance": manifest.get("run_spec_provenance"),
        "status": "interrupted",
        "passed": None,
        "outcome_observed": False,
        "score_available": False,
        "usage_observed": False,
        "replay_permitted": False,
        "error_retryable": False,
        "error_category": "interrupted_unknown_outcome",
        "sealed_at": "2026-08-14T12:00:00+00:00",
        "sealed_from_inflight_marker_sha256": "a" * 64,
    }


def _write_interrupted_seals(results: Path, seals: list[dict[str, Any]]) -> None:
    (results / INTERRUPTED_SEALS_FILENAME).write_text(
        json.dumps({"schema_version": 1, "seals": seals}) + "\n",
        encoding="utf-8",
    )


def _continuation_source(results: Path) -> dict[str, Any]:
    manifest_path = results / "comparison-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record: dict[str, Any] = {
        "schema_version": 1,
        "comparison_manifest_sha256": _sha256(manifest_path),
        "repository_source": {
            "schema_version": 1,
            "git_commit": "1" * 40,
            "git_tree": "2" * 40,
            "remote_tracking_ref": "refs/remotes/origin/main",
            "remote_tracking_commit": "1" * 40,
            "remote_name": "origin",
            "remote_ref": "refs/heads/main",
            "remote_observed_commit": "1" * 40,
            "source_fingerprint": manifest["harness_source"],
        },
    }
    unsigned = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    record["record_sha256"] = _text_sha256(unsigned)
    return record


def _write_continuation_source(results: Path, record: dict[str, Any]) -> None:
    (results / CONTINUATION_SOURCE_FILENAME).write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )


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
    assert "mcnemar_exact_p" not in summary
    assert "stratified_bootstrap_95" not in summary
    assert "holm_adjusted_p" not in summary
    assert summary["rows"][0]["output_sha256"] == summary["rows"][0][
        "expected_output_sha256"
    ]
    assert _tree_hashes(tmp_path) == before


def test_audit_rejects_ambiguous_inflight_marker(tmp_path: Path) -> None:
    results, task, _ = _fixture(tmp_path)
    (results / INFLIGHT_FILENAME).write_text("{}\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert "ambiguous_inflight_arm_task" in summary["reasons"]


def test_audit_accepts_exact_interrupted_seal_as_integral_but_incomplete(
    tmp_path: Path,
) -> None:
    results, task, _ = _fixture(tmp_path)
    (results / "results.jsonl").write_text("", encoding="utf-8")
    _write_interrupted_seals(results, [_interrupted_seal(results, task)])
    before = _tree_hashes(tmp_path)

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is True
    assert summary["journal_integrity_valid"] is True
    assert summary["study_complete"] is False
    assert summary["inference_valid"] is False
    assert summary["inference_invalid_reasons"] == ["interrupted_unknown_outcome"]
    assert summary["interrupted_arm_tasks"] == 1
    assert summary["interrupted_arm_task_keys"] == ["task-1::bare"]
    assert summary["known_passed_rows"] == 0
    assert summary["known_failed_rows"] == 0
    assert summary["mcnemar_exact_p"] is None
    assert summary["stratified_bootstrap_95"] is None
    assert summary["holm_adjusted_p"] is None
    assert summary["rows"][0]["journal_integrity_valid"] is True
    assert summary["rows"][0]["audit_valid"] is False
    assert summary["rows"][0]["outcome_observed"] is False
    assert "missing_result_row" not in summary["rows"][0]["reasons"]
    assert summary["reasons"] == ["interrupted_unknown_outcome:task-1::bare"]
    assert _tree_hashes(tmp_path) == before


def test_audit_allows_missing_empty_journal_when_every_key_is_sealed(
    tmp_path: Path,
) -> None:
    results, task, _ = _fixture(tmp_path)
    (results / "results.jsonl").unlink()
    _write_interrupted_seals(results, [_interrupted_seal(results, task)])

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is True
    assert summary["journal_integrity_valid"] is True
    assert summary["study_complete"] is False
    assert "results_file_missing" not in summary["reasons"]


def test_audit_binds_result_to_exact_continuation_source(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    continuation = _continuation_source(results)
    _write_continuation_source(results, continuation)
    row["continuation_source"] = continuation
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is True
    assert summary["continuation_source"] == continuation
    assert summary["continuation_source_file_sha256"] == _sha256(
        results / CONTINUATION_SOURCE_FILENAME
    )


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("record", "comparison_manifest_sha256", "0" * 64),
        ("record", "record_sha256", "0" * 64),
        ("repository", "git_commit", "A" * 40),
        ("repository", "git_tree", "short"),
        ("repository", "remote_tracking_ref", "refs/remotes/origin/dev"),
        ("repository", "remote_tracking_commit", "3" * 40),
        ("repository", "remote_name", "upstream"),
        ("repository", "remote_ref", "refs/heads/dev"),
        ("repository", "remote_observed_commit", "3" * 40),
        ("repository", "source_fingerprint", {"sha256": "0" * 64, "files": []}),
    ],
)
def test_audit_rejects_invalid_continuation_source(
    tmp_path: Path,
    target: str,
    field: str,
    value: Any,
) -> None:
    results, task, row = _fixture(tmp_path)
    continuation = _continuation_source(results)
    if target == "record":
        continuation[field] = value
    else:
        continuation["repository_source"][field] = value
    _write_continuation_source(results, continuation)
    row["continuation_source"] = continuation
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["journal_integrity_valid"] is False
    assert "continuation_source_invalid" in summary["reasons"]


def test_audit_rejects_result_continuation_source_mismatch(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    continuation = _continuation_source(results)
    _write_continuation_source(results, continuation)
    row["continuation_source"] = {**continuation, "record_sha256": "0" * 64}
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["journal_integrity_valid"] is False
    assert "result_continuation_source_mismatch:1" in summary["reasons"]


def test_audit_rejects_row_continuation_without_sidecar(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    row["continuation_source"] = _continuation_source(results)
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["journal_integrity_valid"] is False
    assert "result_continuation_source_without_record:1" in summary["reasons"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("passed", False),
        ("score_available", True),
        ("replay_permitted", True),
        ("sealed_at", "not-a-timestamp"),
        ("sealed_from_inflight_marker_sha256", "A" * 64),
        ("task_id", "outside-frozen-tasks"),
        ("arm", "ours"),
    ],
)
def test_audit_rejects_malformed_interrupted_seal(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    results, task, _ = _fixture(tmp_path)
    seal = _interrupted_seal(results, task)
    seal[field] = value
    _write_interrupted_seals(results, [seal])

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert summary["journal_integrity_valid"] is False
    assert summary["study_complete"] is False
    assert summary["inference_valid"] is False
    assert "interrupted_arm_task_seals_invalid" in summary["reasons"]
    assert summary["interrupted_arm_tasks"] == 0


def test_audit_rejects_duplicate_interrupted_seal_keys(tmp_path: Path) -> None:
    results, task, _ = _fixture(tmp_path)
    seal = _interrupted_seal(results, task)
    _write_interrupted_seals(results, [seal, dict(seal)])

    summary = audit_comparison(results, [task])

    assert summary["journal_integrity_valid"] is False
    assert "interrupted_arm_task_seals_invalid" in summary["reasons"]


def test_audit_rejects_result_row_conflicting_with_interrupted_seal(
    tmp_path: Path,
) -> None:
    results, task, _ = _fixture(tmp_path)
    _write_interrupted_seals(results, [_interrupted_seal(results, task)])

    summary = audit_comparison(results, [task])

    assert summary["journal_integrity_valid"] is False
    assert "result_row_conflicts_with_interrupted_seal:task-1::bare" in summary[
        "reasons"
    ]


def test_audit_rejects_split_provenance_tampering(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    manifest_path = results / "comparison-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_ids = manifest["task_ids"]
    provenance = {
        "manifest_id": "qwen35-trace2skill-local-unattempted-pilot16-v2",
        "schema_version": "spreadsheetbench-trace2skill-derivative-v2",
        "manifest_sha256": (
            "f29d6e5627161b355c24acfbda6c5dcc250d12b5f4933d3c3fb0c50a8bac39b3"
        ),
        "task_count": len(task_ids),
        "task_ids_sha256": _text_sha256(
            "".join(f"{task_id}\n" for task_id in task_ids)
        ),
        "dataset_json_sha256": manifest["dataset_manifest_sha256"],
    }
    manifest["split_provenance"] = provenance
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    row["comparison_manifest_sha256"] = _sha256(manifest_path)
    row["split_provenance"] = {**provenance, "manifest_sha256": "2" * 64}
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert _row_reason(summary, "row_manifest_mismatch:split_provenance")


def test_audit_rejects_split_provenance_task_order_mismatch(tmp_path: Path) -> None:
    results, task, row = _fixture(tmp_path)
    manifest_path = results / "comparison-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["split_provenance"] = {
        "manifest_id": "pilot-v2",
        "schema_version": "derivative-v2",
        "manifest_sha256": "1" * 64,
        "task_count": 1,
        "task_ids_sha256": "2" * 64,
        "dataset_json_sha256": manifest["dataset_manifest_sha256"],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    row["comparison_manifest_sha256"] = _sha256(manifest_path)
    row["split_provenance"] = manifest["split_provenance"]
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert "comparison_manifest_split_provenance_invalid" in summary["reasons"]


def test_audit_rejects_malformed_split_provenance_without_raising(
    tmp_path: Path,
) -> None:
    results, task, row = _fixture(tmp_path)
    manifest_path = results / "comparison-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["split_provenance"] = {
        "manifest_id": [],
        "schema_version": "spreadsheetbench-trace2skill-derivative-v2",
        "manifest_sha256": "1" * 64,
        "task_count": 1,
        "task_ids_sha256": _text_sha256("task-1\n"),
        "dataset_json_sha256": manifest["dataset_manifest_sha256"],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    row["comparison_manifest_sha256"] = _sha256(manifest_path)
    row["split_provenance"] = manifest["split_provenance"]
    (results / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert "comparison_manifest_split_provenance_invalid" in summary["reasons"]


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


def test_audit_rejects_duplicate_manifest_json_key(tmp_path: Path) -> None:
    results, task, _ = _fixture(tmp_path)
    path = results / "comparison-manifest.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(
        raw[:-1] + ',"schema_version":12}',
        encoding="utf-8",
    )

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert "comparison_manifest_invalid" in summary["reasons"]


def test_audit_rejects_duplicate_result_json_key(tmp_path: Path) -> None:
    results, task, _ = _fixture(tmp_path)
    path = results / "results.jsonl"
    raw = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(raw[:-1] + ',"task_id":"task-1"}\n', encoding="utf-8")

    summary = audit_comparison(results, [task])

    assert summary["audit_valid"] is False
    assert "invalid_jsonl_line:1" in summary["reasons"]


@pytest.mark.parametrize(
    ("filename", "expected_reason"),
    [
        ("comparison-manifest.json", "comparison_manifest_invalid"),
        ("results.jsonl", "results_file_unreadable"),
    ],
)
def test_audit_rejects_symlinked_core_document(
    tmp_path: Path,
    filename: str,
    expected_reason: str,
) -> None:
    results, task, _ = _fixture(tmp_path)
    path = results / filename
    target = tmp_path / f"real-{filename}"
    path.rename(target)
    path.symlink_to(target)

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
