"""Fail-closed, read-only detection of recalculation-bearing OOXML content."""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import stat
import unicodedata
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urlsplit
from zipfile import BadZipFile, LargeZipFile, ZipFile, ZipInfo

OOXML_FORMULA_SCAN_SCHEMA_VERSION = "ooxml-formula-scan-v1"
OOXML_FORMULA_POLICY_VERSION = "semantic-ooxml-formula-policy-v1"
OOXML_FORMULA_SCAN_MAX_FILE_BYTES = 256 * 1024 * 1024
OOXML_NO_FORMULA_BACKEND = "sheetledger-ooxml-noop"
OOXML_NO_FORMULA_PROFILE = "verified-no-formula-byte-identical-v1"

_MAX_PARTS = 10_000
_MAX_XML_PART_BYTES = 64 * 1024 * 1024
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELATIONSHIPS_PART = "_rels/.rels"
_CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
_PACKAGE_RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/package/2006/relationships",
        "http://purl.oclc.org/ooxml/package/relationships",
    }
)
_TRANSITIONAL_OFFICE_RELATIONSHIPS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_STRICT_OFFICE_RELATIONSHIPS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
_OFFICE_RELATIONSHIP_NAMESPACES = frozenset(
    {
        _TRANSITIONAL_OFFICE_RELATIONSHIPS,
        _STRICT_OFFICE_RELATIONSHIPS,
    }
)
_SPREADSHEETML_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "http://purl.oclc.org/ooxml/spreadsheetml/main",
    }
)
_XLSX_WORKBOOK_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "application/vnd.ms-excel.sheet.main+xml",
    }
)
_XLSM_WORKBOOK_CONTENT_TYPES = frozenset({"application/vnd.ms-excel.sheet.macroEnabled.main+xml"})
_SUPPORTED_SHEET_RELATIONSHIP_KINDS = frozenset({"worksheet", "chartsheet", "dialogsheet"})
_OFFICE_DOCUMENT_RELATIONSHIP_TYPES = frozenset(
    {
        f"{_TRANSITIONAL_OFFICE_RELATIONSHIPS}/officeDocument",
        f"{_STRICT_OFFICE_RELATIONSHIPS}/officeDocument",
    }
)
_SHEET_RELATIONSHIP_TYPES = {
    kind: frozenset(
        {
            f"{_TRANSITIONAL_OFFICE_RELATIONSHIPS}/{kind}",
            f"{_STRICT_OFFICE_RELATIONSHIPS}/{kind}",
        }
    )
    for kind in _SUPPORTED_SHEET_RELATIONSHIP_KINDS
}
_RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
_SHEET_CONTENT_TYPES = {
    "worksheet": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            "application/vnd.ms-excel.worksheet+xml",
        }
    ),
    "chartsheet": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml",
            "application/vnd.ms-excel.chartsheet+xml",
        }
    ),
    "dialogsheet": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.dialogsheet+xml",
            "application/vnd.ms-excel.dialogsheet+xml",
        }
    ),
}
_FORMULA_LOCAL_NAMES = frozenset(
    {"f", "formula", "formula1", "formula2", "calculatedcolumnformula", "totalsrowformula"}
)
_FORMULA_ATTRIBUTE_NAMES = frozenset({"formula", "refersto"})
_FORMULA_ATTRIBUTE_EXEMPT_ELEMENTS = frozenset({"ignorederror", "sheetview", "workbookview"})
_FORMULA_TYPED_VALUE_ELEMENTS = frozenset({"cfvo"})
_OPAQUE_FORMULA_CAPABLE_PREFIXES = (
    "customui/",
    "customxml/",
    "xl/activex/",
    "xl/ctrlprops/",
    "xl/diagrams/",
    "xl/embeddings/",
    "xl/externallinks/",
    "xl/model/",
    "xl/pivotcache/",
    "xl/pivottables/",
    "xl/querytables/",
    "xl/richdata/",
    "xl/slicercaches/",
    "xl/slicers/",
    "xl/threadedcomments/",
    "xl/timelines/",
    "xl/webextensions/",
)
_OPAQUE_FORMULA_CAPABLE_PARTS = frozenset(
    {
        "xl/connections.xml",
        "xl/metadata.xml",
        "xl/vbaproject.bin",
    }
)
_OPAQUE_FORMULA_XML_CONTENT_TYPE_TOKENS = frozenset(
    {"activex", "connection", "control", "external", "model", "pivot", "query"}
)
_OPAQUE_FORMULA_BINARY_CONTENT_TYPE_TOKENS = frozenset({"spreadsheetml.sheet"})
_OPAQUE_FORMULA_CAPABLE_RELATIONSHIP_KINDS = frozenset(
    {
        "activexcontrol",
        "calcchain",
        "connection",
        "connections",
        "control",
        "ctrlprop",
        "customui",
        "customxml",
        "diagramcolors",
        "diagramdata",
        "diagramlayout",
        "diagramquickstyle",
        "embeddedpackage",
        "externallink",
        "externallinkpath",
        "model",
        "oleobject",
        "package",
        "person",
        "pivotcachedefinition",
        "pivotcacherecords",
        "pivottable",
        "querytable",
        "richvaluerel",
        "slicer",
        "slicercache",
        "threadedcomment",
        "timeline",
        "timelinecache",
        "webextension",
        "webextensiontaskpanes",
    }
)
_SAFE_OFFICE_RELATIONSHIP_KINDS = frozenset(
    {
        "chart",
        "chartColorStyle",
        "chartStyle",
        "chartUserShapes",
        "chartsheet",
        "comments",
        "custom-properties",
        "dialogsheet",
        "drawing",
        "extended-properties",
        "hyperlink",
        "image",
        "legacyDrawing",
        "officeDocument",
        "printerSettings",
        "sharedStrings",
        "styles",
        "table",
        "theme",
        "themeOverride",
        "vmlDrawing",
        "worksheet",
    }
)
_SAFE_RELATIONSHIP_TYPES = frozenset(
    {
        f"{namespace}/{kind}"
        for namespace in _OFFICE_RELATIONSHIP_NAMESPACES
        for kind in _SAFE_OFFICE_RELATIONSHIP_KINDS
    }
    | {
        "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
    }
)

