from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from spreadsheet_harness.errors import HarnessError
from spreadsheet_harness.spreadsheetbench_harbor import (
    HARBOR_PROVENANCE_FILENAME,
    normalize_spreadsheetbench_harbor,
)
from spreadsheet_harness.spreadsheetbench_v2 import (
    DEFAULT_V2_EVALUATOR,
    _dataset_identity,
    _load_official_evaluator,
    _official_score,
    load_spreadsheetbench_v2_tasks,
)


def _workbook(path: Path, value: int) -> None:
    workbook = Workbook()
    workbook.active["A1"] = value
    workbook.active["A2"] = "unchanged"
    workbook.save(path)
    workbook.close()


def _task(root: Path, task_id: str, *, enhanced: bool) -> None:
    task_dir = root / ("harbor_bundles" if enhanced else "") / task_id
    (task_dir / "preinstall").mkdir(parents=True)
    (task_dir / "solution").mkdir()
    (task_dir / "tests" / "data" / "Financial_Model").mkdir(parents=True)
    input_name = f"{task_id}_input.xlsx"
    golden_name = f"{task_id}_golden.xlsx"
    _workbook(task_dir / "preinstall" / input_name, 1)
    _workbook(task_dir / "solution" / golden_name, 2)
    row = {
        "id": task_id,
        "instruction": "Fill the financial schedule.",
        "spreadsheet_path": input_name,
        "golden_response_path": golden_name,
        "answer_position": "'Sheet'!A1:A2",
    }
    (task_dir / "tests" / "data" / "Financial_Model" / "dataset.json").write_text(
        json.dumps([row]), encoding="utf-8"
    )
    (task_dir / "task.toml").write_text(
        "[metadata]\n"
        f'task_id = "{task_id}"\n'
        'category = "Financial_Model"\n'
        f'input_name = "{input_name}"\n'
        f'golden_name = "{golden_name}"\n'
        'dataset_role = "calibration_only"\n'
        "answer_position = \"'Sheet'!A1:A2\"\n",
        encoding="utf-8",
    )


def test_normalize_v06_harbor_directory_to_v2_layout(tmp_path: Path) -> None:
    source = tmp_path / "SpreadsheetBench-v0.6-Financial_Model"
    _task(source, "fina_Fina_01_c0", enhanced=False)

    canonical = normalize_spreadsheetbench_harbor(source, tmp_path / "canonical")
    tasks = load_spreadsheetbench_v2_tasks(canonical, categories=("Financial_Model",))

    assert [task.task_id for task in tasks] == ["Financial_Model/fina_Fina_01_c0"]
    assert tasks[0].instruction == "Fill the financial schedule."
    assert tasks[0].source_row["dataset_role"] == "calibration_only"
    assert tasks[0].input_path.name == "fina_Fina_01_c0_input.xlsx"
    assert tasks[0].golden_path.name == "fina_Fina_01_c0_golden.xlsx"
    provenance = json.loads((canonical / HARBOR_PROVENANCE_FILENAME).read_text())
    assert provenance["name"] == source.name
    assert provenance["archive_sha256"] is None
    assert provenance["task_count"] == 1
    evaluator, _, _ = _load_official_evaluator(DEFAULT_V2_EVALUATOR)
    score = _official_score(
        evaluator,
        tasks[0],
        tasks[0].golden_path,
        tmp_path / "official-output",
        model_calls=1,
    )
    assert score["accuracy"] == 1.0


def test_normalize_enhanced_harbor_archive(tmp_path: Path) -> None:
    source_root = tmp_path / "SpreadsheetBench-v2-enhanced-Financial_Model"
    _task(source_root, "fina_Fina_dam_example_c0", enhanced=True)
    archive = tmp_path / "financial.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(source_root, arcname=source_root.name)

    canonical = normalize_spreadsheetbench_harbor(archive, tmp_path / "canonical")
    tasks = load_spreadsheetbench_v2_tasks(canonical, categories=("Financial_Model",))

    assert len(tasks) == 1
    assert tasks[0].source_row["answer_position"] == "'Sheet'!A1:A2"
    assert (canonical / "Financial_Model" / "dataset.json").is_file()
    provenance = json.loads((canonical / HARBOR_PROVENANCE_FILENAME).read_text())
    assert provenance["name"] == archive.name
    assert len(provenance["archive_sha256"]) == 64
    assert provenance["task_count"] == 1
    assert _dataset_identity(canonical) == {
        "name": archive.name,
        "revision": "harbor-financial-calibration-v1",
        "format": "harbor-task-bundles-v1",
        "archive_sha256": provenance["archive_sha256"],
    }


def test_harbor_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("../outside.txt")
        payload = b"unsafe"
        info.size = len(payload)
        output.addfile(info, io.BytesIO(payload))

    with pytest.raises(HarnessError, match="Unsafe Harbor archive path"):
        normalize_spreadsheetbench_harbor(archive, tmp_path / "canonical")
