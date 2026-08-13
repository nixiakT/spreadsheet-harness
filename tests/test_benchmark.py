from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from spreadsheet_harness import benchmark as benchmark_module
from spreadsheet_harness.agent import AgentResult
from spreadsheet_harness.benchmark import (
    Comparison,
    SpreadsheetTask,
    VerifiedBenchmarkRunner,
    compare_workbooks,
    comparison_evidence,
    load_verified_tasks,
    summarize_results,
    trace2skill_heldout_manifest,
    verify_trace2skill_heldout_manifest,
)
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import HarnessError, ProviderError
from spreadsheet_harness.trajectory import read_trajectory


class OfflineProcessRunner(VerifiedBenchmarkRunner):
    """Picklable runner used to exercise parent-only result journaling."""

    def _run_task(self, task: SpreadsheetTask, attempt: int) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "task_attempt": attempt,
            "status": "completed",
            "passed": True,
            "calculation_backend": "not_recalculated",
            "elapsed_seconds": 0.001,
        }


class NonRetryableProcessRunner(VerifiedBenchmarkRunner):
    def _run_task(self, task: SpreadsheetTask, attempt: int) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "task_attempt": attempt,
            "status": "error",
            "passed": False,
            "error_retryable": False,
            "error_category": "provider_task",
            "calculation_backend": "not_recalculated",
            "elapsed_seconds": 0.001,
        }


class TransientThenSuccessProcessRunner(VerifiedBenchmarkRunner):
    def _run_task(self, task: SpreadsheetTask, attempt: int) -> dict[str, Any]:
        if attempt == 1:
            return {
                "task_id": task.task_id,
                "task_attempt": attempt,
                "status": "error",
                "passed": False,
                "error_retryable": True,
                "error_category": "provider_transient",
                "calculation_backend": "not_recalculated",
                "elapsed_seconds": 0.001,
            }
        return {
            "task_id": task.task_id,
            "task_attempt": attempt,
            "status": "completed",
            "passed": True,
            "calculation_backend": "not_recalculated",
            "elapsed_seconds": 0.001,
        }


