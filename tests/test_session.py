from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet.table import Table, TableStyleInfo

from spreadsheet_harness.errors import ToolInputError
from spreadsheet_harness.session import WorkbookSession


def test_inspect_write_fill_format_and_undo(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")

    listed = session.list_sheets()
    assert [item["name"] for item in listed["sheets"]] == ["Sales", "Lookup"]

    before = session.inspect_range("Sales", "A1:D3")
    assert before["matrix"][1][3] == "=B2*C2"
    assert before["cells"][0]["style"]["font"]["bold"] is True

    result = session.write_range("Sales", "B4", [[5, 1.25, "=B4*C4"]])
    assert result["cells_written"] == 3
    session.fill_formula("Sales", "D2", "D2:D4")
    session.format_range("Sales", "B2:B4", {"number_format": "0", "fill_color": "FFF2CC"})

    changed = session.inspect_range("Sales", "B4:D4")
    assert changed["matrix"][0] == [5, 1.25, "=B4*C4"]

    session.clear_range("Sales", "C4")
    assert session.inspect_range("Sales", "C4:C4")["matrix"] == [[None]]
    session.undo_last()
    assert session.inspect_range("Sales", "C4:C4")["matrix"] == [[1.25]]

    workbook = load_workbook(session.workbook_path, data_only=False)
    assert workbook["Sales"]["D4"].value == "=B4*C4"
    workbook.close()
    assert session.paths.trajectory.is_file()
    assert len(list(session.paths.snapshots.glob("*.xlsx"))) >= 3


def test_failed_mutation_keeps_workbook_valid(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    original = session.workbook_path.read_bytes()
    with pytest.raises(ToolInputError):
        session.manage_sheet("delete", "missing")
    assert session.workbook_path.read_bytes() == original
    assert session.list_sheets()["sheets"][0]["name"] == "Sales"


def test_range_limits(sample_workbook: Path, tmp_path: Path) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    with pytest.raises(ToolInputError, match="limit"):
        session.inspect_range("Sales", "A1:Z1000")
    with pytest.raises(ToolInputError, match="rectangular"):
        session.write_range("Sales", "A1", [[1], [2, 3]])


def test_inspect_range_reports_tables(sample_workbook: Path, tmp_path: Path) -> None:
    workbook = load_workbook(sample_workbook)
    sheet = workbook["Sales"]
    table = Table(displayName="SalesTable", ref="A1:D3")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    workbook.save(sample_workbook)
    workbook.close()

    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    inspected = session.inspect_range("Sales", "A1:D3")

    assert inspected["tables"] == [{"name": "SalesTable", "ref": "A1:D3"}]
