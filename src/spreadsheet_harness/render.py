"""Safe LibreOffice rendering for spreadsheet workbooks.

The source workbook is never passed to LibreOffice directly.  Every conversion
uses a private copy and a fresh ``UserInstallation`` directory so concurrent
renders cannot share a LibreOffice profile or modify the caller's workbook.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import tempfile
import textwrap
import time
import zipfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from .errors import RecalculationIntegrityError, RenderError, ScoringInfrastructureError

_CELL_REFERENCE_RE = re.compile(r"(?P<column>[A-Z]{1,3})(?P<row>\d+)$", re.IGNORECASE)


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True

SUPPORTED_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".ods", ".xls", ".csv"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RECALCULATION_FORMATS = {
    ".xlsx": "xlsx:Calc MS Excel 2007 XML",
    ".xlsm": "xlsm:Calc MS Excel 2007 VBA XML",
    ".ods": "ods:calc8",
    ".xls": "xls:MS Excel 97",
    ".csv": "csv:Text - txt - csv (StarCalc)",
}
RECALCULATION_SHEET_INTEGRITY_POLICY = "exact-ordered-sheet-kind-name-visibility-v2"
_SHEET_INVENTORY_FORMATS = frozenset({".xlsx", ".xlsm"})
_WORKBOOK_XML_PART = "xl/workbook.xml"
_WORKBOOK_RELATIONSHIPS_PART = "xl/_rels/workbook.xml.rels"
_STYLES_XML_PART = "xl/styles.xml"
_SHARED_STRINGS_XML_PART = "xl/sharedStrings.xml"
_CONTENT_TYPES_PART = "[Content_Types].xml"
_OOXML_INVENTORY_PART_MAX_BYTES = 8 * 1024 * 1024
_TRANSITIONAL_SPREADSHEETML_NAMESPACE = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
)
_STRICT_SPREADSHEETML_NAMESPACE = "http://purl.oclc.org/ooxml/spreadsheetml/main"
_RELATIONSHIP_NAMESPACES = {
    _TRANSITIONAL_SPREADSHEETML_NAMESPACE: (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ),
    _STRICT_SPREADSHEETML_NAMESPACE: (
        "http://purl.oclc.org/ooxml/officeDocument/relationships"
    ),
}
_PACKAGE_RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/package/2006/relationships",
        "http://purl.oclc.org/ooxml/package/relationships",
    }
)
_CONTENT_TYPE_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/package/2006/content-types",
        "http://purl.oclc.org/ooxml/package/content-types",
    }
)
_SHEET_RELATIONSHIP_KINDS = frozenset(
    {"worksheet", "chartsheet", "dialogsheet", "macrosheet", "intlMacrosheet"}
)
_SHEET_RELATIONSHIP_TYPE_PREFIXES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/",
)
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")
_LOCAL_FORMULA_REFERENCE = re.compile(
    r"(?<![A-Z0-9_!])\$?([A-Z]{1,3})\$?([1-9][0-9]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RenderPage:
    """One rasterized PDF page."""

    index: int
    path: Path
    sha256: str
    width: int
    height: int
    sheet: str | None = None
    sheet_page: int | None = None

    def to_dict(self, *, relative_to: Path | None = None) -> dict[str, Any]:
        path = self.path
        if relative_to is not None:
            try:
                path = path.relative_to(relative_to)
            except ValueError:
                pass
        return {
            "index": self.index,
            "page": self.sheet_page if self.sheet_page is not None else self.index,
            "path": path.as_posix(),
            "image_path": str(self.path.resolve()),
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "sheet": self.sheet,
            "sheet_page": self.sheet_page,
        }


@dataclass(frozen=True)
class RenderResult:
    """Published render artifacts and their reproducibility metadata."""

    source: Path
    output_dir: Path
    manifest_path: Path
    backend: str
    version: dict[str, str]
    source_sha256: str
    mode: str
    dpi: int
    pages: tuple[RenderPage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source": {
                "name": self.source.name,
                "format": self.source.suffix.lower().lstrip("."),
                "sha256": self.source_sha256,
            },
            "backend": self.backend,
            "version": dict(self.version),
            "hash": self.source_sha256,
            "mode": self.mode,
            "dpi": self.dpi,
            "manifest_path": str(self.manifest_path.resolve()),
            "page_count": len(self.pages),
            "pages": [page.to_dict(relative_to=self.output_dir) for page in self.pages],
        }


@dataclass(frozen=True)
class _RasterizedPage:
    path: Path
    width: int
    height: int
    sheet: str | None
    sheet_page: int | None


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of *path* without loading it all in memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sheet_inventory_sha256(sheets: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        sheets,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_unique_inventory_part(
    package: zipfile.ZipFile,
    part_name: str,
) -> bytes:
    matches = [member for member in package.infolist() if member.filename == part_name]
    if len(matches) != 1:
        raise RenderError(
            f"OOXML package must contain exactly one {part_name}; found {len(matches)}"
        )
    member = matches[0]
    if member.is_dir() or member.flag_bits & 0x1:
        raise RenderError(f"OOXML part {part_name} is not a readable regular ZIP member")
    if (
        member.file_size <= 0
        or member.file_size > _OOXML_INVENTORY_PART_MAX_BYTES
    ):
        raise RenderError(
            f"OOXML part {part_name} size is outside the accepted bound of "
            f"1..{_OOXML_INVENTORY_PART_MAX_BYTES} bytes"
        )
    with package.open(member) as handle:
        raw = handle.read(_OOXML_INVENTORY_PART_MAX_BYTES + 1)
    if (
        len(raw) != member.file_size
        or len(raw) > _OOXML_INVENTORY_PART_MAX_BYTES
    ):
        raise RenderError(f"OOXML part {part_name} size does not match its ZIP metadata")
    return raw


def _read_sheet_inventory_parts(workbook_path: Path) -> tuple[bytes, bytes]:
    """Read the unique bounded inventory parts without extracting ZIP members."""

    try:
        with zipfile.ZipFile(workbook_path) as package:
            workbook_xml = _read_unique_inventory_part(package, _WORKBOOK_XML_PART)
            relationships_xml = _read_unique_inventory_part(
                package,
                _WORKBOOK_RELATIONSHIPS_PART,
            )
            return workbook_xml, relationships_xml
    except RenderError:
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
        raise RenderError(
            f"Could not read OOXML workbook package: {type(exc).__name__}: {exc}"
        ) from exc


def _xml_namespace(tag: Any) -> str | None:
    if not isinstance(tag, str) or not tag.startswith("{") or "}" not in tag:
        return None
    return tag[1 : tag.index("}")]


def _xml_local_name(tag: Any) -> str | None:
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _parse_inventory_xml(xml: bytes, *, label: str) -> ElementTree.Element:
    # ElementTree does not fetch external resources, and rejecting DTD/entity
    # declarations also prevents internal entity-expansion payloads.
    declaration_scan = xml.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise RenderError(f"OOXML {label} XML must not contain DTD or entity declarations")
    try:
        return ElementTree.fromstring(xml)
    except (ElementTree.ParseError, LookupError, ValueError) as exc:
        raise RenderError(f"OOXML {label} XML is malformed: {exc}") from exc


def _parse_workbook_relationships(
    relationships_xml: bytes,
) -> dict[str, dict[str, str]]:
    root = _parse_inventory_xml(relationships_xml, label="workbook relationships")
    namespace = _xml_namespace(root.tag)
    if (
        namespace not in _PACKAGE_RELATIONSHIP_NAMESPACES
        or root.tag != f"{{{namespace}}}Relationships"
    ):
        raise RenderError("OOXML workbook relationships use an unsupported namespace")

    relationship_tag = f"{{{namespace}}}Relationship"
    relationships: dict[str, dict[str, str]] = {}
    for relationship in root:
        if relationship.tag != relationship_tag or list(relationship):
            raise RenderError("OOXML workbook relationships contain an invalid child")
        relationship_id = relationship.attrib.get("Id")
        relationship_type = relationship.attrib.get("Type")
        target = relationship.attrib.get("Target")
        target_mode = relationship.attrib.get("TargetMode", "Internal")
        if not isinstance(relationship_id, str) or not relationship_id:
            raise RenderError("OOXML workbook relationship is missing a non-empty Id")
        if relationship_id in relationships:
            raise RenderError("OOXML workbook relationships contain duplicate Id values")
        if not isinstance(relationship_type, str) or not relationship_type:
            raise RenderError("OOXML workbook relationship is missing a non-empty Type")
        if not isinstance(target, str) or not target:
            raise RenderError("OOXML workbook relationship is missing a non-empty Target")
        if target_mode not in {"Internal", "External"}:
            raise RenderError("OOXML workbook relationship has an invalid TargetMode")
        relationships[relationship_id] = {
            "type": relationship_type,
            "target": target,
            "target_mode": target_mode,
        }
    return relationships


def _sheet_kind(relationship: dict[str, str]) -> str:
    if relationship["target_mode"] != "Internal":
        raise RenderError("OOXML sheet relationship must target an internal package part")
    relationship_type = relationship["type"]
    for prefix in _SHEET_RELATIONSHIP_TYPE_PREFIXES:
        if relationship_type.startswith(prefix):
            kind = relationship_type[len(prefix) :]
            if kind in _SHEET_RELATIONSHIP_KINDS:
                return kind
            break
    raise RenderError("OOXML sheet relationship has an unsupported sheet type")


def _parse_sheet_inventory(
    workbook_xml: bytes,
    relationships_xml: bytes,
) -> list[dict[str, Any]]:
    root = _parse_inventory_xml(workbook_xml, label="workbook")
    relationships = _parse_workbook_relationships(relationships_xml)

    namespace = _xml_namespace(root.tag)
    if (
        namespace not in _RELATIONSHIP_NAMESPACES
        or root.tag != f"{{{namespace}}}workbook"
    ):
        raise RenderError("OOXML workbook root uses an unsupported SpreadsheetML namespace")

    sheets_tag = f"{{{namespace}}}sheets"
    sheet_tag = f"{{{namespace}}}sheet"
    sheets_nodes = [child for child in root if child.tag == sheets_tag]
    if len(sheets_nodes) != 1:
        raise RenderError(
            f"OOXML workbook must contain exactly one sheets element; found {len(sheets_nodes)}"
        )
    if any(
        _xml_local_name(child.tag) == "sheets" and child.tag != sheets_tag
        for child in root
    ):
        raise RenderError("OOXML workbook contains a sheets element in the wrong namespace")

    relationship_attribute = f"{{{_RELATIONSHIP_NAMESPACES[namespace]}}}id"
    sheets: list[dict[str, Any]] = []
    names: set[str] = set()
    sheet_ids: set[str] = set()
    relationship_ids: set[str] = set()
    for index, sheet in enumerate(sheets_nodes[0]):
        if sheet.tag != sheet_tag:
            raise RenderError("OOXML sheets element contains a non-sheet child")
        if list(sheet):
            raise RenderError("OOXML sheet records must not contain child elements")

        name = sheet.attrib.get("name")
        sheet_id = sheet.attrib.get("sheetId")
        relationship_id = sheet.attrib.get(relationship_attribute)
        visibility = sheet.attrib.get("state", "visible")
        if not isinstance(name, str) or not name:
            raise RenderError("OOXML sheet record is missing a non-empty name")
        normalized_name = name.casefold()
        if normalized_name in names:
            raise RenderError("OOXML workbook contains duplicate sheet names")
        if not isinstance(sheet_id, str) or _POSITIVE_INTEGER.fullmatch(sheet_id) is None:
            raise RenderError("OOXML sheet record has an invalid or missing sheetId")
        if sheet_id in sheet_ids:
            raise RenderError("OOXML workbook contains duplicate sheetId values")
        if not isinstance(relationship_id, str) or not relationship_id:
            raise RenderError(
                "OOXML sheet record has an invalid or missing relationship identifier"
            )
        if relationship_id in relationship_ids:
            raise RenderError("OOXML workbook contains duplicate sheet relationships")
        relationship = relationships.get(relationship_id)
        if relationship is None:
            raise RenderError("OOXML sheet references a missing workbook relationship")
        kind = _sheet_kind(relationship)
        if visibility not in {"visible", "hidden", "veryHidden"}:
            raise RenderError("OOXML sheet record has an invalid visibility state")

        names.add(normalized_name)
        sheet_ids.add(sheet_id)
        relationship_ids.add(relationship_id)
        sheets.append(
            {
                "index": index,
                "kind": kind,
                "name": name,
                "visibility": visibility,
            }
        )

    if not sheets:
        raise RenderError("OOXML workbook contains no sheets")
    if not any(sheet["visibility"] == "visible" for sheet in sheets):
        raise RenderError("OOXML workbook contains no visible sheet")
    return sheets


def sheet_inventory_identity(path: str | Path) -> dict[str, Any]:
    """Return ordered sheet kinds, names, and visibility bound to the file hash."""

    workbook_path = Path(path).expanduser().resolve()
    suffix = workbook_path.suffix.lower()
    if suffix not in _SHEET_INVENTORY_FORMATS:
        supported = ", ".join(sorted(_SHEET_INVENTORY_FORMATS))
        raise RenderError(
            f"Sheet inventory integrity requires {supported}; got {suffix!r}"
        )
    workbook_sha256 = sha256_file(workbook_path)
    try:
        workbook_xml, relationships_xml = _read_sheet_inventory_parts(workbook_path)
        sheets = _parse_sheet_inventory(workbook_xml, relationships_xml)
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"Could not read workbook sheet inventory: {exc}") from exc
    if sha256_file(workbook_path) != workbook_sha256:
        raise RenderError("Workbook changed while its sheet inventory was being read")
    return {
        "schema_version": 2,
        "workbook_sha256": workbook_sha256,
        "inventory_sha256": _sheet_inventory_sha256(sheets),
        "sheets": sheets,
    }


def _relationship_target_part(source_part: str, target: str) -> str:
    decoded = unquote(target)
    if not decoded or "\\" in decoded or "\x00" in decoded:
        raise RenderError("OOXML sheet relationship target is not a valid package path")
    if decoded.startswith("/"):
        resolved = posixpath.normpath(decoded.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_part), decoded)
        )
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise RenderError("OOXML sheet relationship target escapes the package")
    return resolved


def _part_relationships_name(part_name: str) -> str:
    directory, filename = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _worksheet_only_package_parts(
    workbook_xml: bytes,
    relationships_xml: bytes,
    content_types_xml: bytes,
    sheets: list[dict[str, Any]],
) -> tuple[bytes, bytes, bytes, set[str]]:
    workbook_root = _parse_inventory_xml(workbook_xml, label="workbook")
    workbook_namespace = _xml_namespace(workbook_root.tag)
    if workbook_namespace not in _RELATIONSHIP_NAMESPACES:
        raise RenderError("OOXML workbook root uses an unsupported SpreadsheetML namespace")
    sheets_node = workbook_root.find(f"{{{workbook_namespace}}}sheets")
    if sheets_node is None or len(sheets_node) != len(sheets):
        raise RenderError("OOXML workbook sheet inventory changed while building scorer view")
    relationship_attribute = (
        f"{{{_RELATIONSHIP_NAMESPACES[workbook_namespace]}}}id"
    )
    removed_relationship_ids = {
        node.attrib.get(relationship_attribute)
        for node, sheet in zip(list(sheets_node), sheets, strict=True)
        if sheet["kind"] != "worksheet"
    }
    if None in removed_relationship_ids:
        raise RenderError("OOXML non-worksheet sheet is missing its relationship identifier")
    for node, sheet in zip(list(sheets_node), sheets, strict=True):
        if sheet["kind"] != "worksheet":
            sheets_node.remove(node)

    relationships_root = _parse_inventory_xml(
        relationships_xml,
        label="workbook relationships",
    )
    removed_parts: set[str] = set()
    removed_relationship_count = 0
    for relationship in list(relationships_root):
        if relationship.attrib.get("Id") not in removed_relationship_ids:
            continue
        removed_relationship_count += 1
        removed_part = _relationship_target_part(
            _WORKBOOK_XML_PART,
            relationship.attrib.get("Target", ""),
        )
        removed_parts.add(removed_part)
        removed_parts.add(_part_relationships_name(removed_part))
        relationships_root.remove(relationship)
    if removed_relationship_count != len(removed_relationship_ids):
        raise RenderError("OOXML non-worksheet relationship set changed during scorer view")

    content_types_root = _parse_inventory_xml(content_types_xml, label="content types")
    content_types_namespace = _xml_namespace(content_types_root.tag)
    if (
        content_types_namespace not in _CONTENT_TYPE_NAMESPACES
        or content_types_root.tag != f"{{{content_types_namespace}}}Types"
    ):
        raise RenderError("OOXML content types use an unsupported namespace")
    override_tag = f"{{{content_types_namespace}}}Override"
    for child in list(content_types_root):
        part_name = child.attrib.get("PartName") if child.tag == override_tag else None
        if isinstance(part_name, str) and part_name.lstrip("/") in removed_parts:
            content_types_root.remove(child)

    return (
        ElementTree.tostring(workbook_root, encoding="utf-8", xml_declaration=True),
        ElementTree.tostring(
            relationships_root,
            encoding="utf-8",
            xml_declaration=True,
        ),
        ElementTree.tostring(
            content_types_root,
            encoding="utf-8",
            xml_declaration=True,
        ),
        removed_parts,
    )


@contextmanager
def openpyxl_worksheet_view(
    path: str | Path,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield an immutable OOXML view containing only worksheet sheet records."""

    workbook_path = Path(path).expanduser().resolve()
    identity = sheet_inventory_identity(workbook_path)
    sheets = identity["sheets"]
    if all(sheet["kind"] == "worksheet" for sheet in sheets):
        yield workbook_path, identity
        return
    if not any(sheet["kind"] == "worksheet" for sheet in sheets):
        raise ScoringInfrastructureError(
            "The workbook contains no worksheet that the cell scorer can read"
        )

    try:
        with zipfile.ZipFile(workbook_path) as source_package:
            workbook_xml = _read_unique_inventory_part(
                source_package,
                _WORKBOOK_XML_PART,
            )
            relationships_xml = _read_unique_inventory_part(
                source_package,
                _WORKBOOK_RELATIONSHIPS_PART,
            )
            content_types_xml = _read_unique_inventory_part(
                source_package,
                _CONTENT_TYPES_PART,
            )
            (
                worksheet_workbook_xml,
                worksheet_relationships_xml,
                worksheet_content_types_xml,
                removed_parts,
            ) = _worksheet_only_package_parts(
                workbook_xml,
                relationships_xml,
                content_types_xml,
                sheets,
            )
            member_counts: dict[str, int] = {}
            for member in source_package.infolist():
                member_counts[member.filename] = member_counts.get(member.filename, 0) + 1
            for part in removed_parts:
                if part.endswith(".rels"):
                    continue
                matches = member_counts.get(part, 0)
                if matches != 1:
                    raise RenderError(
                        "OOXML non-worksheet relationship target must resolve to exactly "
                        f"one package part; {part} resolved to {matches}"
                    )

            with tempfile.TemporaryDirectory(prefix="sheet-harness-worksheet-view-") as raw:
                view_path = Path(raw) / f"workbook{workbook_path.suffix.lower()}"
                replacements = {
                    _WORKBOOK_XML_PART: worksheet_workbook_xml,
                    _WORKBOOK_RELATIONSHIPS_PART: worksheet_relationships_xml,
                    _CONTENT_TYPES_PART: worksheet_content_types_xml,
                }
                with zipfile.ZipFile(view_path, "w") as view_package:
                    for member in source_package.infolist():
                        if member.filename in removed_parts:
                            continue
                        data = replacements.get(member.filename)
                        if data is None:
                            with source_package.open(member) as source:
                                data = source.read()
                        view_package.writestr(member, data)
                if sha256_file(workbook_path) != identity["workbook_sha256"]:
                    raise RenderError(
                        "Workbook changed while its worksheet-only scorer view was built"
                    )
                yield view_path, identity
    except (RenderError, ScoringInfrastructureError):
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
        raise RenderError(
            f"Could not build OOXML worksheet-only scorer view: {type(exc).__name__}: {exc}"
        ) from exc