_DRAWINGML_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/main",
        "http://purl.oclc.org/ooxml/drawingml/main",
    }
)
_DRAWINGML_CHART_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/chart",
        "http://purl.oclc.org/ooxml/drawingml/chart",
    }
)
_DRAWINGML_SPREADSHEET_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
        "http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
    }
)
_RELATIONSHIP_TARGET_CONTRACTS: dict[
    str,
    tuple[re.Pattern[str], frozenset[str], frozenset[tuple[str, str]] | None],
] = {
    "officeDocument": (
        re.compile(r"xl/workbook\.xml\Z"),
        _XLSX_WORKBOOK_CONTENT_TYPES | _XLSM_WORKBOOK_CONTENT_TYPES,
        frozenset((namespace, "workbook") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "core-properties": (
        re.compile(r"docProps/core\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-package.core-properties+xml"}),
        frozenset(
            {
                (
                    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
                    "coreProperties",
                )
            }
        ),
    ),
    "extended-properties": (
        re.compile(r"docProps/app\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.extended-properties+xml"}),
        frozenset(
            {
                (
                    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
                    "Properties",
                )
            }
        ),
    ),
    "custom-properties": (
        re.compile(r"docProps/custom\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.custom-properties+xml"}),
        frozenset(
            {
                (
                    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
                    "Properties",
                )
            }
        ),
    ),
    "worksheet": (
        re.compile(r"xl/worksheets/[^/]+\.xml\Z"),
        _SHEET_CONTENT_TYPES["worksheet"],
        frozenset((namespace, "worksheet") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "chartsheet": (
        re.compile(r"xl/chartsheets/[^/]+\.xml\Z"),
        _SHEET_CONTENT_TYPES["chartsheet"],
        frozenset((namespace, "chartsheet") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "dialogsheet": (
        re.compile(r"xl/dialogsheets/[^/]+\.xml\Z"),
        _SHEET_CONTENT_TYPES["dialogsheet"],
        frozenset((namespace, "dialogsheet") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "styles": (
        re.compile(r"xl/styles\.xml\Z"),
        frozenset(
            {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
                "application/vnd.ms-excel.styles+xml",
            }
        ),
        frozenset((namespace, "styleSheet") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "sharedStrings": (
        re.compile(r"xl/sharedStrings\.xml\Z"),
        frozenset(
            {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
                "application/vnd.ms-excel.sharedStrings+xml",
            }
        ),
        frozenset((namespace, "sst") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "table": (
        re.compile(r"xl/tables/[^/]+\.xml\Z"),
        frozenset(
            {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml",
                "application/vnd.ms-excel.table+xml",
            }
        ),
        frozenset((namespace, "table") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "comments": (
        re.compile(r"xl/comments/[^/]+\.xml\Z"),
        frozenset(
            {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml",
                "application/vnd.ms-excel.comments+xml",
            }
        ),
        frozenset((namespace, "comments") for namespace in _SPREADSHEETML_NAMESPACES),
    ),
    "drawing": (
        re.compile(r"xl/drawings/[^/]+\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.drawing+xml"}),
        frozenset((namespace, "wsDr") for namespace in _DRAWINGML_SPREADSHEET_NAMESPACES),
    ),
    "chart": (
        re.compile(r"xl/charts/[^/]+\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.drawingml.chart+xml"}),
        frozenset((namespace, "chartSpace") for namespace in _DRAWINGML_CHART_NAMESPACES),
    ),
    "chartUserShapes": (
        re.compile(r"xl/drawings/[^/]+\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.drawingml.chartshapes+xml"}),
        frozenset((namespace, "userShapes") for namespace in _DRAWINGML_CHART_NAMESPACES),
    ),
    "chartStyle": (
        re.compile(r"xl/charts/[^/]+\.xml\Z"),
        frozenset({"application/vnd.ms-office.chartstyle+xml"}),
        frozenset(
            {
                (
                    "http://schemas.microsoft.com/office/drawing/2012/chartStyle",
                    "chartStyle",
                )
            }
        ),
    ),
    "chartColorStyle": (
        re.compile(r"xl/charts/[^/]+\.xml\Z"),
        frozenset({"application/vnd.ms-office.chartcolorstyle+xml"}),
        frozenset(
            {
                (
                    "http://schemas.microsoft.com/office/drawing/2012/chartColorStyle",
                    "colorStyle",
                )
            }
        ),
    ),
    "theme": (
        re.compile(r"xl/theme/[^/]+\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.theme+xml"}),
        frozenset((namespace, "theme") for namespace in _DRAWINGML_NAMESPACES),
    ),
    "themeOverride": (
        re.compile(r"xl/theme/[^/]+\.xml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.themeOverride+xml"}),
        frozenset((namespace, "themeOverride") for namespace in _DRAWINGML_NAMESPACES),
    ),
    "vmlDrawing": (
        re.compile(r"xl/drawings/[^/]+\.vml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.vmlDrawing"}),
        frozenset({("", "xml")}),
    ),
    "legacyDrawing": (
        re.compile(r"xl/drawings/[^/]+\.vml\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.vmlDrawing"}),
        frozenset({("", "xml")}),
    ),
    "printerSettings": (
        re.compile(r"xl/printerSettings/[^/]+\.bin\Z"),
        frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings"}),
        None,
    ),
    "image": (
        re.compile(r"xl/media/[^/]+\.(?:bmp|emf|gif|jpe?g|png|tiff?|wmf)\Z"),
        frozenset(
            {
                "image/bmp",
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/tiff",
                "image/x-emf",
                "image/x-wmf",
            }
        ),
        None,
    ),
    "thumbnail": (
        re.compile(r"docProps/thumbnail\.(?:emf|jpe?g|png|wmf)\Z"),
        frozenset({"image/jpeg", "image/png", "image/x-emf", "image/x-wmf"}),
        None,
    ),
}
_CELL_REFERENCE = re.compile(r"([A-Z]{1,3})([1-9][0-9]*)\Z")
_STATIC_DEFINED_NAME_REFERENCE = re.compile(
    r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_. ]*)!"
    r"(?:\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?"
    r"|\$?[A-Z]{1,3}:\$?[A-Z]{1,3}|\$?[1-9][0-9]*:\$?[1-9][0-9]*)"
)


class OOXMLFormulaScanError(ValueError):
    """Raised when an OOXML package cannot be certified formula-free."""


@dataclass(frozen=True)
class OOXMLFormulaScan:
    """A hash-bound result from an independent, read-only package scan."""

    package_sha256: str
    workbook_format: str
    xml_part_count: int
    worksheet_count: int
    scanned_cell_count: int
    formula_marker_count: int
    formula_kinds: tuple[str, ...]
    formula_policy_version: str = OOXML_FORMULA_POLICY_VERSION

    @property
    def has_formulas(self) -> bool:
        return self.formula_marker_count > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": OOXML_FORMULA_SCAN_SCHEMA_VERSION,
            "formula_policy_version": self.formula_policy_version,
            "package_sha256": self.package_sha256,
            "workbook_format": self.workbook_format,
            "xml_part_count": self.xml_part_count,
            "worksheet_count": self.worksheet_count,
            "scanned_cell_count": self.scanned_cell_count,
            "formula_marker_count": self.formula_marker_count,
            "formula_kinds": list(self.formula_kinds),
        }


@dataclass(frozen=True)
class _Relationship:
    identifier: str
    relationship_type: str
    kind: str
    target: str
    external: bool


def _split_qname(value: str) -> tuple[str, str]:
    if value.startswith("{") and "}" in value:
        namespace, local_name = value[1:].split("}", 1)
        return namespace, local_name
    return "", value


def _local_name(value: str) -> str:
    return _split_qname(value)[1]


def _safe_part_name(info: ZipInfo) -> str | None:
    name = info.filename
    if info.is_dir():
        return None
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or urlsplit(name).query
        or urlsplit(name).fragment
    ):
        raise OOXMLFormulaScanError("OOXML package contains an unsafe part name")
    pure = PurePosixPath(name)
    if any(component in {"", ".", ".."} for component in pure.parts):
        raise OOXMLFormulaScanError("OOXML package contains an ambiguous part name")
    if pure.as_posix() != name or posixpath.normpath(name) != name:
        raise OOXMLFormulaScanError("OOXML package contains a non-canonical part name")
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
        raise OOXMLFormulaScanError("OOXML package contains a non-regular ZIP entry")
    if info.flag_bits & 0x1:
        raise OOXMLFormulaScanError("Encrypted OOXML ZIP entries are unsupported")
    return name