def _book(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(name)
        for row in rows:
            worksheet.append(row)
    workbook.save(path)


def test_corrected_comparator_handles_quoted_commas_and_answer_sheet(tmp_path: Path) -> None:
    golden = tmp_path / "golden.xlsx"
    candidate = tmp_path / "candidate.xlsx"
    sheets = {
        "Wrong first": [[999]],
        "b2b, sez, de": [[1.234, "ok"], [None, 5]],
    }
    _book(golden, sheets)
    _book(candidate, {"Wrong first": [[0]], "b2b, sez, de": [["1.234", "ok"], ["", 5]]})

    qualified = compare_workbooks(golden, candidate, "'b2b, sez, de'!A1:B2")
    assert qualified.passed is True
    assert qualified.checked_cells == 4

    corrected_sheet = compare_workbooks(
        golden,
        candidate,
        "A1:B2",
        answer_sheet="b2b, sez, de",
    )
    assert corrected_sheet.passed is True


def test_comparator_reports_value_differences_and_whole_columns(tmp_path: Path) -> None:
    golden = tmp_path / "golden.xlsx"
    candidate = tmp_path / "candidate.xlsx"
    _book(golden, {"Sheet3": [[1, 2], [3, 4]]})
    _book(candidate, {"Sheet3": [[1, 2], [3, 99]]})

    result = compare_workbooks(golden, candidate, "Sheet3'!A:B")
    assert result.passed is False
    assert result.checked_cells == 4
    assert result.differences[0]["cell"] == "B2"

    repaired = compare_workbooks(golden, candidate, "'Sheet3'!A1:2")
    assert repaired.passed is True
    assert repaired.checked_cells == 2


def test_comparison_evidence_omits_golden_and_candidate_values() -> None:
    comparison = Comparison(
        False,
        2,
        (
            {
                "sheet": "Sheet1",
                "cell": "A1",
                "expected": "'golden secret'",
                "actual": "'candidate value'",
                "reasons": ["value", "number_format"],
            },
            {"range": "bad", "error": "worksheet missing"},
        ),
    )

    evidence = comparison_evidence(comparison)

    assert evidence == {
        "checked_cells": 2,
        "difference_count": 2,
        "difference_categories": {
            "metadata_or_structure": 1,
            "number_format": 1,
            "value": 1,
        },
    }
    assert "golden secret" not in json.dumps(evidence)
    assert "candidate value" not in json.dumps(evidence)


def test_load_verified_tasks_handles_variants_and_excludes(tmp_path: Path) -> None:
    root = tmp_path / "spreadsheetbench_verified_400"
    first = root / "spreadsheet" / "41691"
    second = root / "spreadsheet" / "56225"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _book(first / "1_41691_init.xlsx", {"Sheet": [[1]]})
    _book(first / "1_41691_golden.xlsx", {"Sheet": [[2]]})
    _book(second / "initial.xlsx", {"Sheet": [[1]]})
    _book(second / "golden.xlsx", {"Sheet": [[1]]})
    rows = [
        {
            "id": 41691,
            "instruction": "Fix B7",
            "spreadsheet_path": "spreadsheet/41691",
            "instruction_type": "Cell-Level",
            "answer_position": "B7",
        },
        {
            "id": 56225,
            "instruction": "Excluded",
            "spreadsheet_path": "spreadsheet/56225",
            "instruction_type": "Cell-Level",
            "answer_position": "A1",
            "exclude": "ignore, initial file already passes verification",
        },
    ]
    (root / "dataset.json").write_text(json.dumps(rows), encoding="utf-8")

    tasks = load_verified_tasks(root)
    assert [task.task_id for task in tasks] == ["41691"]
    all_tasks = load_verified_tasks(root, include_excluded=True)
    assert len(all_tasks) == 2
    assert all_tasks[1].input_path.name == "initial.xlsx"


def _split_dataset(root: Path, *, rows: int = 400) -> list[dict[str, Any]]:
    dataset: list[dict[str, Any]] = []
    for index in range(rows):
        task_id = str(index)
        task_dir = root / "spreadsheet" / task_id
        task_dir.mkdir(parents=True)
        _book(task_dir / "initial.xlsx", {"Sheet": [[index]]})
        _book(task_dir / "golden.xlsx", {"Sheet": [[index]]})
        row: dict[str, Any] = {
            "id": task_id,
            "instruction": f"task {index}",
            "instruction_type": "Cell-Level",
            "answer_position": "A1",
        }
        if index in {337, 338}:
            row["exclude"] = f"excluded {index}"
        dataset.append(row)
    (root / "dataset.json").write_text(json.dumps(dataset), encoding="utf-8")
    return dataset


def test_original_index_slice_is_applied_before_exclusion_filter(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    _split_dataset(root)

    tasks = load_verified_tasks(root, original_index_start=200, original_index_stop=400)

    assert len(tasks) == 198
    assert tasks[0].task_id == "200"
    assert tasks[-1].task_id == "399"
    assert "337" not in {task.task_id for task in tasks}
    assert "338" not in {task.task_id for task in tasks}


def test_trace2skill_manifest_fails_closed_on_noncanonical_task_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dataset"
    _split_dataset(root)

    with pytest.raises(ValueError, match="dataset.json checksum changed"):
        trace2skill_heldout_manifest(root)


def test_trace2skill_manifest_build_and_read_only_verify_pinned_dataset(
    tmp_path: Path,
) -> None:
    root = Path("benchmarks/data/spreadsheetbench_verified_400")
    if not root.is_dir():
        pytest.skip("Pinned SpreadsheetBench dataset is not available")
    manifest = trace2skill_heldout_manifest(root)
    path = tmp_path / "heldout-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = (root / "dataset.json").read_bytes()
    report = verify_trace2skill_heldout_manifest(root, path)

    assert report["valid"] is True
    assert report["usable_tasks"] == 198
    assert manifest["selection"]["original_index_start_inclusive"] == 200
    assert manifest["selection"]["original_index_stop_exclusive"] == 400
    assert [item["original_index"] for item in manifest["selection"]["excluded_tasks"]] == [
        337,
        338,
    ]
    assert len(manifest["task_ids"]) == 198
    assert manifest["task_ids_sha256"] == (
        "445ceec8e033601a054babf7997e340cf21d1c1d2d54a4aa421a8ba29b189582"
    )
    assert (root / "dataset.json").read_bytes() == before


DERIVATIVE_POOL_MANIFEST = Path(
    "benchmarks/protocols/qwen35-trace2skill-local-unattempted-v2.json"
)
DERIVATIVE_PILOT_MANIFEST = Path(
    "benchmarks/protocols/qwen35-trace2skill-local-unattempted-pilot16-v2.json"
)


def _ordered_ids_sha256(task_ids: list[str]) -> str:
    return benchmark_module._ordered_task_ids_sha256(task_ids)


def _load_derivative_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_derivative_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _require_pinned_derivative_manifests() -> Path:
    dataset = Path("benchmarks/data/spreadsheetbench_verified_400")
    if not dataset.is_dir():
        pytest.skip("Pinned SpreadsheetBench dataset is not available")
    if not DERIVATIVE_POOL_MANIFEST.is_file() or not DERIVATIVE_PILOT_MANIFEST.is_file():
        pytest.skip("Frozen Trace2Skill derivative manifests are not available")
    return dataset


def _copy_derivative_manifest_tree(destination: Path) -> dict[str, Path]:
    destination.mkdir()
    sources = {
        "v1": Path("benchmarks/protocols/qwen35-trace2skill-heldout-v1.json"),
        "pool": DERIVATIVE_POOL_MANIFEST,
        "pilot": DERIVATIVE_PILOT_MANIFEST,
    }
    copies: dict[str, Path] = {}
    for name, source in sources.items():
        target = destination / source.name
        shutil.copy2(source, target)
        copies[name] = target
    return copies


def _file_state(path: Path) -> tuple[bytes, int, int, int]:
    metadata = path.stat()
    return (
        path.read_bytes(),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def test_trace2skill_derivative_pool_and_pilot_verify_read_only() -> None:
    dataset = _require_pinned_derivative_manifests()
    watched = [
        dataset / "dataset.json",
        Path("benchmarks/protocols/qwen35-trace2skill-heldout-v1.json"),
        DERIVATIVE_POOL_MANIFEST,
        DERIVATIVE_PILOT_MANIFEST,
    ]
    before = {path: _file_state(path) for path in watched}

    pool = benchmark_module.verify_trace2skill_derivative_manifest(
        dataset, DERIVATIVE_POOL_MANIFEST
    )
    pilot = benchmark_module.verify_trace2skill_derivative_manifest(
        dataset, DERIVATIVE_PILOT_MANIFEST
    )

    assert pool["valid"] is True
    assert pool["manifest_id"] == "qwen35-trace2skill-local-unattempted-v2"
    assert pool["usable_tasks"] == 143
    assert pool["task_ids_sha256"] == (
        "7b76ebca59be0e97964108b5e2d0552ea6a9c0f11eb51d15c10552b82efd3386"
    )
    assert pilot["valid"] is True
    assert pilot["manifest_id"] == "qwen35-trace2skill-local-unattempted-pilot16-v2"
    assert pilot["usable_tasks"] == 16
    assert pilot["task_ids_sha256"] == (
        "f25f8b75ac231f81e23e812097d3060d3e1f16597b0a5196cdeee9a833b91b82"
    )
    assert {path: _file_state(path) for path in watched} == before


@pytest.mark.parametrize("manifest_path", [DERIVATIVE_POOL_MANIFEST, DERIVATIVE_PILOT_MANIFEST])
def test_trace2skill_derivative_manifest_rejects_invalid_utf8_and_duplicate_keys(
    manifest_path: Path,
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    invalid_utf8 = tmp_path / manifest_path.name
    invalid_utf8.write_bytes(b'{"schema_version":"bad","manifest_id":"\xff"}\n')

    with pytest.raises(ValueError, match="UTF-8"):
        benchmark_module.verify_trace2skill_derivative_manifest(dataset, invalid_utf8)

    frozen = manifest_path.read_text(encoding="utf-8")
    duplicate = tmp_path / f"duplicate-{manifest_path.name}"
    duplicate.write_text(
        frozen.replace(
            '"schema_version":',
            '"schema_version": "attacker-controlled",\n  "schema_version":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        benchmark_module.verify_trace2skill_derivative_manifest(dataset, duplicate)


@pytest.mark.parametrize("manifest_name,parent_name", [("pool", "v1"), ("pilot", "pool")])
def test_trace2skill_derivative_manifest_binds_parent_file_sha256(
    manifest_name: str,
    parent_name: str,
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    copies = _copy_derivative_manifest_tree(tmp_path / "protocols")
    copies[parent_name].write_bytes(copies[parent_name].read_bytes() + b"\n")

    with pytest.raises(ValueError, match="parent manifest checksum mismatch"):
        benchmark_module.verify_trace2skill_derivative_manifest(
            dataset, copies[manifest_name]
        )


@pytest.mark.parametrize("manifest_name", ["pool", "pilot"])
@pytest.mark.parametrize("parent_path", ["../parent.json", "/tmp/parent.json"])
def test_trace2skill_derivative_manifest_rejects_non_sibling_parent_path(
    manifest_name: str,
    parent_path: str,
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    copies = _copy_derivative_manifest_tree(tmp_path / "protocols")
    frozen = _load_derivative_manifest(copies[manifest_name])
    frozen["parent"]["relative_path"] = parent_path
    _write_derivative_manifest(copies[manifest_name], frozen)

    with pytest.raises(ValueError, match="parent path"):
        benchmark_module.verify_trace2skill_derivative_manifest(
            dataset, copies[manifest_name]
        )


@pytest.mark.parametrize("manifest_name", ["pool", "pilot"])
def test_trace2skill_derivative_manifest_rejects_tampered_parent_hash_field(
    manifest_name: str,
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    copies = _copy_derivative_manifest_tree(tmp_path / "protocols")
    frozen = _load_derivative_manifest(copies[manifest_name])
    frozen["parent"]["manifest_sha256"] = "0" * 64
    _write_derivative_manifest(copies[manifest_name], frozen)

    with pytest.raises(ValueError, match="parent"):
        benchmark_module.verify_trace2skill_derivative_manifest(
            dataset, copies[manifest_name]
        )


def test_trace2skill_local_unattempted_pool_is_parent_ordered_exposure_difference() -> None:
    _require_pinned_derivative_manifests()
    parent = _load_derivative_manifest(
        Path("benchmarks/protocols/qwen35-trace2skill-heldout-v1.json")
    )
    pool = _load_derivative_manifest(DERIVATIVE_POOL_MANIFEST)
    evidence = pool["derivation"]["evidence"]
    attempted = evidence["attempted_or_run"]["task_ids"]
    protocol_listed = evidence["protocol_listed"]["task_ids"]
    exposure_set = set(attempted) | set(protocol_listed)
    expected_union = [task_id for task_id in parent["task_ids"] if task_id in exposure_set]
    expected_pool = [task_id for task_id in parent["task_ids"] if task_id not in exposure_set]

    assert len(attempted) == len(set(attempted)) == 51
    assert len(protocol_listed) == len(set(protocol_listed)) == 4
    assert set(attempted).isdisjoint(protocol_listed)
    assert evidence["union"]["task_ids"] == expected_union
    assert evidence["union"]["task_count"] == 55
    assert evidence["union"]["task_ids_sha256"] == _ordered_ids_sha256(expected_union)
    assert pool["task_ids"] == expected_pool
    assert pool["task_count"] == 143
    assert pool["task_ids_sha256"] == _ordered_ids_sha256(expected_pool)


@pytest.mark.parametrize("target", ["attempted", "protocol_listed", "union", "pool"])
def test_trace2skill_local_unattempted_pool_rejects_self_consistent_reordering(
    target: str,
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    copies = _copy_derivative_manifest_tree(tmp_path / "protocols")
    frozen = _load_derivative_manifest(copies["pool"])
    if target == "attempted":
        section = frozen["derivation"]["evidence"]["attempted_or_run"]
    elif target == "protocol_listed":
        section = frozen["derivation"]["evidence"]["protocol_listed"]
    elif target == "union":
        section = frozen["derivation"]["evidence"]["union"]
    else:
        section = frozen
    section["task_ids"][0], section["task_ids"][1] = (
        section["task_ids"][1],
        section["task_ids"][0],
    )
    section["task_ids_sha256"] = _ordered_ids_sha256(section["task_ids"])
    _write_derivative_manifest(copies["pool"], frozen)

    with pytest.raises(ValueError, match="does not match its anchors"):
        benchmark_module.verify_trace2skill_derivative_manifest(dataset, copies["pool"])


def test_trace2skill_local_unattempted_pool_rejects_self_consistent_exposure_membership_change(
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    copies = _copy_derivative_manifest_tree(tmp_path / "protocols")
    parent = _load_derivative_manifest(copies["v1"])
    frozen = _load_derivative_manifest(copies["pool"])
    evidence = frozen["derivation"]["evidence"]
    listed = evidence["protocol_listed"]
    replacement = frozen["task_ids"][0]
    listed["task_ids"][0] = replacement
    listed["task_ids_sha256"] = _ordered_ids_sha256(listed["task_ids"])
    exposure = set(evidence["attempted_or_run"]["task_ids"]) | set(listed["task_ids"])
    union = [task_id for task_id in parent["task_ids"] if task_id in exposure]
    pool = [task_id for task_id in parent["task_ids"] if task_id not in exposure]
    evidence["union"]["task_ids"] = union
    evidence["union"]["task_count"] = len(union)
    evidence["union"]["task_ids_sha256"] = _ordered_ids_sha256(union)
    frozen["task_ids"] = pool
    frozen["task_count"] = len(pool)
    frozen["task_ids_sha256"] = _ordered_ids_sha256(pool)
    _write_derivative_manifest(copies["pool"], frozen)

    with pytest.raises(ValueError, match="does not match its anchors"):
        benchmark_module.verify_trace2skill_derivative_manifest(dataset, copies["pool"])


def test_trace2skill_pilot_is_fixed_pool_subset_with_parent_ordered_reserve() -> None:
    _require_pinned_derivative_manifests()
    pool = _load_derivative_manifest(DERIVATIVE_POOL_MANIFEST)
    pilot = _load_derivative_manifest(DERIVATIVE_PILOT_MANIFEST)
    pilot_set = set(pilot["task_ids"])
    expected_pilot = [task_id for task_id in pool["task_ids"] if task_id in pilot_set]
    expected_reserve = [task_id for task_id in pool["task_ids"] if task_id not in pilot_set]

    assert len(pilot_set) == 16
    assert pilot["task_ids"] == expected_pilot
    assert pilot["selection"]["task_count"] == len(expected_pilot)
    assert pilot["selection"]["task_ids_sha256"] == _ordered_ids_sha256(expected_pilot)
    assert pilot["reserve"]["task_count"] == len(expected_reserve) == 127
    assert pilot["reserve"]["task_ids_sha256"] == _ordered_ids_sha256(expected_reserve)
    assert pilot_set.isdisjoint(expected_reserve)
    assert pilot_set | set(expected_reserve) == set(pool["task_ids"])


@pytest.mark.parametrize("target", ["membership", "order", "reserve"])
def test_trace2skill_pilot_rejects_self_consistent_selection_or_reserve_tampering(
    target: str,
    tmp_path: Path,
) -> None:
    dataset = _require_pinned_derivative_manifests()
    copies = _copy_derivative_manifest_tree(tmp_path / "protocols")
    pool = _load_derivative_manifest(copies["pool"])
    frozen = _load_derivative_manifest(copies["pilot"])
    if target == "membership":
        replacement = next(task_id for task_id in pool["task_ids"] if task_id not in frozen["task_ids"])
        frozen["task_ids"][0] = replacement
        frozen["task_ids_sha256"] = _ordered_ids_sha256(frozen["task_ids"])
        frozen["selection"]["task_ids_sha256"] = frozen["task_ids_sha256"]
        reserve = [task_id for task_id in pool["task_ids"] if task_id not in set(frozen["task_ids"])]
        frozen["reserve"]["task_count"] = len(reserve)
        frozen["reserve"]["task_ids_sha256"] = _ordered_ids_sha256(reserve)
    elif target == "order":
        frozen["task_ids"][0], frozen["task_ids"][1] = (
            frozen["task_ids"][1],
            frozen["task_ids"][0],
        )
        frozen["task_ids_sha256"] = _ordered_ids_sha256(frozen["task_ids"])
        frozen["selection"]["task_ids_sha256"] = frozen["task_ids_sha256"]
    else:
        frozen["reserve"]["task_count"] -= 1
        frozen["reserve"]["task_ids_sha256"] = "0" * 64
    _write_derivative_manifest(copies["pilot"], frozen)

    with pytest.raises(ValueError, match="does not match its anchors"):
        benchmark_module.verify_trace2skill_derivative_manifest(dataset, copies["pilot"])


def test_summarize_results(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "1",
                        "status": "completed",
                        "passed": True,
                        "calculation_backend": "libreoffice",
                    }
                ),
                json.dumps(
                    {
                        "task_id": "2",
                        "status": "completed",
                        "passed": False,
                        "calculation_backend": "libreoffice",
                    }
                ),
                json.dumps(
                    {
                        "task_id": "3",
                        "status": "error",
                        "passed": False,
                        "calculation_backend": "libreoffice",
                    }
                ),
                json.dumps(
                    {
                        "task_id": "3",
                        "status": "completed",
                        "passed": True,
                        "calculation_backend": "libreoffice",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = summarize_results(path)
    assert summary["attempted"] == 3
    assert summary["completed"] == 3
    assert summary["completion_rate"] == 1
    assert summary["verified_accuracy"] == 2 / 3
    assert summary["completed_accuracy"] == 2 / 3


def test_summarize_results_uses_expected_denominator_and_ignores_torn_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text(
        json.dumps(
            {
                "task_id": "1",
                "status": "completed",
                "passed": True,
                "calculation_backend": "libreoffice",
            }
        )
        + "\n{\"task_id\":",
        encoding="utf-8",
    )

    summary = summarize_results(path, expected_task_ids=["1", "2"])

    assert summary["attempted"] == 1
    assert summary["expected"] == 2
    assert summary["missing"] == 1
    assert summary["verified_accuracy"] == 0.5
    assert summary["invalid_result_rows_ignored"] == 1


def test_summarize_results_refuses_comparison_directory_without_touching_summary(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps({"task_id": "1", "status": "completed", "passed": True}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "comparison-manifest.json").write_text("{}\n", encoding="utf-8")
    sentinel = b'{"sentinel":"comparison-summary"}\n'
    summary_path = tmp_path / "summary.json"
    summary_path.write_bytes(sentinel)

    with pytest.raises(
        HarnessError,
        match="Refusing to summarize comparison results as a single-arm benchmark",
    ):
        summarize_results(results)

    assert summary_path.read_bytes() == sentinel


def test_summarize_results_writes_summary_for_single_arm_directory(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps({"task_id": "1", "status": "completed", "passed": True}) + "\n",
        encoding="utf-8",
    )

    summary = summarize_results(results)

    assert summary["expected"] == 1
    assert summary["verified_accuracy"] == 1
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == summary


def test_benchmark_passes_max_output_tokens_to_agent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    initial = tmp_path / "initial.xlsx"
    golden = tmp_path / "golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, *_: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self, _: str) -> AgentResult:
            return AgentResult("done", 1, 0, {}, "response")

    monkeypatch.setattr("spreadsheet_harness.benchmark.SpreadsheetAgent", FakeAgent)
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = VerifiedBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "results",
        max_output_tokens=1234,
        enable_code=False,
        recalculate=False,
    )

    row = runner._run_task(task, 1)

    assert row["status"] == "completed"
    assert len(row["output_sha256"]) == 64
    assert captured["max_output_tokens"] == 1234
    assert captured["max_turns"] == 30
    assert row["max_turns"] == 30
    assert row["request_interval_seconds"] == 0.0
    assert row["litellm_timeout_seconds"] is None
    assert row["request_pacing_scope"] == "single_worker_process"
    trajectory = read_trajectory(Path(row["run_dir"]) / "trajectory.jsonl")
    configured = [item for item in trajectory if item["event"] == "benchmark.configured"]
    assert configured[0]["payload"]["max_turns"] == 30
    assert configured[0]["payload"]["request_interval_seconds"] == 0.0
    assert configured[0]["payload"]["litellm_timeout_seconds"] is None
    evaluation = [item for item in trajectory if item["event"] == "benchmark.evaluated"]
    assert len(evaluation) == 1
    payload = evaluation[0]["payload"]
    assert payload == {
        "task_id": "1",
        "passed": True,
        "status": "completed",
        "scorer": "cleanroom-corrected-value-v1",
        "style_checked": False,
        "calculation_backend": "not_recalculated",
        "checked_cells": 1,
        "difference_count": 0,
        "difference_categories": {},
        "scoring_metadata_sha256": payload["scoring_metadata_sha256"],
    }
    assert len(payload["scoring_metadata_sha256"]) == 64


def test_benchmark_passes_provider_key_to_registry_redaction(
    tmp_path: Path, monkeypatch: Any
) -> None:
    initial = tmp_path / "redaction-initial.xlsx"
    golden = tmp_path / "redaction-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    secret = "key://benchmark+tenant?signature=" + "B" * 128
    captured: dict[str, Any] = {}

    class CapturingRegistry:
        def __init__(self, session: Any, **kwargs: Any) -> None:
            captured["session"] = session
            captured.update(kwargs)

    class OfflineAgent:
        def __init__(self, _: ProviderConfig, tools: Any, **__: Any) -> None:
            assert tools is not None

        def run(self, _: str) -> AgentResult:
            return AgentResult("done", 1, 0, {}, "response")

    monkeypatch.setattr(benchmark_module, "SpreadsheetToolRegistry", CapturingRegistry)
    monkeypatch.setattr(benchmark_module, "SpreadsheetAgent", OfflineAgent)
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = VerifiedBenchmarkRunner(
        ProviderConfig("https://example.test/v1", secret, "test-model"),
        tmp_path / "redaction-results",
        enable_code=True,
        recalculate=False,
    )

    row = runner._run_task(task, 1)

    assert row["status"] == "completed", row
    assert captured["enable_code"] is True
    assert captured["redaction_secrets"] == (secret,)


def test_benchmark_does_not_task_retry_ambiguous_provider_delivery(
    tmp_path: Path, monkeypatch: Any
) -> None:
    initial = tmp_path / "ambiguous-initial.xlsx"
    golden = tmp_path / "ambiguous-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})

    class AmbiguousAgent:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def run(self, _: str) -> AgentResult:
            raise ProviderError(
                "Responses API returned HTTP 408",
                retryable=True,
                safe_to_retry=False,
                status_code=408,
                phase="response_headers",
                delivery_state="ambiguous_post_send",
            )

    monkeypatch.setattr("spreadsheet_harness.benchmark.SpreadsheetAgent", AmbiguousAgent)
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = VerifiedBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "ambiguous-results",
        enable_code=False,
        recalculate=False,
    )

    row = runner._run_task(task, 1)

    assert row["status"] == "error"
    assert row["error_category"] == "provider_transient"
    assert row["error_retryable"] is False
    assert row["provider_error"]["status_code"] == 408
    assert row["provider_error"]["safe_to_retry"] is False


def test_benchmark_manifest_prevents_mixed_model_resume(tmp_path: Path) -> None:
    initial = tmp_path / "initial.xlsx"
    golden = tmp_path / "golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    output = tmp_path / "results"
    first = VerifiedBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "model-a"),
        output,
        enable_code=False,
        recalculate=False,
    )
    first._prepare_manifest([task])
    second = VerifiedBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "model-b"),
        output,
        enable_code=False,
        recalculate=False,
    )

    with pytest.raises(HarnessError, match="different provider"):
        second._prepare_manifest([task])


def test_benchmark_manifest_prevents_mixed_generation_resume(tmp_path: Path) -> None:
    initial = tmp_path / "initial.xlsx"
    golden = tmp_path / "golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    output = tmp_path / "results"
    first = VerifiedBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "model-a",
            seed=41,
            temperature=1.0,
            litellm_timeout_seconds=600,
        ),
        output,
        enable_code=False,
        recalculate=False,
    )
    first._prepare_manifest([task])
    second = VerifiedBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "model-a",
            seed=42,
            temperature=1.0,
            litellm_timeout_seconds=600,
        ),
        output,
        enable_code=False,
        recalculate=False,
    )

    with pytest.raises(HarnessError, match="different provider"):
        second._prepare_manifest([task])


def test_benchmark_manifest_detects_changed_workbook(tmp_path: Path) -> None:
    initial = tmp_path / "fingerprint-initial.xlsx"
    golden = tmp_path / "fingerprint-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = VerifiedBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "model-a"),
        tmp_path / "fingerprint-results",
        enable_code=False,
        recalculate=False,
    )
    runner._prepare_manifest([task])
    _book(initial, {"Sheet": [[2]]})

    with pytest.raises(HarnessError, match="different provider"):
        runner._prepare_manifest([task])


def test_parallel_runner_writes_each_task_once(tmp_path: Path) -> None:
    initial = tmp_path / "parallel-initial.xlsx"
    golden = tmp_path / "parallel-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    tasks = [
        SpreadsheetTask(str(index), "noop", initial, golden, "Cell-Level", "A1", None)
        for index in range(4)
    ]
    runner = OfflineProcessRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "parallel-results",
        workers=2,
        enable_code=False,
        recalculate=False,
    )

    summary = runner.run(tasks)

    rows = [json.loads(line) for line in runner.results_path.read_text().splitlines()]
    assert len(rows) == 4
    assert {row["task_id"] for row in rows} == {"0", "1", "2", "3"}
    assert summary["expected"] == 4
    assert summary["completed"] == 4
    assert summary["passed"] == 4


