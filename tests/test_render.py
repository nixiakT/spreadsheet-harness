from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from PIL import Image

import spreadsheet_harness.render as render_module
from spreadsheet_harness.errors import RenderError
from spreadsheet_harness.render import (
    PNG_SIGNATURE,
    RenderPage,
    find_libreoffice,
    isolated_user_profile,
    libreoffice_command,
    read_png,
    recalculate_workbook,
    render_workbook,
    sha256_file,
)


def _save_png(path: Path, *, color: str = "white") -> bytes:
    Image.new("RGB", (8, 6), color=color).save(path, format="PNG")
    return path.read_bytes()


def _save_workbook(path: Path, *, two_sheets: bool = True) -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = "Data"
    first["A1"] = "Name"
    first["B1"] = "Value"
    first.append(["one", 1])
    first.append(["two", 2])
    first["B4"] = "=SUM(B2:B3)"
    if two_sheets:
        second = workbook.create_sheet("Summary")
        second["A1"] = "Total"
        second["B1"] = "=Data!B4"
    workbook.save(path)
    workbook.close()


def test_isolated_user_profile_is_unique_and_removed() -> None:
    paths: list[Path] = []
    uris: list[str] = []
    for _ in range(2):
        with isolated_user_profile() as (profile, uri):
            paths.append(profile)
            uris.append(uri)
            assert profile.is_dir()
            assert uri == profile.as_uri()
            assert uri.startswith("file://")
        assert not profile.exists()

    assert paths[0] != paths[1]
    assert uris[0] != uris[1]