def _part_alias(name: str) -> str:
    if "%" in name:
        raise OOXMLFormulaScanError("OOXML part name has ambiguous percent encoding")
    return unicodedata.normalize("NFC", name).casefold()


def _read_part(archive: ZipFile, info: ZipInfo, *, xml: bool) -> bytes | None:
    if info.file_size < 0:
        raise OOXMLFormulaScanError("OOXML ZIP entry has an invalid size")
    if xml and info.file_size > _MAX_XML_PART_BYTES:
        raise OOXMLFormulaScanError("OOXML XML part exceeds the scan size limit")
    try:
        with archive.open(info, "r") as source:
            if xml:
                payload = source.read(_MAX_XML_PART_BYTES + 1)
                if len(payload) > _MAX_XML_PART_BYTES or len(payload) != info.file_size:
                    raise OOXMLFormulaScanError("OOXML XML part size is inconsistent")
                return payload
            observed = 0
            for chunk in iter(lambda: source.read(_READ_CHUNK_BYTES), b""):
                observed += len(chunk)
            if observed != info.file_size:
                raise OOXMLFormulaScanError("OOXML ZIP entry size is inconsistent")
    except (BadZipFile, EOFError, RuntimeError, OSError) as exc:
        raise OOXMLFormulaScanError("OOXML ZIP entry failed integrity validation") from exc
    return None


class _UnsafeXMLDeclaration(Exception):
    pass


def _reject_unsafe_xml_declarations(payload: bytes, *, part_name: str) -> None:
    parser = expat.ParserCreate()

    def reject(*_: object) -> None:
        raise _UnsafeXMLDeclaration

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.UnparsedEntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(payload, True)
    except _UnsafeXMLDeclaration as exc:
        raise OOXMLFormulaScanError(
            f"OOXML XML part has a forbidden DTD or entity declaration: {part_name}"
        ) from exc
    except expat.ExpatError as exc:
        raise OOXMLFormulaScanError(f"OOXML XML part is malformed: {part_name}") from exc


def _parse_xml(payload: bytes, *, part_name: str) -> ET.Element:
    _reject_unsafe_xml_declarations(payload, part_name=part_name)
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise OOXMLFormulaScanError(f"OOXML XML part is malformed: {part_name}") from exc


def _defined_name_terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    start = 0
    quoted = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif character == "," and not quoted:
            terms.append(value[start:index].strip())
            start = index + 1
        index += 1
    if quoted:
        return ()
    terms.append(value[start:].strip())
    return tuple(terms)


def _is_static_defined_name(value: str) -> bool:
    terms = _defined_name_terms(value.strip())
    return bool(terms) and all(
        term and _STATIC_DEFINED_NAME_REFERENCE.fullmatch(term) is not None for term in terms
    )


def _formula_markers(root: ET.Element) -> tuple[int, set[str]]:
    count = 0
    kinds: set[str] = set()
    for element in root.iter():
        namespace, raw_local = _split_qname(element.tag)
        local = raw_local.casefold()
        if local in _FORMULA_LOCAL_NAMES or local.startswith("fmla"):
            count += 1
            kinds.add(local)
        if (
            local == "definedname"
            and (element.text or "").strip()
            and not _is_static_defined_name(element.text or "")
        ):
            count += 1
            kinds.add("definedname-expression")
        if (
            namespace in _SPREADSHEETML_NAMESPACES
            and local in _FORMULA_TYPED_VALUE_ELEMENTS
            and str(element.attrib.get("type", "")).casefold() == "formula"
        ):
            count += 1
            kinds.add(f"{local}-formula-value")
        for attribute, value in element.attrib.items():
            attribute_local = _local_name(attribute).casefold()
            if (
                attribute_local in _FORMULA_ATTRIBUTE_NAMES
                and not (
                    namespace in _SPREADSHEETML_NAMESPACES
                    and local in _FORMULA_ATTRIBUTE_EXEMPT_ELEMENTS
                )
                and not (
                    attribute_local == "refersto"
                    and isinstance(value, str)
                    and _is_static_defined_name(value)
                )
            ):
                count += 1
                kinds.add(f"attribute:{attribute_local}")
    return count, kinds


def _parse_content_types(root: ET.Element) -> tuple[dict[str, str], dict[str, str]]:
    if root.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Types" or root.attrib:
        raise OOXMLFormulaScanError("OOXML content-types root is invalid")
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    override_aliases: set[str] = set()
    for child in root:
        if len(child) or (child.text or "").strip() or (child.tail or "").strip():
            raise OOXMLFormulaScanError("OOXML content-types declaration is ambiguous")
        if child.tag == f"{{{_CONTENT_TYPES_NAMESPACE}}}Default":
            if set(child.attrib) != {"Extension", "ContentType"}:
                raise OOXMLFormulaScanError("OOXML default content type is invalid")
            extension = child.attrib["Extension"].casefold()
            content_type = child.attrib["ContentType"]
            if not extension or "." in extension or not content_type or extension in defaults:
                raise OOXMLFormulaScanError("OOXML default content type is ambiguous")
            defaults[extension] = content_type
            continue
        if child.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Override":
            raise OOXMLFormulaScanError("OOXML content-types declaration is unsupported")
        if set(child.attrib) != {"PartName", "ContentType"}:
            raise OOXMLFormulaScanError("OOXML override content type is invalid")
        raw_name = child.attrib["PartName"]
        content_type = child.attrib["ContentType"]
        if not raw_name.startswith("/") or not content_type:
            raise OOXMLFormulaScanError("OOXML override content type is invalid")
        name = raw_name[1:]
        probe = ZipInfo(name)
        if _safe_part_name(probe) != name:
            raise OOXMLFormulaScanError("OOXML override part name is invalid")
        alias = _part_alias(name)
        if name in overrides or alias in override_aliases:
            raise OOXMLFormulaScanError("OOXML override content type is ambiguous")
        overrides[name] = content_type
        override_aliases.add(alias)
    return defaults, overrides


