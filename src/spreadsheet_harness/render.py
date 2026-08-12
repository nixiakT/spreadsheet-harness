"""Safe LibreOffice rendering for spreadsheet workbooks.

The source workbook is never passed to LibreOffice directly.  Every conversion
uses a private copy and a fresh ``UserInstallation`` directory so concurrent
renders cannot share a LibreOffice profile or modify the caller's workbook.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .errors import RenderError

SUPPORTED_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".ods", ".xls", ".csv"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RECALCULATION_FORMATS = {
    ".xlsx": "xlsx:Calc MS Excel 2007 XML",
    ".xlsm": "xlsm:Calc MS Excel 2007 VBA XML",
    ".ods": "ods:calc8",
    ".xls": "xls:MS Excel 97",
    ".csv": "csv:Text - txt - csv (StarCalc)",
}


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


@contextmanager
def isolated_user_profile() -> Iterator[tuple[Path, str]]:
    """Yield a fresh LibreOffice profile directory and its required file URI."""

    with tempfile.TemporaryDirectory(prefix="spreadsheet-lo-profile-") as raw_profile:
        profile = Path(raw_profile).resolve()
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


def _convert_with_libreoffice(
    source_copy: Path,
    output_dir: Path,
    *,
    target_format: str,
    binary: str,
    timeout_seconds: float,
) -> Path:
    """Convert a disposable source copy with a fresh LibreOffice profile."""

    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_format = target_format.split(":", 1)[0].lower().lstrip(".")
    suffix = f".{normalized_format}"
    with isolated_user_profile() as (_, profile_uri):
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
    workbook = None
    try:
        workbook = load_workbook(
            path,
            read_only=True,
            data_only=False,
            keep_vba=path.suffix.lower() == ".xlsm",
            keep_links=True,
        )
        if not workbook.sheetnames:
            raise RenderError("Recalculated workbook contains no worksheets")
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f"Recalculated workbook validation failed: {exc}") from exc
    finally:
        if workbook is not None:
            workbook.close()


def recalculate_workbook(
    source: str | Path,
    destination: str | Path,
    *,
    libreoffice_binary: str | Path | None = None,
    timeout_seconds: float = 120.0,
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

    source_hash = sha256_file(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spreadsheet-recalculate-") as raw_work:
        work = Path(raw_work)
        private_source = work / "source" / source_path.name
        private_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, private_source)
        converted = _convert_with_libreoffice(
            private_source,
            work / "converted",
            target_format=target_format,
            binary=binary,
            timeout_seconds=timeout_seconds,
        )
        _validate_recalculated_file(converted)

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
            output_hash = sha256_file(temporary_destination)
            temporary_destination.replace(destination_path)
        finally:
            temporary_destination.unlink(missing_ok=True)

    return {
        "backend": "libreoffice-headless",
        "version": libreoffice_version(binary),
        "profile": "isolated-per-invocation",
        "source_path": str(source_path),
        "destination_path": str(destination_path),
        "source_sha256": source_hash,
        "output_sha256": output_hash,
        "format": destination_format.lstrip("."),
        "atomic_replace": True,
    }


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
    "SUPPORTED_SPREADSHEET_EXTENSIONS",
    "convert_spreadsheet_copy",
    "find_libreoffice",
    "isolated_user_profile",
    "libreoffice_command",
    "libreoffice_version",
    "load_png",
    "pymupdf_version",
    "read_png",
    "recalculate_workbook",
    "render",
    "render_workbook",
    "sha256_file",
]
