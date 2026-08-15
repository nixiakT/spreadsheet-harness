"""Local compatibility helpers for supported openpyxl releases."""

from __future__ import annotations

from typing import Any

from openpyxl.chartsheet import Chartsheet
from openpyxl.drawing.spreadsheet_drawing import SpreadsheetDrawing
from openpyxl.packaging.relationship import RelationshipList, get_rels_path
from openpyxl.reader.drawings import find_images
from openpyxl.reader.excel import ExcelReader
from openpyxl.xml.functions import fromstring


class _ChartsheetCompatibleExcelReader(ExcelReader):
    """Keep empty chartsheets loadable without changing worksheet handling."""

    def read_chartsheet(self, sheet: Any, rel: Any) -> None:
        sheet_path = rel.target
        rels_path = get_rels_path(sheet_path)
        if rels_path in self.valid_files:
            # Preserve openpyxl's drawing/image handling whenever relationships exist.
            super().read_chartsheet(sheet, rel)
            self.wb._sheets[-1].sheet_state = sheet.state
            return

        # openpyxl 3.1.5 initializes this as ``[]`` and then calls ``find`` on it.
        # Use the same empty relationship container as the worksheet reader.
        rels = RelationshipList()
        with self.archive.open(sheet_path, "r") as source:
            node = fromstring(source.read())
        chartsheet = Chartsheet.from_tree(node)
        chartsheet._parent = self.wb
        chartsheet.title = sheet.name
        chartsheet.sheet_state = sheet.state
        self.wb._add_sheet(chartsheet)

        for drawing_rel in rels.find(SpreadsheetDrawing._rel_type):
            charts, _images = find_images(self.archive, drawing_rel.target)
            for chart in charts:
                chartsheet.add_chart(chart)


def load_workbook(
    filename: Any,
    read_only: bool = False,
    keep_vba: bool = False,
    data_only: bool = False,
    keep_links: bool = True,
    rich_text: bool = False,
) -> Any:
    """Load a workbook with an isolated, idempotent chartsheet reader fix."""

    reader = _ChartsheetCompatibleExcelReader(
        filename,
        read_only,
        keep_vba,
        data_only,
        keep_links,
        rich_text,
    )
    reader.read()
    return reader.wb


__all__ = ["load_workbook"]