def test_verified_runner_allows_pacing_with_one_worker_only(tmp_path: Path) -> None:
    runner = VerifiedBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            request_interval_seconds=20.0,
        ),
        tmp_path / "paced-single-worker",
        workers=1,
    )

    assert runner._manifest([])["configuration"]["request_interval_seconds"] == 20.0
    assert runner._manifest([])["configuration"]["request_pacing_scope"] == (
        "single_worker_process"
    )
    with pytest.raises(ValueError, match="workers=1"):
        VerifiedBenchmarkRunner(
            ProviderConfig(
                "https://example.test/v1",
                "not-a-real-key",
                "test-model",
                request_interval_seconds=20.0,
            ),
            tmp_path / "paced-process-pool",
            workers=2,
        )


def test_benchmark_process_pacer_is_shared_by_runner_scope(tmp_path: Path) -> None:
    benchmark_module._PROCESS_PACERS.clear()
    runner = VerifiedBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            request_interval_seconds=7.0,
        ),
        tmp_path / "shared-pacer",
        workers=1,
    )

    first = benchmark_module._process_pacer(runner._pacing_scope_id, 7.0)
    second = benchmark_module._process_pacer(runner._pacing_scope_id, 7.0)

    assert first is second
    assert first.interval_seconds == 7.0