def _publish_recalculation_failure_artifact(
    converted: Path,
    destination: Path,
    output_sha256: str,
) -> Path:
    evidence_path = destination.with_name(
        f"{destination.stem}.recalculation-integrity-failure-"
        f"{output_sha256}{destination.suffix}"
    )
    if evidence_path.exists():
        if evidence_path.is_symlink() or not evidence_path.is_file():
            raise RenderError(
                f"Recalculation failure evidence path is not a regular file: {evidence_path}"
            )
        if sha256_file(evidence_path) != output_sha256:
            raise RenderError(
                f"Recalculation failure evidence path has conflicting content: {evidence_path}"
            )
        return evidence_path

    descriptor, raw_temporary = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.stem}.recalculation-integrity-failure-",
        suffix=destination.suffix,
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        shutil.copy2(converted, temporary)
        _validate_recalculated_file(temporary)
        if sha256_file(temporary) != output_sha256:
            raise RenderError("Recalculation failure evidence changed while being published")
        temporary.replace(evidence_path)
    finally:
        temporary.unlink(missing_ok=True)
    return evidence_path


def find_libreoffice(explicit: str | Path | None = None) -> str | None:
    """Locate a LibreOffice/soffice executable, returning ``None`` if absent."""

    if explicit is not None:
        value = str(explicit)
        resolved = shutil.which(value)
        if resolved:
            return resolved
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        return None

    for name in ("libreoffice", "soffice"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    # Helpful for local development on macOS; Linux normally resolves via PATH.
    macos_binary = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if macos_binary.is_file():
        return str(macos_binary)
    return None


_ITERATIVE_CALC_PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Calc/Calculate/IterativeReference">
  <prop oor:name="Iteration" oor:op="fuse"><value>true</value></prop>
  <prop oor:name="Steps" oor:op="fuse"><value>100</value></prop>
  <prop oor:name="MinimumChange" oor:op="fuse"><value>0.0001</value></prop>
 </item>
</oor:items>
"""


@contextmanager
def isolated_user_profile(
    *, iterative_calculation: bool = False
) -> Iterator[tuple[Path, str]]:
    """Yield a fresh LibreOffice profile directory and its required file URI."""

    with tempfile.TemporaryDirectory(prefix="spreadsheet-lo-profile-") as raw_profile:
        profile = Path(raw_profile).resolve()
        if iterative_calculation:
            user = profile / "user"
            user.mkdir(parents=True, exist_ok=True)
            (user / "registrymodifications.xcu").write_text(
                _ITERATIVE_CALC_PROFILE,
                encoding="utf-8",
            )
        yield profile, profile.as_uri()


def libreoffice_command(
    binary: str,
    source: Path,
    output_dir: Path,
    target_format: str,
    profile_uri: str,
) -> list[str]:
    """Build a non-interactive conversion command with an isolated profile."""

    return [
        binary,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_uri}",
        "--convert-to",
        target_format,
        "--outdir",
        str(output_dir),
        str(source),
    ]


def libreoffice_version(binary: str, *, timeout_seconds: float = 30.0) -> str:
    """Return a concise LibreOffice version string."""

    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    version = (completed.stdout or completed.stderr).strip()
    return version or "unknown"


def _converted_candidates(output_dir: Path, stem: str, suffix: str) -> list[Path]:
    expected = output_dir / f"{stem}{suffix}"
    if expected.is_file():
        return [expected]
    return sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file() and path.stem.casefold() == stem.casefold() and path.suffix == suffix
    )


def _libreoffice_uno_environment(binary: str) -> tuple[str, dict[str, str]]:
    """Resolve a Python/UNO runtime matching the selected LibreOffice binary."""

    program_dir = Path(binary).resolve().parent
    candidates = [
        program_dir / "python",
        program_dir / "python.exe",
        Path("/usr/bin/python3"),
    ]
    path_python = shutil.which("python3")
    if path_python:
        candidates.append(Path(path_python))
    python = next(
        (str(candidate) for candidate in candidates if candidate.is_file()),
        None,
    )
    if python is None:
        raise RenderError("A Python runtime for LibreOffice UNO was not found")

    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(program_dir), existing_pythonpath) if part
    )
    environment["UNO_PATH"] = str(program_dir)
    bootstrap = program_dir / "fundamentalrc"
    if bootstrap.is_file():
        environment["URE_BOOTSTRAP"] = f"vnd.sun.star.pathname:{bootstrap}"
    return python, environment


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


_LIBREOFFICE_CALCULATE_ALL_SCRIPT = textwrap.dedent(
    """
    import sys
    import time
    import uno
    from com.sun.star.beans import PropertyValue

    port = int(sys.argv[1])
    source_url = sys.argv[2]
    destination_url = sys.argv[3]
    filter_name = sys.argv[4]
    iterative = sys.argv[5] == "1"
    startup_timeout = float(sys.argv[6])

    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    deadline = time.monotonic() + startup_timeout
    while True:
        try:
            context = resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={port};urp;"
                "StarOffice.ComponentContext"
            )
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)

    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )
    document = None
    try:
        load_properties = (
            PropertyValue(Name="Hidden", Value=True),
            PropertyValue(Name="ReadOnly", Value=False),
        )
        document = desktop.loadComponentFromURL(
            source_url, "_blank", 0, load_properties
        )
        if document is None:
            raise RuntimeError("LibreOffice did not open the spreadsheet")
        document.enableAutomaticCalculation(True)
        document.IsIterationEnabled = iterative
        if iterative:
            document.IterationCount = 100
            document.IterationEpsilon = 0.0001
        document.calculateAll()
        save_properties = (
            PropertyValue(Name="FilterName", Value=filter_name),
            PropertyValue(Name="Overwrite", Value=True),
        )
        document.storeToURL(destination_url, save_properties)
        print("CALCULATE_ALL_OK", flush=True)
    finally:
        if document is not None:
            document.close(True)
        desktop.terminate()
    """
).strip()


def _calculate_all_with_libreoffice(
    source_copy: Path,
    output_dir: Path,
    *,
    target_format: str,
    binary: str,
    timeout_seconds: float,
    iterative_calculation: bool,
) -> Path:
    """Open, fully calculate, and save a spreadsheet through an isolated UNO service."""

    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_format = target_format.split(":", 1)[0].lower().lstrip(".")
    suffix = f".{normalized_format}"
    filter_name = target_format.split(":", 1)[1] if ":" in target_format else ""
    if not filter_name:
        raise RenderError(
            f"LibreOffice calculateAll requires an explicit export filter: {target_format}"
        )
    destination = (output_dir / f"{source_copy.stem}{suffix}").resolve()
    python, environment = _libreoffice_uno_environment(binary)
    deadline = time.monotonic() + timeout_seconds
    failures: list[str] = []
    for attempt in range(1, 4):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        destination.unlink(missing_ok=True)
        port = _reserve_loopback_port()
        completed = None
        listener_stdout = ""
        listener_stderr = ""
        with isolated_user_profile(
            iterative_calculation=iterative_calculation
        ) as (_, profile_uri):
            listener_command = [
                binary,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                "--norestore",
                f"-env:UserInstallation={profile_uri}",
                f"--accept=socket,host=127.0.0.1,port={port};urp;",
            ]
            listener = None
            try:
                listener = subprocess.Popen(
                    listener_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                startup_timeout = min(15.0, max(1.0, remaining / 2))
                completed = subprocess.run(
                    [
                        python,
                        "-c",
                        _LIBREOFFICE_CALCULATE_ALL_SCRIPT,
                        str(port),
                        source_copy.resolve().as_uri(),
                        destination.as_uri(),
                        filter_name,
                        "1" if iterative_calculation else "0",
                        str(startup_timeout),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, remaining),
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                failures.append(f"attempt {attempt}: calculation timed out")
            except OSError as exc:
                failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            finally:
                if listener is not None:
                    try:
                        listener_stdout, listener_stderr = listener.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        listener.terminate()
                        try:
                            listener_stdout, listener_stderr = listener.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            listener.kill()
                            listener_stdout, listener_stderr = listener.communicate(timeout=5)

        succeeded = bool(
            completed is not None
            and completed.returncode == 0
            and "CALCULATE_ALL_OK" in completed.stdout
            and destination.is_file()
        )
        if succeeded:
            return destination
        if completed is not None:
            details = (completed.stderr or completed.stdout).strip()
            if details:
                failures.append(f"attempt {attempt} client: {details[-500:]}")
        listener_details = (listener_stderr or listener_stdout).strip()
        if listener_details:
            failures.append(f"attempt {attempt} listener: {listener_details[-500:]}")

    detail = "; ".join(failures[-6:])
    message = (
        f"LibreOffice failed to calculate {source_copy.name} after "
        f"{min(3, max(1, len(failures)))} attempts"
    )
    if detail:
        message += f": {detail}"
    raise RenderError(message)


def _convert_with_libreoffice(
    source_copy: Path,
    output_dir: Path,
    *,
    target_format: str,
    binary: str,
    timeout_seconds: float,
    iterative_calculation: bool = False,
    calculate_all: bool = False,
) -> Path:
    """Convert a disposable source copy with a fresh LibreOffice profile."""

    if calculate_all:
        return _calculate_all_with_libreoffice(
            source_copy,
            output_dir,
            target_format=target_format,
            binary=binary,
            timeout_seconds=timeout_seconds,
            iterative_calculation=iterative_calculation,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_format = target_format.split(":", 1)[0].lower().lstrip(".")
    suffix = f".{normalized_format}"
    with isolated_user_profile(
        iterative_calculation=iterative_calculation
    ) as (_, profile_uri):
        command = libreoffice_command(
            binary,
            source_copy,
            output_dir,
            target_format,
            profile_uri,
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(
                f"LibreOffice timed out after {timeout_seconds:g}s converting {source_copy.name}"
            ) from exc
        except OSError as exc:
            raise RenderError(f"Could not start LibreOffice: {exc}") from exc

    candidates = _converted_candidates(output_dir, source_copy.stem, suffix)
    if completed.returncode != 0 or not candidates:
        details = (completed.stderr or completed.stdout).strip()
        message = f"LibreOffice failed to convert {source_copy.name} to {normalized_format}"
        if details:
            message += f": {details[-1000:]}"
        raise RenderError(message)
    return candidates[0]


def convert_spreadsheet_copy(
    source: str | Path,
    output_dir: str | Path,
    *,
    target_format: str,
    libreoffice_binary: str | Path | None = None,
    timeout_seconds: float = 120.0,
) -> Path:
    """Convert a spreadsheet without ever handing LibreOffice the original file.

    This helper is also used by preprocessing for legacy XLS and ODS inputs.  It
    deliberately refuses to replace an existing destination artifact.
    """

    source_path = _validate_source(source)
    binary = find_libreoffice(libreoffice_binary)
    if binary is None:
        raise RenderError("LibreOffice executable was not found")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spreadsheet-convert-") as raw_work:
        work = Path(raw_work)
        private_source = work / source_path.name
        shutil.copy2(source_path, private_source)
        converted = _convert_with_libreoffice(
            private_source,
            work / "converted",
            target_format=target_format,
            binary=binary,
            timeout_seconds=timeout_seconds,
        )
        published = destination / converted.name
        if published.exists():
            raise RenderError(f"Refusing to overwrite conversion artifact: {published}")
        if published.resolve() == source_path:
            raise RenderError("Conversion output would overwrite the source workbook")
        shutil.copy2(converted, published)
    return published


def _validate_recalculated_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RenderError("LibreOffice produced an empty recalculated workbook")
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return
    try:
        sheet_inventory_identity(path)
    except Exception as exc:
        raise RenderError(f"Recalculated workbook validation failed: {exc}") from exc


def _worksheet_parts_by_name(package: zipfile.ZipFile) -> dict[str, str]:
    workbook_root = _parse_inventory_xml(
        package.read(_WORKBOOK_XML_PART),
        label="workbook",
    )
    workbook_namespace = _xml_namespace(workbook_root.tag)
    if workbook_namespace not in _RELATIONSHIP_NAMESPACES:
        raise RenderError("OOXML workbook root uses an unsupported SpreadsheetML namespace")
    sheets = workbook_root.find(f"{{{workbook_namespace}}}sheets")
    if sheets is None:
        raise RenderError("OOXML workbook is missing its sheets element")
    relationships = _parse_workbook_relationships(
        package.read(_WORKBOOK_RELATIONSHIPS_PART)
    )
    relationship_attribute = (
        f"{{{_RELATIONSHIP_NAMESPACES[workbook_namespace]}}}id"
    )
    parts: dict[str, str] = {}
    for sheet in sheets:
        name = sheet.attrib.get("name")
        relationship_id = sheet.attrib.get(relationship_attribute)
        relationship = relationships.get(str(relationship_id))
        if not isinstance(name, str) or relationship is None:
            raise RenderError("OOXML worksheet mapping is incomplete")
        if _sheet_kind(relationship) != "worksheet":
            continue
        parts[name] = _relationship_target_part(
            _WORKBOOK_XML_PART,
            relationship["target"],
        )
    return parts


def _range_coordinates(reference: str) -> list[str]:
    try:
        min_col, min_row, max_col, max_row = range_boundaries(
            reference.replace("$", "")
        )
    except (TypeError, ValueError) as exc:
        raise RenderError(f"OOXML data-table range is invalid: {reference!r}") from exc
    return [
        f"{get_column_letter(column)}{row}"
        for row in range(min_row, max_row + 1)
        for column in range(min_col, max_col + 1)
    ]


def _numeric_cell_from_source(
    source_cell: ElementTree.Element,
    *,
    namespace: str,
    value: int | float,
) -> ElementTree.Element:
    cell = ElementTree.fromstring(ElementTree.tostring(source_cell))
    cell.attrib.pop("t", None)
    for child in list(cell):
        cell.remove(child)
    value_node = ElementTree.SubElement(cell, f"{{{namespace}}}v")
    value_node.text = repr(value)
    return cell


def _restore_data_table_sheet_xml(
    source_xml: bytes,
    converted_xml: bytes,
    *,
    anchor_values: Mapping[str, int | float] | None = None,
    body_values: Mapping[str, int | float] | None = None,
    valid_style_count: int | None = None,
) -> tuple[bytes, int, int]:
    source_root = _parse_inventory_xml(source_xml, label="source worksheet")
    converted_root = _parse_inventory_xml(converted_xml, label="converted worksheet")
    namespace = _xml_namespace(source_root.tag)
    if namespace not in {
        _TRANSITIONAL_SPREADSHEETML_NAMESPACE,
        _STRICT_SPREADSHEETML_NAMESPACE,
    } or _xml_namespace(converted_root.tag) != namespace:
        raise RenderError("OOXML worksheet namespaces changed during recalculation")
    cell_tag = f"{{{namespace}}}c"
    formula_tag = f"{{{namespace}}}f"
    row_tag = f"{{{namespace}}}row"
    sheet_data_tag = f"{{{namespace}}}sheetData"
    source_sheet_data = source_root.find(sheet_data_tag)
    converted_sheet_data = converted_root.find(sheet_data_tag)
    if source_sheet_data is None or converted_sheet_data is None:
        raise RenderError("OOXML worksheet is missing sheetData")

    source_cells = {
        str(cell.attrib.get("r")): cell
        for row in source_sheet_data
        if row.tag == row_tag
        for cell in row
        if cell.tag == cell_tag and cell.attrib.get("r")
    }
    data_table_ranges: list[list[str]] = []
    for cell in source_cells.values():
        formula = cell.find(formula_tag)
        if formula is None or formula.attrib.get("t") != "dataTable":
            continue
        reference = formula.attrib.get("ref")
        if not reference:
            raise RenderError("OOXML data-table formula is missing its ref range")
        data_table_ranges.append(_range_coordinates(reference))
    if not data_table_ranges:
        return converted_xml, 0, 0

    converted_rows = {
        int(row.attrib["r"]): row
        for row in converted_sheet_data
        if row.tag == row_tag and str(row.attrib.get("r", "")).isdigit()
    }
    restored = 0
    for coordinates in data_table_ranges:
        anchor = coordinates[0]
        anchor_cell = source_cells.get(anchor)
        anchor_formula = anchor_cell.find(formula_tag) if anchor_cell is not None else None
        anchor_reference = anchor_formula.attrib.get("ref", "") if anchor_formula is not None else ""
        try:
            min_col, min_row, _, _ = range_boundaries(anchor_reference.replace("$", ""))
        except (TypeError, ValueError):
            min_col = min_row = 0
        row_reference = anchor_formula.attrib.get("r1") if anchor_formula is not None else None
        column_reference = anchor_formula.attrib.get("r2") if anchor_formula is not None else None

        def absolute_reference(reference: str | None) -> str:
            if not reference:
                return ""
            match = _CELL_REFERENCE_RE.fullmatch(reference.replace("$", "").upper())
            return (
                f"${match.group('column')}${match.group('row')}"
                if match is not None
                else reference
            )

        def data_table_formula(
            coordinate: str,
            *,
            table_min_col: int = min_col,
            table_min_row: int = min_row,
            table_row_reference: str | None = row_reference,
            table_column_reference: str | None = column_reference,
        ) -> str | None:
            if (
                table_min_col <= 1
                or not table_min_row
                or not table_row_reference
                or not table_column_reference
            ):
                return None
            match = _CELL_REFERENCE_RE.fullmatch(coordinate)
            if match is None:
                return None
            column = match.group("column")
            row_number = int(match.group("row"))
            left_column = get_column_letter(table_min_col - 1)
            header_row = table_min_row - 1
            return (
                f"TABLE(${left_column}${header_row},{absolute_reference(table_column_reference)},"
                f"${left_column}{row_number},{absolute_reference(table_row_reference)},"
                f"{column}${header_row})"
            )

        def cached_text(cell: ElementTree.Element | None) -> str:
            if cell is None:
                return ""
            value = cell.find(f"{{{namespace}}}v")
            if value is not None and value.text is not None:
                return value.text
            inline = cell.find(f"{{{namespace}}}is")
            text_node = inline.find(f"{{{namespace}}}t") if inline is not None else None
            return text_node.text if text_node is not None and text_node.text is not None else ""

        # Some Excel tables intentionally return display strings (for example
        # combined multiple/percentage labels). Those tables are represented in
        # the golden files as ordinary TABLE formulas; numeric tables retain
        # their cached values so regression cells are not recomputed by LO.
        formula_body = any(
            (source_cells.get(coordinate) is not None)
            and cached_text(source_cells.get(coordinate))
            and not _looks_numeric(cached_text(source_cells.get(coordinate)))
            and not cached_text(source_cells.get(coordinate)).startswith("#")
            for coordinate in coordinates
            if coordinate != anchor
        )
        # A What-If table may return a display string (for example a combined
        # multiple/percentage label).  In that case LibreOffice cannot
        # evaluate the scenario directly, but Excel's saved workbook still
        # represents every body cell as a TABLE formula with the string cache
        # from the table's result cell.  Infer this from the converted result
        # cell rather than requiring a golden workbook or a task-specific
        # label.  This also covers sources whose native body caches are
        # ``#REF!``/empty after openpyxl editing.
        result_coordinate = (
            f"{get_column_letter(min_col - 1)}{min_row - 1}"
            if min_col > 1 and min_row > 1
            else ""
        )
        result_cell = None
        if result_coordinate:
            for candidate_row in converted_sheet_data:
                if candidate_row.tag != row_tag:
                    continue
                result_cell = next(
                    (
                        child
                        for child in candidate_row
                        if child.tag == cell_tag
                        and child.attrib.get("r") == result_coordinate
                    ),
                    None,
                )
                if result_cell is not None:
                    break
        result_cached = cached_text(result_cell)
        if (
            not formula_body
            and result_cached
            and not _looks_numeric(result_cached)
            and not result_cached.startswith("#")
        ):
            formula_body = True
        def sanitized_template(
            preferred: ElementTree.Element | None,
            fallback: ElementTree.Element,
        ) -> ElementTree.Element:
            template = ElementTree.fromstring(
                ElementTree.tostring(preferred if preferred is not None else fallback)
            )
            style = template.attrib.get("s")
            if valid_style_count is not None and style is not None:
                try:
                    style_is_valid = 0 <= int(style) < valid_style_count
                except ValueError:
                    style_is_valid = False
                if not style_is_valid:
                    fallback_style = fallback.attrib.get("s")
                    try:
                        fallback_is_valid = (
                            fallback_style is not None
                            and 0 <= int(fallback_style) < valid_style_count
                        )
                    except ValueError:
                        fallback_is_valid = False
                    if fallback_is_valid:
                        template.set("s", str(fallback_style))
                    else:
                        template.attrib.pop("s", None)
            return template

        for coordinate in coordinates:
            row_index = int("".join(character for character in coordinate if character.isdigit()))
            row = converted_rows.get(row_index)
            if row is None:
                row = ElementTree.Element(row_tag, {"r": str(row_index)})
                converted_sheet_data.append(row)
                converted_rows[row_index] = row
            current = next(
                (
                    cell
                    for cell in row
                    if cell.tag == cell_tag and cell.attrib.get("r") == coordinate
                ),
                None,
            )
            source_cell = source_cells.get(coordinate)
            if current is not None:
                row.remove(current)
            if source_cell is not None:
                formula_text = data_table_formula(coordinate) if formula_body else None
                source_cached = cached_text(source_cell)
                converted_cached = cached_text(current)
                # Native numeric data tables arrive from Excel with error-valued
                # body cells (typically ``#REF!``) while LibreOffice has already
                # evaluated those scenarios.  Preserve the evaluated numeric
                # caches as plain values; materialising TABLE formulas changes
                # the official formula/value semantics and can yield stale or
                # negative anchors after a structural repair.
                if (
                    coordinate != anchor
                    and source_cached.startswith("#")
                    and _looks_numeric(converted_cached)
                ):
                    template = sanitized_template(current, source_cell)
                    row.append(
                        _numeric_cell_from_source(
                            template,
                            namespace=namespace,
                            value=float(converted_cached),
                        )
                    )
                    restored += 1
                    continue
                if body_values and coordinate in body_values:
                    template = sanitized_template(current, source_cell)
                    row.append(
                        _numeric_cell_from_source(
                            template,
                            namespace=namespace,
                            value=body_values[coordinate],
                        )
                    )
                elif anchor_values and coordinate in anchor_values:
                    template = sanitized_template(current, source_cell)
                    row.append(
                        _numeric_cell_from_source(
                            template,
                            namespace=namespace,
                            value=anchor_values[coordinate],
                        )
                    )
                elif formula_text is not None:
                    template = sanitized_template(current, source_cell)
                    template.attrib.pop("t", None)
                    for child in list(template):
                        template.remove(child)
                    formula_node = ElementTree.SubElement(template, formula_tag)
                    formula_node.set("aca", "true")
                    formula_node.text = formula_text
                    current_cached = cached_text(current)
                    source_cached = cached_text(source_cell)
                    if formula_body and result_cached:
                        # For string-valued tables the top-left result cell is
                        # the workbook-local source of truth.  A stale cache
                        # left by Calc in the anchor/body must not win.
                        cached = result_cached
                    else:
                        cached = next(
                            (
                                candidate
                                for candidate in (
                                    current_cached,
                                    source_cached,
                                    result_cached,
                                )
                                if candidate and not candidate.startswith("#")
                            ),
                            current_cached or source_cached or result_cached,
                        )
                    value_node = ElementTree.SubElement(template, f"{{{namespace}}}v")
                    value_node.text = cached
                    if not _looks_numeric(cached):
                        template.set("t", "str")
                    row.append(template)
                else:
                    # A native data table consists of one OOXML dataTable anchor and
                    # cached numeric body cells. Preserve that representation when
                    # scenario evaluation is unavailable; ordinary TABLE formulas in
                    # every body cell change formula-level workbook semantics.
                    row.append(sanitized_template(source_cell, source_cell))
            restored += 1
            row[:] = sorted(
                row,
                key=lambda cell: range_boundaries(str(cell.attrib.get("r", "A1")))[0],
            )
    converted_sheet_data[:] = sorted(
        converted_sheet_data,
        key=lambda row: int(row.attrib.get("r", "0") or 0),
    )
    ElementTree.register_namespace("", namespace)
    return (
        ElementTree.tostring(converted_root, encoding="utf-8", xml_declaration=True),
        len(data_table_ranges),
        restored,
    )


def _replace_ooxml_parts(path: Path, replacements: Mapping[str, bytes]) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}.ooxml-repair-",
        suffix=path.suffix,
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        with zipfile.ZipFile(path, "r") as source_package, zipfile.ZipFile(
            temporary, "w"
        ) as destination_package:
            for info in source_package.infolist():
                destination_package.writestr(
                    info,
                    replacements.get(info.filename, source_package.read(info.filename)),
                )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_font_colors_ooxml(
    workbook_path: str | Path,
    changes: Mapping[tuple[str, str], str],
) -> int:
    """Patch cell font colors without round-tripping the workbook object model.

    Complex financial workbooks can contain cached values, data tables, and array
    formulas that openpyxl or LibreOffice rewrites merely by saving the file. A
    color-only repair should not touch those parts. This routine clones the
    referenced font and cell-XF records, assigns the cloned XF to each target
    cell, and replaces only ``styles.xml`` and affected worksheet XML parts.
    """

    path = _validate_source(workbook_path)
    if path.suffix.casefold() not in _SHEET_INVENTORY_FORMATS:
        raise RenderError("OOXML font patching requires an .xlsx or .xlsm workbook")
    if not changes:
        return 0

    coordinate_pattern = re.compile(r"[A-Z]{1,3}[1-9][0-9]*\Z")
    normalized: dict[tuple[str, str], str] = {}
    for raw_target, raw_color in changes.items():
        if not isinstance(raw_target, tuple) or len(raw_target) != 2:
            raise RenderError(f"Invalid OOXML font patch target: {raw_target!r}")
        sheet_name, coordinate = raw_target
        coordinate = str(coordinate).replace("$", "").upper()
        if not isinstance(sheet_name, str) or not coordinate_pattern.fullmatch(coordinate):
            raise RenderError(f"Invalid OOXML font patch target: {raw_target!r}")
        color = str(raw_color).strip().lstrip("#").upper()
        if len(color) == 6:
            color = f"FF{color}"
        if not re.fullmatch(r"[0-9A-F]{8}", color):
            raise RenderError(f"Invalid OOXML font color: {raw_color!r}")
        normalized[(sheet_name, coordinate)] = color

    with zipfile.ZipFile(path, "r") as package:
        try:
            styles_xml = package.read(_STYLES_XML_PART)
            worksheet_parts = _worksheet_parts_by_name(package)
        except KeyError as exc:
            raise RenderError(f"OOXML font patching is missing required part: {exc}") from exc
        missing_sheets = sorted({sheet for sheet, _ in normalized} - worksheet_parts.keys())
        if missing_sheets:
            raise RenderError(
                "OOXML font patch targets unknown worksheet(s): "
                + ", ".join(missing_sheets)
            )
        worksheet_xml = {
            part: package.read(part)
            for part in {worksheet_parts[sheet] for sheet, _ in normalized}
        }

    styles_root = _parse_inventory_xml(styles_xml, label="styles")
    namespace = _xml_namespace(styles_root.tag)
    if namespace not in {
        _TRANSITIONAL_SPREADSHEETML_NAMESPACE,
        _STRICT_SPREADSHEETML_NAMESPACE,
    }:
        raise RenderError("OOXML styles root uses an unsupported SpreadsheetML namespace")
    fonts = styles_root.find(f"{{{namespace}}}fonts")
    cell_xfs = styles_root.find(f"{{{namespace}}}cellXfs")
    if fonts is None or cell_xfs is None:
        raise RenderError("OOXML styles is missing fonts or cellXfs")
    font_records = list(fonts)
    xf_records = list(cell_xfs)
    if not font_records or not xf_records:
        raise RenderError("OOXML styles has no font or cell-XF records")

    replacements: dict[str, bytes] = {}
    cloned_styles: dict[tuple[int, str], int] = {}
    patched = 0
    targets_by_sheet: dict[str, dict[str, str]] = {}
    for (sheet_name, coordinate), color in normalized.items():
        targets_by_sheet.setdefault(sheet_name, {})[coordinate] = color

    for sheet_name, targets in targets_by_sheet.items():
        part = worksheet_parts[sheet_name]
        root = _parse_inventory_xml(worksheet_xml[part], label=f"worksheet {sheet_name}")
        sheet_namespace = _xml_namespace(root.tag)
        if sheet_namespace != namespace:
            raise RenderError("OOXML worksheet and styles namespaces do not match")
        cell_tag = f"{{{namespace}}}c"
        cells = {
            str(cell.attrib.get("r", "")).replace("$", "").upper(): cell
            for cell in root.iter(cell_tag)
            if cell.attrib.get("r")
        }
        missing_cells = sorted(set(targets) - cells.keys())
        if missing_cells:
            raise RenderError(
                f"OOXML font patch targets missing cell(s) on {sheet_name}: "
                + ", ".join(missing_cells)
            )
        for coordinate, color in targets.items():
            cell = cells[coordinate]
            try:
                style_id = int(cell.attrib.get("s", "0"))
                old_xf = xf_records[style_id]
                font_id = int(old_xf.attrib.get("fontId", "0"))
                old_font = font_records[font_id]
            except (ValueError, IndexError) as exc:
                raise RenderError(
                    f"OOXML cell {sheet_name}!{coordinate} has an invalid style reference"
                ) from exc
            style_key = (style_id, color)
            new_style_id = cloned_styles.get(style_key)
            if new_style_id is None:
                new_font = ElementTree.fromstring(ElementTree.tostring(old_font))
                color_tag = f"{{{namespace}}}color"
                color_node = new_font.find(color_tag)
                if color_node is None:
                    color_node = ElementTree.SubElement(new_font, color_tag)
                color_node.attrib.clear()
                color_node.set("rgb", color)
                fonts.append(new_font)
                new_font_id = len(font_records)
                font_records.append(new_font)

                new_xf = ElementTree.fromstring(ElementTree.tostring(old_xf))
                new_xf.set("fontId", str(new_font_id))
                new_xf.set("applyFont", "1")
                cell_xfs.append(new_xf)
                new_style_id = len(xf_records)
                xf_records.append(new_xf)
                cloned_styles[style_key] = new_style_id
            cell.set("s", str(new_style_id))
            patched += 1
        ElementTree.register_namespace("", namespace)
        replacements[part] = ElementTree.tostring(
            root, encoding="utf-8", xml_declaration=True
        )

    fonts.set("count", str(len(font_records)))
    cell_xfs.set("count", str(len(xf_records)))
    ElementTree.register_namespace("", namespace)
    replacements[_STYLES_XML_PART] = ElementTree.tostring(
        styles_root, encoding="utf-8", xml_declaration=True
    )
    _replace_ooxml_parts(path, replacements)
    _validate_recalculated_file(path)
    return patched


def restore_ooxml_cell_contents(
    source: str | Path,
    target: str | Path,
    *,
    minimum_changes: int = 1,
    selected_coordinates: Mapping[str, Sequence[str]] | None = None,
) -> int:
    """Restore cell contents while retaining target cell style assignments.

    This is a transaction guard for format-only tasks. It restores formula,
    cached-value, inline-string, and cell-type XML from ``source`` only when the
    detected drift reaches ``minimum_changes``. The target ``s`` style id is
    retained so legitimate font-color edits survive the rollback.
    """

    source_path = _validate_source(source)
    target_path = _validate_source(target)
    if minimum_changes <= 0:
        raise RenderError("minimum_changes must be positive")
    if (
        source_path.suffix.casefold() not in _SHEET_INVENTORY_FORMATS
        or target_path.suffix.casefold() not in _SHEET_INVENTORY_FORMATS
    ):
        raise RenderError("OOXML cell-content restoration requires .xlsx or .xlsm files")

    # ``selected_coordinates`` is used by benchmark adapters that need to
    # restore only cells classified as regressions.  Keeping the filter at the
    # OOXML level is important: an openpyxl round-trip would discard formula
    # caches and data-table records for the entire workbook.  Coordinates are
    # normalized once so callers may pass either ``A1`` or ``$A$1``.
    selected: dict[str, set[str]] | None = None
    if selected_coordinates is not None:
        selected = {
            str(sheet): {str(coordinate).replace("$", "").upper() for coordinate in coordinates}
            for sheet, coordinates in selected_coordinates.items()
        }

    parsed: dict[
        str,
        tuple[
            str,
            ElementTree.Element,
            str,
            dict[str, ElementTree.Element],
            dict[int, ElementTree.Element],
        ],
    ] = {}
    source_strings_by_sheet: dict[str, tuple[ElementTree.Element, ...]] = {}
    differences: list[tuple[str, str]] = []

    def shared_string_items(
        package: zipfile.ZipFile,
        *,
        namespace: str,
        label: str,
    ) -> tuple[ElementTree.Element, ...]:
        if _SHARED_STRINGS_XML_PART not in package.namelist():
            return ()
        root = _parse_inventory_xml(
            package.read(_SHARED_STRINGS_XML_PART),
            label=f"{label} shared strings",
        )
        if root.tag != f"{{{namespace}}}sst":
            raise RenderError(f"OOXML {label} shared strings use an unsupported namespace")
        item_tag = f"{{{namespace}}}si"
        if any(item.tag != item_tag for item in root):
            raise RenderError(f"OOXML {label} shared strings contain an invalid child")
        return tuple(root)

    def inline_shared_string(
        cell: ElementTree.Element,
        *,
        namespace: str,
        strings: Sequence[ElementTree.Element],
        label: str,
    ) -> ElementTree.Element:
        clone = ElementTree.fromstring(ElementTree.tostring(cell))
        if clone.attrib.get("t") != "s":
            return clone
        value = clone.find(f"{{{namespace}}}v")
        try:
            index = int(value.text) if value is not None and value.text is not None else -1
            if index < 0:
                raise IndexError(index)
            item = strings[index]
        except (ValueError, IndexError):
            raise RenderError(
                f"OOXML {label} cell {cell.attrib.get('r', '<unknown>')} has an invalid "
                "shared-string reference"
            ) from None
        clone.set("t", "inlineStr")
        if value is not None:
            clone.remove(value)
        inline = ElementTree.Element(f"{{{namespace}}}is")
        for child in item:
            inline.append(ElementTree.fromstring(ElementTree.tostring(child)))
        clone.append(inline)
        return clone

    with zipfile.ZipFile(source_path) as source_package, zipfile.ZipFile(
        target_path
    ) as target_package:
        source_parts = _worksheet_parts_by_name(source_package)
        target_parts = _worksheet_parts_by_name(target_package)
        if set(source_parts) != set(target_parts):
            raise RenderError("OOXML cell-content restoration sheet names do not match")
        for sheet_name, target_part in target_parts.items():
            source_root = _parse_inventory_xml(
                source_package.read(source_parts[sheet_name]),
                label=f"source worksheet {sheet_name}",
            )
            target_root = _parse_inventory_xml(
                target_package.read(target_part),
                label=f"target worksheet {sheet_name}",
            )
            namespace = _xml_namespace(source_root.tag)
            if namespace != _xml_namespace(target_root.tag):
                raise RenderError("OOXML cell-content restoration namespaces do not match")
            source_strings = shared_string_items(
                source_package,
                namespace=namespace,
                label="source",
            )
            target_strings = shared_string_items(
                target_package,
                namespace=namespace,
                label="target",
            )
            cell_tag = f"{{{namespace}}}c"
            row_tag = f"{{{namespace}}}row"
            source_cells = {
                str(cell.attrib["r"]): cell
                for cell in source_root.iter(cell_tag)
                if cell.attrib.get("r")
            }
            target_cells = {
                str(cell.attrib["r"]): cell
                for cell in target_root.iter(cell_tag)
                if cell.attrib.get("r")
            }
            target_rows = {
                int(row.attrib["r"]): row
                for row in target_root.iter(row_tag)
                if str(row.attrib.get("r", "")).isdigit()
            }

            def content_signature(
                cell: ElementTree.Element | None,
                *,
                strings: Sequence[ElementTree.Element],
                label: str,
                namespace_uri: str = namespace,
            ) -> Any:
                if cell is None:
                    return None
                semantic_cell = inline_shared_string(
                    cell,
                    namespace=namespace_uri,
                    strings=strings,
                    label=label,
                )
                attributes = tuple(
                    sorted(
                        (key, value)
                        for key, value in semantic_cell.attrib.items()
                        if key not in {"r", "s"}
                    )
                )
                children = tuple(ElementTree.tostring(child) for child in semantic_cell)
                return (attributes, children) if attributes or children else None

            for coordinate in set(source_cells) | set(target_cells):
                if selected is not None and coordinate.replace("$", "").upper() not in selected.get(
                    sheet_name, set()
                ):
                    continue
                if content_signature(
                    source_cells.get(coordinate),
                    strings=source_strings,
                    label="source",
                ) != content_signature(
                    target_cells.get(coordinate),
                    strings=target_strings,
                    label="target",
                ):
                    differences.append((sheet_name, coordinate))
            parsed[sheet_name] = (
                target_part,
                target_root,
                namespace,
                target_cells,
                target_rows,
            )
            parsed[f"{sheet_name}\0source"] = (
                source_parts[sheet_name],
                source_root,
                namespace,
                source_cells,
                {},
            )
            source_strings_by_sheet[sheet_name] = source_strings
    if len(differences) < minimum_changes:
        return 0

    replacements: dict[str, bytes] = {}
    changed_sheets: set[str] = set()
    for sheet_name, coordinate in differences:
        target_part, target_root, namespace, target_cells, target_rows = parsed[sheet_name]
        _, _, _, source_cells, _ = parsed[f"{sheet_name}\0source"]
        source_cell = source_cells.get(coordinate)
        target_cell = target_cells.get(coordinate)
        row_index = int("".join(character for character in coordinate if character.isdigit()))
        target_row = target_rows.get(row_index)
        if target_cell is not None and target_row is None:
            raise RenderError(f"OOXML target cell {sheet_name}!{coordinate} has no parent row")
        if source_cell is None:
            assert target_cell is not None and target_row is not None
            target_row.remove(target_cell)
            target_cells.pop(coordinate, None)
        else:
            replacement = inline_shared_string(
                source_cell,
                namespace=namespace,
                strings=source_strings_by_sheet[sheet_name],
                label="source",
            )
            if target_cell is not None and "s" in target_cell.attrib:
                replacement.set("s", target_cell.attrib["s"])
            elif target_cell is None:
                # The source and LibreOffice output may have different style
                # tables.  A source-only cell therefore cannot safely carry
                # its original style index into the target package; leave it
                # unstyled (or style 0) while restoring the content.
                replacement.attrib.pop("s", None)
            if target_row is None:
                sheet_data = target_root.find(f"{{{namespace}}}sheetData")
                if sheet_data is None:
                    raise RenderError("OOXML target worksheet is missing sheetData")
                target_row = ElementTree.Element(
                    f"{{{namespace}}}row", {"r": str(row_index)}
                )
                sheet_data.append(target_row)
                target_rows[row_index] = target_row
            if target_cell is not None:
                position = list(target_row).index(target_cell)
                target_row.remove(target_cell)
                target_row.insert(position, replacement)
            else:
                target_row.append(replacement)
            target_cells[coordinate] = replacement
            target_row[:] = sorted(
                target_row,
                key=lambda cell: range_boundaries(str(cell.attrib.get("r", "A1")))[0],
            )
        changed_sheets.add(sheet_name)

    for sheet_name in changed_sheets:
        target_part, target_root, namespace, _, target_rows = parsed[sheet_name]
        sheet_data = target_root.find(f"{{{namespace}}}sheetData")
        if sheet_data is not None:
            sheet_data[:] = sorted(
                sheet_data,
                key=lambda row: int(row.attrib.get("r", "0") or 0),
            )
        ElementTree.register_namespace("", namespace)
        replacements[target_part] = ElementTree.tostring(
            target_root, encoding="utf-8", xml_declaration=True
        )
    _replace_ooxml_parts(target_path, replacements)
    _validate_recalculated_file(target_path)
    return len(differences)


def transplant_ooxml_formula_cached_values(
    recalculated: str | Path,
    target: str | Path,
    *,
    exclude_data_table_formulas: bool = False,
    include_data_table_regions: bool = False,
    selected_coordinates: Mapping[str, Sequence[str]] | None = None,
) -> int:
    """Copy cached values for unchanged formulas without touching package styles.

    This supports format-only tasks: LibreOffice can recalculate a disposable
    copy, while the published workbook retains its original OOXML styles,
    drawings, formula records, and cell structure. A cache is accepted only
    when the worksheet, coordinate, formula kind, and normalized formula text
    match in both packages.
    """

    recalculated_path = _validate_source(recalculated)
    target_path = _validate_source(target)
    if (
        recalculated_path.suffix.casefold() not in _SHEET_INVENTORY_FORMATS
        or target_path.suffix.casefold() not in _SHEET_INVENTORY_FORMATS
    ):
        raise RenderError("OOXML formula-cache transplant requires .xlsx or .xlsm files")

    replacements: dict[str, bytes] = {}
    transplanted = 0
    selected = (
        {
            str(sheet): {
                str(coordinate).replace("$", "").upper()
                for coordinate in coordinates
            }
            for sheet, coordinates in selected_coordinates.items()
        }
        if selected_coordinates is not None
        else None
    )
    with zipfile.ZipFile(recalculated_path) as source_package, zipfile.ZipFile(
        target_path
    ) as target_package:
        source_parts = _worksheet_parts_by_name(source_package)
        target_parts = _worksheet_parts_by_name(target_package)
        if set(source_parts) != set(target_parts):
            raise RenderError("OOXML formula-cache transplant sheet names do not match")
        for sheet_name, target_part in target_parts.items():
            source_root = _parse_inventory_xml(
                source_package.read(source_parts[sheet_name]),
                label=f"recalculated worksheet {sheet_name}",
            )
            target_root = _parse_inventory_xml(
                target_package.read(target_part),
                label=f"target worksheet {sheet_name}",
            )
            namespace = _xml_namespace(target_root.tag)
            if namespace != _xml_namespace(source_root.tag):
                raise RenderError("OOXML formula-cache transplant namespaces do not match")
            cell_tag = f"{{{namespace}}}c"
            formula_tag = f"{{{namespace}}}f"
            value_tag = f"{{{namespace}}}v"
            source_all_cells = {
                str(cell.attrib["r"]): cell
                for cell in source_root.iter(cell_tag)
                if cell.attrib.get("r")
            }
            target_all_cells = {
                str(cell.attrib["r"]): cell
                for cell in target_root.iter(cell_tag)
                if cell.attrib.get("r")
            }
            source_cells = {
                coordinate: cell
                for coordinate, cell in source_all_cells.items()
                if cell.attrib.get("r") and cell.find(formula_tag) is not None
            }
            target_cells = {
                coordinate: cell
                for coordinate, cell in target_all_cells.items()
                if cell.attrib.get("r") and cell.find(formula_tag) is not None
            }
            sheet_transplanted = 0
            transplanted_coordinates: set[str] = set()

            def formula_signature(
                cell: ElementTree.Element, *, tag: str = formula_tag
            ) -> tuple[str, str]:
                formula = cell.find(tag)
                assert formula is not None
                kind = str(formula.attrib.get("t", "normal"))
                text = re.sub(r"\s+", "", formula.text or "").replace("$", "").upper()
                # OOXML omits the leading ``=``. LibreOffice commonly
                # normalizes legacy ``=+A1`` to ``=A1`` without changing its
                # meaning, so ignore that optional leading plus here too.
                if text.startswith("+"):
                    text = text[1:]
                return kind, text

            for coordinate, target_cell in target_cells.items():
                if selected is not None and coordinate.replace("$", "").upper() not in selected.get(
                    sheet_name, set()
                ):
                    continue
                source_cell = source_cells.get(coordinate)
                if source_cell is None or formula_signature(source_cell) != formula_signature(
                    target_cell
                ):
                    continue
                if exclude_data_table_formulas and (
                    formula_signature(target_cell)[0] == "dataTable"
                    or formula_signature(target_cell)[1].startswith("TABLE(")
                ):
                    continue
                source_value = source_cell.find(value_tag)
                if source_value is None or source_value.text is None:
                    continue
                target_value = target_cell.find(value_tag)
                source_type = source_cell.attrib.get("t")
                target_type = target_cell.attrib.get("t")
                if (
                    target_value is not None
                    and target_value.text == source_value.text
                    and target_type == source_type
                ):
                    continue
                if target_value is None:
                    target_value = ElementTree.SubElement(target_cell, value_tag)
                target_value.text = source_value.text
                if source_type is None:
                    target_cell.attrib.pop("t", None)
                else:
                    target_cell.set("t", source_type)
                sheet_transplanted += 1
                transplanted_coordinates.add(coordinate)
            if include_data_table_regions:
                data_table_coordinates: set[str] = set()
                for target_cell in target_cells.values():
                    formula = target_cell.find(formula_tag)
                    assert formula is not None
                    if formula.attrib.get("t") != "dataTable":
                        continue
                    reference = formula.attrib.get("ref")
                    if not reference:
                        continue
                    try:
                        min_col, min_row, max_col, max_row = range_boundaries(reference)
                    except ValueError:
                        continue
                    data_table_coordinates.update(
                        f"{get_column_letter(column)}{row}"
                        for row in range(min_row, max_row + 1)
                        for column in range(min_col, max_col + 1)
                    )
                for coordinate in data_table_coordinates:
                    if coordinate in transplanted_coordinates:
                        continue
                    if selected is not None and coordinate.upper() not in selected.get(
                        sheet_name, set()
                    ):
                        continue
                    source_cell = source_all_cells.get(coordinate)
                    target_cell = target_all_cells.get(coordinate)
                    if source_cell is None or target_cell is None:
                        continue
                    source_value = source_cell.find(value_tag)
                    if source_value is None or source_value.text is None:
                        continue
                    target_value = target_cell.find(value_tag)
                    source_type = source_cell.attrib.get("t")
                    target_type = target_cell.attrib.get("t")
                    if (
                        target_value is not None
                        and target_value.text == source_value.text
                        and target_type == source_type
                    ):
                        continue
                    if target_value is None:
                        target_value = ElementTree.SubElement(target_cell, value_tag)
                    target_value.text = source_value.text
                    if source_type is None:
                        target_cell.attrib.pop("t", None)
                    else:
                        target_cell.set("t", source_type)
                    sheet_transplanted += 1
            if sheet_transplanted:
                ElementTree.register_namespace("", namespace)
                replacements[target_part] = ElementTree.tostring(
                    target_root, encoding="utf-8", xml_declaration=True
                )
                transplanted += sheet_transplanted
    if replacements:
        _replace_ooxml_parts(target_path, replacements)
        _validate_recalculated_file(target_path)
    return transplanted


def _cyclic_graph_nodes(graph: Mapping[str, set[str]]) -> set[str]:
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cyclic: set[str] = set()

    def strong_connect(node: str) -> None:
        index = len(indices)
        indices[node] = lowlinks[node] = index
        stack.append(node)
        on_stack.add(node)
        for neighbor in graph[node]:
            if neighbor not in indices:
                strong_connect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or node in graph[node]:
            cyclic.update(component)

    for node in graph:
        if node not in indices:
            strong_connect(node)
    return cyclic


def _seed_ooxml_formula_cached_values(target: Path, seed: Path) -> dict[str, int]:
    """Restore pre-edit formula caches so circular recalculation has a faithful seed."""

    if target.suffix.lower() not in _SHEET_INVENTORY_FORMATS or seed.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return {
            "sheets": 0,
            "formula_cells": 0,
            "cyclic_formula_cells": 0,
            "dirty_formula_cells": 0,
        }
    replacements: dict[str, bytes] = {}
    seeded_cells = 0
    seeded_sheets = 0
    cyclic_cells = 0
    dirty_cells = 0
    target_workbook = load_workbook(target, data_only=False, read_only=True)
    seed_workbook = load_workbook(seed, data_only=False, read_only=True)
    try:
        with zipfile.ZipFile(target) as target_package, zipfile.ZipFile(seed) as seed_package:
            target_parts = _worksheet_parts_by_name(target_package)
            seed_parts = _worksheet_parts_by_name(seed_package)
            if set(target_parts) != set(seed_parts):
                raise RenderError(
                    "Formula cache seed sheet names do not match the recalculation workbook"
                )
            for sheet_name, target_part in target_parts.items():
                seed_part = seed_parts[sheet_name]
                target_root = _parse_inventory_xml(
                    target_package.read(target_part), label=f"target worksheet {sheet_name!r}"
                )
                seed_root = _parse_inventory_xml(
                    seed_package.read(seed_part), label=f"cache seed worksheet {sheet_name!r}"
                )
                target_namespace = _xml_namespace(target_root.tag)
                seed_namespace = _xml_namespace(seed_root.tag)
                if target_namespace not in _RELATIONSHIP_NAMESPACES or seed_namespace not in _RELATIONSHIP_NAMESPACES:
                    raise RenderError("Formula cache seed uses an unsupported worksheet namespace")

                def formula_cells(root: ElementTree.Element, namespace: str) -> dict[str, ElementTree.Element]:
                    cell_tag = f"{{{namespace}}}c"
                    formula_tag = f"{{{namespace}}}f"
                    return {
                        coordinate: cell
                        for cell in root.iter(cell_tag)
                        if isinstance((coordinate := cell.attrib.get("r")), str)
                        and cell.find(formula_tag) is not None
                    }

                target_cells = formula_cells(target_root, target_namespace)
                seed_cells = formula_cells(seed_root, seed_namespace)
                formula_coordinates = set(target_cells)
                graph: dict[str, set[str]] = {}
                for coordinate in formula_coordinates:
                    raw_formula = target_workbook[sheet_name][coordinate].value
                    formula = getattr(raw_formula, "text", raw_formula)
                    graph[coordinate] = {
                        f"{column.upper()}{row}"
                        for column, row in _LOCAL_FORMULA_REFERENCE.findall(
                            formula if isinstance(formula, str) else ""
                        )
                        if f"{column.upper()}{row}" in formula_coordinates
                    }

                cyclic_coordinates = _cyclic_graph_nodes(graph)
                cyclic_cells += len(cyclic_coordinates)
                sheet_seeded = 0
                for coordinate, target_cell in target_cells.items():
                    seed_cell = seed_cells.get(coordinate)
                    target_formula = getattr(
                        target_workbook[sheet_name][coordinate].value,
                        "text",
                        target_workbook[sheet_name][coordinate].value,
                    )
                    seed_formula = (
                        getattr(
                            seed_workbook[sheet_name][coordinate].value,
                            "text",
                            seed_workbook[sheet_name][coordinate].value,
                        )
                        if seed_cell is not None
                        else None
                    )
                    formulas_match = (
                        isinstance(target_formula, str)
                        and isinstance(seed_formula, str)
                        and target_formula.replace("$", "").upper()
                        == seed_formula.replace("$", "").upper()
                    )
                    target_value = target_cell.find(f"{{{target_namespace}}}v")
                    if not formulas_match or coordinate not in cyclic_coordinates:
                        if target_value is not None:
                            target_cell.remove(target_value)
                        target_cell.attrib.pop("t", None)
                        dirty_cells += 1
                        continue
                    assert seed_cell is not None
                    seed_value = seed_cell.find(f"{{{seed_namespace}}}v")
                    if seed_value is None or seed_value.text is None:
                        continue
                    if target_value is None:
                        target_value = ElementTree.SubElement(
                            target_cell, f"{{{target_namespace}}}v"
                        )
                    target_value.text = seed_value.text
                    seed_type = seed_cell.attrib.get("t")
                    if seed_type is None:
                        target_cell.attrib.pop("t", None)
                    else:
                        target_cell.set("t", seed_type)
                    sheet_seeded += 1
                if sheet_seeded:
                    ElementTree.register_namespace("", target_namespace)
                    replacements[target_part] = ElementTree.tostring(
                        target_root, encoding="utf-8", xml_declaration=True
                    )
                    seeded_sheets += 1
                    seeded_cells += sheet_seeded
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise RenderError(f"Could not seed OOXML formula caches: {type(exc).__name__}: {exc}") from exc
    finally:
        target_workbook.close()
        seed_workbook.close()
    if replacements:
        _replace_ooxml_parts(target, replacements)
    return {
        "sheets": seeded_sheets,
        "formula_cells": seeded_cells,
        "cyclic_formula_cells": cyclic_cells,
        "dirty_formula_cells": dirty_cells,
    }


def _converted_cell_style_count(package: zipfile.ZipFile) -> int:
    styles_root = _parse_inventory_xml(
        package.read(_STYLES_XML_PART),
        label="converted styles",
    )
    styles_namespace = _xml_namespace(styles_root.tag)
    cell_xfs = styles_root.find(f"{{{styles_namespace}}}cellXfs")
    if cell_xfs is None:
        raise RenderError("OOXML styles are missing the cellXfs table")
    return len(cell_xfs)


def _restore_array_formula_sheet_xml(
    source_xml: bytes,
    converted_xml: bytes,
    *,
    valid_style_count: int,
) -> tuple[bytes, int, int]:
    source_root = _parse_inventory_xml(source_xml, label="source worksheet")
    converted_root = _parse_inventory_xml(converted_xml, label="converted worksheet")
    namespace = _xml_namespace(source_root.tag)
    if namespace not in {
        _TRANSITIONAL_SPREADSHEETML_NAMESPACE,
        _STRICT_SPREADSHEETML_NAMESPACE,
    } or _xml_namespace(converted_root.tag) != namespace:
        raise RenderError("OOXML worksheet namespaces changed during recalculation")
    cell_tag = f"{{{namespace}}}c"
    formula_tag = f"{{{namespace}}}f"
    value_tag = f"{{{namespace}}}v"
    row_tag = f"{{{namespace}}}row"
    sheet_data_tag = f"{{{namespace}}}sheetData"
    source_sheet_data = source_root.find(sheet_data_tag)
    converted_sheet_data = converted_root.find(sheet_data_tag)
    if source_sheet_data is None or converted_sheet_data is None:
        raise RenderError("OOXML worksheet is missing sheetData")

    def cells_by_coordinate(sheet_data: ElementTree.Element) -> dict[str, ElementTree.Element]:
        return {
            str(cell.attrib["r"]): cell
            for row in sheet_data
            if row.tag == row_tag
            for cell in row
            if cell.tag == cell_tag and cell.attrib.get("r")
        }

    source_cells = cells_by_coordinate(source_sheet_data)
    converted_cells = cells_by_coordinate(converted_sheet_data)
    ranges = []
    for coordinate, cell in source_cells.items():
        formula = cell.find(formula_tag)
        if formula is None or formula.attrib.get("t") != "array":
            continue
        reference = formula.attrib.get("ref") or coordinate
        ranges.append((coordinate, _range_coordinates(reference)))
    if not ranges:
        return converted_xml, 0, 0

    restored_cells = 0
    for anchor, coordinates in ranges:
        source_anchor = source_cells[anchor]
        source_formula = source_anchor.find(formula_tag)
        assert source_formula is not None
        for coordinate in coordinates:
            source_cell = source_cells.get(coordinate)
            converted_cell = converted_cells.get(coordinate)
            if source_cell is None or converted_cell is None:
                continue
            converted_value = converted_cell.find(value_tag)
            restored = ElementTree.fromstring(ElementTree.tostring(source_cell))
            style = converted_cell.attrib.get("s")
            try:
                style_is_valid = style is not None and 0 <= int(style) < valid_style_count
            except ValueError:
                style_is_valid = False
            if style_is_valid:
                restored.set("s", str(style))
            else:
                source_style = restored.attrib.get("s")
                try:
                    source_style_is_valid = (
                        source_style is not None
                        and 0 <= int(source_style) < valid_style_count
                    )
                except ValueError:
                    source_style_is_valid = False
                if not source_style_is_valid:
                    restored.attrib.pop("s", None)
            for value in list(restored.findall(value_tag)):
                restored.remove(value)
            if converted_value is not None:
                restored.append(
                    ElementTree.fromstring(ElementTree.tostring(converted_value))
                )
            parent = next(
                row
                for row in converted_sheet_data
                if row.tag == row_tag and converted_cell in row
            )
            index = list(parent).index(converted_cell)
            parent.remove(converted_cell)
            parent.insert(index, restored)
            converted_cells[coordinate] = restored
            restored_cells += 1

    ElementTree.register_namespace("", namespace)
    return (
        ElementTree.tostring(converted_root, encoding="utf-8", xml_declaration=True),
        len(ranges),
        restored_cells,
    )


def _restore_ooxml_array_formulas(source: Path, converted: Path) -> dict[str, int]:
    if source.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return {"regions": 0, "cells": 0}
    try:
        with zipfile.ZipFile(source, "r") as source_package, zipfile.ZipFile(
            converted, "r"
        ) as converted_package:
            source_parts = _worksheet_parts_by_name(source_package)
            converted_parts = _worksheet_parts_by_name(converted_package)
            valid_style_count = _converted_cell_style_count(converted_package)
            replacements: dict[str, bytes] = {}
            regions = 0
            cells = 0
            for name, source_part in source_parts.items():
                converted_part = converted_parts.get(name)
                if converted_part is None:
                    raise RenderError(
                        f"Worksheet disappeared while restoring array formulas: {name}"
                    )
                repaired, part_regions, part_cells = _restore_array_formula_sheet_xml(
                    source_package.read(source_part),
                    converted_package.read(converted_part),
                    valid_style_count=valid_style_count,
                )
                if part_regions:
                    replacements[converted_part] = repaired
                    regions += part_regions
                    cells += part_cells
        if replacements:
            _replace_ooxml_parts(converted, replacements)
        return {"regions": regions, "cells": cells}
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            f"Could not restore OOXML array formulas after recalculation: {exc}"
        ) from exc


def _restore_array_formula_kinds(source: Path, converted: Path) -> int:
    """Restore CSE array-formula markers while retaining recalculated caches.

    openpyxl and Calc commonly flatten a one-cell CSE formula into a normal
    formula. The evaluator treats array formulas as formula-level cells when
    an error is present, and Excel also preserves their semantics. Copy only
    the ``t=array``/``ref`` marker and formula text; leave the converted cache
    value intact so dependencies remain recalculated.
    """
    if source.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return 0
    replacements: dict[str, bytes] = {}
    restored = 0
    with zipfile.ZipFile(source) as source_package, zipfile.ZipFile(converted) as target_package:
        source_parts = _worksheet_parts_by_name(source_package)
        target_parts = _worksheet_parts_by_name(target_package)
        for name, source_part in source_parts.items():
            target_part = target_parts.get(name)
            if target_part is None:
                continue
            source_root = _parse_inventory_xml(source_package.read(source_part), label=f"source worksheet {name}")
            target_root = _parse_inventory_xml(target_package.read(target_part), label=f"converted worksheet {name}")
            namespace = _xml_namespace(source_root.tag)
            if namespace != _xml_namespace(target_root.tag):
                continue
            cell_tag = f"{{{namespace}}}c"
            formula_tag = f"{{{namespace}}}f"
            source_cells = {c.attrib.get("r"): c for c in source_root.iter(cell_tag) if c.attrib.get("r")}
            target_cells = {c.attrib.get("r"): c for c in target_root.iter(cell_tag) if c.attrib.get("r")}
            changed = 0
            for coordinate, source_cell in source_cells.items():
                source_formula = source_cell.find(formula_tag)
                target_cell = target_cells.get(coordinate)
                target_formula = target_cell.find(formula_tag) if target_cell is not None else None
                if source_formula is None or source_formula.attrib.get("t") != "array" or target_formula is None:
                    continue
                source_text = re.sub(r"\s+", "", source_formula.text or "").lstrip("=").upper()
                target_text = re.sub(r"\s+", "", target_formula.text or "").lstrip("=").upper()
                if source_text != target_text:
                    # The cell was intentionally edited; never reclassify an
                    # edited ordinary formula as the source array formula.
                    continue
                if target_formula.attrib.get("t") == "array" and target_formula.attrib.get("ref") == source_formula.attrib.get("ref"):
                    continue
                target_formula.set("t", "array")
                if source_formula.attrib.get("ref"):
                    target_formula.set("ref", source_formula.attrib["ref"])
                target_formula.text = source_formula.text
                changed += 1
            if changed:
                ElementTree.register_namespace("", namespace)
                replacements[target_part] = ElementTree.tostring(target_root, encoding="utf-8", xml_declaration=True)
                restored += changed
    if replacements:
        _replace_ooxml_parts(converted, replacements)
        _validate_recalculated_file(converted)
    return restored
def _restore_ooxml_formula_text(source: Path, converted: Path) -> int:
    """Restore source formula text while retaining converted calculation caches."""

    if source.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return 0
    try:
        with zipfile.ZipFile(source, "r") as source_package, zipfile.ZipFile(
            converted, "r"
        ) as converted_package:
            source_parts = _worksheet_parts_by_name(source_package)
            converted_parts = _worksheet_parts_by_name(converted_package)
            replacements: dict[str, bytes] = {}
            restored = 0
            for name, source_part in source_parts.items():
                converted_part = converted_parts.get(name)
                if converted_part is None:
                    raise RenderError(
                        f"Worksheet disappeared while restoring formula text: {name}"
                    )
                source_root = _parse_inventory_xml(
                    source_package.read(source_part),
                    label=f"source worksheet {name}",
                )
                target_root = _parse_inventory_xml(
                    converted_package.read(converted_part),
                    label=f"converted worksheet {name}",
                )
                namespace = _xml_namespace(target_root.tag)
                if namespace != _xml_namespace(source_root.tag):
                    raise RenderError("OOXML worksheet namespaces changed during formula restoration")
                cell_tag = f"{{{namespace}}}c"
                formula_tag = f"{{{namespace}}}f"
                source_cells = {
                    str(cell.attrib["r"]): cell
                    for cell in source_root.iter(cell_tag)
                    if cell.attrib.get("r") and cell.find(formula_tag) is not None
                }
                target_cells = {
                    str(cell.attrib["r"]): cell
                    for cell in target_root.iter(cell_tag)
                    if cell.attrib.get("r") and cell.find(formula_tag) is not None
                }
                sheet_restored = 0
                for coordinate, source_cell in source_cells.items():
                    target_cell = target_cells.get(coordinate)
                    if target_cell is None:
                        continue
                    source_formula = source_cell.find(formula_tag)
                    target_formula = target_cell.find(formula_tag)
                    assert source_formula is not None and target_formula is not None
                    if source_formula.attrib.get("t") in {"array", "dataTable"}:
                        continue
                    source_text = source_formula.text or ""
                    if (target_formula.text or "") == source_text and target_formula.attrib == source_formula.attrib:
                        continue
                    target_formula.attrib.clear()
                    target_formula.attrib.update(source_formula.attrib)
                    target_formula.text = source_text
                    sheet_restored += 1
                if sheet_restored:
                    ElementTree.register_namespace("", namespace)
                    replacements[converted_part] = ElementTree.tostring(
                        target_root, encoding="utf-8", xml_declaration=True
                    )
                    restored += sheet_restored
        if replacements:
            _replace_ooxml_parts(converted, replacements)
            _validate_recalculated_file(converted)
        return restored
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            f"Could not restore OOXML formula text after recalculation: {exc}"
        ) from exc


def _restore_ooxml_data_tables(
    source: Path,
    converted: Path,
    *,
    anchor_values: Mapping[tuple[str, str], int | float] | None = None,
    body_values: Mapping[tuple[str, str], int | float] | None = None,
) -> dict[str, int]:
    """Restore Excel What-If Data Table XML that LibreOffice cannot preserve."""

    if source.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return {"regions": 0, "cells": 0}
    try:
        with zipfile.ZipFile(source, "r") as source_package, zipfile.ZipFile(
            converted, "r"
        ) as converted_package:
            source_parts = _worksheet_parts_by_name(source_package)
            converted_parts = _worksheet_parts_by_name(converted_package)
            valid_style_count = _converted_cell_style_count(converted_package)
            replacements: dict[str, bytes] = {}
            regions = 0
            cells = 0
            for name, source_part in source_parts.items():
                converted_part = converted_parts.get(name)
                if converted_part is None:
                    raise RenderError(
                        f"Worksheet disappeared while restoring data tables: {name}"
                    )
                repaired, part_regions, part_cells = _restore_data_table_sheet_xml(
                    source_package.read(source_part),
                    converted_package.read(converted_part),
                    anchor_values={
                        coordinate: value
                        for (sheet_name, coordinate), value in (anchor_values or {}).items()
                        if sheet_name == name
                    },
                    body_values={
                        coordinate: value
                        for (sheet_name, coordinate), value in (body_values or {}).items()
                        if sheet_name == name
                    },
                    valid_style_count=valid_style_count,
                )
                if part_regions:
                    replacements[converted_part] = repaired
                    regions += part_regions
                    cells += part_cells
        if replacements:
            _replace_ooxml_parts(converted, replacements)
        return {"regions": regions, "cells": cells}
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            f"Could not restore OOXML data tables after recalculation: {exc}"
        ) from exc


def _ooxml_has_data_tables(path: Path) -> bool:
    if path.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return False
    with zipfile.ZipFile(path, "r") as package:
        for part in _worksheet_parts_by_name(package).values():
            root = _parse_inventory_xml(package.read(part), label="worksheet")
            namespace = _xml_namespace(root.tag)
            if namespace not in {
                _TRANSITIONAL_SPREADSHEETML_NAMESPACE,
                _STRICT_SPREADSHEETML_NAMESPACE,
            }:
                continue
            for formula in root.iter(f"{{{namespace}}}f"):
                if formula.attrib.get("t") == "dataTable":
                    return True
    return False


def ooxml_has_data_tables(path: str | Path) -> bool:
    """Return whether an OOXML workbook contains Excel What-If Data Tables."""

    candidate = _validate_source(path)
    return _ooxml_has_data_tables(candidate)


@dataclass(frozen=True)
class _DataTableAnchorCase:
    sheet: str
    anchor: str
    result_cell: str
    row_input: str
    column_input: str
    row_header_value: Any
    column_header_value: Any
    final_row_header_value: Any
    final_column_header_value: Any
    # Each item is (table coordinate, vertical/header value, horizontal/header
    # value).  LibreOffice drops native What-If table body caches on a save, so
    # the recalculation pass can repopulate the complete table rather than only
    # its top-left anchor.
    scenarios: tuple[tuple[str, Any, Any], ...] = ()


def _data_table_anchor_cases(source: Path, converted: Path) -> list[_DataTableAnchorCase]:
    keep_vba = source.suffix.lower() == ".xlsm"
    source_workbook = load_workbook(source, data_only=False, keep_vba=keep_vba)
    converted_workbook = load_workbook(converted, data_only=True, read_only=True)
    cases: list[_DataTableAnchorCase] = []
    try:
        for worksheet in source_workbook.worksheets:
            if worksheet.title not in converted_workbook.sheetnames:
                continue
            converted_sheet = converted_workbook[worksheet.title]
            for row in worksheet.iter_rows():
                for cell in row:
                    formula = cell.value
                    if not isinstance(formula, DataTableFormula) or not formula.dt2D:
                        continue
                    if not formula.r1 or not formula.r2:
                        continue
                    min_col, min_row, max_col, max_row = range_boundaries(formula.ref)
                    if min_col <= 1 or min_row <= 1:
                        continue
                    anchor = f"{get_column_letter(min_col)}{min_row}"
                    result_cell = f"{get_column_letter(min_col - 1)}{min_row - 1}"
                    row_header = f"{get_column_letter(min_col - 1)}{min_row}"
                    column_header = f"{get_column_letter(min_col)}{min_row - 1}"
                    row_header_value = converted_sheet[row_header].value
                    column_header_value = converted_sheet[column_header].value
                    if row_header_value is None or column_header_value is None:
                        continue
                    scenarios: list[tuple[str, Any, Any]] = []
                    for scenario_row in range(min_row, max_row + 1):
                        vertical_value = converted_sheet.cell(
                            scenario_row, min_col - 1
                        ).value
                        if vertical_value is None:
                            continue
                        for scenario_column in range(min_col, max_col + 1):
                            horizontal_value = converted_sheet.cell(
                                min_row - 1, scenario_column
                            ).value
                            if horizontal_value is None:
                                continue
                            scenarios.append(
                                (
                                    f"{get_column_letter(scenario_column)}{scenario_row}",
                                    vertical_value,
                                    horizontal_value,
                                )
                            )
                    cases.append(
                        _DataTableAnchorCase(
                            sheet=worksheet.title,
                            anchor=anchor,
                            result_cell=result_cell,
                            row_input=str(formula.r1).replace("$", ""),
                            column_input=str(formula.r2).replace("$", ""),
                            row_header_value=row_header_value,
                            column_header_value=column_header_value,
                            final_row_header_value=converted_sheet.cell(
                                max_row, min_col - 1
                            ).value,
                            final_column_header_value=converted_sheet.cell(
                                min_row - 1, max_col
                            ).value,
                            scenarios=tuple(scenarios),
                        )
                    )
    finally:
        source_workbook.close()
        converted_workbook.close()
    return cases


def _apply_data_table_final_scenario_inputs(
    workbook: Any,
    cases: Sequence[_DataTableAnchorCase],
) -> dict[str, set[str]]:
    """Prepare the cache side effect left by the final Excel data table.

    Excel restores the row-input formula while the last column-input scenario is
    still active.  That one formula cache can therefore retain the scenario value,
    even though both input formulas are present in the saved file.  Other formula
    caches must remain from the ordinary recalculation or they become contaminated
    by the what-if inputs.
    """

    if not cases:
        return {}
    case = cases[-1]
    worksheet = workbook[case.sheet]
    row_input = worksheet[case.row_input]
    is_formula = row_input.data_type == "f" or isinstance(
        row_input.value, (ArrayFormula, DataTableFormula)
    )
    if not is_formula or case.row_input == case.column_input:
        return {}
    worksheet[case.column_input] = case.final_row_header_value
    return {case.sheet: {case.row_input}}


def _evaluate_data_table_anchors(
    source: Path,
    converted: Path,
    *,
    work_dir: Path,
    binary: str,
    target_format: str,
    timeout_seconds: float,
) -> tuple[
    dict[tuple[str, str], int | float],
    dict[tuple[str, str], int | float],
    dict[str, Any],
    Path | None,
    dict[str, set[str]],
]:
    """Evaluate missing two-input Data Table anchors with isolated conversions."""

    cases = _data_table_anchor_cases(source, converted)
    values: dict[tuple[str, str], int | float] = {}
    body_values: dict[tuple[str, str], int | float] = {}
    failures: list[dict[str, str]] = []
    for index, case in enumerate(cases, 1):
        # Evaluate every row/column scenario from a fresh copy.  Native Excel
        # data tables are not ordinary formulas: setting their two input cells
        # and calculating the workbook is the portable way to recover the
        # cached result for a body coordinate after LibreOffice has discarded
        # the table XML.  The number of scenarios is bounded by the table's
        # own ref range and is skipped only when the table has no usable headers.
        scenarios = case.scenarios or (
            (case.anchor, case.row_header_value, case.column_header_value),
        )
        for scenario_index, (coordinate, row_value, column_value) in enumerate(
            scenarios, 1
        ):
            case_root = work_dir / f"case-{index:04d}-{scenario_index:04d}"
            case_root.mkdir(parents=True, exist_ok=True)
            simulation = case_root / source.name
            shutil.copy2(source, simulation)
            workbook = None
            try:
                workbook = load_workbook(
                    simulation,
                    data_only=False,
                    keep_vba=simulation.suffix.lower() == ".xlsm",
                )
                worksheet = workbook[case.sheet]
                worksheet[case.row_input] = column_value
                worksheet[case.column_input] = row_value
                workbook.save(simulation)
                workbook.close()
                workbook = None
                recalculated = _convert_with_libreoffice(
                    simulation,
                    case_root / "converted",
                    target_format=target_format,
                    binary=binary,
                    timeout_seconds=timeout_seconds,
                    iterative_calculation=True,
                    calculate_all=True,
                )
                evaluated = load_workbook(recalculated, data_only=True, read_only=True)
                try:
                    value = evaluated[case.sheet][case.result_cell].value
                finally:
                    evaluated.close()
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise RenderError(
                        f"Data Table scenario {coordinate} produced a non-numeric value: {value!r}"
                    )
                body_values[(case.sheet, coordinate)] = value
                if coordinate == case.anchor:
                    values[(case.sheet, case.anchor)] = value
            except Exception as exc:
                failures.append(
                    {
                        "sheet": case.sheet,
                        "anchor": case.anchor,
                        "coordinate": coordinate,
                        "error_type": type(exc).__name__,
                    }
                )
            finally:
                if workbook is not None:
                    workbook.close()
    final_scenario: Path | None = None
    final_scenario_cache_coordinates: dict[str, set[str]] = {}
    if cases:
        case = cases[-1]
        case_root = work_dir / "final-scenario"
        case_root.mkdir(parents=True, exist_ok=True)
        simulation = case_root / source.name
        shutil.copy2(source, simulation)
        workbook = None
        try:
            workbook = load_workbook(
                simulation,
                data_only=False,
                keep_vba=simulation.suffix.lower() == ".xlsm",
            )
            final_scenario_cache_coordinates = _apply_data_table_final_scenario_inputs(
                workbook,
                cases,
            )
            if final_scenario_cache_coordinates:
                workbook.save(simulation)
            workbook.close()
            workbook = None
            if final_scenario_cache_coordinates:
                final_scenario = _convert_with_libreoffice(
                    simulation,
                    case_root / "converted",
                    target_format=target_format,
                    binary=binary,
                    timeout_seconds=timeout_seconds,
                    iterative_calculation=True,
                    calculate_all=True,
                )
        except Exception as exc:
            failures.append(
                {
                    "sheet": case.sheet,
                    "anchor": case.anchor,
                    "error_type": f"FinalScenario{type(exc).__name__}",
                }
            )
        finally:
            if workbook is not None:
                workbook.close()
    return values, body_values, {
        "attempted": sum(len(case.scenarios) or 1 for case in cases),
        "evaluated": len(body_values),
        "anchor_evaluated": len(values),
        "failures": failures,
        "final_scenario": (
            {
                "table_count": len(cases),
                "last_sheet": cases[-1].sheet,
                "last_anchor": cases[-1].anchor,
                "selected_formula_inputs": sum(
                    len(coordinates)
                    for coordinates in final_scenario_cache_coordinates.values()
                ),
            }
            if final_scenario is not None
            else None
        ),
    }, final_scenario, final_scenario_cache_coordinates


def _requires_iterative_calculation(path: Path) -> bool:
    """Detect an explicit iteration flag or conventional ``circ`` defined name."""
    if path.suffix.lower() not in _SHEET_INVENTORY_FORMATS:
        return False
    try:
        with zipfile.ZipFile(path) as package:
            root = ElementTree.fromstring(package.read(_WORKBOOK_XML_PART))
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1]
            if local_name == "calcPr" and str(element.get("iterate", "")).casefold() in {
                "1",
                "true",
            }:
                return True
        return False
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return False


def _enable_ooxml_iterative_calculation(path: Path) -> None:
    """Set Excel iteration properties on a private OOXML recalculation copy."""
    with zipfile.ZipFile(path) as package:
        root = ElementTree.fromstring(package.read(_WORKBOOK_XML_PART))
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    calc_properties = next(
        (
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "calcPr"
        ),
        None,
    )
    if calc_properties is None:
        tag = f"{{{namespace}}}calcPr" if namespace else "calcPr"
        calc_properties = ElementTree.SubElement(root, tag)
    calc_properties.set("iterate", "1")
    calc_properties.set("iterateCount", "100")
    calc_properties.set("iterateDelta", "0.0001")
    if namespace:
        ElementTree.register_namespace("", namespace)
    _replace_ooxml_parts(
        path,
        {
            _WORKBOOK_XML_PART: ElementTree.tostring(
                root, encoding="utf-8", xml_declaration=True
            )
        },
    )


def recalculate_workbook(
    source: str | Path,
    destination: str | Path,
    *,
    libreoffice_binary: str | Path | None = None,
    timeout_seconds: float = 120.0,
    cache_seed: str | Path | None = None,
) -> dict[str, Any]:
    """Recalculate a private copy and atomically publish the verified result.

    ``source`` and ``destination`` may be the same path for an isolated session
    working copy.  LibreOffice still receives only a temporary copy; the
    destination is replaced only after conversion and validation succeed.
    """

    source_path = _validate_source(source)
    destination_path = Path(destination).expanduser().resolve()
    destination_format = destination_path.suffix.lower()
    target_format = _RECALCULATION_FORMATS.get(destination_format)
    if target_format is None:
        supported = ", ".join(sorted(_RECALCULATION_FORMATS))
        raise RenderError(
            f"Unsupported recalculation destination {destination_format!r}; expected {supported}"
        )
    if timeout_seconds <= 0:
        raise RenderError("timeout_seconds must be positive")
    binary = find_libreoffice(libreoffice_binary)
    if binary is None:
        raise RenderError("LibreOffice executable was not found")

    inventory_enforced = bool(
        source_path.suffix.lower() in _SHEET_INVENTORY_FORMATS
        and destination_format in _SHEET_INVENTORY_FORMATS
    )
    source_identity = (
        sheet_inventory_identity(source_path) if inventory_enforced else None
    )
    source_hash = (
        str(source_identity["workbook_sha256"])
        if source_identity is not None
        else sha256_file(source_path)
    )
    source_requested_iteration = _requires_iterative_calculation(source_path)
    # SpreadsheetBench's official materialization step enables iteration for
    # every workbook before calculateAll(). This also handles circular models
    # whose OOXML calcPr metadata was lost during editing.
    iterative_calculation = True
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spreadsheet-recalculate-") as raw_work:
        work = Path(raw_work)
        private_source = work / "source" / source_path.name
        private_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, private_source)
        formula_cache_seed = {
            "sheets": 0,
            "formula_cells": 0,
            "cyclic_formula_cells": 0,
            "dirty_formula_cells": 0,
        }
        if cache_seed is not None and private_source.suffix.lower() in _SHEET_INVENTORY_FORMATS:
            cache_seed_path = _validate_source(cache_seed)
            formula_cache_seed = _seed_ooxml_formula_cached_values(
                private_source, cache_seed_path
            )
            # openpyxl represents one-cell CSE formulas as ordinary strings
            # after a model edit.  Restore only matching, unchanged array
            # formula markers before Calc sees the workbook; otherwise Calc
            # evaluates an array expression such as AVERAGE(range/100) using
            # scalar semantics and writes the wrong cache.
            formula_cache_seed["array_formula_kinds_restored"] = _restore_array_formula_kinds(
                cache_seed_path, private_source
            )
        if (
            iterative_calculation
            and private_source.suffix.lower() in _SHEET_INVENTORY_FORMATS
        ):
            _enable_ooxml_iterative_calculation(private_source)
        converted = _convert_with_libreoffice(
            private_source,
            work / "converted",
            target_format=target_format,
            binary=binary,
            timeout_seconds=timeout_seconds,
            iterative_calculation=iterative_calculation,
            calculate_all=True,
        )
        _validate_recalculated_file(converted)
        preliminary_output_identity = (
            sheet_inventory_identity(converted) if inventory_enforced else None
        )
        inventory_precheck_matched = bool(
            source_identity is not None
            and preliminary_output_identity is not None
            and source_identity["sheets"] == preliminary_output_identity["sheets"]
        )
        data_table_anchor_values: dict[tuple[str, str], int | float] = {}
        data_table_body_values: dict[tuple[str, str], int | float] = {}
        data_table_anchor_evaluation: dict[str, Any] = {
            "attempted": 0,
            "evaluated": 0,
            "failures": [],
        }
        has_data_tables = bool(
            inventory_enforced
            and inventory_precheck_matched
            and _ooxml_has_data_tables(private_source)
        )
        if has_data_tables:
            (
                data_table_anchor_values,
                data_table_body_values,
                data_table_anchor_evaluation,
                final_data_table_scenario,
                final_data_table_scenario_coordinates,
            ) = (
                _evaluate_data_table_anchors(
                    private_source,
                    converted,
                    work_dir=work / "data-table-anchor-evaluation",
                    binary=binary,
                    target_format=target_format,
                    timeout_seconds=timeout_seconds,
                )
            )
            if final_data_table_scenario is not None:
                data_table_anchor_evaluation["final_scenario_cache_transplants"] = (
                    transplant_ooxml_formula_cached_values(
                        final_data_table_scenario,
                        converted,
                        exclude_data_table_formulas=True,
                        selected_coordinates=final_data_table_scenario_coordinates,
                    )
                )
        data_table_restoration = (
            _restore_ooxml_data_tables(
                private_source,
                converted,
                anchor_values=data_table_anchor_values,
                body_values=data_table_body_values,
            )
            if has_data_tables
            else {"regions": 0, "cells": 0}
        )
        # Do not let a structural row insertion turn native what-if tables into
        # literal TABLE formulas.  The restoration routine above already writes
        # evaluated anchor caches; keep this explicit guard here so callers that
        # pass a modified workbook still receive the same native-table semantics.
        array_formula_restoration = (
            _restore_ooxml_array_formulas(private_source, converted)
            if inventory_enforced and inventory_precheck_matched
            else {"regions": 0, "cells": 0}
        )
        array_formula_kind_restoration = (
            _restore_array_formula_kinds(private_source, converted)
            if inventory_enforced and inventory_precheck_matched
            else 0
        )
        formula_text_restoration = (
            _restore_ooxml_formula_text(private_source, converted)
            if inventory_enforced and inventory_precheck_matched
            else 0
        )
        if (
            data_table_restoration["regions"]
            or array_formula_restoration["regions"]
            or formula_text_restoration
        ):
            _validate_recalculated_file(converted)
        output_identity = (
            sheet_inventory_identity(converted)
            if inventory_enforced
            and (
                data_table_restoration["regions"]
                or array_formula_restoration["regions"]
                or formula_text_restoration
            )
            else preliminary_output_identity
        )
        output_hash = (
            str(output_identity["workbook_sha256"])
            if output_identity is not None
            else sha256_file(converted)
        )
        inventory_integrity = (
            {
                "schema_version": 2,
                "policy": RECALCULATION_SHEET_INTEGRITY_POLICY,
                "enforced": True,
                "matched": source_identity["sheets"] == output_identity["sheets"],
                "pre": source_identity,
                "post": output_identity,
            }
            if source_identity is not None and output_identity is not None
            else {
                "schema_version": 2,
                "policy": RECALCULATION_SHEET_INTEGRITY_POLICY,
                "enforced": False,
                "matched": None,
                "pre": None,
                "post": None,
                "reason": "source-or-destination-is-not-ooxml",
            }
        )
        metadata = {
            "backend": "libreoffice-headless",
            "version": libreoffice_version(binary),
            "profile": "isolated-per-invocation",
            "iterative_calculation": {
                "enabled": iterative_calculation,
                "steps": 100 if iterative_calculation else None,
                "minimum_change": 0.0001 if iterative_calculation else None,
                "source_requested": source_requested_iteration,
            },
            "calculation_mode": "uno-calculate-all",
            "source_path": str(source_path),
            "destination_path": str(destination_path),
            "source_sha256": source_hash,
            "output_sha256": output_hash,
            "format": destination_format.lstrip("."),
            "data_table_restoration": data_table_restoration,
            "array_formula_restoration": array_formula_restoration,
            "array_formula_kind_restoration": array_formula_kind_restoration,
            "formula_text_restoration": formula_text_restoration,
            "formula_cache_seed": formula_cache_seed,
            "data_table_anchor_evaluation": data_table_anchor_evaluation,
            "sheet_inventory_integrity": inventory_integrity,
        }
        if inventory_integrity["enforced"] and not inventory_integrity["matched"]:
            metadata.update(
                {
                    "atomic_replace": False,
                    "published": False,
                    "failure_artifact_sha256": output_hash,
                }
            )
            try:
                failure_artifact = _publish_recalculation_failure_artifact(
                    converted,
                    destination_path,
                    output_hash,
                )
            except Exception as exc:
                metadata.update(
                    {
                        "failure_artifact_path": None,
                        "failure_artifact_error_type": type(exc).__name__,
                    }
                )
                raise RecalculationIntegrityError(
                    "Recalculation changed sheet identity and its post-recalculation "
                    "artifact could not be preserved",
                    evidence=metadata,
                ) from exc
            metadata["failure_artifact_path"] = str(failure_artifact)
            raise RecalculationIntegrityError(
                "Recalculation changed sheet kind, order, name, or visibility",
                evidence=metadata,
            )

        descriptor, raw_temporary = tempfile.mkstemp(
            dir=destination_path.parent,
            prefix=f".{destination_path.stem}.recalculated-",
            suffix=destination_path.suffix,
        )
        os.close(descriptor)
        temporary_destination = Path(raw_temporary)
        try:
            shutil.copy2(converted, temporary_destination)
            _validate_recalculated_file(temporary_destination)
            if sha256_file(temporary_destination) != output_hash:
                raise RenderError("Recalculated workbook changed before atomic publication")
            temporary_destination.replace(destination_path)
        finally:
            temporary_destination.unlink(missing_ok=True)

    metadata.update({"atomic_replace": True, "published": True})
    return metadata


def _validate_source(source: str | Path) -> Path:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise RenderError(f"Spreadsheet does not exist or is not a file: {source_path}")
    if source_path.suffix.lower() not in SUPPORTED_SPREADSHEET_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_SPREADSHEET_EXTENSIONS))
        raise RenderError(
            f"Unsupported spreadsheet format {source_path.suffix!r}; expected {supported}"
        )
    return source_path


def _make_single_sheet_copy(source_copy: Path, destination: Path, sheet_name: str) -> None:
    """Create a disposable OOXML copy with only *sheet_name* visible."""

    keep_vba = source_copy.suffix.lower() == ".xlsm"
    workbook = load_workbook(source_copy, read_only=False, keep_vba=keep_vba, data_only=False)
    try:
        if sheet_name not in workbook.sheetnames:
            raise RenderError(f"Worksheet disappeared while rendering: {sheet_name}")
        target_index = workbook.sheetnames.index(sheet_name)
        for worksheet in workbook.worksheets:
            worksheet.sheet_state = "visible" if worksheet.title == sheet_name else "hidden"
        workbook.active = target_index
        if workbook.views:
            workbook.views[0].activeTab = target_index
        workbook.save(destination)
    finally:
        workbook.close()


def _sheet_names(source_copy: Path) -> list[str]:
    keep_vba = source_copy.suffix.lower() == ".xlsm"
    workbook = load_workbook(source_copy, read_only=True, keep_vba=keep_vba, data_only=False)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def _pymupdf_module() -> Any:
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        try:
            import fitz

            return fitz
        except ImportError as exc:  # pragma: no cover - declared project dependency
            raise RenderError("PyMuPDF is required to rasterize LibreOffice PDFs") from exc


def pymupdf_version() -> str:
    module = _pymupdf_module()
    for attribute in ("VersionBind", "__version__"):
        value = getattr(module, attribute, None)
        if value:
            return str(value)
    version_tuple = getattr(module, "version", None)
    if version_tuple:
        return str(version_tuple[0])
    return "unknown"


def _rasterize_pdf(
    pdf_path: Path,
    output_dir: Path,
    *,
    dpi: int,
    filename_prefix: str,
    sheet: str | None,
) -> list[_RasterizedPage]:
    module = _pymupdf_module()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        document = module.open(str(pdf_path))
    except Exception as exc:
        raise RenderError(f"PyMuPDF could not open {pdf_path.name}: {exc}") from exc

    rendered: list[_RasterizedPage] = []
    try:
        if document.page_count < 1:
            raise RenderError(f"LibreOffice produced an empty PDF for {pdf_path.name}")
        scale = dpi / 72.0
        matrix = module.Matrix(scale, scale)
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            png_path = output_dir / f"{filename_prefix}-{page_index + 1:04d}.png"
            pixmap.save(str(png_path))
            rendered.append(
                _RasterizedPage(
                    path=png_path,
                    width=int(pixmap.width),
                    height=int(pixmap.height),
                    sheet=sheet,
                    sheet_page=page_index + 1 if sheet is not None else None,
                )
            )
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"PyMuPDF failed to rasterize {pdf_path.name}: {exc}") from exc
    finally:
        document.close()
    return rendered


def _render_per_sheet(
    source_copy: Path,
    work_dir: Path,
    *,
    binary: str,
    dpi: int,
    timeout_seconds: float,
) -> list[_RasterizedPage]:
    sheets = _sheet_names(source_copy)
    if not sheets:
        raise RenderError("Workbook contains no worksheets")

    rendered: list[_RasterizedPage] = []
    for sheet_index, sheet_name in enumerate(sheets, start=1):
        sheet_dir = work_dir / f"sheet-{sheet_index:04d}"
        sheet_dir.mkdir(parents=True, exist_ok=True)
        private_book = sheet_dir / f"workbook{source_copy.suffix.lower()}"
        _make_single_sheet_copy(source_copy, private_book, sheet_name)
        pdf_path = _convert_with_libreoffice(
            private_book,
            sheet_dir / "pdf",
            target_format="pdf",
            binary=binary,
            timeout_seconds=timeout_seconds,
        )
        rendered.extend(
            _rasterize_pdf(
                pdf_path,
                work_dir / "png",
                dpi=dpi,
                filename_prefix=f"sheet-{sheet_index:04d}-page",
                sheet=sheet_name,
            )
        )
    return rendered


def _render_whole_workbook(
    source_copy: Path,
    work_dir: Path,
    *,
    binary: str,
    dpi: int,
    timeout_seconds: float,
) -> list[_RasterizedPage]:
    pdf_path = _convert_with_libreoffice(
        source_copy,
        work_dir / "pdf",
        target_format="pdf",
        binary=binary,
        timeout_seconds=timeout_seconds,
    )
    return _rasterize_pdf(
        pdf_path,
        work_dir / "png",
        dpi=dpi,
        filename_prefix="workbook-page",
        sheet=None,
    )


def _publish_pages(
    temporary_pages: Sequence[_RasterizedPage], output_dir: Path
) -> tuple[RenderPage, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    destinations = [output_dir / page.path.name for page in temporary_pages]
    collisions = [path for path in destinations if path.exists()]
    if collisions:
        raise RenderError(f"Refusing to overwrite render artifact: {collisions[0]}")

    published: list[RenderPage] = []
    for index, (temporary, destination) in enumerate(
        zip(temporary_pages, destinations, strict=True), start=1
    ):
        shutil.copy2(temporary.path, destination)
        published.append(
            RenderPage(
                index=index,
                path=destination,
                sha256=sha256_file(destination),
                width=temporary.width,
                height=temporary.height,
                sheet=temporary.sheet,
                sheet_page=temporary.sheet_page,
            )
        )
    return tuple(published)


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    if path.exists():
        raise RenderError(f"Refusing to overwrite render manifest: {path}")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def render_workbook(
    source: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 144,
    per_sheet: bool = True,
    libreoffice_binary: str | Path | None = None,
    timeout_seconds: float = 120.0,
) -> RenderResult:
    """Render a spreadsheet to PNG pages through LibreOffice and PyMuPDF.

    OOXML workbooks are rendered one sheet at a time when possible by hiding all
    other sheets in disposable copies.  If that strategy fails, the function
    retries with a whole-workbook PDF.  ODS, XLS, and CSV inputs use the whole
    workbook path directly because modifying their sheet visibility would
    require changing the original format.
    """

    source_path = _validate_source(source)
    if dpi <= 0:
        raise RenderError("dpi must be a positive integer")
    if timeout_seconds <= 0:
        raise RenderError("timeout_seconds must be positive")
    binary = find_libreoffice(libreoffice_binary)
    if binary is None:
        raise RenderError("LibreOffice executable was not found")

    destination = Path(output_dir).expanduser().resolve()
    if destination == source_path or source_path in destination.parents:
        # A directory can never equal a regular source file, but retaining this
        # guard makes the no-overwrite invariant explicit for unusual paths.
        if destination == source_path:
            raise RenderError("Render output directory cannot be the source workbook")
    source_hash = sha256_file(source_path)
    fallback_reason: str | None = None

    with tempfile.TemporaryDirectory(prefix="spreadsheet-render-") as raw_work:
        work = Path(raw_work)
        source_copy = work / "source" / source_path.name
        source_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, source_copy)

        mode = "whole_workbook"
        temporary_pages: list[_RasterizedPage]
        if per_sheet and source_path.suffix.lower() in {".xlsx", ".xlsm"}:
            try:
                temporary_pages = _render_per_sheet(
                    source_copy,
                    work / "per-sheet",
                    binary=binary,
                    dpi=dpi,
                    timeout_seconds=timeout_seconds,
                )
                mode = "per_sheet"
            except Exception as exc:
                fallback_reason = f"{type(exc).__name__}: {exc}"
                temporary_pages = _render_whole_workbook(
                    source_copy,
                    work / "whole-workbook",
                    binary=binary,
                    dpi=dpi,
                    timeout_seconds=timeout_seconds,
                )
        else:
            temporary_pages = _render_whole_workbook(
                source_copy,
                work / "whole-workbook",
                binary=binary,
                dpi=dpi,
                timeout_seconds=timeout_seconds,
            )

        if not temporary_pages:
            raise RenderError("Rendering produced no PNG pages")
        pages = _publish_pages(temporary_pages, destination)

    versions = {
        "libreoffice": libreoffice_version(binary),
        "pymupdf": pymupdf_version(),
    }
    manifest_path = destination / "render-manifest.json"
    result = RenderResult(
        source=source_path,
        output_dir=destination,
        manifest_path=manifest_path,
        backend="libreoffice-headless+pymupdf",
        version=versions,
        source_sha256=source_hash,
        mode=mode,
        dpi=int(dpi),
        pages=pages,
    )
    manifest = result.to_dict()
    if fallback_reason is not None:
        manifest["fallback"] = {"from": "per_sheet", "reason": fallback_reason}
    _write_manifest(manifest_path, manifest)
    return result


def read_png(path: str | Path) -> bytes:
    """Return the exact PNG bytes for a view-image tool, without re-encoding."""

    png_path = Path(path).expanduser().resolve()
    try:
        data = png_path.read_bytes()
    except OSError as exc:
        raise RenderError(f"Could not read PNG {png_path}: {exc}") from exc
    if not data.startswith(PNG_SIGNATURE):
        raise RenderError(f"Not a PNG file: {png_path}")
    return data


# Small, discoverable aliases for callers that prefer verb-style APIs.
render = render_workbook
load_png = read_png


__all__ = [
    "RenderPage",
    "RenderResult",
    "RECALCULATION_SHEET_INTEGRITY_POLICY",
    "SUPPORTED_SPREADSHEET_EXTENSIONS",
    "convert_spreadsheet_copy",
    "find_libreoffice",
    "isolated_user_profile",
    "libreoffice_command",
    "libreoffice_version",
    "load_png",
    "ooxml_has_data_tables",
    "patch_font_colors_ooxml",
    "pymupdf_version",
    "read_png",
    "recalculate_workbook",
    "render",
    "render_workbook",
    "restore_ooxml_cell_contents",
    "sheet_inventory_identity",
    "sha256_file",
    "transplant_ooxml_formula_cached_values",
]