def _content_type_for(
    part_name: str,
    *,
    defaults: dict[str, str],
    overrides: dict[str, str],
) -> str:
    override = overrides.get(part_name)
    if override is not None:
        return override
    filename = posixpath.basename(part_name)
    if "." not in filename:
        raise OOXMLFormulaScanError(f"OOXML part has no declared content type: {part_name}")
    extension = filename.rsplit(".", 1)[1].casefold()
    try:
        return defaults[extension]
    except KeyError as exc:
        raise OOXMLFormulaScanError(
            f"OOXML part has no declared content type: {part_name}"
        ) from exc


def _is_xml_part(part_name: str, content_type: str) -> bool:
    lowered = content_type.casefold()
    return (
        part_name.casefold().endswith((".xml", ".rels", ".vml"))
        or lowered.endswith("+xml")
        or lowered in {"application/xml", "text/xml"}
    )


def _reject_opaque_formula_capable_part(
    part_name: str,
    content_type: str,
    *,
    xml: bool,
) -> None:
    lowered_name = part_name.casefold()
    if lowered_name in _OPAQUE_FORMULA_CAPABLE_PARTS or lowered_name.startswith(
        _OPAQUE_FORMULA_CAPABLE_PREFIXES
    ):
        raise OOXMLFormulaScanError("OOXML package contains an unsupported formula-capable part")
    lowered = content_type.casefold()
    if "vbaproject" in lowered or "macrosheet" in lowered:
        raise OOXMLFormulaScanError("OOXML package contains an unsupported formula-capable part")
    if lowered_name.startswith("xl/media/") and lowered.startswith(("image/", "audio/", "video/")):
        return
    if lowered_name.startswith("xl/printersettings/") and lowered == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.printersettings"
    ):
        return
    if lowered_name.startswith("docprops/thumbnail.") and lowered.startswith("image/"):
        return
    if any(token in lowered for token in _OPAQUE_FORMULA_XML_CONTENT_TYPE_TOKENS) or (
        not xml and any(token in lowered for token in _OPAQUE_FORMULA_BINARY_CONTENT_TYPE_TOKENS)
    ):
        raise OOXMLFormulaScanError("OOXML package contains an opaque formula-capable content type")
    if xml:
        return
    raise OOXMLFormulaScanError("OOXML package contains an unsupported opaque binary part")


def _relationship_part(source_part: str) -> str:
    if not source_part:
        return _ROOT_RELATIONSHIPS_PART
    directory, filename = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _relationship_source(relationship_part: str, *, part_names: set[str]) -> str:
    if relationship_part == _ROOT_RELATIONSHIPS_PART:
        return ""
    directory, filename = posixpath.split(relationship_part)
    if posixpath.basename(directory) != "_rels" or not filename.endswith(".rels"):
        raise OOXMLFormulaScanError("OOXML relationship part name is invalid")
    source_name = filename[: -len(".rels")]
    if not source_name:
        raise OOXMLFormulaScanError("OOXML relationship source is ambiguous")
    source_directory = posixpath.dirname(directory)
    source_part = posixpath.join(source_directory, source_name)
    if source_part not in part_names:
        raise OOXMLFormulaScanError("OOXML relationship source part is missing")
    return source_part


def _resolve_relationship_target(source_part: str, raw_target: str) -> str:
    parsed = urlsplit(raw_target)
    if (
        not raw_target
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in raw_target
    ):
        raise OOXMLFormulaScanError("OOXML relationship target is invalid")
    decoded = parsed.path
    if "%" in decoded or "\\" in decoded:
        raise OOXMLFormulaScanError("OOXML relationship target encoding is ambiguous")
    if decoded.startswith("/"):
        normalized = posixpath.normpath(decoded.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), decoded))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise OOXMLFormulaScanError("OOXML relationship target escapes the package")
    return normalized


def _parse_relationships(root: ET.Element, *, source_part: str) -> dict[str, _Relationship]:
    namespace, local_name = _split_qname(root.tag)
    if (
        namespace not in _PACKAGE_RELATIONSHIP_NAMESPACES
        or local_name != "Relationships"
        or root.attrib
    ):
        raise OOXMLFormulaScanError("OOXML relationships root is invalid")
    relationships: dict[str, _Relationship] = {}
    for child in root:
        child_namespace, child_name = _split_qname(child.tag)
        if (
            child_namespace != namespace
            or child_name != "Relationship"
            or len(child)
            or (child.text or "").strip()
            or (child.tail or "").strip()
        ):
            raise OOXMLFormulaScanError("OOXML relationship declaration is invalid")
        if not {"Id", "Type", "Target"}.issubset(child.attrib) or not set(child.attrib).issubset(
            {"Id", "Type", "Target", "TargetMode"}
        ):
            raise OOXMLFormulaScanError("OOXML relationship attributes are invalid")
        identifier = child.attrib["Id"]
        relationship_type = child.attrib["Type"]
        target_mode = child.attrib.get("TargetMode")
        if not identifier or not relationship_type or identifier in relationships:
            raise OOXMLFormulaScanError("OOXML relationship identity is ambiguous")
        if target_mode not in {None, "Internal", "External"}:
            raise OOXMLFormulaScanError("OOXML relationship target mode is invalid")
        external = target_mode == "External"
        target = (
            child.attrib["Target"]
            if external
            else _resolve_relationship_target(source_part, child.attrib["Target"])
        )
        relationships[identifier] = _Relationship(
            identifier=identifier,
            relationship_type=relationship_type,
            kind=relationship_type.rsplit("/", 1)[-1],
            target=target,
            external=external,
        )
    return relationships


