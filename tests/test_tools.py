from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from PIL import Image

from spreadsheet_harness.session import WorkbookSession
from spreadsheet_harness.tools import SpreadsheetToolRegistry


def test_tool_registry_dispatch_and_errors(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    names = {item["name"] for item in tools.schemas}
    assert {"list_sheets", "inspect_range", "range_to_latex", "view_image"} <= names
    assert "code_interpreter" not in names
    assert tools.invoke("list_sheets", {}).data["ok"] is True
    result = tools.invoke("inspect_range", {"sheet": "Nope", "range_ref": "A1"})
    assert result.data["ok"] is False
    assert result.data["type"] == "ToolInputError"


def test_code_interpreter_schema_requires_self_contained_calls(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=True)
    schema = next(item for item in tools.schemas if item["name"] == "code_interpreter")
    description = schema["description"]

    assert "fresh Python process" in description
    assert "variables, imports, and workbook objects do not persist" in description
    assert "editing or recovery script self-contained" in description
    workflow = description.split("self-contained:", 1)[1]
    expected_steps = (
        "import",
        "load",
        "re-read the request and inspected workbook state",
        "edit",
        "save",
        "close",
        "reopen",
        "verify the requested change and nearby cells",
        "print compact verification",
    )
    positions = [workflow.index(step) for step in expected_steps]
    assert positions == sorted(positions)


def test_tool_registry_redacts_configured_secret_from_recorded_outcome(
    sample_workbook: Path, tmp_path: Path
) -> None:
    unusual_secret = "credential-with-an-unusual-shape"
    session = WorkbookSession.create(
        sample_workbook,
        tmp_path / "run",
        recorder_secrets=(unusual_secret,),
    )
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=True,
        allowed_tools={"code_interpreter"},
    )

    class LeakingInterpreter:
        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "stdout": unusual_secret,
                "nested": {"message": unusual_secret},
            }

    tools.interpreter = LeakingInterpreter()  # type: ignore[assignment]
    result = tools.invoke("code_interpreter", {"code": "print('x')"})

    assert result.data["stdout"] == unusual_secret
    assert unusual_secret not in session.paths.trajectory.read_text(encoding="utf-8")


def test_allowed_tools_filters_schemas_and_dispatch(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    allowed = {"list_sheets", "range_to_latex"}
    tools = SpreadsheetToolRegistry(session, enable_code=False, allowed_tools=allowed)
    allowed.clear()

    assert {item["name"] for item in tools.schemas} == {"list_sheets", "range_to_latex"}
    assert tools.invoke("list_sheets", {}).data["ok"] is True

    before = session.workbook_path.read_bytes()
    blocked = tools.invoke(
        "write_range", {"sheet": "Sales", "start_cell": "A1", "values": [["changed"]]}
    )
    assert blocked.data == {
        "ok": False,
        "error": "Unknown tool: write_range",
        "type": "UnknownTool",
    }
    assert session.workbook_path.read_bytes() == before


def test_view_image_only_accepts_page_from_most_recent_render(
    sample_workbook: Path, tmp_path: Path
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session,
        enable_code=False,
        allowed_tools={"render_workbook", "view_image"},
    )
    render_root = session.paths.artifacts / "render"
    stale = render_root / "render-old" / "page.png"
    latest = render_root / "render-new" / "page.png"
    for path in (stale, latest):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), "white").save(path)

    before_render = tools.invoke("view_image", {"image_path": str(stale)}).data
    assert before_render["ok"] is False
    assert "render_workbook before" in before_render["error"]

    tools._last_render = {"pages": [{"image_path": str(latest.resolve())}]}
    stale_result = tools.invoke("view_image", {"image_path": str(stale)}).data
    assert stale_result["ok"] is False
    assert "most recent render_workbook" in stale_result["error"]

    latest_result = tools.invoke("view_image", {"image_path": str(latest)})
    assert latest_result.data["ok"] is True
    assert latest_result.image_path == latest.resolve()


def test_range_to_latex_escapes_values_and_reports_structure(
    sample_workbook: Path, tmp_path: Path
) -> None:
    workbook = load_workbook(sample_workbook)
    workbook["Sales"]["A2"] = "unsafe &_%$#{}~^\\\nnext"
    workbook.save(sample_workbook)
    workbook.close()

    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(
        session, enable_code=False, allowed_tools={"range_to_latex"}
    )
    before = session.workbook_path.read_bytes()
    result = tools.invoke("range_to_latex", {"sheet": "Sales", "range_ref": "A1:D5"}).data

    assert result["ok"] is True
    assert (result["rows"], result["columns"], result["cell_count"]) == (5, 4, 20)
    assert result["latex"].startswith(r"\begin{tabular}{llll}")
    assert result["latex"].endswith(r"\end{tabular}")
    assert r"unsafe \&\_\%\$\#\{\}\textasciitilde{}\textasciicircum{}" in result[
        "latex"
    ]
    assert r"\textbackslash{}\newline{}next" in result["latex"]
    assert result["merged_ranges"] == ["A5:B5"]
    heading_style = next(
        style for style in result["style_summary"]["styles"] if "A1" in style["sample_cells"]
    )
    assert heading_style["font"]["bold"] is True
    assert heading_style["fill"] == "FF336699"
    assert session.workbook_path.read_bytes() == before


def test_range_to_latex_enforces_cell_and_output_limits(
    sample_workbook: Path, tmp_path: Path
) -> None:
    workbook = load_workbook(sample_workbook)
    workbook["Sales"]["A2"] = "\\" * 30_000
    workbook.save(sample_workbook)
    workbook.close()

    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    bounded = tools.invoke("range_to_latex", {"sheet": "Sales", "range_ref": "A2"}).data

    assert bounded["ok"] is True
    assert bounded["latex_truncated"] is True
    assert bounded["truncated_cell_count"] == 1
    assert len(bounded["latex"]) <= bounded["limits"]["max_latex_chars"] == 65_536

    too_large = tools.invoke(
        "range_to_latex", {"sheet": "Sales", "range_ref": "A1:Z20"}
    ).data
    assert too_large["ok"] is False
    assert too_large["type"] == "ToolInputError"
    assert "limit is 500" in too_large["error"]
