"""Independent OOXML semantic-diff oracle used only by tests.

This module intentionally uses only the Python standard library.  In particular,
it does not import openpyxl, ``workbook_diff``, or evidence-contract types.  The
implementation therefore gives tests a parser and representation that do not
share the production comparator's failure modes.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_DCTERMS = "http://purl.org/dc/terms/"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"
_LIBREOFFICE = "http://schemas.libreoffice.org/"
_LIBREOFFICE_CALC_A1_URI = "{7626C862-2A13-11E5-B345-FEFF819CDC9F}"
_CUSTOM_PROPERTIES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
)
_CUSTOM_PROPERTIES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.custom-properties+xml"
)
_CUSTOM_PROPERTIES_PART = "docProps/custom.xml"
_CUSTOM_PROPERTIES_RELATIONSHIP_TYPES = frozenset(
    {
        f"{_DOC_REL}/custom-properties",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/custom-properties",
    }
)
_ROOT_RELATIONSHIPS_PART = "_rels/.rels"
_PKG_CORE_REL = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
)

_R_ID = f"{{{_DOC_REL}}}id"
_CELL_REF = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_MAX_PARTS = 10_000
_MAX_PART_SIZE = 64 * 1024 * 1024
_MAX_PACKAGE_SIZE = 256 * 1024 * 1024
_SUPPORTED_WORKSHEET_CHILDREN = frozenset(
    {
        "sheetPr",
        "dimension",
        "sheetViews",
        "sheetFormatPr",
        "cols",
        "sheetData",
        "sheetProtection",
        "autoFilter",
        "sortState",
        "mergeCells",
        "phoneticPr",
        "conditionalFormatting",
        "dataValidations",
        "hyperlinks",
        "printOptions",
        "pageMargins",
        "pageSetup",
        "headerFooter",
        "rowBreaks",
        "colBreaks",
        "drawing",
        "tableParts",
    }
)


class _UnsupportedOOXML(ValueError):
    """The oracle cannot interpret the package without risking a false negative."""


@dataclass(frozen=True, order=True)
class OracleCell:
    sheet: str
    coordinate: str


@dataclass(frozen=True)
class OracleDiff:
    """A deliberately small, production-independent semantic footprint."""

    semantic_changed: bool
    complete: bool
    effects: frozenset[str]
    cells: frozenset[OracleCell]
    formula_cells: frozenset[OracleCell]
    sheets: frozenset[str]
    workbook_scope: bool
    reasons: tuple[str, ...] = ()

    @classmethod
    def unknown(cls, reason: str) -> OracleDiff:
        return cls(
            semantic_changed=True,
            complete=False,
            effects=frozenset({"unknown"}),
            cells=frozenset(),
            formula_cells=frozenset(),
            sheets=frozenset(),
            workbook_scope=True,
            reasons=(reason,),
        )


@dataclass(frozen=True)
class _Relationship:
    identifier: str
    kind: str
    target: str
    external: bool


@dataclass(frozen=True)
class _Cell:
    value: Any
    formula: Any
    cached_formula_value: Any
    style: Any
    residual: Any
    shared_formula_index: str | None
    shared_formula_master: bool
    array_formula_ref: str | None


@dataclass(frozen=True)
class _Table:
    signature: Any
    parts: frozenset[str]


@dataclass(frozen=True)
class _Drawing:
    signature: Any
    parts: frozenset[str]


@dataclass(frozen=True)
class _Sheet:
    name: str
    state: str
    part: str
    relationships_part: str | None
    cells: dict[str, _Cell]
    merges: frozenset[str]
    tables: tuple[_Table, ...]
    drawings: tuple[_Drawing, ...]
    residual: Any
    relationship_residual: Any

    @property
    def all_parts(self) -> frozenset[str]:
        parts = {self.part}
        if self.relationships_part is not None:
            parts.add(self.relationships_part)
        for table in self.tables:
            parts.update(table.parts)
        for drawing in self.drawings:
            parts.update(drawing.parts)
        return frozenset(parts)


@dataclass(frozen=True)
class _Styles:
    cell_xfs: tuple[Any, ...]
    resources: frozenset[Any]
    residual: Any


@dataclass(frozen=True)
class _Snapshot:
    parts: dict[str, bytes]
    part_signatures: dict[str, Any]
    recognized_parts: frozenset[str]
    workbook_part: str
    workbook_relationships_part: str
    workbook_relationship_residual: Any
    root_relationship_residual: Any
    sheet_order: tuple[str, ...]
    sheets: dict[str, _Sheet]
    defined_names: Any
    workbook_residual: Any
    styles_part: str | None
    styles: _Styles
    theme_part: str | None
    theme: Any
    shared_strings_part: str | None
    shared_strings: tuple[Any, ...]
    content_types: tuple[tuple[str, str, str], ...]
    auxiliary_signatures: dict[str, Any]
    empty_custom_properties_part: str | None


def _qname(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _parse_xml(payload: bytes, *, part_name: str) -> ET.Element:
    upper = payload[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise _UnsupportedOOXML(f"unsafe XML declaration in {part_name}")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise _UnsupportedOOXML(f"malformed XML part: {part_name}") from exc


def _semantic_text(value: str | None) -> str | None:
    if value is not None and not value.strip():
        return None
    return value


def _canonical_text(element: ET.Element) -> str | None:
    # Whitespace in leaf nodes such as SpreadsheetML ``t`` is data; whitespace
    # around child elements is only serialization indentation.
    return element.text if not len(element) else _semantic_text(element.text)


def _canonical_element(element: ET.Element) -> tuple[Any, ...]:
    return (
        element.tag,
        tuple(sorted((name, value) for name, value in element.attrib.items())),
        _canonical_text(element),
        _semantic_text(element.tail),
        tuple(_canonical_element(child) for child in element),
    )


def _canonical_core(element: ET.Element) -> tuple[Any, ...]:
    if element.tag == _qname(_DCTERMS, "modified"):
        return (
            element.tag,
            tuple(sorted((name, value) for name, value in element.attrib.items())),
            None,
            _semantic_text(element.tail),
            tuple(_canonical_core(child) for child in element),
        )
    return (
        element.tag,
        tuple(sorted((name, value) for name, value in element.attrib.items())),
        _canonical_text(element),
        _semantic_text(element.tail),
        tuple(_canonical_core(child) for child in element),
    )


def _is_exact_calc_a1_extension(element: ET.Element) -> bool:
    if (
        element.tag != _qname(_MAIN, "extLst")
        or element.attrib
        or _semantic_text(element.text) is not None
        or _semantic_text(element.tail) is not None
        or len(element) != 1
    ):
        return False
    extension = element[0]
    if (
        extension.tag != _qname(_MAIN, "ext")
        or extension.attrib != {"uri": _LIBREOFFICE_CALC_A1_URI}
        or _semantic_text(extension.text) is not None
        or _semantic_text(extension.tail) is not None
        or len(extension) != 1
    ):
        return False
    marker = extension[0]
    return bool(
        marker.tag == _qname(_LIBREOFFICE, "extCalcPr")
        and marker.attrib == {"stringRefSyntax": "ExcelA1"}
        and _semantic_text(marker.text) is None
        and _semantic_text(marker.tail) is None
        and not len(marker)
    )


def _validate_workbook_extensions(root: ET.Element, *, part_name: str) -> frozenset[int]:
    allowed_non_main: set[int] = set()
    exact_extension_count = 0
    for element in root.iter():
        if element.tag == _qname(_MC, "AlternateContent"):
            raise _UnsupportedOOXML(f"potentially lossy extension markup in {part_name}")
        if _local_name(element.tag) != "extLst":
            continue
        if element not in root or not _is_exact_calc_a1_extension(element):
            raise _UnsupportedOOXML(f"potentially lossy extension markup in {part_name}")
        exact_extension_count += 1
        if exact_extension_count > 1:
            raise _UnsupportedOOXML(f"ambiguous calculation extension in {part_name}")
        allowed_non_main.add(id(element[0][0]))
    return frozenset(allowed_non_main)


def _canonical_workbook_payload(element: ET.Element) -> tuple[Any, ...]:
    if element.tag == _qname(_MAIN, "calcPr"):
        attributes = dict(element.attrib)
        if attributes.get("calcMode") in {None, "auto"}:
            attributes["calcMode"] = "auto"
        for name in ("fullCalcOnLoad", "forceFullCalc"):
            if attributes.get(name) in {None, "1", "true"}:
                attributes[name] = "1"
        return (
            element.tag,
            tuple(sorted(attributes.items())),
            _canonical_text(element),
            _semantic_text(element.tail),
            tuple(
                _canonical_workbook_payload(child)
                for child in element
                if not _is_exact_calc_a1_extension(child)
            ),
        )
    return (
        element.tag,
        tuple(sorted((name, value) for name, value in element.attrib.items())),
        _canonical_text(element),
        _semantic_text(element.tail),
        tuple(
            _canonical_workbook_payload(child)
            for child in element
            if not _is_exact_calc_a1_extension(child)
        ),
    )


def _rels_signature(root: ET.Element) -> tuple[Any, ...]:
    values = []
    for child in root:
        if child.tag != _qname(_PKG_REL, "Relationship"):
            raise _UnsupportedOOXML("unexpected relationships element")
        values.append(_canonical_element(child))
    return (
        root.tag,
        tuple(sorted(root.attrib.items())),
        _semantic_text(root.text),
        _semantic_text(root.tail),
        tuple(sorted(values, key=repr)),
    )


def _content_types_signature(root: ET.Element) -> tuple[Any, ...]:
    values = []
    for child in root:
        if child.tag not in {
            _qname(_CONTENT_TYPES, "Default"),
            _qname(_CONTENT_TYPES, "Override"),
        }:
            raise _UnsupportedOOXML("unexpected content-types element")
        values.append(_canonical_element(child))
    return (
        root.tag,
        tuple(sorted(root.attrib.items())),
        _semantic_text(root.text),
        _semantic_text(root.tail),
        tuple(sorted(values, key=repr)),
    )


def _payload_signature(part_name: str, payload: bytes) -> Any:
    if not part_name.endswith((".xml", ".rels", ".vml")):
        return hashlib.sha256(payload).hexdigest()
    root = _parse_xml(payload, part_name=part_name)
    if part_name == "docProps/core.xml":
        return _canonical_core(root)
    if part_name == "[Content_Types].xml":
        return _content_types_signature(root)
    if part_name.endswith(".rels"):
        return _rels_signature(root)
    if part_name == "xl/workbook.xml":
        return _canonical_workbook_payload(root)
    return _canonical_element(root)


def _read_package(path: Path) -> dict[str, bytes]:
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_PARTS:
                raise _UnsupportedOOXML("OOXML package has too many ZIP entries")
            names = [info.filename for info in infos if not info.is_dir()]
            if len(names) != len(set(names)):
                raise _UnsupportedOOXML("duplicate OOXML part names are ambiguous")
            total_size = 0
            parts: dict[str, bytes] = {}
            for info in infos:
                if info.is_dir():
                    continue
                name = info.filename
                if not name or name.startswith("/") or "\\" in name or ".." in name.split("/"):
                    raise _UnsupportedOOXML("unsafe OOXML part name")
                if info.flag_bits & 0x1:
                    raise _UnsupportedOOXML("encrypted OOXML ZIP entries are unsupported")
                if info.file_size > _MAX_PART_SIZE:
                    raise _UnsupportedOOXML("OOXML part exceeds oracle size limit")
                total_size += info.file_size
                if total_size > _MAX_PACKAGE_SIZE:
                    raise _UnsupportedOOXML("OOXML package exceeds oracle size limit")
                parts[name] = archive.read(info)
            return parts
    except BadZipFile as exc:
        raise _UnsupportedOOXML("invalid OOXML ZIP package") from exc


def _relationships_part(source_part: str) -> str:
    if not source_part:
        return "_rels/.rels"
    directory, filename = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _resolve_target(source_part: str, raw_target: str) -> str:
    if not raw_target or "\\" in raw_target:
        raise _UnsupportedOOXML("invalid OOXML relationship target")
    if raw_target.startswith("/"):
        normalized = posixpath.normpath(raw_target.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), raw_target))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise _UnsupportedOOXML("OOXML relationship escapes the package")
    return normalized


def _parse_relationships(
    parts: dict[str, bytes], source_part: str
) -> tuple[dict[str, _Relationship], str | None]:
    relationships_part = _relationships_part(source_part)
    payload = parts.get(relationships_part)
    if payload is None:
        return {}, None
    root = _parse_xml(payload, part_name=relationships_part)
    if root.tag != _qname(_PKG_REL, "Relationships"):
        raise _UnsupportedOOXML(f"invalid relationships root: {relationships_part}")
    if root.attrib or _semantic_text(root.text) is not None:
        raise _UnsupportedOOXML(f"unsupported relationships root content: {relationships_part}")
    relationships: dict[str, _Relationship] = {}
    for child in root:
        if child.tag != _qname(_PKG_REL, "Relationship"):
            raise _UnsupportedOOXML(f"unexpected relationship node: {relationships_part}")
        if (
            set(child.attrib) - {"Id", "Type", "Target", "TargetMode"}
            or len(child)
            or _semantic_text(child.text) is not None
            or _semantic_text(child.tail) is not None
        ):
            raise _UnsupportedOOXML(f"unsupported relationship content: {relationships_part}")
        identifier = child.attrib.get("Id", "")
        relation_type = child.attrib.get("Type", "")
        raw_target = child.attrib.get("Target", "")
        if not identifier or identifier in relationships or not relation_type:
            raise _UnsupportedOOXML(f"ambiguous relationship entry: {relationships_part}")
        target_mode = child.attrib.get("TargetMode")
        if target_mode not in {None, "Internal", "External"}:
            raise _UnsupportedOOXML(f"invalid relationship target mode: {relationships_part}")
        external = target_mode == "External"
        target = raw_target if external else _resolve_target(source_part, raw_target)
        if relation_type == _PKG_CORE_REL:
            kind = "core-properties"
        elif relation_type in _CUSTOM_PROPERTIES_RELATIONSHIP_TYPES:
            kind = "custom-properties"
        elif relation_type.startswith(f"{_DOC_REL}/"):
            kind = relation_type[len(_DOC_REL) + 1 :]
        else:
            kind = f"unsupported:{relation_type}"
        relationships[identifier] = _Relationship(
            identifier=identifier,
            kind=kind,
            target=target,
            external=external,
        )
    return relationships, relationships_part


def _relationship(
    relationships: dict[str, _Relationship],
    identifier: str | None,
    *,
    expected_kind: str,
    parts: dict[str, bytes],
) -> _Relationship:
    if identifier is None or identifier not in relationships:
        raise _UnsupportedOOXML(f"missing {expected_kind} relationship")
    relationship = relationships[identifier]
    if relationship.kind != expected_kind or relationship.external:
        raise _UnsupportedOOXML(f"invalid {expected_kind} relationship")
    if relationship.target not in parts:
        raise _UnsupportedOOXML(f"missing relationship target: {relationship.target}")
    return relationship


def _relationship_residual(
    relationships: dict[str, _Relationship], consumed: set[str]
) -> tuple[Any, ...]:
    return tuple(
        sorted(
            (
                relationship.identifier,
                relationship.kind,
                relationship.target,
                relationship.external,
            )
            for identifier, relationship in relationships.items()
            if identifier not in consumed
        )
    )


def _assert_no_lossy_extensions(element: ET.Element, *, part_name: str) -> None:
    for descendant in element.iter():
        if _local_name(descendant.tag) == "extLst" or descendant.tag == _qname(
            _MC, "AlternateContent"
        ):
            raise _UnsupportedOOXML(f"potentially lossy extension markup in {part_name}")


def _direct_child(element: ET.Element, local_name: str) -> ET.Element | None:
    expected = _qname(_MAIN, local_name)
    return next((child for child in element if child.tag == expected), None)


def _children(element: ET.Element | None, local_name: str) -> tuple[ET.Element, ...]:
    if element is None:
        return ()
    expected = _qname(_MAIN, local_name)
    return tuple(child for child in element if child.tag == expected)


def _collection(root: ET.Element, local_name: str) -> tuple[ET.Element, ...]:
    element = _direct_child(root, local_name)
    return tuple(element) if element is not None else ()


def _indexed(items: tuple[ET.Element, ...], raw_index: str, *, context: str) -> ET.Element:
    try:
        index = int(raw_index)
        return items[index]
    except (ValueError, IndexError) as exc:
        raise _UnsupportedOOXML(f"invalid {context} index") from exc


def _parse_styles(parts: dict[str, bytes], part_name: str | None) -> _Styles:
    if part_name is None:
        return _Styles(cell_xfs=(None,), resources=frozenset(), residual=None)
    root = _parse_xml(parts[part_name], part_name=part_name)
    if root.tag != _qname(_MAIN, "styleSheet"):
        raise _UnsupportedOOXML("invalid styles root")
    if any(_namespace(element.tag) != _MAIN for element in root.iter()):
        raise _UnsupportedOOXML("unsupported styles XML namespace")
    _assert_no_lossy_extensions(root, part_name=part_name)
    num_formats: dict[str, str] = {}
    for item in _collection(root, "numFmts"):
        identifier = item.attrib.get("numFmtId", "")
        format_code = item.attrib.get("formatCode", "")
        if not identifier or not format_code or identifier in num_formats:
            raise _UnsupportedOOXML("ambiguous custom number format")
        num_formats[identifier] = format_code
    fonts = _collection(root, "fonts")
    fills = _collection(root, "fills")
    borders = _collection(root, "borders")
    base_xfs = _collection(root, "cellStyleXfs")

    def xf_signature(element: ET.Element) -> tuple[Any, ...]:
        attributes = dict(element.attrib)
        resolved: list[tuple[str, Any]] = []
        for name, collection in (("fontId", fonts), ("fillId", fills), ("borderId", borders)):
            raw_index = attributes.pop(name, "0")
            resource = _indexed(collection, raw_index, context=name)
            resolved.append((name, _canonical_element(resource)))
        num_format_id = attributes.pop("numFmtId", "0")
        resolved.append(
            (
                "numFmt",
                ("custom", num_formats[num_format_id])
                if num_format_id in num_formats
                else ("builtin", num_format_id),
            )
        )
        base_id = attributes.pop("xfId", None)
        if base_id is not None:
            base = _indexed(base_xfs, base_id, context="xfId")
            resolved.append(("baseXf", _canonical_element(base)))
        return (
            tuple(sorted(attributes.items())),
            tuple(resolved),
            tuple(_canonical_element(child) for child in element),
        )

    cell_xfs = tuple(xf_signature(item) for item in _collection(root, "cellXfs"))
    if not cell_xfs:
        raise _UnsupportedOOXML("styles part has no cellXfs")
    resources = {
        (kind, _canonical_element(item))
        for kind, collection in (
            ("fontId", fonts),
            ("fillId", fills),
            ("borderId", borders),
            ("baseXf", base_xfs),
        )
        for item in collection
    }
    resources.update(("numFmt", ("custom", code)) for code in num_formats.values())
    handled = {
        _qname(_MAIN, name)
        for name in ("numFmts", "fonts", "fills", "borders", "cellStyleXfs", "cellXfs")
    }
    residual = (
        tuple(sorted(root.attrib.items())),
        tuple(_canonical_element(child) for child in root if child.tag not in handled),
    )
    return _Styles(cell_xfs=cell_xfs, resources=frozenset(resources), residual=residual)


def _parse_shared_strings(parts: dict[str, bytes], part_name: str | None) -> tuple[Any, ...]:
    if part_name is None:
        return ()
    root = _parse_xml(parts[part_name], part_name=part_name)
    if root.tag != _qname(_MAIN, "sst"):
        raise _UnsupportedOOXML("invalid shared strings root")
    if any(_namespace(element.tag) != _MAIN for element in root.iter()):
        raise _UnsupportedOOXML("unsupported shared-strings XML namespace")
    _assert_no_lossy_extensions(root, part_name=part_name)
    if any(child.tag != _qname(_MAIN, "si") for child in root):
        raise _UnsupportedOOXML("unsupported shared-strings child")
    return tuple(_canonical_element(item) for item in _children(root, "si"))


def _normalize_number(text: str | None) -> Any:
    if text is None or text == "":
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite():
        return text
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _coordinate_position(coordinate: str) -> tuple[int, int]:
    letters = coordinate.rstrip("0123456789")
    row_number = int(coordinate[len(letters) :])
    column_number = 0
    for character in letters:
        column_number = column_number * 26 + ord(character) - ord("A") + 1
    return row_number, column_number


def _column_letters(column: int) -> str:
    value = column
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _expected_dimension(
    cells: dict[str, _Cell], merges: frozenset[str] = frozenset()
) -> frozenset[str]:
    coordinates = list(cells)
    for merge in merges:
        endpoints = merge.replace("$", "").upper().split(":")
        if len(endpoints) not in {1, 2} or any(
            not _CELL_REF.fullmatch(endpoint) for endpoint in endpoints
        ):
            raise _UnsupportedOOXML(f"invalid merge range: {merge}")
        coordinates.extend(endpoints)
    if not coordinates:
        return frozenset({"A1", "A1:A1"})
    positions = [_coordinate_position(coordinate) for coordinate in coordinates]
    min_row = min(row for row, _column in positions)
    max_row = max(row for row, _column in positions)
    min_column = min(column for _row, column in positions)
    max_column = max(column for _row, column in positions)
    start = f"{_column_letters(min_column)}{min_row}"
    end = f"{_column_letters(max_column)}{max_row}"
    return frozenset({start, f"{start}:{end}"}) if start == end else frozenset({f"{start}:{end}"})


def _parse_cell(
    element: ET.Element,
    *,
    styles: _Styles,
    shared_strings: tuple[Any, ...],
) -> tuple[str, _Cell]:
    coordinate = element.attrib.get("r", "")
    if not _CELL_REF.fullmatch(coordinate):
        raise _UnsupportedOOXML(f"invalid cell reference: {coordinate!r}")
    if _semantic_text(element.text) is not None or _semantic_text(element.tail) is not None:
        raise _UnsupportedOOXML(f"unsupported mixed cell content at {coordinate}")
    row_number, column_number = _coordinate_position(coordinate)
    if row_number > 1_048_576 or column_number > 16_384:
        raise _UnsupportedOOXML(f"cell reference exceeds worksheet bounds: {coordinate}")
    style_index = element.attrib.get("s", "0")
    try:
        parsed_style_index = int(style_index)
        if parsed_style_index < 0:
            raise ValueError
        style = styles.cell_xfs[parsed_style_index]
    except (ValueError, IndexError) as exc:
        raise _UnsupportedOOXML(f"invalid cell style index at {coordinate}") from exc
    cell_type = element.attrib.get("t", "n")
    if cell_type not in {"n", "s", "str", "inlineStr", "b", "e", "d"}:
        raise _UnsupportedOOXML(f"unsupported cell type at {coordinate}: {cell_type}")
    allowed_children = {
        _qname(_MAIN, "f"),
        _qname(_MAIN, "v"),
        _qname(_MAIN, "is"),
    }
    if any(child.tag not in allowed_children for child in element):
        raise _UnsupportedOOXML(f"unsupported cell child at {coordinate}")
    formula_nodes = [child for child in element if child.tag == _qname(_MAIN, "f")]
    value_nodes = [child for child in element if child.tag == _qname(_MAIN, "v")]
    inline_nodes = [child for child in element if child.tag == _qname(_MAIN, "is")]
    if max(len(formula_nodes), len(value_nodes), len(inline_nodes)) > 1:
        raise _UnsupportedOOXML(f"duplicate cell payload at {coordinate}")
    formula_node = formula_nodes[0] if formula_nodes else None
    value_node = value_nodes[0] if value_nodes else None
    inline_node = inline_nodes[0] if inline_nodes else None
    if value_node is not None and (
        value_node.attrib or len(value_node) or _semantic_text(value_node.tail) is not None
    ):
        raise _UnsupportedOOXML(f"unsupported value-node content at {coordinate}")
    if (inline_node is not None) != (cell_type == "inlineStr") or (
        inline_node is not None and (formula_node is not None or value_node is not None)
    ):
        raise _UnsupportedOOXML(f"inconsistent inline string at {coordinate}")
    formula = _canonical_element(formula_node) if formula_node is not None else None
    shared_formula_index: str | None = None
    shared_formula_master = False
    if formula_node is not None and formula_node.attrib.get("t") == "shared":
        shared_formula_index = formula_node.attrib.get("si")
        if not shared_formula_index:
            raise _UnsupportedOOXML(f"shared formula has no index at {coordinate}")
        shared_formula_master = bool(formula_node.text)
        if shared_formula_master != bool(formula_node.attrib.get("ref")):
            raise _UnsupportedOOXML(f"ambiguous shared formula master at {coordinate}")
    array_formula_ref: str | None = None
    if formula_node is not None and formula_node.attrib.get("t") == "array":
        array_formula_ref = formula_node.attrib.get("ref")
        if not array_formula_ref or not formula_node.text:
            raise _UnsupportedOOXML(f"ambiguous array formula at {coordinate}")
    cached_formula_value = (
        (cell_type, value_node.text if value_node is not None else None)
        if formula is not None
        else None
    )
    if formula is not None:
        value: Any = None
    elif cell_type == "s":
        try:
            value = ("shared", shared_strings[int(value_node.text or "")])
        except (ValueError, IndexError, AttributeError) as exc:
            raise _UnsupportedOOXML(f"invalid shared string at {coordinate}") from exc
    elif cell_type == "inlineStr":
        if inline_node is None:
            raise _UnsupportedOOXML(f"missing inline string at {coordinate}")
        value = ("inline", _canonical_element(inline_node))
    elif cell_type == "n":
        value = ("number", _normalize_number(value_node.text if value_node is not None else None))
    else:
        value = (cell_type, value_node.text if value_node is not None else None)

    residual_attributes = tuple(
        sorted((name, raw) for name, raw in element.attrib.items() if name not in {"r", "s", "t"})
    )
    handled_children = {_qname(_MAIN, "f"), _qname(_MAIN, "is")}
    if formula is None:
        handled_children.add(_qname(_MAIN, "v"))
    residual_children = tuple(
        _canonical_element(child) for child in element if child.tag not in handled_children
    )
    residual = (residual_attributes, residual_children)
    return coordinate, _Cell(
        value,
        formula,
        cached_formula_value,
        style,
        residual,
        shared_formula_index,
        shared_formula_master,
        array_formula_ref,
    )


def _bounded_range_coordinates(range_ref: str) -> frozenset[str]:
    endpoints = range_ref.replace("$", "").upper().split(":")
    if len(endpoints) not in {1, 2} or any(
        not _CELL_REF.fullmatch(endpoint) for endpoint in endpoints
    ):
        raise _UnsupportedOOXML(f"invalid shared-formula range: {range_ref}")
    start_row, start_column = _coordinate_position(endpoints[0])
    end_row, end_column = _coordinate_position(endpoints[-1])
    if start_row > end_row or start_column > end_column:
        raise _UnsupportedOOXML(f"reversed shared-formula range: {range_ref}")
    count = (end_row - start_row + 1) * (end_column - start_column + 1)
    if count > 250_000:
        raise _UnsupportedOOXML("shared-formula range exceeds oracle limit")
    return frozenset(
        f"{_column_letters(column)}{row}"
        for row in range(start_row, end_row + 1)
        for column in range(start_column, end_column + 1)
    )


def _resolve_shared_formulas(cells: dict[str, _Cell]) -> None:
    groups: dict[str, list[tuple[str, _Cell]]] = {}
    for coordinate, cell in cells.items():
        if cell.shared_formula_index is not None:
            groups.setdefault(cell.shared_formula_index, []).append((coordinate, cell))
    for index, members in groups.items():
        masters = [(coordinate, cell) for coordinate, cell in members if cell.shared_formula_master]
        if len(masters) != 1:
            raise _UnsupportedOOXML(f"shared formula {index} must have exactly one master")
        master_coordinate, master = masters[0]
        assert master.formula is not None
        formula_attributes = dict(master.formula[1])
        range_ref = formula_attributes.get("ref", "")
        member_coordinates = frozenset(coordinate for coordinate, _cell in members)
        if member_coordinates != _bounded_range_coordinates(range_ref):
            raise _UnsupportedOOXML(
                f"shared formula {index} members do not match master range at {master_coordinate}"
            )
        master_signature = master.formula
        for coordinate, cell in members:
            cells[coordinate] = replace(
                cell,
                formula=("shared-effective", master_signature, cell.formula),
            )


def _resolve_array_formulas(cells: dict[str, _Cell], *, default_style: Any) -> None:
    occupied: set[str] = set()
    masters = [
        (coordinate, cell)
        for coordinate, cell in tuple(cells.items())
        if cell.array_formula_ref is not None
    ]
    for master_coordinate, master in masters:
        assert master.formula is not None and master.array_formula_ref is not None
        members = _bounded_range_coordinates(master.array_formula_ref)
        if master_coordinate not in members or occupied & members:
            raise _UnsupportedOOXML(f"invalid or overlapping array formula at {master_coordinate}")
        occupied.update(members)
        for coordinate in members:
            cell = cells.get(coordinate)
            if cell is None:
                cell = _Cell(
                    value=None,
                    formula=None,
                    cached_formula_value=None,
                    style=default_style,
                    residual=((), ()),
                    shared_formula_index=None,
                    shared_formula_master=False,
                    array_formula_ref=None,
                )
            elif coordinate != master_coordinate and cell.formula is not None:
                raise _UnsupportedOOXML(
                    f"array formula member contains another formula at {coordinate}"
                )
            cached_value = cell.cached_formula_value
            if coordinate != master_coordinate and cell.value is not None:
                cached_value = cell.value
            cells[coordinate] = replace(
                cell,
                value=None,
                formula=("array-effective", master_coordinate, master.formula, coordinate),
                cached_formula_value=cached_value,
            )


def _sheet_data_residual(sheet_data: ET.Element) -> tuple[Any, ...]:
    residual_rows: list[Any] = []
    for row in sheet_data:
        if row.tag != _qname(_MAIN, "row"):
            residual_rows.append(_canonical_element(row))
            continue
        row_reference = row.attrib.get("r", "")
        row_attributes = tuple(
            sorted((name, value) for name, value in row.attrib.items() if name != "r")
        )
        residual_cells = []
        for cell in row:
            if cell.tag != _qname(_MAIN, "c"):
                residual_cells.append(_canonical_element(cell))
                continue
            attributes = tuple(
                sorted(
                    (name, value)
                    for name, value in cell.attrib.items()
                    if name not in {"r", "s", "t"}
                )
            )
            formula_present = any(child.tag == _qname(_MAIN, "f") for child in cell)
            children = tuple(
                _canonical_element(child)
                for child in cell
                if child.tag not in {_qname(_MAIN, "f"), _qname(_MAIN, "is")}
                and not (child.tag == _qname(_MAIN, "v") and not formula_present)
            )
            if attributes or children:
                residual_cells.append((cell.attrib.get("r", ""), attributes, children))
        if row_attributes or residual_cells:
            residual_rows.append((row_reference, row_attributes, tuple(residual_cells)))
    return tuple(residual_rows)


def _worksheet_residual(root: ET.Element) -> tuple[Any, ...]:
    handled = {
        _qname(_MAIN, "dimension"),
        _qname(_MAIN, "mergeCells"),
        _qname(_MAIN, "tableParts"),
        _qname(_MAIN, "drawing"),
    }
    children = []
    for child in root:
        if child.tag in handled:
            continue
        if child.tag == _qname(_MAIN, "sheetData"):
            residual = _sheet_data_residual(child)
            if residual:
                children.append(("sheetDataResidual", residual))
            continue
        children.append(_canonical_element(child))
    return (tuple(sorted(root.attrib.items())), tuple(children))


def _parse_table(parts: dict[str, bytes], part_name: str) -> _Table:
    root = _parse_xml(parts[part_name], part_name=part_name)
    if root.tag != _qname(_MAIN, "table"):
        raise _UnsupportedOOXML(f"invalid table root: {part_name}")
    if any(_namespace(element.tag) != _MAIN for element in root.iter()):
        raise _UnsupportedOOXML(f"unsupported table XML namespace: {part_name}")
    _assert_no_lossy_extensions(root, part_name=part_name)
    return _Table(_canonical_element(root), frozenset({part_name}))


def _related_payload_signature(
    parts: dict[str, bytes], relationship: _Relationship, *, consumed: set[str]
) -> tuple[Any, frozenset[str]]:
    consumed.add(relationship.identifier)
    if relationship.external:
        raise _UnsupportedOOXML("external drawing relationships are unsupported")
    if relationship.target not in parts:
        raise _UnsupportedOOXML(f"missing drawing target: {relationship.target}")
    if relationship.kind == "chart":
        root = _parse_xml(parts[relationship.target], part_name=relationship.target)
        if root.tag != _qname(_CHART, "chartSpace"):
            raise _UnsupportedOOXML(f"invalid chart root: {relationship.target}")
        if any(_namespace(element.tag) not in {_CHART, _DRAWINGML} for element in root.iter()):
            raise _UnsupportedOOXML(f"unsupported chart XML namespace: {relationship.target}")
        _assert_no_lossy_extensions(root, part_name=relationship.target)
        signature: Any = _canonical_element(root)
    elif relationship.kind == "image":
        signature = hashlib.sha256(parts[relationship.target]).hexdigest()
    else:
        raise _UnsupportedOOXML(f"unsupported drawing relationship: {relationship.kind}")
    return (relationship.kind, signature), frozenset({relationship.target})


def _expand_drawing_relationships(
    element: ET.Element,
    *,
    parts: dict[str, bytes],
    relationships: dict[str, _Relationship],
    consumed: set[str],
    related_parts: set[str],
) -> tuple[Any, ...]:
    attributes: list[tuple[str, Any]] = []
    for name, value in element.attrib.items():
        if name != _R_ID:
            attributes.append((name, value))
            continue
        relationship = relationships.get(value)
        if relationship is None:
            raise _UnsupportedOOXML("drawing references a missing relationship")
        signature, target_parts = _related_payload_signature(parts, relationship, consumed=consumed)
        related_parts.update(target_parts)
        attributes.append((name, signature))
    return (
        element.tag,
        tuple(sorted(attributes, key=lambda item: item[0])),
        _canonical_text(element),
        _semantic_text(element.tail),
        tuple(
            _expand_drawing_relationships(
                child,
                parts=parts,
                relationships=relationships,
                consumed=consumed,
                related_parts=related_parts,
            )
            for child in element
        ),
    )


def _parse_drawing(parts: dict[str, bytes], part_name: str) -> _Drawing:
    root = _parse_xml(parts[part_name], part_name=part_name)
    if root.tag != _qname(_XDR, "wsDr"):
        raise _UnsupportedOOXML(f"invalid spreadsheet drawing root: {part_name}")
    if any(_namespace(element.tag) not in {_XDR, _DRAWINGML, _CHART} for element in root.iter()):
        raise _UnsupportedOOXML(f"unsupported drawing XML namespace: {part_name}")
    _assert_no_lossy_extensions(root, part_name=part_name)
    relationships, relationships_part = _parse_relationships(parts, part_name)
    consumed: set[str] = set()
    related_parts: set[str] = {part_name}
    signature = _expand_drawing_relationships(
        root,
        parts=parts,
        relationships=relationships,
        consumed=consumed,
        related_parts=related_parts,
    )
    residual = _relationship_residual(relationships, consumed)
    if residual:
        raise _UnsupportedOOXML(f"unaccounted drawing relationships in {part_name}")
    if relationships_part is not None:
        related_parts.add(relationships_part)
    return _Drawing(signature, frozenset(related_parts))


def _parse_sheet(
    parts: dict[str, bytes],
    *,
    name: str,
    state: str,
    part_name: str,
    styles: _Styles,
    shared_strings: tuple[Any, ...],
) -> _Sheet:
    root = _parse_xml(parts[part_name], part_name=part_name)
    if root.tag != _qname(_MAIN, "worksheet"):
        raise _UnsupportedOOXML(f"invalid worksheet root: {part_name}")
    if any(_namespace(element.tag) != _MAIN for element in root.iter()):
        raise _UnsupportedOOXML(f"unsupported worksheet XML namespace: {part_name}")
    _assert_no_lossy_extensions(root, part_name=part_name)
    unknown_children = {
        _local_name(element.tag)
        for element in root
        if _local_name(element.tag) not in _SUPPORTED_WORKSHEET_CHILDREN
    }
    if unknown_children:
        raise _UnsupportedOOXML(
            f"unsupported worksheet element in {part_name}: {sorted(unknown_children)[0]}"
        )
    cells: dict[str, _Cell] = {}
    sheet_data = _direct_child(root, "sheetData")
    if sheet_data is not None:
        if sheet_data.attrib or _semantic_text(sheet_data.text) is not None:
            raise _UnsupportedOOXML(f"unsupported sheetData content: {part_name}")
        for row in sheet_data:
            if row.tag != _qname(_MAIN, "row"):
                raise _UnsupportedOOXML(f"unsupported sheetData child: {part_name}")
            for element in row:
                if element.tag != _qname(_MAIN, "c"):
                    raise _UnsupportedOOXML(f"unsupported row child: {part_name}")
                coordinate, cell = _parse_cell(
                    element, styles=styles, shared_strings=shared_strings
                )
                if coordinate in cells:
                    raise _UnsupportedOOXML(f"duplicate cell reference: {name}!{coordinate}")
                cells[coordinate] = cell
    _resolve_shared_formulas(cells)
    _resolve_array_formulas(cells, default_style=styles.cell_xfs[0])
    merge_container = _direct_child(root, "mergeCells")
    if merge_container is not None and (
        set(merge_container.attrib) - {"count"}
        or _semantic_text(merge_container.text) is not None
        or any(
            item.tag != _qname(_MAIN, "mergeCell")
            or set(item.attrib) != {"ref"}
            or len(item)
            or _semantic_text(item.text) is not None
            for item in merge_container
        )
    ):
        raise _UnsupportedOOXML(f"unsupported mergeCells content: {part_name}")
    merge_values = [item.attrib.get("ref", "") for item in _children(merge_container, "mergeCell")]
    merges = frozenset(merge_values)
    if "" in merges or len(merges) != len(merge_values):
        raise _UnsupportedOOXML(f"invalid merge reference in {part_name}")
    if merge_container is not None and "count" in merge_container.attrib:
        try:
            merge_count = int(merge_container.attrib["count"])
        except ValueError as exc:
            raise _UnsupportedOOXML(f"invalid merge count in {part_name}") from exc
        if merge_count != len(merge_values):
            raise _UnsupportedOOXML(f"inconsistent merge count in {part_name}")
    dimension = _direct_child(root, "dimension")
    if dimension is not None and (
        set(dimension.attrib) != {"ref"}
        or len(dimension)
        or _semantic_text(dimension.text) is not None
        or dimension.attrib["ref"] not in _expected_dimension(cells, merges)
    ):
        raise _UnsupportedOOXML(f"inconsistent worksheet dimension: {part_name}")

    relationships, relationships_part = _parse_relationships(parts, part_name)
    consumed: set[str] = set()
    tables: list[_Table] = []
    table_container = _direct_child(root, "tableParts")
    if table_container is not None and (
        set(table_container.attrib) - {"count"}
        or _semantic_text(table_container.text) is not None
        or any(
            item.tag != _qname(_MAIN, "tablePart")
            or set(item.attrib) != {_R_ID}
            or len(item)
            or _semantic_text(item.text) is not None
            for item in table_container
        )
    ):
        raise _UnsupportedOOXML(f"unsupported tableParts content: {part_name}")
    table_nodes = _children(table_container, "tablePart")
    if table_container is not None and "count" in table_container.attrib:
        try:
            table_count = int(table_container.attrib["count"])
        except ValueError as exc:
            raise _UnsupportedOOXML(f"invalid table count in {part_name}") from exc
        if table_count != len(table_nodes):
            raise _UnsupportedOOXML(f"inconsistent table count in {part_name}")
    for table_node in table_nodes:
        relationship = _relationship(
            relationships,
            table_node.attrib.get(_R_ID),
            expected_kind="table",
            parts=parts,
        )
        consumed.add(relationship.identifier)
        tables.append(_parse_table(parts, relationship.target))

    drawings: list[_Drawing] = []
    drawing_nodes = _children(root, "drawing")
    if any(
        set(drawing_node.attrib) != {_R_ID}
        or len(drawing_node)
        or _semantic_text(drawing_node.text) is not None
        for drawing_node in drawing_nodes
    ):
        raise _UnsupportedOOXML(f"unsupported drawing reference: {part_name}")
    for drawing_node in drawing_nodes:
        relationship = _relationship(
            relationships,
            drawing_node.attrib.get(_R_ID),
            expected_kind="drawing",
            parts=parts,
        )
        consumed.add(relationship.identifier)
        drawings.append(_parse_drawing(parts, relationship.target))

    return _Sheet(
        name=name,
        state=state,
        part=part_name,
        relationships_part=relationships_part,
        cells=cells,
        merges=merges,
        tables=tuple(sorted(tables, key=lambda item: repr(item.signature))),
        drawings=tuple(sorted(drawings, key=lambda item: repr(item.signature))),
        residual=_worksheet_residual(root),
        relationship_residual=_relationship_residual(relationships, consumed),
    )


def _workbook_residual(root: ET.Element) -> tuple[Any, ...]:
    handled = {_qname(_MAIN, "sheets"), _qname(_MAIN, "definedNames")}
    return (
        tuple(sorted(root.attrib.items())),
        tuple(
            _canonical_workbook_payload(child)
            for child in root
            if child.tag not in handled and not _is_exact_calc_a1_extension(child)
        ),
    )


def _parse_content_types(parts: dict[str, bytes]) -> tuple[tuple[str, str, str], ...]:
    part_name = "[Content_Types].xml"
    if part_name not in parts:
        raise _UnsupportedOOXML("missing [Content_Types].xml")
    root = _parse_xml(parts[part_name], part_name=part_name)
    if root.tag != _qname(_CONTENT_TYPES, "Types"):
        raise _UnsupportedOOXML("invalid content-types root")
    if root.attrib or _semantic_text(root.text) is not None:
        raise _UnsupportedOOXML("unsupported content-types root content")
    declarations: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for child in root:
        local_name = _local_name(child.tag)
        if local_name == "Default":
            expected_attributes = {"Extension", "ContentType"}
            key = child.attrib.get("Extension", "").lower()
        elif local_name == "Override":
            expected_attributes = {"PartName", "ContentType"}
            key = child.attrib.get("PartName", "").lstrip("/")
        else:
            raise _UnsupportedOOXML("unexpected content-types declaration")
        if (
            child.tag
            not in {
                _qname(_CONTENT_TYPES, "Default"),
                _qname(_CONTENT_TYPES, "Override"),
            }
            or set(child.attrib) != expected_attributes
            or len(child)
            or _semantic_text(child.text) is not None
            or _semantic_text(child.tail) is not None
        ):
            raise _UnsupportedOOXML("unsupported content-types declaration")
        content_type = child.attrib.get("ContentType", "")
        identity = (local_name, key)
        if not key or not content_type or identity in seen:
            raise _UnsupportedOOXML("ambiguous content-types declaration")
        seen.add(identity)
        declarations.append((local_name, key, content_type))
    return tuple(sorted(declarations))


def _parse_empty_custom_properties(payload: bytes) -> None:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise _UnsupportedOOXML("unsafe custom-properties XML declaration")
    try:
        parser = ET.iterparse(BytesIO(payload), events=("start", "comment", "pi"))
        for event, _element in parser:
            if event != "start":
                raise _UnsupportedOOXML(
                    "custom-properties payload contains comments or processing instructions"
                )
        root = parser.root
    except ET.ParseError as exc:
        raise _UnsupportedOOXML("malformed custom-properties XML") from exc
    if (
        root.tag != _qname(_CUSTOM_PROPERTIES, "Properties")
        or root.attrib
        or _semantic_text(root.text) is not None
        or _semantic_text(root.tail) is not None
        or len(root)
    ):
        raise _UnsupportedOOXML("custom-properties payload is not exactly empty")


def _consume_empty_custom_properties(
    *,
    parts: dict[str, bytes],
    content_types: tuple[tuple[str, str, str], ...],
    root_relationships: dict[str, _Relationship],
    root_consumed: set[str],
    recognized: set[str],
) -> str | None:
    relevant_relationships = [
        item
        for item in root_relationships.values()
        if item.kind == "custom-properties" or item.target == _CUSTOM_PROPERTIES_PART
    ]
    valid_relationships = [
        item
        for item in relevant_relationships
        if item.kind == "custom-properties"
        and item.target == _CUSTOM_PROPERTIES_PART
        and not item.external
    ]
    overrides = [
        item
        for item in content_types
        if item[0] == "Override" and item[1] == _CUSTOM_PROPERTIES_PART
    ]
    defaults = [
        item for item in content_types if item[0] == "Default" and item[1] == "xml"
    ]
    part_present = _CUSTOM_PROPERTIES_PART in parts
    if not part_present:
        if relevant_relationships or overrides:
            raise _UnsupportedOOXML("custom-properties plumbing is missing its payload part")
        return None
    if len(relevant_relationships) != 1 or len(valid_relationships) != 1:
        raise _UnsupportedOOXML(
            "custom-properties plumbing requires exactly one valid internal root relationship"
        )
    effective_content_type = overrides[0][2] if overrides else (defaults[0][2] if defaults else None)
    if effective_content_type != _CUSTOM_PROPERTIES_CONTENT_TYPE:
        raise _UnsupportedOOXML("invalid custom-properties content-type declaration")
    _parse_empty_custom_properties(parts[_CUSTOM_PROPERTIES_PART])
    root_consumed.add(valid_relationships[0].identifier)
    recognized.add(_CUSTOM_PROPERTIES_PART)
    return _CUSTOM_PROPERTIES_PART


def _load_snapshot(path: Path) -> _Snapshot:
    parts = _read_package(path)
    content_types = _parse_content_types(parts)
    root_relationships, root_relationships_part = _parse_relationships(parts, "")
    if root_relationships_part is None:
        raise _UnsupportedOOXML("missing package relationships")
    office_relationships = [
        item for item in root_relationships.values() if item.kind == "officeDocument"
    ]
    if len(office_relationships) != 1:
        raise _UnsupportedOOXML("package must have one officeDocument relationship")
    office_relationship = office_relationships[0]
    if office_relationship.external or office_relationship.target not in parts:
        raise _UnsupportedOOXML("invalid officeDocument relationship")
    root_consumed = {office_relationship.identifier}
    recognized = {"[Content_Types].xml", root_relationships_part}
    empty_custom_properties_part = _consume_empty_custom_properties(
        parts=parts,
        content_types=content_types,
        root_relationships=root_relationships,
        root_consumed=root_consumed,
        recognized=recognized,
    )
    auxiliary_signatures: dict[str, Any] = {}
    for relationship in root_relationships.values():
        if relationship.kind not in {"core-properties", "extended-properties"}:
            continue
        if relationship.external or relationship.target not in parts:
            raise _UnsupportedOOXML(f"missing package property part: {relationship.target}")
        root_consumed.add(relationship.identifier)
        recognized.add(relationship.target)
        auxiliary_signatures[relationship.target] = _payload_signature(
            relationship.target, parts[relationship.target]
        )

    workbook_part = office_relationship.target
    workbook_root = _parse_xml(parts[workbook_part], part_name=workbook_part)
    if workbook_root.tag != _qname(_MAIN, "workbook"):
        raise _UnsupportedOOXML("invalid workbook root")
    allowed_non_main = _validate_workbook_extensions(workbook_root, part_name=workbook_part)
    if any(
        _namespace(element.tag) != _MAIN and id(element) not in allowed_non_main
        for element in workbook_root.iter()
    ):
        raise _UnsupportedOOXML("unsupported workbook XML namespace")
    workbook_relationships, workbook_relationships_part = _parse_relationships(parts, workbook_part)
    if workbook_relationships_part is None:
        raise _UnsupportedOOXML("missing workbook relationships")
    recognized.update({workbook_part, workbook_relationships_part})
    workbook_consumed: set[str] = set()

    def related_part(kind: str, *, required: bool = False) -> str | None:
        matches = [item for item in workbook_relationships.values() if item.kind == kind]
        if len(matches) > 1 or (required and not matches):
            raise _UnsupportedOOXML(f"ambiguous workbook {kind} relationship")
        if not matches:
            return None
        relationship = matches[0]
        if relationship.external or relationship.target not in parts:
            raise _UnsupportedOOXML(f"invalid workbook {kind} relationship")
        workbook_consumed.add(relationship.identifier)
        recognized.add(relationship.target)
        return relationship.target

    styles_part = related_part("styles")
    shared_strings_part = related_part("sharedStrings")
    theme_part = related_part("theme")
    styles = _parse_styles(parts, styles_part)
    shared_strings = _parse_shared_strings(parts, shared_strings_part)
    if theme_part is not None:
        theme_root = _parse_xml(parts[theme_part], part_name=theme_part)
        _assert_no_lossy_extensions(theme_root, part_name=theme_part)
        theme = _canonical_element(theme_root)
    else:
        theme = None

    sheets_element = _direct_child(workbook_root, "sheets")
    if sheets_element is None:
        raise _UnsupportedOOXML("workbook has no sheets collection")
    sheet_order: list[str] = []
    sheets: dict[str, _Sheet] = {}
    for element in _children(sheets_element, "sheet"):
        name = element.attrib.get("name", "")
        if not name or name in sheets:
            raise _UnsupportedOOXML("worksheet names must be non-empty and unique")
        relationship = _relationship(
            workbook_relationships,
            element.attrib.get(_R_ID),
            expected_kind="worksheet",
            parts=parts,
        )
        workbook_consumed.add(relationship.identifier)
        sheet = _parse_sheet(
            parts,
            name=name,
            state=element.attrib.get("state", "visible"),
            part_name=relationship.target,
            styles=styles,
            shared_strings=shared_strings,
        )
        sheet_order.append(name)
        sheets[name] = sheet
        recognized.update(sheet.all_parts)

    defined_names_element = _direct_child(workbook_root, "definedNames")
    defined_names = tuple(
        sorted(
            (_canonical_element(item) for item in _children(defined_names_element, "definedName")),
            key=repr,
        )
    )
    part_signatures = {
        part_name: _payload_signature(part_name, payload) for part_name, payload in parts.items()
    }
    return _Snapshot(
        parts=parts,
        part_signatures=part_signatures,
        recognized_parts=frozenset(recognized),
        workbook_part=workbook_part,
        workbook_relationships_part=workbook_relationships_part,
        workbook_relationship_residual=_relationship_residual(
            workbook_relationships, workbook_consumed
        ),
        root_relationship_residual=_relationship_residual(root_relationships, root_consumed),
        sheet_order=tuple(sheet_order),
        sheets=sheets,
        defined_names=defined_names,
        workbook_residual=_workbook_residual(workbook_root),
        styles_part=styles_part,
        styles=styles,
        theme_part=theme_part,
        theme=theme,
        shared_strings_part=shared_strings_part,
        shared_strings=shared_strings,
        content_types=content_types,
        auxiliary_signatures=auxiliary_signatures,
        empty_custom_properties_part=empty_custom_properties_part,
    )


def _changed_parts(before: _Snapshot, after: _Snapshot) -> frozenset[str]:
    names = before.part_signatures.keys() | after.part_signatures.keys()
    recognized = before.recognized_parts | after.recognized_parts
    return frozenset(
        name
        for name in names
        if (
            before.part_signatures.get(name) != after.part_signatures.get(name)
            if name in recognized
            else (name in before.parts) != (name in after.parts)
            or hashlib.sha256(before.parts.get(name, b"")).digest()
            != hashlib.sha256(after.parts.get(name, b"")).digest()
        )
    )


def _content_type_delta_explained(before: _Snapshot, after: _Snapshot) -> bool:
    before_declarations = set(before.content_types)
    after_declarations = set(after.content_types)
    changed = before_declarations ^ after_declarations
    if not changed:
        return True
    recognized = before.recognized_parts | after.recognized_parts
    before_names = before.parts.keys()
    after_names = after.parts.keys()
    for kind, key, _content_type in changed:
        if kind != "Override" or key not in recognized:
            return False
        if (key in before_names) == (key in after_names):
            return False
    return True


def _table_parts(sheet: _Sheet) -> frozenset[str]:
    return frozenset(part for table in sheet.tables for part in table.parts)


def _drawing_parts(sheet: _Sheet) -> frozenset[str]:
    return frozenset(part for drawing in sheet.drawings for part in drawing.parts)


def _style_dependencies(style: Any) -> frozenset[Any]:
    if style is None:
        return frozenset()
    try:
        resolved = style[1]
    except (IndexError, TypeError):
        return frozenset()
    return frozenset(resolved)


def _shared_string_signature(value: Any) -> Any | None:
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "shared":
        return value[1]
    return None


def _diff_snapshots(before: _Snapshot, after: _Snapshot) -> OracleDiff:
    changed_parts = _changed_parts(before, after)
    effects: set[str] = set()
    cells: set[OracleCell] = set()
    formula_cells: set[OracleCell] = set()
    sheets_scope: set[str] = set()
    workbook_scope = False
    explained: set[str] = set()
    changed_styles: set[Any] = set()
    changed_style_dependencies: set[Any] = set()
    changed_shared_strings: set[Any] = set()

    opaque_changes = changed_parts - (before.recognized_parts | after.recognized_parts)
    if opaque_changes:
        preview = ", ".join(sorted(opaque_changes)[:5])
        return OracleDiff.unknown(f"opaque or unreachable OOXML part changed: {preview}")
    if before.root_relationship_residual != after.root_relationship_residual:
        return OracleDiff.unknown("unaccounted package relationship changed")
    if before.workbook_relationship_residual != after.workbook_relationship_residual:
        return OracleDiff.unknown("unaccounted workbook relationship changed")
    if before.auxiliary_signatures != after.auxiliary_signatures:
        return OracleDiff.unknown("document property payload changed")
    if before.workbook_residual != after.workbook_residual:
        return OracleDiff.unknown("unclassified workbook XML changed")
    if before.styles.residual != after.styles.residual:
        return OracleDiff.unknown("unclassified style XML changed")
    if before.empty_custom_properties_part != after.empty_custom_properties_part:
        explained.add(_ROOT_RELATIONSHIPS_PART)
        if before.empty_custom_properties_part is not None:
            explained.add(before.empty_custom_properties_part)
        if after.empty_custom_properties_part is not None:
            explained.add(after.empty_custom_properties_part)

    sheet_topology_changed = before.sheet_order != after.sheet_order
    if sheet_topology_changed:
        effects.update({"structure", "visual"})
        workbook_scope = True
        explained.update(
            {
                before.workbook_part,
                after.workbook_part,
                before.workbook_relationships_part,
                after.workbook_relationships_part,
            }
        )
        for name in set(before.sheets) ^ set(after.sheets):
            old_sheet = before.sheets.get(name)
            new_sheet = after.sheets.get(name)
            sheet = old_sheet or new_sheet
            assert sheet is not None
            explained.update(sheet.all_parts)
            opposite_sheets = (
                after.sheets.values() if old_sheet is not None else before.sheets.values()
            )
            if any(candidate.part == sheet.part for candidate in opposite_sheets):
                continue
            for coordinate, cell in sheet.cells.items():
                reference = OracleCell(name, coordinate)
                if cell.formula is not None:
                    effects.add("formula")
                    cells.add(reference)
                    formula_cells.add(reference)
                elif cell.value is not None:
                    effects.add("value")
                    cells.add(reference)
                    shared_signature = _shared_string_signature(cell.value)
                    if shared_signature is not None:
                        changed_shared_strings.add(shared_signature)
                        if before.shared_strings_part is not None:
                            explained.add(before.shared_strings_part)
                        if after.shared_strings_part is not None:
                            explained.add(after.shared_strings_part)
                absent_style = (
                    after.styles.cell_xfs[0] if old_sheet is not None else before.styles.cell_xfs[0]
                )
                if cell.style != absent_style:
                    effects.update({"style", "visual"})
                    cells.add(reference)
                    changed_styles.update({cell.style, absent_style})
                    changed_style_dependencies.update(_style_dependencies(cell.style))
                    changed_style_dependencies.update(_style_dependencies(absent_style))
                    if before.styles_part is not None:
                        explained.add(before.styles_part)
                    if after.styles_part is not None:
                        explained.add(after.styles_part)

    if before.defined_names != after.defined_names:
        effects.add("structure")
        sheets_scope.update(before.sheet_order)
        sheets_scope.update(after.sheet_order)
        explained.update({before.workbook_part, after.workbook_part})

    if before.theme != after.theme:
        effects.update({"style", "visual"})
        workbook_scope = True
        if before.theme_part is not None:
            explained.add(before.theme_part)
        if after.theme_part is not None:
            explained.add(after.theme_part)
        explained.update({before.workbook_relationships_part, after.workbook_relationships_part})

    for name in sorted(set(before.sheets) & set(after.sheets)):
        old_sheet = before.sheets[name]
        new_sheet = after.sheets[name]
        if old_sheet.state != new_sheet.state:
            effects.update({"structure", "visual"})
            sheets_scope.add(name)
            explained.update({before.workbook_part, after.workbook_part})
        if old_sheet.residual != new_sheet.residual:
            return OracleDiff.unknown(f"unclassified worksheet XML changed: {name}")
        if old_sheet.relationship_residual != new_sheet.relationship_residual:
            return OracleDiff.unknown(f"unaccounted worksheet relationship changed: {name}")
        coordinates = old_sheet.cells.keys() | new_sheet.cells.keys()
        for coordinate in sorted(coordinates):
            old_cell = old_sheet.cells.get(coordinate)
            new_cell = new_sheet.cells.get(coordinate)
            default_style = before.styles.cell_xfs[0]
            old_value = old_cell.value if old_cell is not None else None
            new_value = new_cell.value if new_cell is not None else None
            old_formula = old_cell.formula if old_cell is not None else None
            new_formula = new_cell.formula if new_cell is not None else None
            old_style = old_cell.style if old_cell is not None else default_style
            new_default_style = after.styles.cell_xfs[0]
            new_style = new_cell.style if new_cell is not None else new_default_style
            old_cached = old_cell.cached_formula_value if old_cell is not None else None
            new_cached = new_cell.cached_formula_value if new_cell is not None else None
            old_residual = old_cell.residual if old_cell is not None else ((), ())
            new_residual = new_cell.residual if new_cell is not None else ((), ())
            if old_residual != new_residual:
                return OracleDiff.unknown(f"unclassified cell XML changed: {name}!{coordinate}")
            if old_cached != new_cached and old_formula == new_formula:
                return OracleDiff.unknown(
                    f"formula cache changed without a formula edit: {name}!{coordinate}"
                )
            if old_formula != new_formula:
                effects.add("formula")
                reference = OracleCell(name, coordinate)
                cells.add(reference)
                formula_cells.add(reference)
                explained.update({old_sheet.part, new_sheet.part})
            elif old_value != new_value:
                effects.add("value")
                cells.add(OracleCell(name, coordinate))
                explained.update({old_sheet.part, new_sheet.part})
                for value in (old_value, new_value):
                    shared_signature = _shared_string_signature(value)
                    if shared_signature is not None:
                        changed_shared_strings.add(shared_signature)
                if before.shared_strings_part is not None:
                    explained.add(before.shared_strings_part)
                if after.shared_strings_part is not None:
                    explained.add(after.shared_strings_part)
            if old_style != new_style:
                effects.update({"style", "visual"})
                cells.add(OracleCell(name, coordinate))
                changed_styles.update({old_style, new_style})
                changed_style_dependencies.update(_style_dependencies(old_style))
                changed_style_dependencies.update(_style_dependencies(new_style))
                explained.update({old_sheet.part, new_sheet.part})
                if before.styles_part is not None:
                    explained.add(before.styles_part)
                if after.styles_part is not None:
                    explained.add(after.styles_part)

        if old_sheet.merges != new_sheet.merges:
            effects.update({"structure", "visual"})
            sheets_scope.add(name)
            explained.update({old_sheet.part, new_sheet.part})
        if tuple(item.signature for item in old_sheet.tables) != tuple(
            item.signature for item in new_sheet.tables
        ):
            effects.update({"structure", "visual"})
            sheets_scope.add(name)
            explained.update({old_sheet.part, new_sheet.part})
            explained.update(_table_parts(old_sheet) | _table_parts(new_sheet))
            if old_sheet.relationships_part is not None:
                explained.add(old_sheet.relationships_part)
            if new_sheet.relationships_part is not None:
                explained.add(new_sheet.relationships_part)
        if tuple(item.signature for item in old_sheet.drawings) != tuple(
            item.signature for item in new_sheet.drawings
        ):
            effects.update({"structure", "visual"})
            sheets_scope.add(name)
            explained.update({old_sheet.part, new_sheet.part})
            explained.update(_drawing_parts(old_sheet) | _drawing_parts(new_sheet))
            if old_sheet.relationships_part is not None:
                explained.add(old_sheet.relationships_part)
            if new_sheet.relationships_part is not None:
                explained.add(new_sheet.relationships_part)

    changed_style_definitions = set(before.styles.cell_xfs) ^ set(after.styles.cell_xfs)
    if changed_style_definitions - changed_styles:
        return OracleDiff.unknown("unreferenced cell-style definition changed")
    changed_style_resources = before.styles.resources ^ after.styles.resources
    if changed_style_resources - changed_style_dependencies:
        return OracleDiff.unknown("unreferenced style resource changed")
    changed_shared_definitions = set(before.shared_strings) ^ set(after.shared_strings)
    if changed_shared_definitions - changed_shared_strings:
        return OracleDiff.unknown("unreferenced shared-string definition changed")

    if "[Content_Types].xml" in changed_parts:
        if not _content_type_delta_explained(before, after):
            return OracleDiff.unknown("unexplained content-types declaration changed")
        explained.add("[Content_Types].xml")
    unexplained = changed_parts - explained
    if unexplained:
        preview = ", ".join(sorted(unexplained)[:5])
        return OracleDiff.unknown(
            f"changed OOXML payload was not semantically explained: {preview}"
        )
    return OracleDiff(
        semantic_changed=bool(effects),
        complete=True,
        effects=frozenset(effects),
        cells=frozenset(cells),
        formula_cells=frozenset(formula_cells),
        sheets=frozenset(sheets_scope),
        workbook_scope=workbook_scope,
    )


def diff_ooxml(before: str | Path, after: str | Path) -> OracleDiff:
    """Compare two OOXML packages with an independent, fail-closed parser."""

    try:
        before_snapshot = _load_snapshot(Path(before))
        after_snapshot = _load_snapshot(Path(after))
        return _diff_snapshots(before_snapshot, after_snapshot)
    except (OSError, _UnsupportedOOXML, ValueError) as exc:
        return OracleDiff.unknown(f"{type(exc).__name__}: {exc}")


__all__ = ["OracleCell", "OracleDiff", "diff_ooxml"]