def test_libreoffice_command_contains_private_profile(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.touch()
    output = tmp_path / "output"
    profile_uri = (tmp_path / "profile with spaces").resolve().as_uri()

    command = libreoffice_command("soffice", source, output, "pdf", profile_uri)

    assert command[0] == "soffice"
    assert "--headless" in command
    assert f"-env:UserInstallation={profile_uri}" in command
    assert command[-1] == str(source)
    assert command[command.index("--outdir") + 1] == str(output)


def test_read_png_returns_original_bytes(tmp_path: Path) -> None:
    png = tmp_path / "original.png"
    original = _save_png(png, color="navy")

    loaded = read_png(png)

    assert loaded == original
    assert loaded.startswith(PNG_SIGNATURE)


def test_read_png_rejects_other_files(tmp_path: Path) -> None:
    text = tmp_path / "not-an-image.png"
    text.write_text("not png", encoding="utf-8")

    with pytest.raises(RenderError, match="Not a PNG"):
        read_png(text)


def test_render_page_dict_has_view_image_fields(tmp_path: Path) -> None:
    png = tmp_path / "page.png"
    _save_png(png)
    page = RenderPage(
        index=3,
        path=png,
        sha256=sha256_file(png),
        width=8,
        height=6,
        sheet="Data",
        sheet_page=2,
    )

    payload = page.to_dict(relative_to=tmp_path)

    assert payload["image_path"] == str(png.resolve())
    assert payload["path"] == "page.png"
    assert payload["index"] == 3
    assert payload["page"] == 2
    assert payload["sheet"] == "Data"
    assert payload["sheet_page"] == 2
    assert payload["width"] == 8
    assert payload["height"] == 6
    assert len(payload["sha256"]) == 64


def test_single_sheet_copy_hides_only_disposable_copy(tmp_path: Path) -> None:
    source = tmp_path / "book.xlsx"
    target = tmp_path / "single.xlsx"
    _save_workbook(source)
    before = source.read_bytes()

    render_module._make_single_sheet_copy(source, target, "Summary")

    copied = load_workbook(target)
    try:
        assert copied["Data"].sheet_state == "hidden"
        assert copied["Summary"].sheet_state == "visible"
        assert copied.active.title == "Summary"
    finally:
        copied.close()
    assert source.read_bytes() == before


def test_pymupdf_rasterization_produces_unmodified_png(tmp_path: Path) -> None:
    module = render_module._pymupdf_module()
    pdf = tmp_path / "source.pdf"
    document = module.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((20, 50), "Spreadsheet")
    document.save(str(pdf))
    document.close()

    pages = render_module._rasterize_pdf(
        pdf,
        tmp_path / "png",
        dpi=72,
        filename_prefix="page",
        sheet="Data",
    )

    assert len(pages) == 1
    assert read_png(pages[0].path).startswith(PNG_SIGNATURE)
    assert pages[0].width == 200
    assert pages[0].height == 100
    assert pages[0].sheet == "Data"
    assert pages[0].sheet_page == 1


def test_per_sheet_failure_falls_back_and_records_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "book.xlsx"
    _save_workbook(source)
    before = source.read_bytes()

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")
    monkeypatch.setattr(render_module, "pymupdf_version", lambda: "PyMuPDF test")

    def fail_per_sheet(*args: object, **kwargs: object) -> list[object]:
        raise RenderError("deliberate per-sheet failure")

    def fake_whole(
        source_copy: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> list[render_module._RasterizedPage]:
        assert source_copy != source
        png = work_dir / "png" / "workbook-page-0001.png"
        png.parent.mkdir(parents=True)
        _save_png(png)
        return [
            render_module._RasterizedPage(
                path=png,
                width=8,
                height=6,
                sheet=None,
                sheet_page=None,
            )
        ]

    monkeypatch.setattr(render_module, "_render_per_sheet", fail_per_sheet)
    monkeypatch.setattr(render_module, "_render_whole_workbook", fake_whole)

    result = render_workbook(source, tmp_path / "rendered")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert source.read_bytes() == before
    assert result.mode == "whole_workbook"
    assert manifest["backend"] == "libreoffice-headless+pymupdf"
    assert manifest["version"]["libreoffice"] == "LibreOffice test"
    assert manifest["hash"] == sha256_file(source)
    assert manifest["page_count"] == 1
    assert manifest["fallback"]["from"] == "per_sheet"
    assert "deliberate per-sheet failure" in manifest["fallback"]["reason"]
    page_payload = manifest["pages"][0]
    assert Path(page_payload["image_path"]).is_absolute()
    assert page_payload["sheet"] is None
    assert page_payload["page"] == 1
    assert read_png(page_payload["image_path"]).startswith(PNG_SIGNATURE)


def test_recalculation_uses_private_copy_and_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "working.xlsx"
    _save_workbook(source)
    original_hash = sha256_file(source)
    seen_sources: list[Path] = []

    monkeypatch.setattr(render_module, "find_libreoffice", lambda explicit=None: "/fake/soffice")
    monkeypatch.setattr(render_module, "libreoffice_version", lambda binary: "LibreOffice test")

    def fake_convert(
        source_copy: Path,
        output_dir: Path,
        **kwargs: object,
    ) -> Path:
        seen_sources.append(source_copy.resolve())
        assert source_copy.resolve() != source.resolve()
        output_dir.mkdir(parents=True)
        converted = output_dir / "working.xlsx"
        converted.write_bytes(source_copy.read_bytes())
        return converted

    monkeypatch.setattr(render_module, "_convert_with_libreoffice", fake_convert)

    metadata = recalculate_workbook(source, source)

    assert seen_sources and seen_sources[0] != source.resolve()
    assert sha256_file(source) == original_hash
    assert metadata["backend"] == "libreoffice-headless"
    assert metadata["version"] == "LibreOffice test"
    assert metadata["source_sha256"] == original_hash
    assert metadata["output_sha256"] == original_hash
    assert metadata["destination_path"] == str(source.resolve())
    assert metadata["atomic_replace"] is True


@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
def test_libreoffice_render_integration_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "integration.xlsx"
    _save_workbook(source)
    before_hash = sha256_file(source)

    result = render_workbook(source, tmp_path / "rendered", dpi=72)

    assert sha256_file(source) == before_hash
    assert result.pages
    assert result.backend == "libreoffice-headless+pymupdf"
    assert result.version["libreoffice"] != "unknown"
    if result.mode == "per_sheet":
        assert {page.sheet for page in result.pages} == {"Data", "Summary"}
    for page in result.pages:
        assert read_png(page.path).startswith(PNG_SIGNATURE)
