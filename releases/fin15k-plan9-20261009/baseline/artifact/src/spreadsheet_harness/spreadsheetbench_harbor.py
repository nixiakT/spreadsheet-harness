"""Normalize Harbor-packaged SpreadsheetBench financial tasks.

The financial task archives distributed with the v0.6 generator and the
enhanced v2 release are Harbor bundles.  They contain one task directory per
workbook, rather than the category-root ``dataset.json`` layout consumed by
the paired v2 runner.  This module converts either layout (or its ``.tar.gz``
archive) into that canonical, evaluator-compatible layout.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .errors import HarnessError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


HARBOR_PROVENANCE_FILENAME = "harbor_source.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extract only normalization inputs while rejecting unsafe members."""

    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        selected: list[tarfile.TarInfo] = []
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise HarnessError(f"Unsafe Harbor archive path: {member.name}")
            if member.issym() or member.islnk():
                raise HarnessError(f"Links are not accepted in Harbor archives: {member.name}")
            name = member.name
            if member.isdir() or (
                member.isfile()
                and (
                    name.endswith(("/task.toml", "/instruction.md", "/dataset.json"))
                    or (name.endswith(".xlsx") and ("/preinstall/" in name or "/solution/" in name))
                    or ("/_meta/" in name and name.endswith(".json"))
                )
            ):
                selected.append(member)
        tar.extractall(destination, members=selected)  # noqa: S202 - validated above


def _task_directories(root: Path) -> list[Path]:
    """Find task directories in v0.6 and enhanced-v2 extracted layouts."""

    bundle_root = root / "harbor_bundles"
    if bundle_root.is_dir():
        root = bundle_root
    candidates = [
        path for path in root.iterdir() if path.is_dir() and (path / "task.toml").is_file()
    ]
    if not candidates:
        raise HarnessError(
            "No Harbor task directories found; expected task.toml directories "
            "or an enhanced-v2 harbor_bundles/ directory"
        )
    return sorted(candidates, key=lambda path: path.name)


def _metadata(task_dir: Path) -> dict[str, Any]:
    try:
        document = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"Unable to read Harbor task metadata: {task_dir}") from exc
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise HarnessError(f"Harbor task metadata is missing: {task_dir}")
    return metadata


def _dataset_row(task_dir: Path) -> dict[str, Any] | None:
    matches = sorted(task_dir.glob("tests/data/*/dataset.json"))
    if not matches:
        return None
    try:
        rows = json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Unable to read Harbor task dataset metadata: {matches[0]}") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise HarnessError(f"Harbor task dataset.json must contain one object: {matches[0]}")
    return dict(rows[0])


def _workbook_pair(task_dir: Path, metadata: dict[str, Any]) -> tuple[Path, Path]:
    input_name = str(metadata.get("input_name", ""))
    golden_name = str(metadata.get("golden_name", ""))
    if not input_name or not golden_name:
        raise HarnessError(f"Harbor task workbook names are missing: {task_dir}")
    task_root = task_dir.resolve()

    def child(directory: Path, name: str) -> Path:
        path = (directory / name).resolve()
        if path != task_root and task_root not in path.parents:
            raise HarnessError(f"Harbor task workbook path escaped task directory: {task_dir}")
        return path

    input_candidates = (
        child(task_dir / "preinstall", input_name),
        child(task_dir / "tests" / "data" / "Financial_Model", input_name),
    )
    golden_candidates = (
        child(task_dir / "solution", golden_name),
        child(task_dir / "tests" / "data" / "Financial_Model", golden_name),
    )
    input_path = next((path for path in input_candidates if path.is_file()), None)
    golden_path = next((path for path in golden_candidates if path.is_file()), None)
    if input_path is None or golden_path is None:
        raise HarnessError(f"Harbor task workbook pair is incomplete: {task_dir}")
    return input_path, golden_path


def _instruction(task_dir: Path, row: dict[str, Any] | None) -> str:
    if row is not None and row.get("instruction"):
        return str(row["instruction"])
    path = task_dir / "instruction.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"Harbor task instruction is missing: {task_dir}") from exc
    marker = "**Instruction:**"
    if marker in text:
        text = text.split(marker, 1)[1].split("\n\n", 1)[0]
    return text.strip()


