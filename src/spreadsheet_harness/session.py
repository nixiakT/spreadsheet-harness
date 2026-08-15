"""Transactional workbook workspace and spreadsheet operations."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable
from copy import copy
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl.formula.translate import Translator
from openpyxl.styles import Border, PatternFill, Side
from openpyxl.styles.cell_style import StyleArray
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

from .code_interpreter import validate_formula_transaction
from .errors import RecalculationIntegrityError, ToolInputError, WorkbookValidationError
from .openpyxl_compat import load_workbook
from .trajectory import TrajectoryRecorder

SUPPORTED_EDIT_FORMATS = {".xlsx", ".xlsm"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_FORMULA_RANGE_RE = re.compile(
    r"(?P<sheet>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_ .]*)!)?"
    r"(?P<start>\$?[A-Za-z]{1,3}\$?\d+):(?P<end>\$?[A-Za-z]{1,3}\$?\d+)"
)
_CELL_REF_RE = re.compile(
    r"(?P<col_abs>\$?)(?P<col>[A-Za-z]{1,3})(?P<row_abs>\$?)(?P<row>\d+)\Z"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _cell_ref_parts(ref: str) -> dict[str, Any] | None:
    match = _CELL_REF_RE.fullmatch(ref)
    if match is None:
        return None
    return {
        "column_absolute": bool(match.group("col_abs")),
        "row_absolute": bool(match.group("row_abs")),
    }


def _formula_sample_coordinates(
    source_cell: str,
    bounds: tuple[int, int, int, int],
) -> list[str]:
    min_col, min_row, max_col, max_row = bounds
    candidates = [
        source_cell.replace("$", ""),
        f"{get_column_letter(min_col)}{min_row}",
        f"{get_column_letter(min(min_col + 1, max_col))}{min_row}",
        f"{get_column_letter(min_col)}{min(min_row + 1, max_row)}",
        f"{get_column_letter(max_col)}{max_row}",
    ]
    return list(dict.fromkeys(candidates))


def _normalize_fill_target_range(
    source_cell: str,
    target_range: str,
) -> tuple[str, bool]:
    source = source_cell.replace("$", "")
    target = target_range.replace("$", "")
    if ":" in target:
        return target_range, False
    try:
        range_boundaries(target)
        range_boundaries(source)
    except (TypeError, ValueError):
        return target_range, False
    if target.upper() == source.upper():
        return target_range, False
    return f"{source}:{target}", True


def _fill_formula_warnings(
    source_formula: str,
    source_cell: str,
    bounds: tuple[int, int, int, int],
    samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    min_col, min_row, max_col, max_row = bounds
    fills_horizontally = max_col > min_col
    fills_vertically = max_row > min_row
    if not fills_horizontally and not fills_vertically:
        return []

    sample_cells = [
        str(sample["cell"])
        for sample in samples
        if sample.get("cell") != source_cell.replace("$", "")
    ]
    warnings: list[dict[str, Any]] = []
    for match in _FORMULA_RANGE_RE.finditer(source_formula):
        start = _cell_ref_parts(match.group("start"))
        end = _cell_ref_parts(match.group("end"))
        if start is None or end is None:
            continue
        issues: list[str] = []
        if fills_horizontally and not (
            start["column_absolute"] and end["column_absolute"]
        ):
            issues.append("column endpoints are not both absolute")
        if fills_vertically and start["row_absolute"] != end["row_absolute"]:
            issues.append("mixed row anchors")
        if not issues:
            continue

        translated_examples: list[dict[str, str]] = []
        for destination in sample_cells:
            translated = Translator(
                "=" + match.group(0),
                origin=source_cell,
            ).translate_formula(destination)[1:]
            if translated != match.group(0):
                translated_examples.append(
                    {"cell": destination, "translated_range": translated}
                )
            if len(translated_examples) >= 3:
                break
        if not translated_examples:
            continue
        warnings.append(
            {
                "type": "possible_expanding_or_drifting_range",
                "source_range": match.group(0),
                "issues": issues,
                "examples": translated_examples,
                "message": (
                    "This range changes during fill_formula. If the range should stay "
                    "fixed across the fill direction, lock both endpoints, e.g. use "
                    "$E6:$G6 instead of E6:G6 or $E6:G6, then refill and verify cached "
                    "values."
                ),
            }
        )
    return warnings


def _color_value(color: Any) -> str | None:
    if color is None:
        return None
    value = getattr(color, "rgb", None)
    if value and value not in {"00000000", "000000"}:
        return str(value)
    indexed = getattr(color, "indexed", None)
    if indexed is not None:
        return f"indexed:{indexed}"
    theme = getattr(color, "theme", None)
    if theme is not None:
        return f"theme:{theme}"
    return None


def _normalize_color(value: str) -> str:
    cleaned = value.strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}|[0-9A-F]{8}", cleaned):
        raise ToolInputError(f"Invalid RGB/ARGB color: {value!r}")
    return cleaned if len(cleaned) == 8 else "FF" + cleaned


def _intersects(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    l_min_col, l_min_row, l_max_col, l_max_row = left
    r_min_col, r_min_row, r_max_col, r_max_row = right
    return not (
        l_max_col < r_min_col
        or r_max_col < l_min_col
        or l_max_row < r_min_row
        or r_max_row < l_min_row
    )


def _table_ref(table: Any) -> str:
    """Return an openpyxl table range across TableList API variants."""
    ref = getattr(table, "ref", table)
    if not isinstance(ref, str):
        raise ToolInputError(f"Workbook table has invalid ref: {ref!r}")
    return ref


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    input: Path
    workbook: Path
    snapshots: Path
    artifacts: Path
    trajectory: Path


class WorkbookSession:
    """Own one isolated workbook copy and apply atomic, auditable mutations."""

    def __init__(
        self,
        paths: SessionPaths,
        run_id: str,
        *,
        recorder_secrets: tuple[str, ...] = (),
    ) -> None:
        self.paths = paths
        self.run_id = run_id
        self._write_lock = threading.RLock()
        self._snapshot_counter = 0
        self.recorder = TrajectoryRecorder(
            paths.trajectory,
            run_id,
            secrets=recorder_secrets,
        )

    @classmethod
    def create(
        cls,
        source: str | Path,
        run_dir: str | Path,
        *,
        run_id: str | None = None,
        recorder_secrets: tuple[str, ...] = (),
    ) -> WorkbookSession:
        source_path = Path(source).expanduser().resolve(strict=True)
        if source_path.suffix.lower() not in SUPPORTED_EDIT_FORMATS:
            raise ToolInputError(
                f"Editing requires .xlsx or .xlsm, got {source_path.suffix}. "
                "Normalize legacy/ODS/CSV input with preprocess first."
            )
        root = Path(run_dir).expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"Run directory is not empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
        input_dir = root / "input"
        artifacts = root / "artifacts"
        snapshots = root / "snapshots"
        for directory in (input_dir, artifacts, snapshots):
            directory.mkdir(mode=0o700)
        safe_name = _SAFE_NAME.sub("_", source_path.name)
        input_copy = input_dir / safe_name
        workbook_copy = artifacts / f"output{source_path.suffix.lower()}"
        shutil.copy2(source_path, input_copy)
        shutil.copy2(source_path, workbook_copy)
        paths = SessionPaths(
            root=root,
            input=input_copy,
            workbook=workbook_copy,
            snapshots=snapshots,
            artifacts=artifacts,
            trajectory=root / "trajectory.jsonl",
        )
        session = cls(
            paths,
            run_id or root.name or uuid.uuid4().hex,
            recorder_secrets=recorder_secrets,
        )
        session._validate(workbook_copy)
        session.recorder.record(
            "session.created",
            {
                "source": str(source_path),
                "input_copy": str(input_copy),
                "workbook": str(workbook_copy),
            },
        )
        return session

    @property
    def workbook_path(self) -> Path:
        return self.paths.workbook

    @property
    def workspace(self) -> Path:
        return self.paths.root

    def _load(self, *, data_only: bool = False):
        return load_workbook(
            self.workbook_path,
            data_only=data_only,
            keep_vba=self.workbook_path.suffix.lower() == ".xlsm",
            keep_links=True,
        )

    def _validate(self, path: Path) -> None:
        try:
            workbook = load_workbook(
                path,
                read_only=True,
                data_only=False,
                keep_vba=path.suffix.lower() == ".xlsm",
                keep_links=True,
            )
            if not workbook.sheetnames:
                raise WorkbookValidationError("Workbook contains no worksheets")
            workbook.close()
        except WorkbookValidationError:
            raise
        except Exception as exc:
            raise WorkbookValidationError(f"Workbook validation failed: {exc}") from exc

    def _sheet(self, workbook: Any, name: str) -> Worksheet:
        if name not in workbook.sheetnames:
            raise ToolInputError(f"Unknown sheet {name!r}; available: {workbook.sheetnames}")
        return workbook[name]

    def _bounds(
        self,
        range_ref: str,
        *,
        max_cells: int = 10_000,
    ) -> tuple[int, int, int, int]:
        try:
            bounds = range_boundaries(range_ref.replace("$", ""))
        except (TypeError, ValueError) as exc:
            raise ToolInputError(f"Invalid A1 range: {range_ref!r}") from exc
        min_col, min_row, max_col, max_row = bounds
        if not all(isinstance(item, int) and item >= 1 for item in bounds):
            raise ToolInputError(f"Range must be bounded: {range_ref!r}")
        count = (max_col - min_col + 1) * (max_row - min_row + 1)
        if count > max_cells:
            raise ToolInputError(f"Range contains {count} cells; limit is {max_cells}")
        return min_col, min_row, max_col, max_row

    def _mutate(
        self,
        operation: str,
        arguments: dict[str, Any],
        callback: Callable[[Any], Any],
    ) -> Any:
        with self._write_lock:
            before_sha256 = _sha256(self.workbook_path)
            self._snapshot_counter += 1
            snapshot = (
                self.paths.snapshots
                / f"{self._snapshot_counter:04d}_{operation}{self.workbook_path.suffix}"
            )
            shutil.copy2(self.workbook_path, snapshot)
            temporary = self.workbook_path.with_name(
                f".{self.workbook_path.stem}.{uuid.uuid4().hex}.tmp{self.workbook_path.suffix}"
            )
            self.recorder.record(
                "workbook.mutation.started",
                {"operation": operation, "arguments": arguments, "snapshot": snapshot},
            )
            workbook = None
            try:
                workbook = self._load(data_only=False)
                result = callback(workbook)
                calculation = getattr(workbook, "calculation", None)
                if calculation is not None:
                    calculation.fullCalcOnLoad = True
                    calculation.forceFullCalc = True
                    calculation.calcMode = "auto"
                workbook.save(temporary)
                workbook.close()
                workbook = None
                self._validate(temporary)
                invalid_references, formula_text = validate_formula_transaction(
                    snapshot,
                    temporary,
                )
                if invalid_references or formula_text:
                    issue_locations = sorted(
                        {(sheet, cell) for sheet, cell, *_ in invalid_references}
                        | {(sheet, cell) for sheet, cell, _ in formula_text}
                    )
                    locations = ", ".join(
                        f"{sheet}!{cell}" for sheet, cell in issue_locations[:8]
                    )
                    raise ToolInputError(
                        "Mutation introduced invalid or high-confidence formula-like text at "
                        f"{locations}. Excel formulas must be strings beginning with '='; "
                        "correct every reported formula issue and retry the complete edit."
                    )
                after_sha256 = _sha256(temporary)
                if isinstance(result, dict):
                    result = {
                        **result,
                        "workbook_sha256_before": before_sha256,
                        "workbook_sha256_after": after_sha256,
                        "workbook_changed": before_sha256 != after_sha256,
                        "message": (
                            "Workbook changed. If the target range has been verified, finish now; "
                            "otherwise run one narrow verification or correction."
                            if before_sha256 != after_sha256
                            else "Workbook did not change; revise the mutation before submitting."
                        ),
                    }
                temporary.replace(self.workbook_path)
                self.recorder.record(
                    "workbook.mutation.committed",
                    {"operation": operation, "snapshot": snapshot, "result": result},
                )
                return result
            except Exception as exc:
                self.recorder.record(
                    "workbook.mutation.rolled_back",
                    {"operation": operation, "snapshot": snapshot, "error": str(exc)},
                )
                raise
            finally:
                if workbook is not None:
                    workbook.close()
                temporary.unlink(missing_ok=True)

    def list_sheets(self) -> dict[str, Any]:
        workbook = self._load(data_only=False)
        try:
            sheets = []
            for index, worksheet in enumerate(workbook.worksheets):
                dimension = worksheet.calculate_dimension()
                sheets.append(
                    {
                        "index": index,
                        "name": worksheet.title,
                        "state": worksheet.sheet_state,
                        "dimension": dimension,
                        "max_row": worksheet.max_row,
                        "max_column": worksheet.max_column,
                        "merged_ranges": len(worksheet.merged_cells.ranges),
                        "tables": list(worksheet.tables.keys()),
                    }
                )
            return {"ok": True, "sheets": sheets, "active": workbook.active.title}
        finally:
            workbook.close()

    def inspect_range(
        self,
        sheet: str,
        range_ref: str,
        *,
        include_styles: bool = True,
        max_cells: int = 500,
    ) -> dict[str, Any]:
        bounds = self._bounds(range_ref, max_cells=max_cells)
        min_col, min_row, max_col, max_row = bounds
        formula_book = self._load(data_only=False)
        value_book = self._load(data_only=True)
        try:
            formula_sheet = self._sheet(formula_book, sheet)
            value_sheet = self._sheet(value_book, sheet)
            matrix: list[list[Any]] = []
            cells: list[dict[str, Any]] = []
            for row in range(min_row, max_row + 1):
                matrix_row: list[Any] = []
                for column in range(min_col, max_col + 1):
                    cell = formula_sheet.cell(row, column)
                    cached_cell = value_sheet.cell(row, column)
                    cached = cached_cell.value
                    raw = cell.value
                    display = raw if isinstance(raw, str) and raw.startswith("=") else cached
                    if display is None:
                        display = raw
                    matrix_row.append(_json_value(display))
                    if raw is not None or cached is not None or cell.has_style:
                        item: dict[str, Any] = {
                            "coordinate": cell.coordinate,
                            "value": _json_value(cached if cached is not None else raw),
                            "formula": raw
                            if isinstance(raw, str) and raw.startswith("=")
                            else None,
                            "data_type": cell.data_type,
                            "cached_data_type": cached_cell.data_type,
                        }
                        if include_styles:
                            item["style"] = {
                                "style_id": cell.style_id,
                                "number_format": cell.number_format,
                                "font": {
                                    "bold": bool(cell.font.bold),
                                    "italic": bool(cell.font.italic),
                                    "color": _color_value(cell.font.color),
                                },
                                "fill": _color_value(cell.fill.fgColor),
                                "alignment": {
                                    "horizontal": cell.alignment.horizontal,
                                    "vertical": cell.alignment.vertical,
                                    "wrap_text": cell.alignment.wrap_text,
                                },
                            }
                        cells.append(item)
                matrix.append(matrix_row)

            merged = [
                str(item)
                for item in formula_sheet.merged_cells.ranges
                if _intersects(bounds, range_boundaries(str(item)))
            ]
            tables = []
            for name in formula_sheet.tables.keys():
                ref = _table_ref(formula_sheet.tables[name])
                if _intersects(bounds, range_boundaries(ref)):
                    tables.append({"name": name, "ref": ref})
            return {
                "ok": True,
                "sheet": sheet,
                "range": f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}",
                "matrix": matrix,
                "cells": cells,
                "merged_ranges": merged,
                "tables": tables,
            }
        finally:
            formula_book.close()
            value_book.close()

    def find_cells(
        self,
        query: str,
        *,
        sheet: str | None = None,
        use_regex: bool = False,
        match_case: bool = False,
        search_formulas: bool = True,
        max_results: int = 200,
        max_scanned_cells: int = 250_000,
    ) -> dict[str, Any]:
        if not query:
            raise ToolInputError("query must not be empty")
        flags = 0 if match_case else re.IGNORECASE
        pattern = re.compile(query if use_regex else re.escape(query), flags)
        workbook = self._load(data_only=not search_formulas)
        scanned = 0
        matches: list[dict[str, Any]] = []
        truncated = False
        try:
            worksheets: Iterable[Worksheet]
            worksheets = [self._sheet(workbook, sheet)] if sheet else workbook.worksheets
            for worksheet in worksheets:
                for row in worksheet.iter_rows():
                    for cell in row:
                        scanned += 1
                        if scanned > max_scanned_cells:
                            truncated = True
                            break
                        value = cell.value
                        if value is not None and pattern.search(str(value)):
                            matches.append(
                                {
                                    "sheet": worksheet.title,
                                    "coordinate": cell.coordinate,
                                    "value": _json_value(value),
                                }
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
                    if truncated:
                        break
                if truncated:
                    break
            return {
                "ok": True,
                "query": query,
                "matches": matches,
                "scanned_cells": scanned,
                "truncated": truncated,
            }
        finally:
            workbook.close()

    def write_range(self, sheet: str, start_cell: str, values: list[list[Any]]) -> dict[str, Any]:
        if (
            not values
            or not isinstance(values, list)
            or any(not isinstance(row, list) for row in values)
        ):
            raise ToolInputError("values must be a non-empty two-dimensional list")
        width = max((len(row) for row in values), default=0)
        if width == 0 or any(len(row) != width for row in values):
            raise ToolInputError("values must be rectangular and contain at least one column")
        if len(values) * width > 10_000:
            raise ToolInputError("write_range is limited to 10,000 cells")
        try:
            start_row, start_column = coordinate_to_tuple(start_cell.replace("$", ""))
        except (TypeError, ValueError) as exc:
            raise ToolInputError(f"Invalid start_cell: {start_cell!r}") from exc

        def apply(workbook: Any) -> dict[str, Any]:
            worksheet = self._sheet(workbook, sheet)
            for row_offset, row_values in enumerate(values):
                for column_offset, value in enumerate(row_values):
                    worksheet.cell(
                        start_row + row_offset, start_column + column_offset
                    ).value = value
            end = f"{get_column_letter(start_column + width - 1)}{start_row + len(values) - 1}"
            return {
                "ok": True,
                "sheet": sheet,
                "range": f"{start_cell}:{end}",
                "cells_written": len(values) * width,
            }

        return self._mutate(
            "write_range", {"sheet": sheet, "start_cell": start_cell, "values": values}, apply
        )

    def fill_formula(self, sheet: str, source_cell: str, target_range: str) -> dict[str, Any]:
        normalized_target_range, expanded_from_endpoint = _normalize_fill_target_range(
            source_cell, target_range
        )
        bounds = self._bounds(normalized_target_range)

        def apply(workbook: Any) -> dict[str, Any]:
            worksheet = self._sheet(workbook, sheet)
            formula = worksheet[source_cell].value
            if not isinstance(formula, str) or not formula.startswith("="):
                if (
                    isinstance(formula, str)
                    and formula == formula.strip()
                    and _FORMULA_RANGE_RE.search(formula) is not None
                    and re.match(r"[A-Za-z][A-Za-z0-9_.]*\(", formula) is not None
                ):
                    raise ToolInputError(
                        f"Source {sheet}!{source_cell} contains formula-like text without a "
                        "leading '='; assign an Excel formula string beginning with '=' before "
                        "calling fill_formula"
                    )
                raise ToolInputError(f"Source {sheet}!{source_cell} does not contain a formula")
            min_col, min_row, max_col, max_row = bounds
            count = 0
            samples: list[dict[str, Any]] = []
            sample_coordinates = set(_formula_sample_coordinates(source_cell, bounds))
            for row in range(min_row, max_row + 1):
                for column in range(min_col, max_col + 1):
                    destination = f"{get_column_letter(column)}{row}"
                    translated = Translator(
                        formula, origin=source_cell
                    ).translate_formula(destination)
                    worksheet[destination] = translated
                    if destination in sample_coordinates:
                        samples.append({"cell": destination, "formula": translated})
                    count += 1
            warnings = _fill_formula_warnings(
                formula,
                source_cell.replace("$", ""),
                bounds,
                samples,
            )
            return {
                "ok": True,
                "sheet": sheet,
                "range": normalized_target_range,
                "requested_range": target_range,
                "target_range_expanded_from_endpoint": expanded_from_endpoint,
                "cells_filled": count,
                "source_formula": formula,
                "sample_formulas": samples,
                "warnings": warnings,
            }

        return self._mutate(
            "fill_formula",
            {
                "sheet": sheet,
                "source_cell": source_cell,
                "target_range": target_range,
                "normalized_target_range": normalized_target_range,
            },
            apply,
        )

    def clear_range(
        self,
        sheet: str,
        range_ref: str,
        *,
        contents: bool = True,
        formats: bool = False,
    ) -> dict[str, Any]:
        if not contents and not formats:
            raise ToolInputError("At least one of contents or formats must be true")
        bounds = self._bounds(range_ref)

        def apply(workbook: Any) -> dict[str, Any]:
            worksheet = self._sheet(workbook, sheet)
            min_col, min_row, max_col, max_row = bounds
            count = 0
            for row in range(min_row, max_row + 1):
                for column in range(min_col, max_col + 1):
                    cell = worksheet.cell(row, column)
                    if contents:
                        cell.value = None
                        cell.comment = None
                        cell.hyperlink = None
                    if formats:
                        cell._style = StyleArray()
                    count += 1
            return {"ok": True, "sheet": sheet, "range": range_ref, "cells_cleared": count}

        return self._mutate(
            "clear_range",
            {"sheet": sheet, "range_ref": range_ref, "contents": contents, "formats": formats},
            apply,
        )

    def format_range(
        self, sheet: str, range_ref: str, format_spec: dict[str, Any]
    ) -> dict[str, Any]:
        if not format_spec:
            raise ToolInputError("format_spec must not be empty")
        bounds = self._bounds(range_ref)
        supported = {
            "number_format",
            "font",
            "fill_color",
            "alignment",
            "border",
            "protection",
            "row_height",
            "column_width",
        }
        unknown = set(format_spec) - supported
        if unknown:
            raise ToolInputError(f"Unsupported format keys: {sorted(unknown)}")

        def apply(workbook: Any) -> dict[str, Any]:
            worksheet = self._sheet(workbook, sheet)
            min_col, min_row, max_col, max_row = bounds
            for row in range(min_row, max_row + 1):
                if "row_height" in format_spec:
                    worksheet.row_dimensions[row].height = float(format_spec["row_height"])
                for column in range(min_col, max_col + 1):
                    if "column_width" in format_spec:
                        worksheet.column_dimensions[get_column_letter(column)].width = float(
                            format_spec["column_width"]
                        )
                    cell = worksheet.cell(row, column)
                    if "number_format" in format_spec:
                        cell.number_format = str(format_spec["number_format"])
                    if "fill_color" in format_spec:
                        cell.fill = PatternFill(
                            fill_type="solid", fgColor=_normalize_color(format_spec["fill_color"])
                        )
                    if "font" in format_spec:
                        font_spec = dict(format_spec["font"])
                        if "color" in font_spec:
                            font_spec["color"] = _normalize_color(font_spec["color"])
                        base = copy(cell.font)
                        for key, value in font_spec.items():
                            if not hasattr(base, key):
                                raise ToolInputError(f"Unsupported font property: {key}")
                            setattr(base, key, value)
                        cell.font = base
                    if "alignment" in format_spec:
                        alignment = copy(cell.alignment)
                        for key, value in dict(format_spec["alignment"]).items():
                            if not hasattr(alignment, key):
                                raise ToolInputError(f"Unsupported alignment property: {key}")
                            setattr(alignment, key, value)
                        cell.alignment = alignment
                    if "protection" in format_spec:
                        protection = copy(cell.protection)
                        for key, value in dict(format_spec["protection"]).items():
                            if not hasattr(protection, key):
                                raise ToolInputError(f"Unsupported protection property: {key}")
                            setattr(protection, key, value)
                        cell.protection = protection
                    if "border" in format_spec:
                        border_spec = dict(format_spec["border"])
                        style = border_spec.get("style", "thin")
                        color = _normalize_color(border_spec.get("color", "000000"))
                        side = Side(style=style, color=color)
                        sides = border_spec.get("sides", ["left", "right", "top", "bottom"])
                        current = copy(cell.border)
                        values = {
                            name: getattr(current, name)
                            for name in ("left", "right", "top", "bottom")
                        }
                        for name in sides:
                            if name not in values:
                                raise ToolInputError(f"Unsupported border side: {name}")
                            values[name] = side
                        cell.border = Border(**values)
            count = (max_col - min_col + 1) * (max_row - min_row + 1)
            return {"ok": True, "sheet": sheet, "range": range_ref, "cells_formatted": count}

        return self._mutate(
            "format_range",
            {"sheet": sheet, "range_ref": range_ref, "format_spec": format_spec},
            apply,
        )

    def delete_rows(self, sheet: str, start: int, amount: int = 1) -> dict[str, Any]:
        if start < 1 or amount < 1 or amount > 10_000:
            raise ToolInputError("start and amount must be positive; amount limit is 10,000")

        def apply(workbook: Any) -> dict[str, Any]:
            self._sheet(workbook, sheet).delete_rows(start, amount)
            return {"ok": True, "sheet": sheet, "start": start, "rows_deleted": amount}

        return self._mutate(
            "delete_rows", {"sheet": sheet, "start": start, "amount": amount}, apply
        )

    def delete_columns(self, sheet: str, start: int, amount: int = 1) -> dict[str, Any]:
        if start < 1 or amount < 1 or amount > 1_000:
            raise ToolInputError("start and amount must be positive; amount limit is 1,000")

        def apply(workbook: Any) -> dict[str, Any]:
            self._sheet(workbook, sheet).delete_cols(start, amount)
            return {"ok": True, "sheet": sheet, "start": start, "columns_deleted": amount}

        return self._mutate(
            "delete_columns", {"sheet": sheet, "start": start, "amount": amount}, apply
        )

    def manage_sheet(
        self,
        action: str,
        name: str,
        *,
        new_name: str | None = None,
        source: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"create", "rename", "delete", "copy"}:
            raise ToolInputError("action must be create, rename, delete, or copy")

        def apply(workbook: Any) -> dict[str, Any]:
            if action == "create":
                if name in workbook.sheetnames:
                    raise ToolInputError(f"Sheet already exists: {name}")
                workbook.create_sheet(name, index)
            elif action == "rename":
                if not new_name:
                    raise ToolInputError("new_name is required for rename")
                self._sheet(workbook, name).title = new_name
            elif action == "delete":
                if len(workbook.sheetnames) == 1:
                    raise ToolInputError("Cannot delete the only worksheet")
                workbook.remove(self._sheet(workbook, name))
            else:
                origin = self._sheet(workbook, source or name)
                copied = workbook.copy_worksheet(origin)
                copied.title = new_name or f"{origin.title} Copy"
            return {"ok": True, "action": action, "sheets": workbook.sheetnames}

        return self._mutate(
            "manage_sheet",
            {
                "action": action,
                "name": name,
                "new_name": new_name,
                "source": source,
                "index": index,
            },
            apply,
        )

    def undo_last(self) -> dict[str, Any]:
        with self._write_lock:
            snapshots = sorted(self.paths.snapshots.glob(f"*{self.workbook_path.suffix}"))
            if not snapshots:
                raise ToolInputError("No snapshot is available")
            snapshot = snapshots[-1]
            temporary = self.workbook_path.with_name(
                f".{self.workbook_path.stem}.undo-{uuid.uuid4().hex}{self.workbook_path.suffix}"
            )
            shutil.copy2(snapshot, temporary)
            self._validate(temporary)
            temporary.replace(self.workbook_path)
            snapshot.unlink()
            self.recorder.record("workbook.undo", {"snapshot": snapshot})
            return {"ok": True, "restored_snapshot": snapshot.name}

    def recalculate(self) -> dict[str, Any]:
        """Recalculate with LibreOffice while preserving a pre-operation snapshot."""

        from .render import recalculate_workbook

        with self._write_lock:
            self._snapshot_counter += 1
            snapshot = (
                self.paths.snapshots
                / f"{self._snapshot_counter:04d}_recalculate{self.workbook_path.suffix}"
            )
            shutil.copy2(self.workbook_path, snapshot)
            self.recorder.record(
                "workbook.mutation.started",
                {"operation": "recalculate", "arguments": {}, "snapshot": snapshot},
            )
            try:
                metadata = recalculate_workbook(self.workbook_path, self.workbook_path)
                self._validate(self.workbook_path)
                self.recorder.record(
                    "workbook.mutation.committed",
                    {"operation": "recalculate", "snapshot": snapshot, "result": metadata},
                )
                return metadata
            except Exception as exc:
                # The renderer publishes atomically, but restore explicitly in case a
                # platform-specific replace succeeded immediately before validation.
                shutil.copy2(snapshot, self.workbook_path)
                failure_evidence = (
                    exc.evidence if isinstance(exc, RecalculationIntegrityError) else None
                )
                self.recorder.record(
                    "workbook.mutation.rolled_back",
                    {
                        "operation": "recalculate",
                        "snapshot": snapshot,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "recalculation": failure_evidence,
                    },
                )
                raise

    def write_manifest(self, values: dict[str, Any]) -> Path:
        path = self.paths.root / "run.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
        return path