def _validate_package_relationships(
    roots: dict[str, ET.Element],
    *,
    part_names: set[str],
    part_content_types: dict[str, str],
) -> None:
    relationship_parts = {
        name
        for name, content_type in part_content_types.items()
        if content_type == _RELATIONSHIPS_CONTENT_TYPE or name.casefold().endswith(".rels")
    }
    for name in relationship_parts:
        if part_content_types[name] != _RELATIONSHIPS_CONTENT_TYPE or name not in roots:
            raise OOXMLFormulaScanError("OOXML relationship part content type is invalid")
        source_part = _relationship_source(name, part_names=part_names)
        relationships = _parse_relationships(roots[name], source_part=source_part)
        for relationship in relationships.values():
            if relationship.kind.casefold() in _OPAQUE_FORMULA_CAPABLE_RELATIONSHIP_KINDS:
                raise OOXMLFormulaScanError(
                    "OOXML package contains an unsupported formula-capable relationship"
                )
            if relationship.relationship_type not in _SAFE_RELATIONSHIP_TYPES:
                raise OOXMLFormulaScanError(
                    "OOXML package contains an unsupported or ambiguous relationship"
                )
            if relationship.external:
                if relationship.kind != "hyperlink":
                    raise OOXMLFormulaScanError(
                        "OOXML relationship has an invalid external target mode"
                    )
                parsed_target = urlsplit(relationship.target)
                if (
                    not relationship.target
                    or any(ord(character) < 0x20 for character in relationship.target)
                    or not parsed_target.scheme
                ):
                    raise OOXMLFormulaScanError("OOXML external hyperlink target is invalid")
                continue
            if relationship.kind == "hyperlink":
                raise OOXMLFormulaScanError(
                    "OOXML relationship has an invalid internal target mode"
                )
            if relationship.target not in part_names:
                raise OOXMLFormulaScanError("OOXML relationship target part is missing")
            try:
                target_pattern, content_types, expected_roots = _RELATIONSHIP_TARGET_CONTRACTS[
                    relationship.kind
                ]
            except KeyError as exc:
                raise OOXMLFormulaScanError(
                    "OOXML relationship lacks an exact target contract"
                ) from exc
            if target_pattern.fullmatch(relationship.target) is None:
                raise OOXMLFormulaScanError(
                    "OOXML relationship target path does not match its type"
                )
            if part_content_types.get(relationship.target) not in content_types:
                raise OOXMLFormulaScanError(
                    "OOXML relationship target content type does not match its type"
                )
            if expected_roots is not None:
                try:
                    target_root = roots[relationship.target]
                except KeyError as exc:
                    raise OOXMLFormulaScanError(
                        "OOXML relationship target is not the required XML part"
                    ) from exc
                if _split_qname(target_root.tag) not in expected_roots:
                    raise OOXMLFormulaScanError(
                        "OOXML relationship target XML root does not match its type"
                    )


def _workbook_part(
    roots: dict[str, ET.Element],
    part_names: set[str],
    part_content_types: dict[str, str],
) -> str:
    if part_content_types.get(_ROOT_RELATIONSHIPS_PART) != _RELATIONSHIPS_CONTENT_TYPE:
        raise OOXMLFormulaScanError("OOXML root relationships content type is invalid")
    try:
        root_relationships = _parse_relationships(roots[_ROOT_RELATIONSHIPS_PART], source_part="")
    except KeyError as exc:
        raise OOXMLFormulaScanError("OOXML package is missing root relationships") from exc
    candidates = [
        item
        for item in root_relationships.values()
        if item.relationship_type in _OFFICE_DOCUMENT_RELATIONSHIP_TYPES
    ]
    if len(candidates) != 1 or candidates[0].external:
        raise OOXMLFormulaScanError("OOXML office-document relationship is ambiguous")
    workbook_part = candidates[0].target
    if workbook_part not in part_names or workbook_part not in roots:
        raise OOXMLFormulaScanError("OOXML workbook part is missing")
    return workbook_part


def _sheet_parts(
    roots: dict[str, ET.Element],
    *,
    workbook_part: str,
    part_names: set[str],
    part_content_types: dict[str, str],
) -> tuple[str, ...]:
    workbook = roots[workbook_part]
    namespace, local_name = _split_qname(workbook.tag)
    if namespace not in _SPREADSHEETML_NAMESPACES or local_name != "workbook":
        raise OOXMLFormulaScanError("OOXML workbook root is unsupported")
    relationships_name = _relationship_part(workbook_part)
    if part_content_types.get(relationships_name) != _RELATIONSHIPS_CONTENT_TYPE:
        raise OOXMLFormulaScanError("OOXML workbook relationships content type is invalid")
    try:
        relationships = _parse_relationships(roots[relationships_name], source_part=workbook_part)
    except KeyError as exc:
        raise OOXMLFormulaScanError("OOXML workbook relationships are missing") from exc
    sheet_nodes = [item for item in workbook.iter() if _local_name(item.tag) == "sheet"]
    if not sheet_nodes:
        raise OOXMLFormulaScanError("OOXML workbook has no sheets")
    identifiers: set[str] = set()
    sheet_ids: set[str] = set()
    sheet_name_aliases: set[str] = set()
    targets: list[str] = []
    for sheet in sheet_nodes:
        sheet_namespace, sheet_local_name = _split_qname(sheet.tag)
        sheet_name = sheet.attrib.get("name")
        sheet_id = sheet.attrib.get("sheetId")
        if (
            sheet_namespace != namespace
            or sheet_local_name != "sheet"
            or not isinstance(sheet_name, str)
            or not sheet_name
            or not isinstance(sheet_id, str)
            or not sheet_id.isdecimal()
            or int(sheet_id) < 1
        ):
            raise OOXMLFormulaScanError("OOXML sheet declaration is invalid")
        sheet_name_alias = unicodedata.normalize("NFC", sheet_name).casefold()
        if sheet_id in sheet_ids or sheet_name_alias in sheet_name_aliases:
            raise OOXMLFormulaScanError("OOXML sheet identity is ambiguous")
        sheet_ids.add(sheet_id)
        sheet_name_aliases.add(sheet_name_alias)
        relationship_ids = [
            value
            for attribute, value in sheet.attrib.items()
            if _split_qname(attribute)[0] in _OFFICE_RELATIONSHIP_NAMESPACES
            and _split_qname(attribute)[1] == "id"
        ]
        if len(relationship_ids) != 1 or relationship_ids[0] in identifiers:
            raise OOXMLFormulaScanError("OOXML sheet relationship is ambiguous")
        identifier = relationship_ids[0]
        identifiers.add(identifier)
        try:
            relationship = relationships[identifier]
        except KeyError as exc:
            raise OOXMLFormulaScanError("OOXML sheet relationship is missing") from exc
        if relationship.external:
            raise OOXMLFormulaScanError("External OOXML sheets are unsupported")
        if relationship.kind in {"macrosheet", "intlMacrosheet"}:
            raise OOXMLFormulaScanError("OOXML macro sheets cannot be certified formula-free")
        if relationship.kind not in _SUPPORTED_SHEET_RELATIONSHIP_KINDS:
            raise OOXMLFormulaScanError("OOXML sheet relationship type is unsupported")
        if relationship.relationship_type not in _SHEET_RELATIONSHIP_TYPES[relationship.kind]:
            raise OOXMLFormulaScanError("OOXML sheet relationship URI is unsupported")
        if relationship.target not in part_names or relationship.target not in roots:
            raise OOXMLFormulaScanError("OOXML sheet part is missing or non-XML")
        if (
            part_content_types.get(relationship.target)
            not in _SHEET_CONTENT_TYPES[relationship.kind]
        ):
            raise OOXMLFormulaScanError("OOXML sheet content type is invalid")
        sheet_namespace, sheet_root = _split_qname(roots[relationship.target].tag)
        if sheet_namespace not in _SPREADSHEETML_NAMESPACES or sheet_root != relationship.kind:
            raise OOXMLFormulaScanError("OOXML sheet root does not match its relationship")
        targets.append(relationship.target)
    if len(targets) != len(set(targets)):
        raise OOXMLFormulaScanError("OOXML sheet targets are duplicated")
    return tuple(targets)


