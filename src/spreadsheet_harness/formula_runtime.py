"""Raw-OOXML formula state used by the runtime validation gate."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from .errors import WorkbookValidationError
from .render import sha256_file, sheet_inventory_identity

FormulaCoordinate = tuple[str, str]

_WORKBOOK_PART = "xl/workbook.xml"
_WORKBOOK_RELATIONSHIPS_PART = "xl/_rels/workbook.xml.rels"
_WORKBOOK_PART_MAX_BYTES = 8 * 1024 * 1024
_WORKSHEET_PART_MAX_BYTES = 128 * 1024 * 1024
_SPREADSHEET_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "http://purl.oclc.org/ooxml/spreadsheetml/main",
    }
)
_OFFICE_RELATIONSHIP_NAMESPACES = {
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main": (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ),
    "http://purl.oclc.org/ooxml/spreadsheetml/main": (
        "http://purl.oclc.org/ooxml/officeDocument/relationships"
    ),
}
_PACKAGE_RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/package/2006/relationships",
        "http://purl.oclc.org/ooxml/package/relationships",
    }
)
_WORKSHEET_RELATIONSHIP_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/worksheet",
    }
)
_A1_COORDINATE = re.compile(r"([A-Z]{1,3})([1-9][0-9]*)\Z")
_SPREADSHEET_ERROR_VALUES = frozenset(
    {
        "#BLOCKED!",
        "#BUSY!",
        "#CALC!",
        "#CONNECT!",
        "#DIV/0!",
        "#FIELD!",
        "#GETTING_DATA",
        "#N/A",
        "#NAME?",
        "#NULL!",
        "#NUM!",
        "#PYTHON!",
        "#REF!",
        "#SPILL!",
        "#UNKNOWN!",
        "#VALUE!",
    }
)
_LIBREOFFICE_ERROR_VALUE = re.compile(r"Err:\s*\d{3}\Z", re.IGNORECASE)
_EVIDENCE_SAMPLE_LIMIT = 32
_ERROR_TEXT_LIMIT = 256


@dataclass(frozen=True)
class FormulaCellState:
    formula_sha256: str
    cached_type: str | None
    cached_value: str | None


@dataclass(frozen=True)
class FormulaInventory:
    workbook_sha256: str
    state_sha256: str
    cells: dict[FormulaCoordinate, FormulaCellState]


def _namespace(tag: Any) -> str | None:
    if not isinstance(tag, str) or not tag.startswith("{") or "}" not in tag:
        return None
    return tag[1 : tag.index("}")]


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _read_unique_part(
    package: zipfile.ZipFile,
    part_name: str,
    *,
    maximum_bytes: int,
) -> bytes:
    matches = [member for member in package.infolist() if member.filename == part_name]
    if len(matches) != 1:
        raise WorkbookValidationError(
            f"OOXML formula inventory requires exactly one {part_name}; found {len(matches)}"
        )
    member = matches[0]
    if member.is_dir() or member.flag_bits & 0x1:
        raise WorkbookValidationError(
            f"OOXML formula inventory part {part_name} is not a readable regular member"
        )
    if member.file_size <= 0 or member.file_size > maximum_bytes:
        raise WorkbookValidationError(
            f"OOXML formula inventory part {part_name} exceeds its accepted size bound"
        )
    with package.open(member) as handle:
        raw = handle.read(maximum_bytes + 1)
    if len(raw) != member.file_size or len(raw) > maximum_bytes:
        raise WorkbookValidationError(
            f"OOXML formula inventory part {part_name} does not match its ZIP metadata"
        )
    return raw


def _parse_xml(raw: bytes, *, label: str) -> ElementTree.Element:
    declaration_scan = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise WorkbookValidationError(
            f"OOXML formula inventory {label} must not contain DTD or entity declarations"
        )
    try:
        return ElementTree.fromstring(raw)
    except (ElementTree.ParseError, LookupError, ValueError) as exc:
        raise WorkbookValidationError(
            f"OOXML formula inventory {label} is malformed: {exc}"
        ) from exc


def _relationship_targets(raw: bytes) -> dict[str, tuple[str, str, str]]:
    root = _parse_xml(raw, label="workbook relationships")
    namespace = _namespace(root.tag)
    if namespace not in _PACKAGE_RELATIONSHIP_NAMESPACES:
        raise WorkbookValidationError(
            "OOXML formula inventory workbook relationships use an unsupported namespace"
        )
    relationship_tag = f"{{{namespace}}}Relationship"
    relationships: dict[str, tuple[str, str, str]] = {}
    for child in root:
        if child.tag != relationship_tag or list(child):
            raise WorkbookValidationError(
                "OOXML formula inventory workbook relationships contain an invalid child"
            )
        relationship_id = child.attrib.get("Id")
        relationship_type = child.attrib.get("Type")
        target = child.attrib.get("Target")
        target_mode = child.attrib.get("TargetMode", "Internal")
        if not all(
            isinstance(value, str) and value
            for value in (relationship_id, relationship_type, target)
        ):
            raise WorkbookValidationError(
                "OOXML formula inventory workbook relationship is incomplete"
            )
        assert isinstance(relationship_id, str)
        assert isinstance(relationship_type, str)
        assert isinstance(target, str)
        if relationship_id in relationships:
            raise WorkbookValidationError(
                "OOXML formula inventory workbook relationships contain duplicate Id values"
            )
        relationships[relationship_id] = (relationship_type, target, target_mode)
    return relationships


def _target_part(target: str) -> str:
    decoded = unquote(target)
    if not decoded or "\\" in decoded or "\x00" in decoded:
        raise WorkbookValidationError(
            "OOXML formula inventory worksheet target is not a valid package path"
        )
    if decoded.startswith("/"):
        resolved = posixpath.normpath(decoded.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join("xl", decoded))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise WorkbookValidationError(
            "OOXML formula inventory worksheet target escapes the package"
        )
    return resolved


def _worksheet_parts(
    workbook_xml: bytes,
    relationships_xml: bytes,
    inventory_sheets: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    root = _parse_xml(workbook_xml, label="workbook")
    namespace = _namespace(root.tag)
    if namespace not in _SPREADSHEET_NAMESPACES:
        raise WorkbookValidationError(
            "OOXML formula inventory workbook uses an unsupported namespace"
        )
    sheets_node = root.find(f"{{{namespace}}}sheets")
    if sheets_node is None or len(sheets_node) != len(inventory_sheets):
        raise WorkbookValidationError(
            "OOXML formula inventory sheet records do not match the validated inventory"
        )
    relationships = _relationship_targets(relationships_xml)
    relationship_attribute = (
        f"{{{_OFFICE_RELATIONSHIP_NAMESPACES[namespace]}}}id"
    )
    sheet_tag = f"{{{namespace}}}sheet"
    parts: list[tuple[str, str]] = []
    for node, sheet in zip(list(sheets_node), inventory_sheets, strict=True):
        if node.tag != sheet_tag or node.attrib.get("name") != sheet.get("name"):
            raise WorkbookValidationError(
                "OOXML formula inventory sheet names changed during extraction"
            )
        relationship_id = node.attrib.get(relationship_attribute)
        relationship = relationships.get(str(relationship_id))
        if relationship is None:
            raise WorkbookValidationError(
                "OOXML formula inventory sheet relationship is missing"
            )
        relationship_type, target, target_mode = relationship
        if sheet.get("kind") != "worksheet":
            continue
        if (
            target_mode != "Internal"
            or relationship_type not in _WORKSHEET_RELATIONSHIP_TYPES
        ):
            raise WorkbookValidationError(
                "OOXML formula inventory worksheet relationship is invalid"
            )
        parts.append((str(sheet["name"]), _target_part(target)))
    return parts


def _canonical_formula_payload(formula: ElementTree.Element) -> dict[str, Any]:
    if list(formula):
        raise WorkbookValidationError(
            "OOXML formula inventory encountered a formula with child elements"
        )
    return {
        "attributes": sorted(
            (_local_name(key), value) for key, value in formula.attrib.items()
        ),
        "text": formula.text or "",
    }


def _sheet_formula_cells(raw: bytes, *, sheet: str) -> dict[FormulaCoordinate, FormulaCellState]:
    root = _parse_xml(raw, label=f"worksheet {sheet!r}")
    namespace = _namespace(root.tag)
    if namespace not in _SPREADSHEET_NAMESPACES:
        raise WorkbookValidationError(
            "OOXML formula inventory worksheet uses an unsupported namespace"
        )
    cell_tag = f"{{{namespace}}}c"
    formula_tag = f"{{{namespace}}}f"
    value_tag = f"{{{namespace}}}v"
    raw_formulas: list[
        tuple[str, dict[str, Any], str | None, str | None]
    ] = []
    shared_masters: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for cell in root.iter(cell_tag):
        coordinate = cell.attrib.get("r")
        formulas = [child for child in cell if child.tag == formula_tag]
        coordinate_match = (
            _A1_COORDINATE.fullmatch(coordinate.upper())
            if isinstance(coordinate, str)
            else None
        )
        if not formulas:
            continue
        if (
            len(formulas) != 1
            or coordinate_match is None
        ):
            raise WorkbookValidationError(
                "OOXML formula inventory encountered an invalid formula cell"
            )
        assert isinstance(coordinate, str)
        column_letters, row_text = coordinate_match.groups()
        column_index = 0
        for character in column_letters:
            column_index = column_index * 26 + ord(character) - ord("A") + 1
        if column_index > 16_384 or int(row_text) > 1_048_576:
            raise WorkbookValidationError(
                "OOXML formula inventory encountered an out-of-bounds formula cell"
            )
        coordinate = coordinate.upper()
        if coordinate in seen:
            raise WorkbookValidationError(
                "OOXML formula inventory encountered duplicate formula coordinates"
            )
        seen.add(coordinate)
        formula = formulas[0]
        payload = _canonical_formula_payload(formula)
        attributes = dict(payload["attributes"])
        shared_index = attributes.get("si") if attributes.get("t") == "shared" else None
        if shared_index is not None and payload["text"]:
            previous = shared_masters.get(shared_index)
            if previous is not None and previous != payload:
                raise WorkbookValidationError(
                    "OOXML formula inventory encountered conflicting shared formula masters"
                )
            shared_masters[shared_index] = payload
        values = [child for child in cell if child.tag == value_tag]
        if len(values) > 1 or (values and list(values[0])):
            raise WorkbookValidationError(
                "OOXML formula inventory encountered an invalid cached value"
            )
        cached_value = values[0].text if values else None
        raw_formulas.append(
            (coordinate, payload, cell.attrib.get("t"), cached_value)
        )

    result: dict[FormulaCoordinate, FormulaCellState] = {}
    for coordinate, payload, cached_type, cached_value in raw_formulas:
        attributes = dict(payload["attributes"])
        shared_index = attributes.get("si") if attributes.get("t") == "shared" else None
        if (
            shared_index is not None
            and not payload["text"]
            and shared_index not in shared_masters
        ):
            raise WorkbookValidationError(
                "OOXML formula inventory encountered a shared formula without a master"
            )
        effective = {
            "cell_formula": payload,
            "shared_master": (
                shared_masters.get(shared_index)
                if shared_index is not None and not payload["text"]
                else None
            ),
        }
        formula_sha256 = hashlib.sha256(
            json.dumps(
                effective,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        result[(sheet, coordinate)] = FormulaCellState(
            formula_sha256=formula_sha256,
            cached_type=cached_type,
            cached_value=cached_value,
        )
    return result


def formula_coordinate_sha256(coordinates: Iterable[FormulaCoordinate]) -> str:
    normalized = sorted({(str(sheet), str(coordinate).upper()) for sheet, coordinate in coordinates})
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def formula_inventory(path: str | Path) -> FormulaInventory:
    """Return formula hashes and cached results without loading workbook objects."""

    workbook_path = Path(path).expanduser().resolve()
    identity = sheet_inventory_identity(workbook_path)
    cells: dict[FormulaCoordinate, FormulaCellState] = {}
    try:
        with zipfile.ZipFile(workbook_path) as package:
            workbook_xml = _read_unique_part(
                package,
                _WORKBOOK_PART,
                maximum_bytes=_WORKBOOK_PART_MAX_BYTES,
            )
            relationships_xml = _read_unique_part(
                package,
                _WORKBOOK_RELATIONSHIPS_PART,
                maximum_bytes=_WORKBOOK_PART_MAX_BYTES,
            )
            parts = _worksheet_parts(
                workbook_xml,
                relationships_xml,
                list(identity["sheets"]),
            )
            for sheet, part_name in parts:
                raw = _read_unique_part(
                    package,
                    part_name,
                    maximum_bytes=_WORKSHEET_PART_MAX_BYTES,
                )
                sheet_cells = _sheet_formula_cells(raw, sheet=sheet)
                duplicate = set(cells) & set(sheet_cells)
                if duplicate:
                    raise WorkbookValidationError(
                        "OOXML formula inventory contains duplicate sheet coordinates"
                    )
                cells.update(sheet_cells)
    except WorkbookValidationError:
        raise
    except (
        OSError,
        RuntimeError,
        EOFError,
        ValueError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ) as exc:
        raise WorkbookValidationError(
            f"Could not read OOXML formula inventory: {type(exc).__name__}: {exc}"
        ) from exc
    if sha256_file(workbook_path) != identity["workbook_sha256"]:
        raise WorkbookValidationError(
            "Workbook changed while its formula inventory was being read"
        )
    state_sha256 = hashlib.sha256(
        json.dumps(
            [
                [sheet, coordinate, state.formula_sha256]
                for (sheet, coordinate), state in sorted(cells.items())
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return FormulaInventory(
        workbook_sha256=str(identity["workbook_sha256"]),
        state_sha256=state_sha256,
        cells=cells,
    )


def formula_runtime_report(
    inventory: FormulaInventory,
    coordinates: Iterable[FormulaCoordinate],
) -> dict[str, Any]:
    """Report cached Calc errors for one exact sparse formula-change scope."""

    scope = tuple(sorted({(str(sheet), str(cell).upper()) for sheet, cell in coordinates}))
    errors: list[dict[str, str]] = []
    blanks: list[dict[str, str]] = []
    present = 0
    for sheet, coordinate in scope:
        state = inventory.cells.get((sheet, coordinate))
        if state is None:
            continue
        present += 1
        cached = state.cached_value
        normalized = cached.strip().upper() if isinstance(cached, str) else None
        error_typed = state.cached_type == "e"
        if (
            (error_typed and normalized in _SPREADSHEET_ERROR_VALUES)
            or (
                error_typed
                and isinstance(normalized, str)
                and normalized.startswith("#")
            )
            or (
                isinstance(cached, str)
                and _LIBREOFFICE_ERROR_VALUE.fullmatch(cached.strip()) is not None
            )
        ):
            error_text = (cached or "#UNKNOWN!").strip() or "#UNKNOWN!"
            errors.append(
                {
                    "sheet": sheet,
                    "coordinate": coordinate,
                    "error": error_text[:_ERROR_TEXT_LIMIT],
                }
            )
        elif cached in {None, ""}:
            blanks.append({"sheet": sheet, "coordinate": coordinate})
    sampled_errors = errors[:_EVIDENCE_SAMPLE_LIMIT]
    sampled_blanks = blanks[:_EVIDENCE_SAMPLE_LIMIT]
    return {
        "kind": "pending_formula_changes",
        "coordinate_count": len(scope),
        "coordinate_sha256": formula_coordinate_sha256(scope),
        "coverage_complete": True,
        "formula_cells_present": present,
        "formula_cells_absent": len(scope) - present,
        "cached_blank_count": len(blanks),
        "cached_blank_coordinates": sampled_blanks,
        "cached_blank_coordinates_truncated": len(sampled_blanks) < len(blanks),
        "calculation_errors": {
            "count": len(errors),
            "coordinates": sampled_errors,
            "coordinate_limit": _EVIDENCE_SAMPLE_LIMIT,
            "coordinates_truncated": len(sampled_errors) < len(errors),
        },
    }
