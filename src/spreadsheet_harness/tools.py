"""Model-facing spreadsheet tool registry."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .code_interpreter import LocalCodeInterpreter
from .errors import CodeIsolationError, HarnessError, ToolInputError
from .evidence_contract import (
    PIXEL_SHA256_ALGORITHM,
    ArtifactRef,
    ContractMode,
    ContractSpec,
    ContractStateError,
    EffectKind,
    EventKind,
    EvidenceContractMonitor,
    EvidenceEvent,
    EvidenceScope,
)
from .session import WorkbookSession
from .target_grounding import TargetGroundingMode

_LATEX_MAX_CELLS = 500
_LATEX_MAX_CHARS = 65_536
_LATEX_MAX_CELL_CHARS = 512
_STYLE_MAX_GROUPS = 64
_STYLE_MAX_SAMPLE_CELLS = 24
_STYLE_MAX_TEXT_CHARS = 160
_INSPECT_DEFAULT_INCLUDE_STYLES = False
_CALC_ERROR_VALUES = frozenset(
    {
        "#DIV/0!",
        "#N/A",
        "#NAME?",
        "#NULL!",
        "#NUM!",
        "#REF!",
        "#VALUE!",
    }
)
_DIRECT_MUTATION_TOOLS = frozenset(
    {
        "write_range",
        "fill_formula",
        "format_range",
        "clear_range",
        "delete_rows",
        "delete_columns",
        "manage_sheet",
        "undo_last",
        "code_interpreter",
    }
)
TARGET_GROUNDING_CONTROL_TOOLS = frozenset(
    {"inspect_range", "declare_edit_target"}
)


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _latex_escape(value: Any, *, max_chars: int) -> tuple[str, bool]:
    """Escape one scalar without ever cutting through a LaTeX escape sequence."""
    if value is None:
        return "", False
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "\n": r"\newline{}",
        "\t": " ",
    }
    output: list[str] = []
    length = 0
    truncated = False
    for character in str(value).replace("\r\n", "\n").replace("\r", "\n"):
        token = replacements.get(character, character if character.isprintable() else " ")
        if length + len(token) > max_chars:
            truncated = True
            break
        output.append(token)
        length += len(token)
    if truncated:
        marker = r"\ldots{}"
        while output and length + len(marker) > max_chars:
            removed = output.pop()
            length -= len(removed)
        if len(marker) <= max_chars:
            output.append(marker)
    return "".join(output), truncated


def _style_summary(cells: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[int, dict[str, Any]] = {}
    for cell in cells:
        style = cell.get("style")
        if not isinstance(style, dict):
            continue
        style_id = int(style.get("style_id", 0))
        group = groups.get(style_id)
        if group is None:
            font = style.get("font") if isinstance(style.get("font"), dict) else {}
            alignment = style.get("alignment") if isinstance(style.get("alignment"), dict) else {}
            group = {
                "style_id": style_id,
                "cell_count": 0,
                "sample_cells": [],
                "sample_cells_truncated": False,
                "number_format": _bounded_text(style.get("number_format"), _STYLE_MAX_TEXT_CHARS),
                "font": {
                    "bold": bool(font.get("bold")),
                    "italic": bool(font.get("italic")),
                    "color": _bounded_text(font.get("color"), _STYLE_MAX_TEXT_CHARS),
                },
                "fill": _bounded_text(style.get("fill"), _STYLE_MAX_TEXT_CHARS),
                "alignment": {
                    "horizontal": _bounded_text(alignment.get("horizontal"), _STYLE_MAX_TEXT_CHARS),
                    "vertical": _bounded_text(alignment.get("vertical"), _STYLE_MAX_TEXT_CHARS),
                    "wrap_text": alignment.get("wrap_text"),
                },
            }
            groups[style_id] = group
        group["cell_count"] += 1
        samples = group["sample_cells"]
        if len(samples) < _STYLE_MAX_SAMPLE_CELLS:
            samples.append(str(cell.get("coordinate", "")))
        else:
            group["sample_cells_truncated"] = True

    all_groups = list(groups.values())
    returned = all_groups[:_STYLE_MAX_GROUPS]
    return {
        "distinct_style_count": len(all_groups),
        "styles_returned": len(returned),
        "truncated": len(returned) < len(all_groups),
        "styles": returned,
    }


def _matrix_to_latex(matrix: list[list[Any]]) -> tuple[str, int, int]:
    row_count = len(matrix)
    column_count = max((len(row) for row in matrix), default=0)
    cell_count = row_count * column_count
    column_spec = "l" * column_count
    prefix = rf"\begin{{tabular}}{{{column_spec}}}" + "\n"
    suffix = "\n" + r"\end{tabular}"
    separator_chars = row_count * len(" \\\\") + max(0, cell_count - row_count) * len(" & ")
    newline_chars = max(0, row_count - 1)
    fixed_chars = len(prefix) + len(suffix) + separator_chars + newline_chars
    available = max(0, _LATEX_MAX_CHARS - fixed_chars)
    per_cell_limit = min(
        _LATEX_MAX_CELL_CHARS,
        available // cell_count if cell_count else _LATEX_MAX_CELL_CHARS,
    )

    rendered_rows: list[str] = []
    truncated_cells = 0
    for row in matrix:
        padded = list(row) + [None] * (column_count - len(row))
        rendered: list[str] = []
        for value in padded:
            escaped, truncated = _latex_escape(value, max_chars=per_cell_limit)
            rendered.append(escaped)
            truncated_cells += int(truncated)
        rendered_rows.append(" & ".join(rendered) + " \\\\")
    latex = prefix + "\n".join(rendered_rows) + suffix
    if len(latex) > _LATEX_MAX_CHARS:  # Defensive guard if structure changes later.
        raise RuntimeError("Internal LaTeX output limit was exceeded")
    return latex, truncated_cells, per_cell_limit


@dataclass
class ToolOutcome:
    data: dict[str, Any]
    image_path: Path | None = None

    def as_output(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class _PendingVisualConfirmation:
    confirmation_id: str
    image_path: Path
    artifact: ArtifactRef
    render_id: str
    render_manifest_sha256: str
    render_mode: str
    page_id: str
    page_index: int
    file_sha256: str
    pixel_sha256: str
    width: int
    height: int
    image_mode: str
    sheet: str | None
    sheet_page: int | None
    cell_scope: EvidenceScope | None


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class SpreadsheetToolRegistry:
    def __init__(
        self,
        session: WorkbookSession,
        *,
        enable_code: bool = True,
        allowed_tools: set[str] | None = None,
        require_code_isolation: bool = False,
        redaction_secrets: tuple[str, ...] = (),
        evidence_contract: ContractSpec | None = None,
        contract_mode: ContractMode = ContractMode.SHADOW,
        enable_target_grounding: bool = False,
        target_grounding_mode: TargetGroundingMode | str | None = None,
    ) -> None:
        self.session = session
        if target_grounding_mode is None:
            resolved_grounding_mode = (
                TargetGroundingMode.ENFORCE
                if bool(enable_target_grounding)
                else TargetGroundingMode.OFF
            )
        else:
            try:
                resolved_grounding_mode = TargetGroundingMode(target_grounding_mode)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "target_grounding_mode must be off, advisory, or enforce"
                ) from exc
            if bool(enable_target_grounding) and (
                resolved_grounding_mode is not TargetGroundingMode.ENFORCE
            ):
                raise ValueError(
                    "enable_target_grounding=True is compatible only with enforce mode"
                )
        raw_session_mode = getattr(
            session,
            "target_grounding_mode",
            TargetGroundingMode.OFF,
        )
        try:
            session_mode = TargetGroundingMode(raw_session_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("session target-grounding mode is invalid") from exc
        if session_mode is not TargetGroundingMode.OFF and (
            session_mode is not resolved_grounding_mode
        ):
            raise ValueError(
                "tool registry target-grounding mode conflicts with the active session"
            )
        self.target_grounding_mode = resolved_grounding_mode
        # Both active treatments expose identical prompts and tool schemas; the
        # resolved policy determines whether a failed assessment blocks publication.
        self.target_grounding_active = (
            resolved_grounding_mode is not TargetGroundingMode.OFF
        )
        self.target_grounding_enforced = (
            resolved_grounding_mode is TargetGroundingMode.ENFORCE
        )
        # Keep the legacy boolean aligned with its historical blocking policy;
        # callers that need tool availability use the explicit active predicate.
        self.target_grounding_enabled = self.target_grounding_enforced
        if self.target_grounding_active:
            self.session.enable_target_grounding(resolved_grounding_mode)
        self._allowed_tools = (
            frozenset(
                set(allowed_tools)
                | (
                    TARGET_GROUNDING_CONTROL_TOOLS
                    if self.target_grounding_active
                    else set()
                )
            )
            if allowed_tools is not None
            else None
        )
        self.interpreter = (
            LocalCodeInterpreter(
                session.workspace,
                session.workbook_path,
                require_isolation=require_code_isolation,
                secrets=redaction_secrets,
            )
            if enable_code
            else None
        )
        self._last_render: dict[str, Any] | None = None
        self._pending_visual_confirmations: dict[
            str, _PendingVisualConfirmation
        ] = {}
        self.evidence_monitor = (
            EvidenceContractMonitor(
                evidence_contract,
                session.artifact_ref().sha256,
                mode=contract_mode,
            )
            if evidence_contract is not None
            else None
        )
        self._handlers: dict[str, Callable[[dict[str, Any]], ToolOutcome]] = {
            "list_sheets": self._list_sheets,
            "inspect_range": self._inspect_range,
            "range_to_latex": self._range_to_latex,
            "find_cells": self._find_cells,
            "write_range": self._write_range,
            "fill_formula": self._fill_formula,
            "format_range": self._format_range,
            "clear_range": self._clear_range,
            "delete_rows": self._delete_rows,
            "delete_columns": self._delete_columns,
            "manage_sheet": self._manage_sheet,
            "recalculate_and_read": self._recalculate_and_read,
            "render_workbook": self._render_workbook,
            "view_image": self._view_image,
            "undo_last": self._undo_last,
        }
        if self.interpreter:
            self._handlers["code_interpreter"] = self._code_interpreter
        if self.target_grounding_active:
            self._handlers["declare_edit_target"] = self._declare_edit_target

    @property
    def schemas(self) -> list[dict[str, Any]]:
        sheet = {"type": "string", "description": "Exact worksheet name."}
        a1 = {"type": "string", "description": "Bounded A1 range such as A1:F20."}
        schemas = [
            self._schema(
                "list_sheets",
                "List worksheets, visibility, used dimensions, merges and tables.",
                _object_schema({}, []),
            ),
            self._schema(
                "inspect_range",
                (
                    "Read formulas, cached values, merges and tables in a bounded range. "
                    "Set include_styles=true only for formatting/layout tasks because style "
                    "details are verbose."
                ),
                _object_schema(
                    {
                        "sheet": sheet,
                        "range_ref": a1,
                        "include_styles": {
                            "type": "boolean",
                            "default": _INSPECT_DEFAULT_INCLUDE_STYLES,
                        },
                    },
                    ["sheet", "range_ref"],
                ),
            ),
            self._schema(
                "range_to_latex",
                "Convert up to 500 cells to a bounded, safely escaped LaTeX tabular and summarize merges and styles.",
                _object_schema({"sheet": sheet, "range_ref": a1}, ["sheet", "range_ref"]),
            ),
            self._schema(
                "find_cells",
                "Search cell values or formulas across one sheet or the workbook.",
                _object_schema(
                    {
                        "query": {"type": "string"},
                        "sheet": {"type": ["string", "null"]},
                        "use_regex": {"type": "boolean", "default": False},
                        "match_case": {"type": "boolean", "default": False},
                        "search_formulas": {"type": "boolean", "default": True},
                    },
                    ["query"],
                ),
            ),
            self._schema(
                "write_range",
                "Write a rectangular 2-D array starting at one cell. Strings beginning with '=' are formulas.",
                _object_schema(
                    {
                        "sheet": sheet,
                        "start_cell": {"type": "string", "description": "Top-left cell, e.g. C4."},
                        "values": {
                            "type": "array",
                            "items": {"type": "array", "items": {}},
                            "minItems": 1,
                        },
                    },
                    ["sheet", "start_cell", "values"],
                ),
            ),
            self._schema(
                "fill_formula",
                (
                    "Translate one source formula into every cell of a target range, preserving "
                    "relative references. Returns sample translated formulas and warnings for "
                    "ranges that may drift during fill. If target_range is a single cell "
                    "different from source_cell, it is treated as the endpoint of source_cell:target_range."
                ),
                _object_schema(
                    {"sheet": sheet, "source_cell": {"type": "string"}, "target_range": a1},
                    ["sheet", "source_cell", "target_range"],
                ),
            ),
            self._schema(
                "format_range",
                "Apply number/font/fill/alignment/border/size formatting without changing values.",
                _object_schema(
                    {"sheet": sheet, "range_ref": a1, "format_spec": {"type": "object"}},
                    ["sheet", "range_ref", "format_spec"],
                ),
            ),
            self._schema(
                "clear_range",
                "Clear cell contents and optionally formatting in a bounded range.",
                _object_schema(
                    {
                        "sheet": sheet,
                        "range_ref": a1,
                        "contents": {"type": "boolean", "default": True},
                        "formats": {"type": "boolean", "default": False},
                    },
                    ["sheet", "range_ref"],
                ),
            ),
            self._schema(
                "delete_rows",
                "Delete worksheet rows. This shifts cells and can affect references.",
                _object_schema(
                    {
                        "sheet": sheet,
                        "start": {"type": "integer", "minimum": 1},
                        "amount": {"type": "integer", "minimum": 1, "default": 1},
                    },
                    ["sheet", "start"],
                ),
            ),
            self._schema(
                "delete_columns",
                "Delete worksheet columns. This shifts cells and can affect references.",
                _object_schema(
                    {
                        "sheet": sheet,
                        "start": {"type": "integer", "minimum": 1},
                        "amount": {"type": "integer", "minimum": 1, "default": 1},
                    },
                    ["sheet", "start"],
                ),
            ),
            self._schema(
                "manage_sheet",
                "Create, rename, delete or copy a worksheet.",
                _object_schema(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["create", "rename", "delete", "copy"],
                        },
                        "name": {"type": "string"},
                        "new_name": {"type": ["string", "null"]},
                        "source": {"type": ["string", "null"]},
                        "index": {"type": ["integer", "null"], "minimum": 0},
                    },
                    ["action", "name"],
                ),
            ),
            self._schema(
                "recalculate_and_read",
                "Recalculate a safe workspace copy with LibreOffice, replace the working copy atomically, then inspect a range.",
                _object_schema({"sheet": sheet, "range_ref": a1}, ["sheet", "range_ref"]),
            ),
            self._schema(
                "render_workbook",
                "Render the current workbook to per-sheet PNG pages with headless LibreOffice.",
                _object_schema(
                    {"dpi": {"type": "integer", "minimum": 72, "maximum": 240, "default": 144}}, []
                ),
            ),
            self._schema(
                "view_image",
                "Return one original PNG page to the vision-capable model. Call render_workbook first.",
                _object_schema(
                    {
                        "image_path": {
                            "type": "string",
                            "description": "Path returned by render_workbook.",
                        }
                    },
                    ["image_path"],
                ),
            ),
            self._schema(
                "undo_last",
                "Restore the snapshot immediately before the latest committed mutation.",
                _object_schema({}, []),
            ),
        ]
        if self.interpreter:
            schemas.append(
                self._schema(
                    "code_interpreter",
                    (
                        "Run trusted Python in the task workspace for analysis and direct "
                        "workbook edits. The preloaded sheet_harness.load_workbook() and "
                        "sheet_harness.save_workbook(wb) no-path calls load and save the managed "
                        "SHEET_WORKBOOK; never spell or guess its path. "
                        "sheet_harness.workbook_overview(wb) returns a list of dictionaries, "
                        "one per worksheet. Openpyxl compatibility shims are also preloaded. "
                        "Every call starts a fresh Python process: variables, "
                        "imports, and workbook objects do not persist across calls. Make each "
                        "editing or recovery script self-contained: import, load, re-read the "
                        "request and inspected workbook state, edit, save, close, reopen, verify "
                        "the requested change and nearby cells, then print compact verification."
                    ),
                    _object_schema(
                        {
                            "code": {"type": "string"},
                            "timeout_seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 60,
                                "default": 30,
                            },
                        },
                        ["code"],
                    ),
                )
            )
        if self.target_grounding_active:
            schemas.append(
                self._schema(
                    "declare_edit_target",
                    (
                        "Declare one bounded, single-use edit scope grounded only in cited "
                        "observation_id values returned by successful inspect_range calls on "
                        "the current artifact. Every target requires a finite A1 rectangle; "
                        "worksheet and workbook wildcards are never allowed."
                    ),
                    _object_schema(
                        {
                            "targets": {
                                "type": "array",
                                "minItems": 1,
                                "items": _object_schema(
                                    {
                                        "sheet": sheet,
                                        "range_ref": {
                                            "type": "string",
                                            "description": "Bounded A1 cell range.",
                                        },
                                    },
                                    ["sheet", "range_ref"],
                                ),
                            },
                            "observation_ids": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": {"type": "integer", "minimum": 1},
                            },
                        },
                        ["targets", "observation_ids"],
                    ),
                )
            )
            declaration = {
                "type": "integer",
                "minimum": 1,
                "description": "Single-use ID returned by declare_edit_target.",
            }
            for schema in schemas:
                if schema["name"] not in _DIRECT_MUTATION_TOOLS:
                    continue
                parameters = schema["parameters"]
                parameters["properties"]["declaration_id"] = declaration
                parameters["required"] = [*parameters["required"], "declaration_id"]
        if self._allowed_tools is None:
            return schemas
        return [schema for schema in schemas if schema["name"] in self._allowed_tools]

    def _schema(self, name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": parameters,
            "strict": False,
        }

    @staticmethod
    def _valid_sha256(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _render_page_id(page: dict[str, Any]) -> str:
        sheet = page.get("sheet")
        sheet_page = page.get("sheet_page")
        if isinstance(sheet, str) and sheet and type(sheet_page) is int and sheet_page > 0:
            return f"{sheet}:{sheet_page}"
        path = page.get("path")
        if not isinstance(path, str) or not path:
            raise ContractStateError("Render manifest page omitted its portable path")
        return f"workbook:{Path(path).name}"

    @staticmethod
    def _decode_page_identity(data: bytes) -> tuple[int, int, str, str]:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise ContractStateError("Rendered page is not a PNG image")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                width, height = image.size
                image_mode = str(image.mode)
                pixels = image.tobytes()
        except ContractStateError:
            raise
        except Exception as exc:
            raise ContractStateError(
                f"Rendered PNG could not be decoded: {type(exc).__name__}"
            ) from exc
        header = json.dumps(
            {"height": height, "mode": image_mode, "width": width},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        pixel_sha256 = hashlib.sha256(header + b"\0" + pixels).hexdigest()
        return width, height, image_mode, pixel_sha256

    def _validated_current_render(self) -> dict[str, Any]:
        from .render import sha256_file

        cached = self._last_render
        if not isinstance(cached, dict):
            raise ToolInputError("Call render_workbook before view_image")
        render_id = cached.get("render_id")
        manifest_value = cached.get("manifest_path")
        manifest_sha256 = cached.get("render_manifest_sha256")
        if (
            not isinstance(render_id, str)
            or not render_id
            or not isinstance(manifest_value, str)
            or not self._valid_sha256(manifest_sha256)
        ):
            raise ContractStateError("Most recent render identity is incomplete")
        raw_manifest_path = Path(manifest_value)
        if raw_manifest_path.is_symlink():
            raise ContractStateError("Render manifest must not be a symbolic link")
        manifest_path = raw_manifest_path.resolve()
        render_root = (self.session.paths.artifacts / "render").resolve()
        if (
            render_root not in manifest_path.parents
            or manifest_path.parent.name != render_id
            or manifest_path.name != "render-manifest.json"
            or not manifest_path.is_file()
        ):
            raise ContractStateError("Render manifest escaped its authenticated render directory")
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise ContractStateError("Render manifest could not be read") from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
            raise ContractStateError("Render manifest hash no longer matches the render identity")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key {key!r}")
                result[key] = value
            return result

        try:
            manifest = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ContractStateError("Render manifest is unreadable or invalid JSON") from exc
        required_keys = {
            "schema_version",
            "source",
            "backend",
            "version",
            "hash",
            "mode",
            "dpi",
            "manifest_path",
            "page_count",
            "pages",
        }
        if not isinstance(manifest, dict) or not required_keys <= set(manifest):
            raise ContractStateError("Render manifest has an invalid schema")
        if set(manifest) - required_keys - {"fallback"}:
            raise ContractStateError("Render manifest contains unsupported fields")
        if manifest.get("schema_version") != 1:
            raise ContractStateError("Render manifest schema version is unsupported")
        source = manifest.get("source")
        current_artifact = self.session.artifact_ref()
        if (
            not isinstance(source, dict)
            or set(source) != {"name", "format", "sha256"}
            or source.get("sha256") != manifest.get("hash")
            or source.get("sha256") != current_artifact.sha256
            or cached.get("artifact_revision") != current_artifact.revision
            or cached.get("artifact_sha256") != current_artifact.sha256
        ):
            raise ToolInputError(
                "The most recent render is stale because the workbook revision changed; "
                "call render_workbook again before view_image"
            )
        if Path(str(manifest.get("manifest_path"))).resolve() != manifest_path:
            raise ContractStateError("Render manifest does not name its own authenticated file")
        for field in (
            "source",
            "backend",
            "version",
            "hash",
            "mode",
            "dpi",
            "manifest_path",
            "page_count",
            "pages",
        ):
            if cached.get(field) != manifest.get(field):
                raise ContractStateError(f"Cached render field {field!r} differs from its manifest")
        mode = manifest.get("mode")
        if mode not in {"per_sheet", "whole_workbook"}:
            raise ContractStateError("Render manifest mode is unsupported")
        dpi = manifest.get("dpi")
        pages = manifest.get("pages")
        page_count = manifest.get("page_count")
        if (
            type(dpi) is not int
            or dpi < 1
            or type(page_count) is not int
            or page_count < 1
            or not isinstance(pages, list)
            or len(pages) != page_count
        ):
            raise ContractStateError("Render manifest page inventory is invalid")

        portable_pages: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_paths: set[Path] = set()
        for expected_index, raw_page in enumerate(pages, start=1):
            required_page_keys = {
                "index",
                "page",
                "path",
                "image_path",
                "sha256",
                "width",
                "height",
                "sheet",
                "sheet_page",
            }
            if not isinstance(raw_page, dict) or not required_page_keys <= set(raw_page):
                raise ContractStateError("Render manifest page has an invalid schema")
            if set(raw_page) - required_page_keys - {"cell_scope"}:
                raise ContractStateError("Render manifest page contains unsupported fields")
            if raw_page.get("index") != expected_index:
                raise ContractStateError("Render manifest page indices are not contiguous")
            raw_relative = raw_page.get("path")
            if (
                not isinstance(raw_relative, str)
                or not raw_relative
                or Path(raw_relative).is_absolute()
                or PureWindowsPath(raw_relative).is_absolute()
                or ".." in Path(raw_relative).parts
            ):
                raise ContractStateError("Render manifest page path is not portable")
            raw_image_path = manifest_path.parent / raw_relative
            if raw_image_path.is_symlink():
                raise ContractStateError("Rendered pages must not be symbolic links")
            image_path = raw_image_path.resolve()
            if (
                manifest_path.parent not in image_path.parents
                or image_path.suffix.lower() != ".png"
                or not image_path.is_file()
                or Path(str(raw_page.get("image_path"))).resolve() != image_path
            ):
                raise ContractStateError("Render manifest page escaped its render directory")
            if image_path in seen_paths:
                raise ContractStateError("Render manifest contains duplicate page paths")
            seen_paths.add(image_path)
            file_sha256 = sha256_file(image_path)
            if not self._valid_sha256(raw_page.get("sha256")) or file_sha256 != raw_page.get(
                "sha256"
            ):
                raise ContractStateError("Rendered page bytes no longer match the manifest")
            width = raw_page.get("width")
            height = raw_page.get("height")
            if type(width) is not int or width < 1 or type(height) is not int or height < 1:
                raise ContractStateError("Render manifest page dimensions are invalid")
            sheet = raw_page.get("sheet")
            sheet_page = raw_page.get("sheet_page")
            if mode == "per_sheet":
                if (
                    not isinstance(sheet, str)
                    or not sheet
                    or type(sheet_page) is not int
                    or sheet_page < 1
                    or raw_page.get("page") != sheet_page
                ):
                    raise ContractStateError("Per-sheet render page mapping is invalid")
            elif sheet is not None or sheet_page is not None or raw_page.get("page") != expected_index:
                raise ContractStateError("Whole-workbook render page mapping is invalid")
            raw_cell_scope = raw_page.get("cell_scope")
            cell_scope: EvidenceScope | None = None
            if raw_cell_scope is not None:
                try:
                    cell_scope = EvidenceScope.from_dict(raw_cell_scope)
                except (TypeError, ValueError) as exc:
                    raise ContractStateError("Render page cell mapping is invalid") from exc
                if cell_scope.empty or cell_scope.wildcard or cell_scope.sheets:
                    raise ContractStateError("Render page cell mapping must be finite ranges")
                if isinstance(sheet, str) and any(
                    item.sheet != sheet for item in cell_scope.ranges
                ):
                    raise ContractStateError("Render page cell mapping crosses worksheets")
            page_id = self._render_page_id(raw_page)
            if page_id in seen_ids:
                raise ContractStateError("Render manifest contains duplicate page IDs")
            seen_ids.add(page_id)
            portable_pages.append(
                {
                    "page_id": page_id,
                    "page_index": expected_index,
                    "file_sha256": file_sha256,
                    "width": width,
                    "height": height,
                    "sheet": sheet,
                    "sheet_page": sheet_page,
                    "cell_scope": cell_scope.to_dict() if cell_scope is not None else None,
                    "image_path": image_path,
                }
            )
        return {
            "render_id": render_id,
            "render_manifest_sha256": manifest_sha256,
            "backend": manifest.get("backend"),
            "version": manifest.get("version"),
            "mode": mode,
            "dpi": dpi,
            "page_count": page_count,
            "pages": portable_pages,
        }

    @staticmethod
    def _portable_render_metadata(render: dict[str, Any]) -> dict[str, Any]:
        return {
            "producer_tool": "render_workbook",
            "backend": render["backend"],
            "version": render["version"],
            "mode": render["mode"],
            "dpi": render["dpi"],
            "page_count": render["page_count"],
            "pages": [
                {key: value for key, value in page.items() if key != "image_path"}
                for page in render["pages"]
            ],
        }

    @staticmethod
    def _declared_target_scope(targets: Any) -> EvidenceScope:
        if not isinstance(targets, list) or not targets:
            raise ToolInputError("targets must be a non-empty list")
        scope = EvidenceScope()
        for index, target in enumerate(targets):
            if not isinstance(target, dict) or set(target) != {"sheet", "range_ref"}:
                raise ToolInputError(f"targets[{index}] has an invalid schema")
            sheet = target.get("sheet")
            if not isinstance(sheet, str) or not sheet:
                raise ToolInputError(f"targets[{index}].sheet must be a non-empty string")
            range_ref = target.get("range_ref")
            if not isinstance(range_ref, str) or not range_ref:
                raise ToolInputError(f"targets[{index}].range_ref must be a bounded A1 range")
            try:
                item = EvidenceScope.one(sheet, range_ref)
            except (TypeError, ValueError) as exc:
                raise ToolInputError(f"targets[{index}] is not a finite A1 scope") from exc
            scope = scope.merged(item)
        if scope.empty or scope.wildcard:
            raise ToolInputError("declared edit target must be finite and non-empty")
        return scope

    @staticmethod
    def _normalized_scope(
        sheet: Any,
        range_ref: Any,
        *,
        fallback_to_workbook: bool = True,
    ) -> EvidenceScope:
        if isinstance(sheet, str) and sheet and isinstance(range_ref, str) and range_ref:
            try:
                return EvidenceScope.one(sheet, range_ref)
            except ValueError as exc:
                raise ContractStateError(
                    f"Trusted tool returned an invalid evidence range: {sheet}!{range_ref}"
                ) from exc
        if fallback_to_workbook:
            return EvidenceScope.workbook()
        raise ContractStateError("Trusted tool omitted a required evidence scope")

    def _mutation_scope(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> EvidenceScope:
        if name in {"write_range", "fill_formula", "format_range", "clear_range"}:
            range_ref = result.get("range") or arguments.get("range_ref")
            if name == "fill_formula":
                range_ref = result.get("range") or arguments.get("target_range")
            return self._normalized_scope(
                result.get("sheet") or arguments.get("sheet"),
                range_ref,
            )
        return EvidenceScope.workbook()

    @staticmethod
    def _trusted_workbook_effects(
        result: dict[str, Any],
    ) -> (
        tuple[
            bool,
            bool,
            frozenset[EffectKind],
            EvidenceScope,
            EvidenceScope,
            dict[str, Any],
        ]
        | None
    ):
        raw = result.get("workbook_effects")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ContractStateError("workbook_effects must be a trusted mapping")
        required = {
            "schema_version",
            "semantic_changed",
            "complete",
            "effects",
            "scope",
            "formula_scope",
            "changed_cell_count",
            "scanned_cell_count",
            "reasons",
        }
        if set(raw) != required or raw.get("schema_version") != "workbook-effect-diff-v1":
            raise ContractStateError("workbook_effects has an invalid schema")
        if not isinstance(raw["semantic_changed"], bool) or not isinstance(raw["complete"], bool):
            raise ContractStateError("workbook_effects flags must be boolean")
        if not isinstance(raw["effects"], list) or not all(
            isinstance(item, str) for item in raw["effects"]
        ):
            raise ContractStateError("workbook_effects.effects must be a string list")
        if len(raw["effects"]) != len(set(raw["effects"])):
            raise ContractStateError("workbook_effects.effects must not contain duplicates")
        if any(
            type(raw[key]) is not int or raw[key] < 0
            for key in ("changed_cell_count", "scanned_cell_count")
        ):
            raise ContractStateError("workbook_effects counts must be non-negative integers")
        if raw["changed_cell_count"] > raw["scanned_cell_count"]:
            raise ContractStateError(
                "workbook_effects changed_cell_count cannot exceed scanned_cell_count"
            )
        if not isinstance(raw["reasons"], list) or not all(
            isinstance(item, str) and item for item in raw["reasons"]
        ):
            raise ContractStateError(
                "workbook_effects.reasons must be a string list with non-empty entries"
            )
        if len(raw["reasons"]) != len(set(raw["reasons"])):
            raise ContractStateError("workbook_effects.reasons must not contain duplicates")
        try:
            effects = frozenset(EffectKind(item) for item in raw["effects"])
            scope = EvidenceScope.from_dict(raw["scope"])
            formula_scope = EvidenceScope.from_dict(raw["formula_scope"])
        except (TypeError, ValueError) as exc:
            raise ContractStateError(f"Invalid workbook effect footprint: {exc}") from exc
        if raw["semantic_changed"] and (not effects or scope.empty):
            raise ContractStateError(
                "A semantic workbook change requires non-empty effects and scope"
            )
        if not raw["semantic_changed"] and (
            effects or not scope.empty or not formula_scope.empty or raw["changed_cell_count"] != 0
        ):
            raise ContractStateError(
                "A semantic no-op must not report effects, changed scope, or changed cells"
            )
        if EffectKind.FORMULA in effects:
            if formula_scope.empty:
                raise ContractStateError("A formula effect requires a non-empty formula_scope")
            if not scope.covers(formula_scope):
                raise ContractStateError("workbook_effects.scope must cover formula_scope")
        elif not formula_scope.empty:
            raise ContractStateError("A non-formula diff must not report formula_scope")
        if raw["complete"]:
            if EffectKind.UNKNOWN in effects:
                raise ContractStateError(
                    "A complete workbook effect diff must not report unknown effects"
                )
            if (
                effects
                & {
                    EffectKind.VALUE,
                    EffectKind.FORMULA,
                    EffectKind.STYLE,
                }
                and raw["changed_cell_count"] == 0
            ):
                raise ContractStateError(
                    "Complete cell effects require a positive changed_cell_count"
                )
        elif not (
            raw["semantic_changed"]
            and effects == frozenset({EffectKind.UNKNOWN})
            and scope.wildcard
            and formula_scope.empty
            and raw["reasons"]
        ):
            raise ContractStateError(
                "An incomplete diff must be fail-closed as an unknown workbook-wide change"
            )
        metadata = {
            "diff_complete": raw["complete"],
            "changed_cell_count": raw["changed_cell_count"],
            "scanned_cell_count": raw["scanned_cell_count"],
            "diff_reasons": raw["reasons"],
        }
        return (
            raw["semantic_changed"],
            raw["complete"],
            effects,
            scope,
            formula_scope,
            metadata,
        )

    @staticmethod
    def _portable_recalculation_metadata(
        calculation: dict[str, Any],
        artifact_before: ArtifactRef,
        artifact_after: ArtifactRef,
    ) -> dict[str, Any]:
        required = {
            "backend",
            "version",
            "source_sha256",
            "output_sha256",
            "atomic_replace",
        }
        missing = sorted(required - set(calculation))
        if missing:
            raise ContractStateError(
                "recalculate_and_read calculation metadata is missing: " + ", ".join(missing)
            )
        if not all(
            isinstance(calculation[key], str) and calculation[key] for key in ("backend", "version")
        ):
            raise ContractStateError(
                "recalculate_and_read backend and version must be non-empty strings"
            )
        if any(
            Path(calculation[key]).is_absolute() or PureWindowsPath(calculation[key]).is_absolute()
            for key in ("backend", "version")
        ):
            raise ContractStateError(
                "recalculate_and_read portable metadata must not contain an absolute path"
            )
        if calculation["source_sha256"] != artifact_before.sha256:
            raise ContractStateError(
                "recalculate_and_read source_sha256 does not match the input artifact"
            )
        if calculation["output_sha256"] != artifact_after.sha256:
            raise ContractStateError(
                "recalculate_and_read output_sha256 does not match the output artifact"
            )
        if calculation["atomic_replace"] is not True:
            raise ContractStateError("recalculate_and_read must attest an atomic replacement")
        return {key: calculation[key] for key in sorted(required)}

    @staticmethod
    def _validate_artifact_observation(
        artifact_before: ArtifactRef,
        artifact_after: ArtifactRef,
    ) -> None:
        if artifact_after.sha256 == artifact_before.sha256:
            if artifact_after.revision != artifact_before.revision:
                raise ContractStateError(
                    "An unchanged artifact hash must retain its revision index"
                )
            return
        if artifact_after.revision != artifact_before.revision + 1:
            raise ContractStateError("Changed artifact bytes must advance exactly one revision")

    @staticmethod
    def _inspection_predicates(inspection: dict[str, Any]) -> frozenset[str]:
        cells = inspection.get("cells")
        if not isinstance(cells, list):
            return frozenset()
        has_calc_error = any(
            isinstance(cell, dict)
            and isinstance(cell.get("value"), str)
            and str(cell["value"]).upper() in _CALC_ERROR_VALUES
            for cell in cells
        )
        return frozenset() if has_calc_error else frozenset({"no_calc_error"})

    def _observe_evidence_contract(
        self,
        name: str,
        arguments: dict[str, Any],
        outcome: ToolOutcome,
        artifact_before: ArtifactRef,
        artifact_after: ArtifactRef,
    ) -> None:
        monitor = self.evidence_monitor
        if monitor is None:
            return
        self._validate_artifact_observation(artifact_before, artifact_after)
        result = outcome.data
        ok = result.get("ok") is True
        if name in _DIRECT_MUTATION_TOOLS:
            if ok and artifact_after != artifact_before:
                footprint = self._trusted_workbook_effects(result)
                if footprint is None:
                    raise ContractStateError(
                        f"{name} changed workbook bytes without a trusted "
                        "workbook_effects footprint"
                    )
                (
                    semantic_changed,
                    _,
                    effects,
                    scope,
                    formula_scope,
                    diff_metadata,
                ) = footprint
                common_metadata = {
                    "producer_tool": name,
                    "artifact_revision_before": artifact_before.revision,
                    "artifact_revision_after": artifact_after.revision,
                    **diff_metadata,
                }
                if semantic_changed:
                    monitor.observe(
                        EvidenceEvent(
                            EventKind.MUTATION_COMMITTED,
                            artifact_before.sha256,
                            artifact_after.sha256,
                            effects=effects,
                            scope=scope,
                            formula_scope=formula_scope,
                            metadata=common_metadata,
                        )
                    )
                else:
                    monitor.observe(
                        EvidenceEvent(
                            EventKind.ARTIFACT_REWRITTEN,
                            artifact_before.sha256,
                            artifact_after.sha256,
                            metadata=common_metadata,
                        )
                    )
            elif not ok and artifact_after != artifact_before:
                monitor.observe(
                    EvidenceEvent(
                        EventKind.MUTATION_ROLLED_BACK,
                        artifact_after.sha256,
                        scope=self._mutation_scope(name, arguments, result),
                        metadata={"producer_tool": name},
                    )
                )

        if name == "inspect_range" and ok:
            scope = self._normalized_scope(result.get("sheet"), result.get("range"))
            monitor.observe(
                EvidenceEvent(
                    EventKind.RANGE_INSPECTED,
                    artifact_after.sha256,
                    scope=scope,
                    predicates=self._inspection_predicates(result),
                    metadata={"producer_tool": name},
                )
            )
        elif name == "recalculate_and_read" and ok:
            calculation = result.get("calculation")
            if not isinstance(calculation, dict):
                raise ContractStateError(
                    "recalculate_and_read omitted trusted calculation metadata"
                )
            portable_calculation = self._portable_recalculation_metadata(
                calculation,
                artifact_before,
                artifact_after,
            )
            monitor.observe(
                EvidenceEvent(
                    EventKind.WORKBOOK_RECALCULATED,
                    artifact_before.sha256,
                    artifact_after.sha256,
                    metadata={
                        "producer_tool": name,
                        "calculation": portable_calculation,
                    },
                )
            )
            inspection = result.get("inspection")
            if not isinstance(inspection, dict):
                raise ContractStateError(
                    "recalculate_and_read omitted its authenticated inspection"
                )
            monitor.observe(
                EvidenceEvent(
                    EventKind.RANGE_INSPECTED,
                    artifact_after.sha256,
                    scope=self._normalized_scope(
                        inspection.get("sheet"),
                        inspection.get("range"),
                    ),
                    predicates=self._inspection_predicates(inspection),
                    metadata={"producer_tool": name},
                )
            )
        elif name == "render_workbook" and ok:
            render = self._validated_current_render()
            monitor.observe(
                EvidenceEvent(
                    EventKind.WORKBOOK_RENDERED,
                    artifact_after.sha256,
                    render_id=str(render["render_id"]),
                    render_manifest_sha256=str(render["render_manifest_sha256"]),
                    metadata=self._portable_render_metadata(render),
                )
            )

    def invoke(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return ToolOutcome(
                {"ok": False, "error": f"Unknown tool: {name}", "type": "UnknownTool"}
            )
        handler = self._handlers.get(name)
        if handler is None:
            return ToolOutcome(
                {"ok": False, "error": f"Unknown tool: {name}", "type": "UnknownTool"}
            )
        self.session.recorder.record("tool.called", {"name": name, "arguments": arguments})
        artifact_before = self.session.artifact_ref() if self.evidence_monitor is not None else None
        try:
            outcome = handler(arguments)
        except (CodeIsolationError, ContractStateError):
            # Required comparison isolation is a harness invariant, not a
            # recoverable model tool error. Never let the agent continue.
            raise
        except (HarnessError, TypeError, ValueError, KeyError) as exc:
            outcome = ToolOutcome({"ok": False, "error": str(exc), "type": type(exc).__name__})
        except Exception as exc:  # keep a malformed tool call from killing the agent loop
            outcome = ToolOutcome({"ok": False, "error": str(exc), "type": type(exc).__name__})
        if self.evidence_monitor is not None:
            assert artifact_before is not None
            artifact_after = self.session.artifact_ref()
            self._observe_evidence_contract(
                name,
                arguments,
                outcome,
                artifact_before,
                artifact_after,
            )
            outcome = ToolOutcome(
                {
                    **outcome.data,
                    "_evidence_contract": self.evidence_monitor.compact_status(),
                },
                image_path=outcome.image_path,
            )
            self.session.recorder.record(
                "evidence_contract.observed",
                {
                    "tool": name,
                    "status": self.evidence_monitor.status(),
                },
            )
        self.session.recorder.record("tool.returned", {"name": name, "result": outcome.data})
        return outcome

    def _list_sheets(self, _: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(self.session.list_sheets())

    def _inspect_range(self, args: dict[str, Any]) -> ToolOutcome:
        result = self.session.inspect_range(
            args["sheet"],
            args["range_ref"],
            include_styles=args.get("include_styles", _INSPECT_DEFAULT_INCLUDE_STYLES),
        )
        if self.target_grounding_active:
            observation = self.session.record_target_observation(
                artifact=ArtifactRef(
                    int(result["artifact_revision"]),
                    str(result["artifact_sha256"]),
                ),
                scope=self._normalized_scope(
                    result.get("sheet"),
                    result.get("range"),
                    fallback_to_workbook=False,
                ),
            )
            result = {
                **result,
                "observation_id": observation["observation_id"],
                "target_observation": observation,
            }
        return ToolOutcome(result)

    def _declare_edit_target(self, args: dict[str, Any]) -> ToolOutcome:
        declaration = self.session.declare_edit_target(
            target_scope=self._declared_target_scope(args.get("targets")),
            observation_ids=args.get("observation_ids"),
        )
        return ToolOutcome(
            {
                "ok": True,
                "declaration_id": declaration["declaration_id"],
                "declaration": declaration,
                "message": (
                    "The declaration is bound to the current workbook bytes and will be "
                    "consumed by exactly one staged target assessment."
                ),
            }
        )

    def _range_to_latex(self, args: dict[str, Any]) -> ToolOutcome:
        inspection = self.session.inspect_range(
            args["sheet"], args["range_ref"], include_styles=True, max_cells=_LATEX_MAX_CELLS
        )
        matrix = inspection["matrix"]
        latex, truncated_cells, per_cell_limit = _matrix_to_latex(matrix)
        row_count = len(matrix)
        column_count = max((len(row) for row in matrix), default=0)
        return ToolOutcome(
            {
                "ok": True,
                "sheet": inspection["sheet"],
                "range": inspection["range"],
                "rows": row_count,
                "columns": column_count,
                "cell_count": row_count * column_count,
                "latex": latex,
                "latex_truncated": truncated_cells > 0,
                "truncated_cell_count": truncated_cells,
                "merged_ranges": inspection["merged_ranges"],
                "style_summary": _style_summary(inspection["cells"]),
                "limits": {
                    "max_cells": _LATEX_MAX_CELLS,
                    "max_latex_chars": _LATEX_MAX_CHARS,
                    "max_cell_latex_chars": per_cell_limit,
                },
            }
        )

    def _find_cells(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.find_cells(
                args["query"],
                sheet=args.get("sheet"),
                use_regex=args.get("use_regex", False),
                match_case=args.get("match_case", False),
                search_formulas=args.get("search_formulas", True),
            )
        )

    def _write_range(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.write_range(
                args["sheet"],
                args["start_cell"],
                args["values"],
                declaration_id=args.get("declaration_id"),
            )
        )

    def _fill_formula(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.fill_formula(
                args["sheet"],
                args["source_cell"],
                args["target_range"],
                declaration_id=args.get("declaration_id"),
            )
        )

    def _format_range(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.format_range(
                args["sheet"],
                args["range_ref"],
                args["format_spec"],
                declaration_id=args.get("declaration_id"),
            )
        )

    def _clear_range(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.clear_range(
                args["sheet"],
                args["range_ref"],
                contents=args.get("contents", True),
                formats=args.get("formats", False),
                declaration_id=args.get("declaration_id"),
            )
        )

    def _delete_rows(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.delete_rows(
                args["sheet"],
                args["start"],
                args.get("amount", 1),
                declaration_id=args.get("declaration_id"),
            )
        )

    def _delete_columns(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.delete_columns(
                args["sheet"],
                args["start"],
                args.get("amount", 1),
                declaration_id=args.get("declaration_id"),
            )
        )

    def _manage_sheet(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(
            self.session.manage_sheet(
                args["action"],
                args["name"],
                new_name=args.get("new_name"),
                source=args.get("source"),
                index=args.get("index"),
                declaration_id=args.get("declaration_id"),
            )
        )

    def _render_workbook(self, args: dict[str, Any]) -> ToolOutcome:
        from .render import render_workbook, sha256_file

        output_dir = self.session.paths.artifacts / "render" / f"render-{uuid.uuid4().hex[:12]}"
        self._pending_visual_confirmations.clear()
        with self.session.read_artifact() as artifact:
            result = render_workbook(
                self.session.workbook_path,
                output_dir,
                dpi=args.get("dpi", 144),
            )
            self._last_render = {
                **result.to_dict(),
                "render_id": output_dir.name,
                "render_manifest_sha256": sha256_file(result.manifest_path),
                "artifact_revision": artifact.revision,
                "artifact_sha256": artifact.sha256,
            }
            try:
                self._validated_current_render()
            except Exception:
                self._last_render = None
                raise
        return ToolOutcome({"ok": True, **self._last_render})

    def _view_image(self, args: dict[str, Any]) -> ToolOutcome:
        candidate = Path(args["image_path"])
        if not candidate.is_absolute():
            candidate = (self.session.workspace / candidate).resolve()
        else:
            candidate = candidate.resolve()
        render_root = (self.session.paths.artifacts / "render").resolve()
        if render_root not in candidate.parents or candidate.suffix.lower() != ".png":
            raise ToolInputError(
                "view_image only accepts PNGs produced in this run's render directory"
            )
        current_artifact = self.session.artifact_ref()
        render = self._validated_current_render()
        matching_pages = [
            page for page in render["pages"] if page["image_path"] == candidate
        ]
        if len(matching_pages) != 1:
            raise ToolInputError(
                "view_image only accepts a page returned by the most recent render_workbook call"
            )
        selected_page = matching_pages[0]
        try:
            page_bytes = candidate.read_bytes()
        except OSError as exc:
            raise ContractStateError("Rendered page could not be read") from exc
        file_sha256 = hashlib.sha256(page_bytes).hexdigest()
        if file_sha256 != selected_page["file_sha256"]:
            raise ContractStateError("Rendered page bytes changed after manifest validation")
        width, height, image_mode, pixel_sha256 = self._decode_page_identity(page_bytes)
        if (width, height) != (selected_page["width"], selected_page["height"]):
            raise ContractStateError("Decoded page dimensions do not match the render manifest")
        confirmation_id = uuid.uuid4().hex
        cell_scope = (
            EvidenceScope.from_dict(selected_page["cell_scope"])
            if isinstance(selected_page["cell_scope"], dict)
            else None
        )
        confirmation = _PendingVisualConfirmation(
            confirmation_id=confirmation_id,
            image_path=candidate,
            artifact=current_artifact,
            render_id=str(render["render_id"]),
            render_manifest_sha256=str(render["render_manifest_sha256"]),
            render_mode=str(render["mode"]),
            page_id=str(selected_page["page_id"]),
            page_index=int(selected_page["page_index"]),
            file_sha256=file_sha256,
            pixel_sha256=pixel_sha256,
            width=width,
            height=height,
            image_mode=image_mode,
            sheet=selected_page["sheet"],
            sheet_page=selected_page["sheet_page"],
            cell_scope=cell_scope,
        )
        self._pending_visual_confirmations[confirmation_id] = confirmation
        return ToolOutcome(
            {
                "ok": True,
                "image_path": str(candidate),
                "width": width,
                "height": height,
                "image_mode": image_mode,
                "render_id": confirmation.render_id,
                "render_manifest_sha256": confirmation.render_manifest_sha256,
                "render_mode": confirmation.render_mode,
                "page_id": confirmation.page_id,
                "page_index": confirmation.page_index,
                "page_sha256": confirmation.file_sha256,
                "page_file_sha256": confirmation.file_sha256,
                "page_pixel_sha256": confirmation.pixel_sha256,
                "pixel_sha256_algorithm": PIXEL_SHA256_ALGORITHM,
                "sheet": confirmation.sheet,
                "sheet_page": confirmation.sheet_page,
                "cell_scope": (
                    confirmation.cell_scope.to_dict()
                    if confirmation.cell_scope is not None
                    else None
                ),
                "artifact_revision": current_artifact.revision,
                "artifact_sha256": current_artifact.sha256,
                "visual_confirmation_id": confirmation_id,
                "visual_evidence_status": "pending_provider_response",
                "message": (
                    "The original PNG is eligible for the next multimodal request. "
                    "Visual evidence remains pending until the harness confirms that exact "
                    "attachment after a successful provider response."
                ),
            },
            image_path=candidate,
        )

    def confirm_view_image_delivery(
        self,
        confirmation_id: str,
        *,
        attached_file_sha256: str,
        provider_response_id: str,
    ) -> dict[str, Any]:
        """Commit one staged page view only after model-visible delivery succeeds."""

        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise ContractStateError("Visual confirmation ID must be non-empty")
        confirmation = self._pending_visual_confirmations.pop(confirmation_id, None)
        if confirmation is None:
            raise ContractStateError("Visual confirmation is unknown, expired, or already used")
        if not self._valid_sha256(attached_file_sha256):
            raise ContractStateError("Attached image SHA-256 is invalid")
        if (
            not isinstance(provider_response_id, str)
            or not provider_response_id
            or len(provider_response_id) > 512
            or Path(provider_response_id).is_absolute()
            or PureWindowsPath(provider_response_id).is_absolute()
        ):
            raise ContractStateError("A successful portable provider response ID is required")
        if attached_file_sha256 != confirmation.file_sha256:
            raise ContractStateError("Attached image bytes do not match the staged render page")
        if self.session.artifact_ref() != confirmation.artifact:
            raise ContractStateError("Visual confirmation belongs to a stale workbook revision")

        render = self._validated_current_render()
        matching_pages = [
            page
            for page in render["pages"]
            if page["page_id"] == confirmation.page_id
            and page["image_path"] == confirmation.image_path
        ]
        if len(matching_pages) != 1:
            raise ContractStateError("Confirmed page is absent from the current render manifest")
        page = matching_pages[0]
        try:
            page_bytes = confirmation.image_path.read_bytes()
        except OSError as exc:
            raise ContractStateError("Confirmed render page could not be reread") from exc
        file_sha256 = hashlib.sha256(page_bytes).hexdigest()
        width, height, image_mode, pixel_sha256 = self._decode_page_identity(page_bytes)
        if (
            render["render_id"] != confirmation.render_id
            or render["render_manifest_sha256"] != confirmation.render_manifest_sha256
            or render["mode"] != confirmation.render_mode
            or page["file_sha256"] != confirmation.file_sha256
            or file_sha256 != confirmation.file_sha256
            or pixel_sha256 != confirmation.pixel_sha256
            or (width, height, image_mode)
            != (confirmation.width, confirmation.height, confirmation.image_mode)
        ):
            raise ContractStateError("Render page identity changed before delivery confirmation")

        monitor = self.evidence_monitor
        if monitor is not None:
            monitor.observe(
                EvidenceEvent(
                    EventKind.RENDERED_PAGE_VIEWED,
                    confirmation.artifact.sha256,
                    scope=confirmation.cell_scope or EvidenceScope(),
                    related_render_id=confirmation.render_id,
                    related_render_manifest_sha256=confirmation.render_manifest_sha256,
                    page_id=confirmation.page_id,
                    page_sha256=confirmation.file_sha256,
                    metadata={
                        "producer_tool": "view_image",
                        "delivery_status": "provider_response_confirmed",
                        "confirmation_id": confirmation.confirmation_id,
                        "provider_response_id": provider_response_id,
                        "attachment_file_sha256": attached_file_sha256,
                        "page_file_sha256": confirmation.file_sha256,
                        "page_pixel_sha256": confirmation.pixel_sha256,
                        "pixel_sha256_algorithm": PIXEL_SHA256_ALGORITHM,
                        "width": confirmation.width,
                        "height": confirmation.height,
                        "image_mode": confirmation.image_mode,
                        "render_mode": confirmation.render_mode,
                        "page_index": confirmation.page_index,
                        "sheet": confirmation.sheet,
                        "sheet_page": confirmation.sheet_page,
                        "cell_scope": (
                            confirmation.cell_scope.to_dict()
                            if confirmation.cell_scope is not None
                            else None
                        ),
                    },
                )
            )
            status = monitor.compact_status()
            self.session.recorder.record(
                "evidence_contract.rendered_page_delivery_confirmed",
                {
                    "confirmation_id": confirmation.confirmation_id,
                    "provider_response_id": provider_response_id,
                    "render_id": confirmation.render_id,
                    "render_manifest_sha256": confirmation.render_manifest_sha256,
                    "page_id": confirmation.page_id,
                    "page_file_sha256": confirmation.file_sha256,
                    "page_pixel_sha256": confirmation.pixel_sha256,
                    "pixel_sha256_algorithm": PIXEL_SHA256_ALGORITHM,
                    "width": confirmation.width,
                    "height": confirmation.height,
                    "image_mode": confirmation.image_mode,
                    "status": status,
                },
            )
            return status
        return {
            "confirmed": True,
            "confirmation_id": confirmation.confirmation_id,
            "provider_response_id": provider_response_id,
            "page_file_sha256": confirmation.file_sha256,
            "page_pixel_sha256": confirmation.pixel_sha256,
            "pixel_sha256_algorithm": PIXEL_SHA256_ALGORITHM,
        }

    def _recalculate_and_read(self, args: dict[str, Any]) -> ToolOutcome:
        metadata = self.session.recalculate()
        inspected = self.session.inspect_range(args["sheet"], args["range_ref"])
        return ToolOutcome({"ok": True, "calculation": metadata, "inspection": inspected})

    def _undo_last(self, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(self.session.undo_last(declaration_id=args.get("declaration_id")))

    def _code_interpreter(self, args: dict[str, Any]) -> ToolOutcome:
        if not self.interpreter:
            raise ToolInputError("code_interpreter is disabled")
        if self.target_grounding_active:
            result = self.session.run_staged_external_mutation(
                operation="code_interpreter",
                declaration_id=args.get("declaration_id"),
                runner=lambda staged: self.interpreter.isolated_copy(
                    staged.parent,
                    staged,
                ).run(
                    args["code"],
                    timeout_seconds=args.get("timeout_seconds"),
                ),
            )
            return ToolOutcome(result)
        artifact_before = self.session.artifact_ref()
        result = self.interpreter.run(args["code"], timeout_seconds=args.get("timeout_seconds"))
        transition = self.session.reconcile_external_artifact(
            artifact_before,
            operation="code_interpreter",
        )
        result = {
            **result,
            "artifact_revision_before": artifact_before.revision,
            "artifact_revision_after": self.session.artifact_ref().revision,
            "artifact_transition_id": (
                transition.transition_id if transition is not None else None
            ),
        }
        return ToolOutcome(result)