def _worksheet_cell_count(root: ET.Element) -> int:
    namespace, local_name = _split_qname(root.tag)
    if namespace not in _SPREADSHEETML_NAMESPACES or local_name != "worksheet":
        return 0
    sheet_data = [child for child in root if _split_qname(child.tag) == (namespace, "sheetData")]
    if len(sheet_data) != 1:
        raise OOXMLFormulaScanError("OOXML worksheet sheetData is ambiguous")
    rows: set[int] = set()
    cells: set[str] = set()
    for row in sheet_data[0]:
        row_namespace, row_local_name = _split_qname(row.tag)
        if row_namespace != namespace or row_local_name != "row":
            raise OOXMLFormulaScanError("OOXML sheetData contains an unsupported element")
        row_number = row.attrib.get("r")
        if (
            not isinstance(row_number, str)
            or not row_number.isdecimal()
            or int(row_number) < 1
            or int(row_number) > 1_048_576
            or int(row_number) in rows
        ):
            raise OOXMLFormulaScanError("OOXML worksheet row identity is ambiguous")
        rows.add(int(row_number))
        for cell in row:
            cell_namespace, cell_local_name = _split_qname(cell.tag)
            if cell_namespace != namespace or cell_local_name != "c":
                raise OOXMLFormulaScanError("OOXML worksheet row contains an unsupported element")
            reference = cell.attrib.get("r")
            match = _CELL_REFERENCE.fullmatch(reference or "")
            if match is None or int(match.group(2)) != int(row_number):
                raise OOXMLFormulaScanError("OOXML worksheet cell identity is invalid")
            column_number = 0
            for character in match.group(1):
                column_number = column_number * 26 + ord(character) - ord("A") + 1
            if column_number > 16_384 or reference in cells:
                raise OOXMLFormulaScanError("OOXML worksheet cell identity is ambiguous")
            cells.add(reference)
    return len(cells)


def _scan_ooxml_snapshot(
    package_handle: BinaryIO,
    *,
    package_sha256: str,
    workbook_format: str,
) -> OOXMLFormulaScan:
    package_handle.seek(0)
    try:
        with ZipFile(package_handle) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_PARTS:
                raise OOXMLFormulaScanError("OOXML package part count is invalid")
            part_infos: dict[str, ZipInfo] = {}
            aliases: set[str] = set()
            total_size = 0
            for info in infos:
                name = _safe_part_name(info)
                if name is None:
                    continue
                alias = _part_alias(name)
                if name in part_infos or alias in aliases:
                    raise OOXMLFormulaScanError(
                        "OOXML package contains duplicate or aliased part names"
                    )
                total_size += info.file_size
                if total_size > _MAX_PACKAGE_BYTES:
                    raise OOXMLFormulaScanError("OOXML package exceeds the scan size limit")
                part_infos[name] = info
                aliases.add(alias)
            try:
                content_types_payload = _read_part(
                    archive,
                    part_infos[_CONTENT_TYPES_PART],
                    xml=True,
                )
            except KeyError as exc:
                raise OOXMLFormulaScanError("OOXML package is missing content types") from exc
            assert content_types_payload is not None
            content_types_root = _parse_xml(
                content_types_payload,
                part_name=_CONTENT_TYPES_PART,
            )
            defaults, overrides = _parse_content_types(content_types_root)
            if any(name not in part_infos for name in overrides):
                raise OOXMLFormulaScanError("OOXML content types reference a missing part")

            roots = {_CONTENT_TYPES_PART: content_types_root}
            part_content_types: dict[str, str] = {}
            formula_count = 0
            formula_kinds: set[str] = set()
            for name, info in part_infos.items():
                if name == _CONTENT_TYPES_PART:
                    continue
                content_type = _content_type_for(
                    name,
                    defaults=defaults,
                    overrides=overrides,
                )
                part_content_types[name] = content_type
                xml = _is_xml_part(name, content_type)
                _reject_opaque_formula_capable_part(name, content_type, xml=xml)
                payload = _read_part(archive, info, xml=xml)
                if not xml:
                    continue
                assert payload is not None
                root = _parse_xml(payload, part_name=name)
                roots[name] = root
                observed_count, observed_kinds = _formula_markers(root)
                formula_count += observed_count
                formula_kinds.update(observed_kinds)

            part_names = set(part_infos)
            workbook_part = _workbook_part(
                roots,
                part_names,
                part_content_types,
            )
            workbook_content_type = part_content_types[workbook_part]
            allowed_content_types = (
                _XLSM_WORKBOOK_CONTENT_TYPES
                if workbook_format == "xlsm"
                else _XLSX_WORKBOOK_CONTENT_TYPES
            )
            if workbook_content_type not in allowed_content_types:
                raise OOXMLFormulaScanError("OOXML workbook content type does not match its format")
            sheets = _sheet_parts(
                roots,
                workbook_part=workbook_part,
                part_names=part_names,
                part_content_types=part_content_types,
            )
            _validate_package_relationships(
                roots,
                part_names=part_names,
                part_content_types=part_content_types,
            )
            scanned_cells = sum(_worksheet_cell_count(roots[sheet_name]) for sheet_name in sheets)
            calc_chain_parts = {
                name
                for name, content_type in part_content_types.items()
                if "calcchain" in content_type.casefold()
                or posixpath.basename(name).casefold() == "calcchain.xml"
            }
            if calc_chain_parts:
                formula_count += len(calc_chain_parts)
                formula_kinds.add("calcchain")
    except OOXMLFormulaScanError:
        raise
    except (BadZipFile, LargeZipFile, EOFError, RuntimeError, OSError) as exc:
        raise OOXMLFormulaScanError("Invalid OOXML ZIP package") from exc

    return OOXMLFormulaScan(
        package_sha256=package_sha256,
        workbook_format=workbook_format,
        xml_part_count=len(roots),
        worksheet_count=len(sheets),
        scanned_cell_count=scanned_cells,
        formula_marker_count=formula_count,
        formula_kinds=tuple(sorted(formula_kinds)),
    )