def test_runner_repairs_torn_tail_before_appending(tmp_path: Path) -> None:
    initial = tmp_path / "recovery-initial.xlsx"
    golden = tmp_path / "recovery-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = OfflineProcessRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "recovered-results",
        enable_code=False,
        recalculate=False,
    )
    runner._prepare_manifest([task])
    runner.results_path.write_text('{"task_id":', encoding="utf-8")

    summary = runner.run([task])

    rows = [json.loads(line) for line in runner.results_path.read_text().splitlines()]
    assert [row["task_id"] for row in rows] == ["1"]
    assert summary["completed"] == 1
    assert summary["recovered_invalid_result_rows"] == 1


def test_resume_does_not_resample_nonretryable_task(tmp_path: Path) -> None:
    initial = tmp_path / "final-initial.xlsx"
    golden = tmp_path / "final-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = NonRetryableProcessRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "final-results",
        enable_code=False,
        recalculate=False,
    )

    runner.run([task])
    runner.run([task])

    assert len(runner.results_path.read_text().splitlines()) == 1


def test_resume_uses_persisted_transient_retry_budget(tmp_path: Path) -> None:
    initial = tmp_path / "retry-initial.xlsx"
    golden = tmp_path / "retry-golden.xlsx"
    _book(initial, {"Sheet": [[1]]})
    _book(golden, {"Sheet": [[1]]})
    task = SpreadsheetTask("1", "noop", initial, golden, "Cell-Level", "A1", None)
    runner = TransientThenSuccessProcessRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "retry-results",
        enable_code=False,
        recalculate=False,
        task_retries=1,
        circuit_breaker_threshold=3,
    )
    runner._prepare_manifest([task])
    runner._append_result(runner._run_task(task, 1))
    second = runner.run([task])

    rows = [json.loads(line) for line in runner.results_path.read_text().splitlines()]
    assert [row["task_attempt"] for row in rows] == [1, 2]
    assert second["completed"] == 1
    assert second["passed"] == 1
