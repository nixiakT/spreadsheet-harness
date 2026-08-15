#!/usr/bin/env python3
"""Freeze and verify a development cohort without inspecting task content."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "spreadsheet-development-cohort-prereg-v1"
TOOL_VERSION = 1
SELECTION_POLICY = "raw-dataset-order_instruction-type_exposure-membership-only_v1"
INVENTORY_POLICY = "conservative-structured-exposure-inventory_v1"
TASK_ID_MAPPING_KEYS = frozenset({"arm_order", "task_order_by_id"})
INVENTORY_SUFFIXES = frozenset({".json", ".jsonl", ".txt"})


class CohortFreezeError(RuntimeError):
    """Raised when cohort freezing cannot be proved safe."""


@dataclass(frozen=True)
class DatasetRow:
    raw_index: int
    task_id: str
    instruction_type: str


@dataclass(frozen=True)
class FreezeConfig:
    repository_root: Path
    dataset: Path
    window_start: int
    window_stop: int
    public_roots: tuple[Path, ...]
    results_roots: tuple[Path, ...]
    private_roots: tuple[Path, ...]
    code_inventories: tuple[Path, ...]
    code_symbols: tuple[str, ...]
    count: int
    quotas: tuple[tuple[str, int], ...]
    expected_window_count: int | None
    expected_dataset_sha256: str
    expected_exposed_count: int | None
    expected_eligible_count: int | None
    expected_type_counts: tuple[tuple[str, int], ...]
    prereg: Path
    task_list: Path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ids_sha256(task_ids: list[str] | tuple[str, ...]) -> str:
    return _sha256("".join(f"{task_id}\n" for task_id in task_ids).encode("utf-8"))


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CohortFreezeError("A JSON inventory artifact contains a duplicate object key")
        result[key] = value
    return result


def _reject_nonfinite_json(_: str) -> None:
    raise CohortFreezeError("A JSON inventory artifact contains a non-finite number")


def _parse_json(data: bytes, *, label: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CohortFreezeError(f"{label} is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite_json,
        )
    except CohortFreezeError:
        raise
    except json.JSONDecodeError as exc:
        raise CohortFreezeError(f"{label} is not valid JSON") from exc


def _read_regular_file(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[bytes, int]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CohortFreezeError(f"Cannot inspect {label}") from exc
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise CohortFreezeError(f"{label} must be a regular, non-symlink file")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CohortFreezeError(f"Cannot open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise CohortFreezeError(f"{label} changed while it was opened")
        if (
            expected_identity is not None
            and (
                opened.st_dev,
                opened.st_ino,
            )
            != expected_identity
        ):
            raise CohortFreezeError(f"{label} has an unexpected file identity")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise CohortFreezeError(f"Cannot re-inspect {label}") from exc
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or (path_after.st_dev, path_after.st_ino) != (opened.st_dev, opened.st_ino)
        or path.is_symlink()
    ):
        raise CohortFreezeError(f"{label} changed while it was read")
    return b"".join(chunks), stat.S_IMODE(after.st_mode)


def _resolve_input_file(value: str, *, label: str) -> Path:
    path = Path(value).expanduser()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CohortFreezeError(f"{label} does not exist") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise CohortFreezeError(f"{label} must be a regular, non-symlink file")
    return path.resolve(strict=True)


def _resolve_input_directory(value: str, *, label: str) -> Path:
    path = Path(value).expanduser()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CohortFreezeError(f"{label} does not exist") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise CohortFreezeError(f"{label} must be a non-symlink directory")
    return path.resolve(strict=True)


def _resolve_dataset(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CohortFreezeError("Dataset does not exist") from exc
    if path.is_symlink():
        raise CohortFreezeError("Dataset must not be a symlink")
    if stat.S_ISDIR(metadata.st_mode):
        path = path / "dataset.json"
    return _resolve_input_file(str(path), label="dataset metadata file")


def _assert_owner_only_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CohortFreezeError(f"Cannot inspect the {label}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise CohortFreezeError(f"The {label} must be a non-symlink directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise CohortFreezeError(f"The {label} must have mode 0700")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise CohortFreezeError(f"The {label} must be owned by the current user")


def _resolve_output_file(value: str, *, label: str) -> Path:
    requested = Path(value).expanduser()
    if not requested.name:
        raise CohortFreezeError(f"{label} must name a file")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise CohortFreezeError(f"Cannot resolve the {label} parent directory") from exc
    _assert_owner_only_directory(parent, label=f"{label} parent directory")
    path = parent / requested.name
    if os.path.lexists(path):
        raise CohortFreezeError(f"Refusing to overwrite an existing {label}")
    return path


def _assert_private_output_location(
    path: Path,
    *,
    repository_root: Path,
    private_roots: tuple[Path, ...],
    label: str,
) -> None:
    if path == repository_root or repository_root in path.parents:
        raise CohortFreezeError(f"The {label} must be outside the repository")
    if not any(path == root or root in path.parents for root in private_roots):
        raise CohortFreezeError(f"The {label} must be inside a private inventory root")


def _validate_task_id(value: Any, *, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CohortFreezeError(f"{label} contains a non-scalar task ID")
    task_id = str(value)
    if (
        not task_id
        or len(task_id) > 256
        or task_id.startswith("#")
        or any(character.isspace() or ord(character) < 32 for character in task_id)
    ):
        raise CohortFreezeError(f"{label} contains an invalid task ID")
    return task_id


def _is_task_id_key(key: str) -> bool:
    normalized = key.lower()
    return normalized == "task_id" or normalized.endswith("_task_id")


def _is_task_ids_key(key: str) -> bool:
    normalized = key.lower()
    return normalized == "task_ids" or normalized.endswith("_task_ids")


def _structured_task_ids(value: Any, *, label: str) -> set[str]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for raw_key, child in node.items():
                key = str(raw_key)
                if _is_task_id_key(key):
                    found.add(_validate_task_id(child, label=label))
                elif _is_task_ids_key(key):
                    if not isinstance(child, list):
                        raise CohortFreezeError(f"{label} has a malformed task-ID list")
                    for item in child:
                        found.add(_validate_task_id(item, label=label))
                elif key.lower() in TASK_ID_MAPPING_KEYS:
                    if not isinstance(child, dict):
                        raise CohortFreezeError(f"{label} has a malformed task-ID mapping")
                    for task_id in child:
                        found.add(_validate_task_id(task_id, label=label))
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def _jsonl_task_ids(data: bytes, *, label: str) -> set[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CohortFreezeError(f"{label} is not valid UTF-8") from exc
    found: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(
                line,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonfinite_json,
            )
        except CohortFreezeError:
            raise
        except json.JSONDecodeError as exc:
            raise CohortFreezeError(f"{label} contains an invalid JSONL record") from exc
        if not isinstance(row, dict):
            raise CohortFreezeError(f"{label} contains a non-object JSONL record")
        found.update(_structured_task_ids(row, label=label))
    return found


def _task_list_ids(data: bytes, *, label: str) -> set[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CohortFreezeError(f"{label} is not valid UTF-8") from exc
    found: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line.split()) != 1:
            raise CohortFreezeError(f"{label} is not a one-task-ID-per-line list")
        found.add(_validate_task_id(line, label=label))
    return found


def _inventory_files(root: Path, *, excluded: frozenset[Path]) -> list[Path]:
    files: list[Path] = []

    def fail_closed(error: OSError) -> None:
        raise CohortFreezeError("An inventory root could not be traversed completely") from error

    for current, directory_names, file_names in os.walk(
        root, followlinks=False, onerror=fail_closed
    ):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            child = current_path / name
            if child.is_symlink():
                raise CohortFreezeError("An inventory root contains a symlink directory")
        for name in file_names:
            path = current_path / name
            if path.suffix.lower() not in INVENTORY_SUFFIXES:
                continue
            if path.is_symlink() or not path.is_file():
                raise CohortFreezeError("An inventory artifact is not a regular file")
            if path in excluded:
                continue
            files.append(path)
    return files


def _scan_inventory_root(
    root: Path,
    *,
    kind: str,
    root_index: int,
    excluded: frozenset[Path],
) -> tuple[list[dict[str, Any]], set[str]]:
    sources: list[dict[str, Any]] = []
    exposed: set[str] = set()
    for path in _inventory_files(root, excluded=excluded):
        relative_path = path.relative_to(root).as_posix()
        label = f"{kind} inventory artifact"
        data, mode = _read_regular_file(path, label=label)
        suffix = path.suffix.lower()
        if suffix == ".json":
            task_ids = _structured_task_ids(_parse_json(data, label=label), label=label)
        elif suffix == ".jsonl":
            task_ids = _jsonl_task_ids(data, label=label)
        else:
            task_ids = _task_list_ids(data, label=label)
        ordered = sorted(task_ids)
        exposed.update(task_ids)
        sources.append(
            {
                "kind": kind,
                "root_index": root_index,
                "relative_path": relative_path,
                "format": suffix.removeprefix("."),
                "byte_count": len(data),
                "mode": mode,
                "sha256": _sha256(data),
                "extracted_task_id_count": len(ordered),
                "extracted_task_ids_sha256": _ids_sha256(ordered),
            }
        )
    return sources, exposed


class _UnresolvedAstValue(Exception):
    pass


def _safe_ast_value(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, bool, type(None))):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise _UnresolvedAstValue
        return values[node.id]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return tuple(_safe_ast_value(item, values) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _safe_ast_value(key, values): _safe_ast_value(value, values)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    raise _UnresolvedAstValue


def _assignment_nodes(tree: ast.Module) -> dict[str, ast.AST]:
    result: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                result[target.id] = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.value is not None:
                result[statement.target.id] = statement.value
    return result


def _auto_code_symbol(name: str) -> bool:
    upper = name.upper()
    if any(marker in upper for marker in ("SHA256", "CHECKSUM", "_HASH", "_COUNT")):
        return False
    protected_or_quarantined = "PROTECTED" in upper or "QUARANTIN" in upper
    cohort_value = "TASK_IDS" in upper or "COHORT" in upper
    return protected_or_quarantined and cohort_value


def _task_ids_from_code_value(value: Any, *, label: str) -> set[str]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for child in node.values():
                visit(child)
        elif isinstance(node, (tuple, list, set, frozenset)):
            for child in node:
                visit(child)
        elif isinstance(node, (str, int)) and not isinstance(node, bool):
            found.add(_validate_task_id(node, label=label))
        elif node is not None:
            raise CohortFreezeError(f"{label} contains an unsupported value")

    visit(value)
    return found


def _scan_code_inventory(
    path: Path,
    *,
    index: int,
    requested_symbols: tuple[str, ...],
) -> tuple[dict[str, Any], set[str], set[str]]:
    data, mode = _read_regular_file(path, label="code-owned cohort inventory")
    try:
        tree = ast.parse(data.decode("utf-8"), filename="<code-inventory>")
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise CohortFreezeError("A code-owned cohort inventory cannot be parsed") from exc
    nodes = _assignment_nodes(tree)
    values: dict[str, Any] = {}
    remaining = dict(nodes)
    while remaining:
        progressed = False
        for name, node in tuple(remaining.items()):
            try:
                values[name] = _safe_ast_value(node, values)
            except _UnresolvedAstValue:
                continue
            del remaining[name]
            progressed = True
        if not progressed:
            break

    symbols = (
        [name for name in requested_symbols if name in nodes]
        if requested_symbols
        else [name for name in nodes if _auto_code_symbol(name)]
    )
    unresolved = [name for name in symbols if name not in values]
    if unresolved:
        raise CohortFreezeError("A requested code-owned cohort symbol cannot be resolved safely")
    if not symbols:
        return (
            {
                "index": index,
                "mode": mode,
                "byte_count": len(data),
                "sha256": _sha256(data),
                "symbols": [],
            },
            set(),
            set(),
        )

    exposed: set[str] = set()
    symbol_records: list[dict[str, Any]] = []
    for name in sorted(symbols):
        task_ids = sorted(
            _task_ids_from_code_value(values[name], label="code-owned cohort inventory")
        )
        exposed.update(task_ids)
        symbol_records.append(
            {
                "name": name,
                "task_id_count": len(task_ids),
                "task_ids_sha256": _ids_sha256(task_ids),
            }
        )
    return (
        {
            "index": index,
            "mode": mode,
            "byte_count": len(data),
            "sha256": _sha256(data),
            "symbols": symbol_records,
        },
        exposed,
        set(symbols),
    )


def _load_dataset(path: Path) -> tuple[bytes, list[DatasetRow]]:
    data, _ = _read_regular_file(path, label="dataset metadata file")
    document = _parse_json(data, label="dataset metadata file")
    if not isinstance(document, list):
        raise CohortFreezeError("Dataset metadata must be a JSON array")
    rows: list[DatasetRow] = []
    seen: set[str] = set()
    for raw_index, raw_row in enumerate(document):
        if not isinstance(raw_row, dict):
            raise CohortFreezeError("Dataset metadata contains a non-object row")
        if "id" not in raw_row or "instruction_type" not in raw_row:
            raise CohortFreezeError("Dataset metadata is missing a selection field")
        task_id = _validate_task_id(raw_row["id"], label="dataset metadata")
        instruction_type = raw_row["instruction_type"]
        if not isinstance(instruction_type, str) or not instruction_type:
            raise CohortFreezeError("Dataset metadata has an invalid instruction type")
        if task_id in seen:
            raise CohortFreezeError("Dataset metadata contains duplicate task IDs")
        seen.add(task_id)
        rows.append(DatasetRow(raw_index, task_id, instruction_type))
    return data, rows


def _root_entries(config: FreezeConfig) -> list[tuple[str, int, Path]]:
    result: list[tuple[str, int, Path]] = []
    for kind, roots in (
        ("public_protocol", config.public_roots),
        ("result", config.results_roots),
        ("private", config.private_roots),
    ):
        result.extend((kind, index, root) for index, root in enumerate(roots))
    return result


def _configuration_record(config: FreezeConfig) -> dict[str, Any]:
    return {
        "repository_root": str(config.repository_root),
        "dataset": str(config.dataset),
        "window_start": config.window_start,
        "window_stop": config.window_stop,
        "public_protocol_roots": [str(path) for path in config.public_roots],
        "results_roots": [str(path) for path in config.results_roots],
        "private_inventory_roots": [str(path) for path in config.private_roots],
        "code_inventories": [str(path) for path in config.code_inventories],
        "code_symbols": list(config.code_symbols),
        "selection_count": config.count,
        "instruction_type_quotas": dict(config.quotas),
        "expected_counts": {
            "dataset_sha256": config.expected_dataset_sha256,
            "window": config.expected_window_count,
            "exposed": config.expected_exposed_count,
            "eligible": config.expected_eligible_count,
            "eligible_by_instruction_type": dict(config.expected_type_counts),
        },
        "outputs": {
            "prereg": str(config.prereg),
            "task_list": str(config.task_list),
        },
    }


def _select_rows(
    rows: list[DatasetRow], exposed: set[str], config: FreezeConfig
) -> tuple[list[DatasetRow], dict[str, int]]:
    window = rows[config.window_start : config.window_stop]
    eligible = [row for row in window if row.task_id not in exposed]
    eligible_by_type: dict[str, int] = {}
    for row in eligible:
        eligible_by_type[row.instruction_type] = eligible_by_type.get(row.instruction_type, 0) + 1

    if config.quotas:
        remaining = dict(config.quotas)
        selected: list[DatasetRow] = []
        for row in eligible:
            if remaining.get(row.instruction_type, 0) > 0:
                selected.append(row)
                remaining[row.instruction_type] -= 1
        if any(remaining.values()):
            raise CohortFreezeError("The eligible pool cannot satisfy the instruction-type quotas")
    else:
        selected = eligible[: config.count]
    if len(selected) != config.count:
        raise CohortFreezeError("The eligible pool cannot satisfy the requested cohort count")
    return selected, eligible_by_type


def _assert_expected_counts(
    *,
    window_count: int,
    exposed_count: int,
    eligible_count: int,
    eligible_by_type: dict[str, int],
    config: FreezeConfig,
) -> None:
    assertions = (
        (config.expected_window_count, window_count, "window"),
        (config.expected_exposed_count, exposed_count, "exposed"),
        (config.expected_eligible_count, eligible_count, "eligible"),
    )
    for expected, actual, label in assertions:
        if expected is not None and expected != actual:
            raise CohortFreezeError(f"The expected {label} count does not match the inventory")
    for instruction_type, expected in config.expected_type_counts:
        if eligible_by_type.get(instruction_type, 0) != expected:
            raise CohortFreezeError(
                "An expected eligible instruction-type count does not match the inventory"
            )


def _build_prereg(config: FreezeConfig) -> tuple[dict[str, Any], bytes, dict[str, int]]:
    dataset_bytes, rows = _load_dataset(config.dataset)
    if _sha256(dataset_bytes) != config.expected_dataset_sha256:
        raise CohortFreezeError("The dataset SHA-256 does not match the preregistered anchor")
    excluded = frozenset({config.prereg, config.task_list})
    inventory_sources: list[dict[str, Any]] = []
    exposed: set[str] = set()
    for kind, root_index, root in _root_entries(config):
        sources, root_exposed = _scan_inventory_root(
            root,
            kind=kind,
            root_index=root_index,
            excluded=excluded,
        )
        inventory_sources.extend(sources)
        exposed.update(root_exposed)

    code_sources: list[dict[str, Any]] = []
    found_requested_symbols: set[str] = set()
    for index, path in enumerate(config.code_inventories):
        record, code_exposed, found_symbols = _scan_code_inventory(
            path,
            index=index,
            requested_symbols=config.code_symbols,
        )
        code_sources.append(record)
        exposed.update(code_exposed)
        found_requested_symbols.update(found_symbols)
    if config.code_symbols and found_requested_symbols != set(config.code_symbols):
        raise CohortFreezeError("A requested code-owned cohort symbol was not found")
    if not config.code_symbols and not any(source["symbols"] for source in code_sources):
        raise CohortFreezeError("No code-owned protected or quarantined cohort symbol was found")

    window = rows[config.window_start : config.window_stop]
    window_exposed = [row for row in window if row.task_id in exposed]
    selected, eligible_by_type = _select_rows(rows, exposed, config)
    eligible_count = len(window) - len(window_exposed)
    _assert_expected_counts(
        window_count=len(window),
        exposed_count=len(window_exposed),
        eligible_count=eligible_count,
        eligible_by_type=eligible_by_type,
        config=config,
    )

    selected_ids = [row.task_id for row in selected]
    task_list_bytes = "".join(f"{task_id}\n" for task_id in selected_ids).encode("utf-8")
    ordered_exposed = sorted(exposed)
    prereg = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "selection_policy": SELECTION_POLICY,
        "inventory_policy": INVENTORY_POLICY,
        "configuration": _configuration_record(config),
        "dataset": {
            "byte_count": len(dataset_bytes),
            "sha256": _sha256(dataset_bytes),
            "row_count": len(rows),
        },
        "inventory": {
            "source_count": len(inventory_sources),
            "sources": inventory_sources,
            "code_source_count": len(code_sources),
            "code_sources": code_sources,
            "exposed_task_id_count": len(ordered_exposed),
            "exposed_task_ids_sha256": _ids_sha256(ordered_exposed),
        },
        "window": {
            "row_count": len(window),
            "exposed_count": len(window_exposed),
            "eligible_count": eligible_count,
            "eligible_by_instruction_type": dict(sorted(eligible_by_type.items())),
        },
        "selection": {
            "task_count": len(selected),
            "task_ids": selected_ids,
            "task_ids_sha256": _ids_sha256(selected_ids),
            "tasks": [
                {
                    "raw_index": row.raw_index,
                    "task_id": row.task_id,
                    "instruction_type": row.instruction_type,
                }
                for row in selected
            ],
        },
        "task_list": {
            "byte_count": len(task_list_bytes),
            "line_count": len(selected_ids),
            "sha256": _sha256(task_list_bytes),
        },
    }
    counts = {
        "inventory_sources": len(inventory_sources) + len(code_sources),
        "window_rows": len(window),
        "exposed_rows": len(window_exposed),
        "eligible_rows": eligible_count,
        "selected_rows": len(selected),
    }
    return prereg, task_list_bytes, counts


def _write_temp(path: Path, data: bytes) -> tuple[Path, tuple[int, int]]:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        return temp_path, (metadata.st_dev, metadata.st_ino)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def _fsync_directories(paths: set[Path]) -> None:
    for path in sorted(paths):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _unlink_if_inode(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise CohortFreezeError("A newly created output changed before rollback")
    path.unlink()


def _atomic_create_pair(
    first_path: Path,
    first_data: bytes,
    second_path: Path,
    second_data: bytes,
) -> dict[Path, tuple[int, int]]:
    if first_path == second_path:
        raise CohortFreezeError("Preregistration and task-list outputs must be different files")
    if os.path.lexists(first_path) or os.path.lexists(second_path):
        raise CohortFreezeError("Refusing to overwrite an existing output")
    first_temp, first_identity = _write_temp(first_path, first_data)
    try:
        second_temp, second_identity = _write_temp(second_path, second_data)
    except BaseException:
        first_temp.unlink(missing_ok=True)
        raise
    created: dict[Path, tuple[int, int]] = {}
    try:
        os.link(first_temp, first_path)
        created[first_path] = first_identity
        os.link(second_temp, second_path)
        created[second_path] = second_identity
        _fsync_directories({first_path.parent, second_path.parent})
    except BaseException:
        for path, identity in reversed(tuple(created.items())):
            _unlink_if_inode(path, identity)
        _fsync_directories({first_path.parent, second_path.parent})
        raise
    finally:
        first_temp.unlink(missing_ok=True)
        second_temp.unlink(missing_ok=True)
    return created


def _remove_created_outputs(created: dict[Path, tuple[int, int]]) -> None:
    for path, identity in reversed(tuple(created.items())):
        _unlink_if_inode(path, identity)
    _fsync_directories({path.parent for path in created})


def _assert_owner_only(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
    expected_bytes: bytes | None = None,
) -> bytes:
    data, mode = _read_regular_file(
        path,
        label=label,
        expected_identity=expected_identity,
    )
    if mode != 0o600:
        raise CohortFreezeError(f"{label} must have mode 0600")
    try:
        owner = path.stat().st_uid
    except OSError as exc:
        raise CohortFreezeError(f"Cannot inspect {label} ownership") from exc
    if hasattr(os, "getuid") and owner != os.getuid():
        raise CohortFreezeError(f"{label} must be owned by the current user")
    if expected_bytes is not None and data != expected_bytes:
        raise CohortFreezeError(f"{label} content does not match the frozen output")
    return data


def freeze(config: FreezeConfig) -> dict[str, int]:
    first_prereg, first_task_list, first_counts = _build_prereg(config)
    second_prereg, second_task_list, second_counts = _build_prereg(config)
    if (
        first_prereg != second_prereg
        or first_task_list != second_task_list
        or first_counts != second_counts
    ):
        raise CohortFreezeError("The exposure inventory changed during freezing")
    created = _atomic_create_pair(
        config.prereg,
        (prereg_bytes := _canonical_json(second_prereg)),
        config.task_list,
        second_task_list,
    )
    try:
        final_prereg, final_task_list, final_counts = _build_prereg(config)
        if (
            final_prereg != second_prereg
            or final_task_list != second_task_list
            or final_counts != second_counts
        ):
            raise CohortFreezeError("The exposure inventory changed while outputs were created")
        _assert_owner_only(
            config.prereg,
            label="preregistration output",
            expected_identity=created[config.prereg],
            expected_bytes=prereg_bytes,
        )
        _assert_owner_only(
            config.task_list,
            label="task-list output",
            expected_identity=created[config.task_list],
            expected_bytes=second_task_list,
        )
    except BaseException:
        _remove_created_outputs(created)
        raise
    return second_counts


def verify(config: FreezeConfig) -> dict[str, int]:
    prereg_bytes = _assert_owner_only(config.prereg, label="preregistration output")
    task_list_bytes = _assert_owner_only(config.task_list, label="task-list output")
    expected, expected_task_list, counts = _build_prereg(config)
    expected_again, expected_task_list_again, counts_again = _build_prereg(config)
    if (
        expected != expected_again
        or expected_task_list != expected_task_list_again
        or counts != counts_again
    ):
        raise CohortFreezeError("The exposure inventory changed during verification")
    if prereg_bytes != _canonical_json(expected):
        raise CohortFreezeError("Preregistration verification failed")
    if task_list_bytes != expected_task_list:
        raise CohortFreezeError("Task-list verification failed")
    if _assert_owner_only(config.prereg, label="preregistration output") != prereg_bytes:
        raise CohortFreezeError("Preregistration output changed during verification")
    if _assert_owner_only(config.task_list, label="task-list output") != task_list_bytes:
        raise CohortFreezeError("Task-list output changed during verification")
    return counts


def _parse_count_map(values: list[str], *, label: str) -> tuple[tuple[str, int], ...]:
    parsed: dict[str, int] = {}
    for raw in values:
        name, separator, count_text = raw.rpartition("=")
        if not separator or not name:
            raise CohortFreezeError(f"{label} entries must use NAME=COUNT")
        try:
            count = int(count_text)
        except ValueError as exc:
            raise CohortFreezeError(f"{label} counts must be integers") from exc
        if count < 0 or name in parsed:
            raise CohortFreezeError(f"{label} entries must be unique and non-negative")
        parsed[name] = count
    return tuple(sorted(parsed.items()))


def _validate_non_overlapping_roots(roots: list[Path]) -> None:
    if len(roots) != len(set(roots)):
        raise CohortFreezeError("Inventory roots must be unique")
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise CohortFreezeError("Inventory roots must not overlap")


def _config_from_args(args: argparse.Namespace, *, outputs_must_exist: bool) -> FreezeConfig:
    repository_root = _resolve_input_directory(args.repository_root, label="repository root")
    dataset = _resolve_dataset(args.dataset)
    public_roots = tuple(
        _resolve_input_directory(value, label="public protocol inventory root")
        for value in args.public_protocol_root
    )
    results_roots = tuple(
        _resolve_input_directory(value, label="results inventory root")
        for value in args.results_root
    )
    private_roots = tuple(
        _resolve_input_directory(value, label="private inventory root")
        for value in args.private_inventory_root
    )
    code_inventories = tuple(
        _resolve_input_file(value, label="code-owned cohort inventory")
        for value in args.code_inventory
    )
    all_roots = [*public_roots, *results_roots, *private_roots]
    _validate_non_overlapping_roots(all_roots)
    if any(dataset == root or root in dataset.parents for root in all_roots):
        raise CohortFreezeError("The dataset must be outside every inventory root")

    if outputs_must_exist:
        prereg = _resolve_input_file(args.prereg, label="preregistration output")
        task_list = _resolve_input_file(args.task_list, label="task-list output")
        _assert_owner_only_directory(prereg.parent, label="preregistration output parent directory")
        _assert_owner_only_directory(task_list.parent, label="task-list output parent directory")
    else:
        prereg = _resolve_output_file(args.prereg, label="preregistration output")
        task_list = _resolve_output_file(args.task_list, label="task-list output")
    for output, label in (
        (prereg, "preregistration output"),
        (task_list, "task-list output"),
    ):
        _assert_private_output_location(
            output,
            repository_root=repository_root,
            private_roots=private_roots,
            label=label,
        )
    quotas = _parse_count_map(args.quota, label="Quota")
    expected_type_counts = _parse_count_map(
        args.expected_eligible_type_count,
        label="Expected eligible instruction-type count",
    )
    if args.count is None and not quotas:
        raise CohortFreezeError("Either --count or at least one --quota is required")
    count = sum(value for _, value in quotas) if args.count is None else args.count
    if count <= 0:
        raise CohortFreezeError("The cohort count must be positive")
    if quotas and sum(value for _, value in quotas) != count:
        raise CohortFreezeError("The quota total must equal --count")
    if args.window_start < 0 or args.window_stop <= args.window_start:
        raise CohortFreezeError("The raw dataset window is invalid")

    dataset_bytes, dataset_rows = _load_dataset(dataset)
    if _sha256(dataset_bytes) != args.expected_dataset_sha256:
        raise CohortFreezeError("The dataset SHA-256 does not match the preregistered anchor")
    if args.window_stop > len(dataset_rows):
        raise CohortFreezeError("The raw dataset window exceeds the dataset")
    return FreezeConfig(
        repository_root=repository_root,
        dataset=dataset,
        window_start=args.window_start,
        window_stop=args.window_stop,
        public_roots=public_roots,
        results_roots=results_roots,
        private_roots=private_roots,
        code_inventories=code_inventories,
        code_symbols=tuple(sorted(set(args.code_symbol))),
        count=count,
        quotas=quotas,
        expected_window_count=args.expected_window_count,
        expected_dataset_sha256=args.expected_dataset_sha256,
        expected_exposed_count=args.expected_exposed_count,
        expected_eligible_count=args.expected_eligible_count,
        expected_type_counts=expected_type_counts,
        prereg=prereg,
        task_list=task_list,
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--expected-dataset-sha256", required=True)
    parser.add_argument("--window-start", "--raw-start", type=int, required=True)
    parser.add_argument("--window-stop", "--raw-stop", type=int, required=True)
    parser.add_argument("--public-protocol-root", "--protocol-root", action="append", required=True)
    parser.add_argument("--results-root", action="append", required=True)
    parser.add_argument(
        "--private-inventory-root", "--private-root", action="append", required=True
    )
    parser.add_argument("--code-inventory", action="append", required=True)
    parser.add_argument(
        "--code-symbol",
        action="append",
        default=[],
        help="Exact protected/quarantined cohort symbol; auto-detected when omitted",
    )
    parser.add_argument("--count", type=int)
    parser.add_argument("--quota", action="append", default=[], metavar="TYPE=COUNT")
    parser.add_argument("--expected-window-count", type=int)
    parser.add_argument("--expected-exposed-count", type=int)
    parser.add_argument("--expected-eligible-count", "--expected-eligible", type=int)
    parser.add_argument(
        "--expected-eligible-type-count",
        action="append",
        default=[],
        metavar="TYPE=COUNT",
    )
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--task-list", "--task-id-file", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze or verify a content-blind spreadsheet development cohort"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("freeze", "verify"):
        subparser = subparsers.add_parser(command)
        _add_common_arguments(subparser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args, outputs_must_exist=args.command == "verify")
        counts = freeze(config) if args.command == "freeze" else verify(config)
    except CohortFreezeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("error: a filesystem operation failed", file=sys.stderr)
        return 2
    print(json.dumps(counts, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