def scan_ooxml_formula_bytes(
    payload: bytes,
    *,
    workbook_format: str,
) -> OOXMLFormulaScan:
    """Scan one immutable byte string and bind parsing to its exact SHA-256."""

    normalized_format = workbook_format.casefold().lstrip(".")
    if normalized_format not in {"xlsx", "xlsm"}:
        raise OOXMLFormulaScanError("Formula scanning supports only xlsx and xlsm")
    if not isinstance(payload, bytes):
        raise OOXMLFormulaScanError("OOXML byte snapshot must be immutable bytes")
    if not payload or len(payload) > OOXML_FORMULA_SCAN_MAX_FILE_BYTES:
        raise OOXMLFormulaScanError("OOXML workbook file size exceeds the scan limit")
    package_sha256 = hashlib.sha256(payload).hexdigest()
    return _scan_ooxml_snapshot(
        io.BytesIO(payload),
        package_sha256=package_sha256,
        workbook_format=normalized_format,
    )


def _stable_file_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _formula_scan_lease_hook(stage: str, path: Path) -> None:
    """A no-op synchronization point used by deterministic race tests."""


def _close_formula_scan_descriptors(
    workbook_descriptor: int | None,
    parent_descriptors: tuple[int, ...] | list[int],
) -> OSError | None:
    failure: OSError | None = None
    for descriptor in (
        *((workbook_descriptor,) if workbook_descriptor is not None else ()),
        *reversed(parent_descriptors),
    ):
        try:
            os.close(descriptor)
        except OSError as exc:
            failure = failure or exc
    return failure


def _verify_bound_parent_chain(
    parent_descriptors: tuple[int, ...] | list[int],
    parent_identities: tuple[tuple[int, int], ...] | list[tuple[int, int]],
    child_names: tuple[str, ...] | list[str],
) -> None:
    if not parent_descriptors or len(parent_descriptors) != len(parent_identities):
        raise OOXMLFormulaScanError("OOXML workbook parent lease is invalid")
    if len(child_names) + 1 != len(parent_descriptors):
        raise OOXMLFormulaScanError("OOXML workbook parent lease is incomplete")
    for descriptor, expected_identity in zip(
        parent_descriptors,
        parent_identities,
        strict=True,
    ):
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (
                metadata.st_dev,
                metadata.st_ino,
            )
            != expected_identity
        ):
            raise OOXMLFormulaScanError("OOXML workbook parent capability changed identity")
    for index, child_name in enumerate(child_names, start=1):
        metadata = os.stat(
            child_name,
            dir_fd=parent_descriptors[index - 1],
            follow_symlinks=False,
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (
                metadata.st_dev,
                metadata.st_ino,
            )
            != parent_identities[index]
        ):
            raise OOXMLFormulaScanError("OOXML workbook parent chain changed identity")


def _descriptor_snapshot(descriptor: int, *, expected_size: int) -> bytes:
    payload = bytearray()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(
            descriptor,
            min(_READ_CHUNK_BYTES, expected_size - offset),
            offset,
        )
        if not chunk:
            break
        payload.extend(chunk)
        offset += len(chunk)
    if offset != expected_size or os.pread(descriptor, 1, offset):
        raise OOXMLFormulaScanError("OOXML workbook changed during snapshotting")
    return bytes(payload)