def _row_for_task(task_dir: Path) -> tuple[str, dict[str, Any], Path, Path]:
    metadata = _metadata(task_dir)
    row = _dataset_row(task_dir) or {}
    task_id = str(row.get("id") or metadata.get("task_id") or task_dir.name)
    if (
        not task_id
        or task_id in {".", ".."}
        or "/" in task_id
        or "\\" in task_id
        or Path(task_id).name != task_id
    ):
        raise HarnessError(f"Harbor task ID is not a safe filename: {task_id!r}")
    input_path, golden_path = _workbook_pair(task_dir, metadata)
    answer_position = str(row.get("answer_position") or metadata.get("answer_position") or "")
    if not answer_position:
        raise HarnessError(f"Harbor task answer_position is missing: {task_id}")
    row.update(
        {
            "id": task_id,
            "instruction": _instruction(task_dir, row),
            "spreadsheet_path": f"spreadsheet/{task_id}_input.xlsx",
            "golden_response_path": f"spreadsheet/{task_id}_golden.xlsx",
            "answer_position": answer_position,
        }
    )
    # Keep Harbor provenance available to experiment manifests.  The official
    # evaluator ignores these additional fields, while evolution/held-out
    # policy code can distinguish calibration bundles from scored data.
    for key in (
        "category",
        "task_type",
        "subtype",
        "complexity",
        "instruction_specificity",
        "dataset_role",
        "generator",
        "source_workbook",
        "record_id",
    ):
        if key in metadata:
            row.setdefault(key, metadata[key])
    return task_id, row, input_path, golden_path


def normalize_spreadsheetbench_harbor(
    source: str | Path,
    destination: str | Path,
) -> Path:
    """Convert a Harbor directory or ``.tar.gz`` into canonical v2 layout.

    ``source`` can be either the archive itself, the v0.6 top-level directory,
    or the enhanced-v2 top-level directory.  The returned path contains a
    ``Financial_Model/dataset.json`` and copied workbook pairs, and can be
    passed directly to :func:`load_spreadsheetbench_v2_tasks`.
    """

    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise HarnessError("Harbor normalization destination must differ from source")
    if destination_path.exists() and any(destination_path.iterdir()):
        raise HarnessError(f"Harbor normalization destination is not empty: {destination_path}")
    destination_path.mkdir(parents=True, exist_ok=True)

    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if source_path.is_file():
            if source_path.suffixes[-2:] != [".tar", ".gz"]:
                raise HarnessError("Harbor source file must be a .tar.gz archive")
            temporary = tempfile.TemporaryDirectory(prefix="spreadsheetbench-harbor-")
            extracted = Path(temporary.name)
            _safe_extract(source_path, extracted)
            roots = [path for path in extracted.iterdir() if path.is_dir()]
            if len(roots) != 1:
                raise HarnessError("Harbor archive must contain one top-level directory")
            source_root = roots[0]
        elif source_path.is_dir():
            source_root = source_path
        else:
            raise HarnessError(f"Harbor source is not a file or directory: {source_path}")

        output_category = destination_path / "Financial_Model"
        spreadsheet_dir = output_category / "spreadsheet"
        spreadsheet_dir.mkdir(parents=True)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task_dir in _task_directories(source_root):
            task_id, row, input_path, golden_path = _row_for_task(task_dir)
            if task_id in seen:
                raise HarnessError(f"Duplicate Harbor task ID: {task_id}")
            seen.add(task_id)
            shutil.copy2(input_path, spreadsheet_dir / f"{task_id}_input.xlsx")
            shutil.copy2(golden_path, spreadsheet_dir / f"{task_id}_golden.xlsx")
            rows.append(row)
        metadata_root = source_root / "_meta"
        if metadata_root.is_dir():
            # Enhanced-v2 carries generated instruction/validation context in
            # _meta. Keep it outside the evaluator-facing category tree so it
            # remains auditable without entering model prompts.
            shutil.copytree(metadata_root, destination_path / "harbor_meta")
        (output_category / "dataset.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        provenance = {
            "schema_version": 1,
            "name": source_path.name,
            "revision": "harbor-financial-calibration-v1",
            "format": "harbor-task-bundles-v1",
            "archive_sha256": _sha256_file(source_path) if source_path.is_file() else None,
            "task_count": len(rows),
        }
        (destination_path / HARBOR_PROVENANCE_FILENAME).write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # A failed conversion must not leave a directory that looks usable.
        shutil.rmtree(destination_path, ignore_errors=True)
        raise
    finally:
        if temporary is not None:
            temporary.cleanup()
    return destination_path


__all__ = ["HARBOR_PROVENANCE_FILENAME", "normalize_spreadsheetbench_harbor"]