class OOXMLFormulaScanLease:
    """Descriptor-bound scan evidence valid for the lifetime of this lease only."""

    def __init__(
        self,
        *,
        path: Path,
        scan: OOXMLFormulaScan,
        snapshot_bytes: bytes,
        workbook_descriptor: int,
        workbook_identity: tuple[int, ...],
        parent_descriptors: tuple[int, ...],
        parent_identities: tuple[tuple[int, int], ...],
        child_names: tuple[str, ...],
    ) -> None:
        self.path = path
        self.scan = scan
        self.snapshot_bytes = snapshot_bytes
        self.workbook_identity = workbook_identity
        self.parent_identities = parent_identities
        self._workbook_descriptor: int | None = workbook_descriptor
        self._parent_descriptors: tuple[int, ...] = parent_descriptors
        self._child_names = child_names

    @classmethod
    def open(cls, path: str | Path) -> OOXMLFormulaScanLease:
        raw_path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        workbook_format = raw_path.suffix.casefold().lstrip(".")
        if workbook_format not in {"xlsx", "xlsm"}:
            raise OOXMLFormulaScanError("Formula scanning supports only xlsx and xlsm")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_descriptors: list[int] = []
        parent_identities: list[tuple[int, int]] = []
        workbook_descriptor: int | None = None
        try:
            root_descriptor = os.open(os.path.sep, directory_flags)
            parent_descriptors.append(root_descriptor)
            root_metadata = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise OOXMLFormulaScanError("OOXML workbook root capability is invalid")
            parent_identities.append((root_metadata.st_dev, root_metadata.st_ino))
            child_names = tuple(raw_path.parent.parts[1:])
            for child_name in child_names:
                try:
                    descriptor = os.open(
                        child_name,
                        directory_flags,
                        dir_fd=parent_descriptors[-1],
                    )
                except OSError as exc:
                    raise OOXMLFormulaScanError(
                        "OOXML workbook parent chain must contain only real directories"
                    ) from exc
                parent_descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OOXMLFormulaScanError(
                        "OOXML workbook parent chain must contain only real directories"
                    )
                parent_identities.append((metadata.st_dev, metadata.st_ino))
            _formula_scan_lease_hook("parent_chain_opened", raw_path)
            _verify_bound_parent_chain(
                parent_descriptors,
                parent_identities,
                child_names,
            )
            named_metadata = os.stat(
                raw_path.name,
                dir_fd=parent_descriptors[-1],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(named_metadata.st_mode)
                or stat.S_ISLNK(named_metadata.st_mode)
                or named_metadata.st_nlink != 1
            ):
                raise OOXMLFormulaScanError(
                    "OOXML workbook must be a one-link regular non-symbolic file"
                )
            workbook_descriptor = os.open(
                raw_path.name,
                file_flags,
                dir_fd=parent_descriptors[-1],
            )
            opened_metadata = os.fstat(workbook_descriptor)
            workbook_identity = _stable_file_metadata(opened_metadata)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_nlink != 1
                or _stable_file_metadata(named_metadata) != workbook_identity
            ):
                raise OOXMLFormulaScanError(
                    "OOXML workbook must be a one-link regular non-symbolic file"
                )
            if (
                opened_metadata.st_size <= 0
                or opened_metadata.st_size > OOXML_FORMULA_SCAN_MAX_FILE_BYTES
            ):
                raise OOXMLFormulaScanError("OOXML workbook file size exceeds the scan limit")
            _formula_scan_lease_hook("workbook_opened", raw_path)
            _verify_bound_parent_chain(
                parent_descriptors,
                parent_identities,
                child_names,
            )
            payload = _descriptor_snapshot(
                workbook_descriptor,
                expected_size=opened_metadata.st_size,
            )
            _formula_scan_lease_hook("snapshot_copied", raw_path)
            copied_metadata = os.fstat(workbook_descriptor)
            rebound_metadata = os.stat(
                raw_path.name,
                dir_fd=parent_descriptors[-1],
                follow_symlinks=False,
            )
            if (
                _stable_file_metadata(copied_metadata) != workbook_identity
                or _stable_file_metadata(rebound_metadata) != workbook_identity
            ):
                raise OOXMLFormulaScanError("OOXML workbook changed during snapshotting")
            _verify_bound_parent_chain(
                parent_descriptors,
                parent_identities,
                child_names,
            )
            package_sha256 = hashlib.sha256(payload).hexdigest()
            scan = _scan_ooxml_snapshot(
                io.BytesIO(payload),
                package_sha256=package_sha256,
                workbook_format=workbook_format,
            )
            lease = cls(
                path=raw_path,
                scan=scan,
                snapshot_bytes=payload,
                workbook_descriptor=workbook_descriptor,
                workbook_identity=workbook_identity,
                parent_descriptors=tuple(parent_descriptors),
                parent_identities=tuple(parent_identities),
                child_names=child_names,
            )
            _formula_scan_lease_hook("scan_complete", raw_path)
            lease.verify_binding()
            workbook_descriptor = None
            parent_descriptors = []
            return lease
        except OOXMLFormulaScanError:
            raise
        except OSError as exc:
            raise OOXMLFormulaScanError("OOXML workbook could not be safely opened") from exc
        finally:
            close_error = _close_formula_scan_descriptors(
                workbook_descriptor,
                parent_descriptors,
            )
            if close_error is not None:
                raise OOXMLFormulaScanError(
                    f"OOXML workbook descriptor cleanup failed: {close_error}"
                )

    @property
    def closed(self) -> bool:
        return self._workbook_descriptor is None

    def verify_binding(self, *, checkpoint: str | None = None) -> None:
        descriptor = self._workbook_descriptor
        if descriptor is None:
            raise OOXMLFormulaScanError("OOXML formula scan lease is closed")
        if checkpoint is not None:
            _formula_scan_lease_hook(checkpoint, self.path)
        try:
            _verify_bound_parent_chain(
                self._parent_descriptors,
                self.parent_identities,
                self._child_names,
            )
            descriptor_metadata = os.fstat(descriptor)
            named_metadata = os.stat(
                self.path.name,
                dir_fd=self._parent_descriptors[-1],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(descriptor_metadata.st_mode)
                or descriptor_metadata.st_nlink != 1
                or _stable_file_metadata(descriptor_metadata) != self.workbook_identity
                or _stable_file_metadata(named_metadata) != self.workbook_identity
            ):
                raise OOXMLFormulaScanError("OOXML workbook changed after snapshotting")
            rebound = _descriptor_snapshot(
                descriptor,
                expected_size=descriptor_metadata.st_size,
            )
            if (
                hashlib.sha256(rebound).hexdigest() != self.scan.package_sha256
                or rebound != self.snapshot_bytes
            ):
                raise OOXMLFormulaScanError("OOXML workbook bytes changed after snapshotting")
            final_descriptor_metadata = os.fstat(descriptor)
            final_named_metadata = os.stat(
                self.path.name,
                dir_fd=self._parent_descriptors[-1],
                follow_symlinks=False,
            )
            if (
                _stable_file_metadata(final_descriptor_metadata) != self.workbook_identity
                or _stable_file_metadata(final_named_metadata) != self.workbook_identity
            ):
                raise OOXMLFormulaScanError("OOXML workbook changed after snapshotting")
            _verify_bound_parent_chain(
                self._parent_descriptors,
                self.parent_identities,
                self._child_names,
            )
        except OOXMLFormulaScanError:
            raise
        except OSError as exc:
            raise OOXMLFormulaScanError("OOXML workbook binding could not be verified") from exc

    def close(self) -> None:
        descriptor = self._workbook_descriptor
        parent_descriptors = self._parent_descriptors
        self._workbook_descriptor = None
        self._parent_descriptors = ()
        close_error = _close_formula_scan_descriptors(descriptor, parent_descriptors)
        if close_error is not None:
            raise OOXMLFormulaScanError(f"OOXML workbook descriptor cleanup failed: {close_error}")

    def __enter__(self) -> OOXMLFormulaScanLease:
        if self.closed:
            raise OOXMLFormulaScanError("OOXML formula scan lease is closed")
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> bool:
        try:
            if exception_type is None:
                self.verify_binding(checkpoint="before_lease_exit")
        finally:
            self.close()
        return False


def scan_ooxml_formulas(path: str | Path) -> OOXMLFormulaScan:
    """Return inode/byte snapshot evidence; no path-stability claim survives return."""

    with OOXMLFormulaScanLease.open(path) as lease:
        return lease.scan


__all__ = [
    "OOXML_FORMULA_SCAN_SCHEMA_VERSION",
    "OOXML_FORMULA_POLICY_VERSION",
    "OOXML_FORMULA_SCAN_MAX_FILE_BYTES",
    "OOXML_NO_FORMULA_BACKEND",
    "OOXML_NO_FORMULA_PROFILE",
    "OOXMLFormulaScan",
    "OOXMLFormulaScanLease",
    "OOXMLFormulaScanError",
    "scan_ooxml_formula_bytes",
    "scan_ooxml_formulas",
]
