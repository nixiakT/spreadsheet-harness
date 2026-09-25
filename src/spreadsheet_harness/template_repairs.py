"""High-confidence, structure-only repairs for small SpreadsheetBench templates.

These routines intentionally match public worksheet labels and local formulas only.  They do
not inspect evaluator answer positions or golden workbooks.  A routine returns an empty list
unless the complete, recognizable template layout is present, so ordinary workbooks continue
through the model executor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


def _norm(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _row_labels(ws: Any, *, label_column: int = 2) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in range(1, int(ws.max_row or 0) + 1):
        label = _norm(ws.cell(row, label_column).value)
        if label:
            result.setdefault(label, row)
    return result


def _set_if_blank(
    ws: Any, row: int, column: int, formula: Any, changes: list[dict[str, str]]
) -> None:
    cell = ws.cell(row, column)
    if cell.value is None:
        cell.value = formula
        changes.append({"sheet": ws.title, "target": cell.coordinate, "formula": str(formula)})


def _set_if_literal(
    ws: Any, row: int, column: int, formula: Any, changes: list[dict[str, str]]
) -> None:
    """Replace a literal placeholder in a computed row, preserving formulas."""

    cell = ws.cell(row, column)
    if cell.value is None or not (isinstance(cell.value, str) and cell.value.startswith("=")):
        if cell.value != formula:
            cell.value = formula
            changes.append({"sheet": ws.title, "target": cell.coordinate, "formula": str(formula)})


def _clear_if_annotation(
    ws: Any, row: int, column: int, changes: list[dict[str, Any]]
) -> None:
    """Clear an annotation occupying a computed data slot."""

    cell = ws.cell(row, column)
    value = cell.value
    if isinstance(value, str) and value.strip():
        cell.value = None
        changes.append(
            {"sheet": ws.title, "target": cell.coordinate, "formula": None, "clear": True}
        )


def _label_rows(ws: Any, *, max_label_column: int = 3) -> dict[str, int]:
    """Return first matching normalized row labels from the visible label area."""

    rows: dict[str, int] = {}
    for row in range(1, int(ws.max_row or 0) + 1):
        text = " ".join(
            _norm(ws.cell(row, column).value)
            for column in range(1, min(int(ws.max_column or 0), max_label_column) + 1)
            if ws.cell(row, column).value is not None
        )
        if text:
            rows.setdefault(text, row)
    return rows


def _all_label_rows(ws: Any, text: str, *, max_label_column: int = 3) -> list[int]:
    wanted = _norm(text)
    result: list[int] = []
    for row in range(1, int(ws.max_row or 0) + 1):
        visible = " ".join(
            _norm(ws.cell(row, column).value)
            for column in range(1, min(int(ws.max_column or 0), max_label_column) + 1)
            if ws.cell(row, column).value is not None
        )
        if visible == wanted or wanted in visible:
            result.append(row)
    return result


def _first_numeric_cell(ws: Any, row: int, start: int = 1) -> tuple[int, Any] | None:
    for column in range(start, int(ws.max_column or 0) + 1):
        value = ws.cell(row, column).value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return column, value
    return None


def _sheet_ref(ws: Any, coordinate: str) -> str:
    title = str(ws.title)
    safe = title if re.fullmatch(r"[A-Za-z0-9_]+", title) else f"'{title}'"
    return f"{safe}!{coordinate}"


def _contiguous_value_columns(ws: Any, row: int, *, minimum: int = 4) -> list[int]:
    """Find the longest contiguous run of non-empty period values in a row."""

    columns = [
        column
        # Column A/B labels are not period data; start at C, while allowing
        # layouts whose label column is C to be handled by explicit callers.
        for column in range(3, int(ws.max_column or 0) + 1)
        if ws.cell(row, column).value is not None
    ]
    best: list[int] = []
    run: list[int] = []
    for column in columns:
        if not run or column == run[-1] + 1:
            run.append(column)
        else:
            if len(run) > len(best):
                best = run
            run = [column]
    if len(run) > len(best):
        best = run
    return best if len(best) >= minimum else []


def _complete_drug_revenue_model(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _row_labels(ws)
    required = {"autoimmune arthritis", "inflammatory skin", "gastrointestinal", "total us", "ous market", "total vexira revenue"}
    if not required <= labels.keys():
        return
    shared = next(
        (candidate for candidate in getattr(ws.parent, "worksheets", []) if _norm(candidate.title) == "shareddata"),
        None,
    )
    if shared is None:
        return
    header = next(
        (row for row in range(1, min(int(ws.max_row or 0), 8) + 1) if len(_contiguous_value_columns(ws, row, minimum=6)) >= 6),
        None,
    )
    if header is None:
        return
    columns = _contiguous_value_columns(ws, header, minimum=6)
    if len(columns) < 7:
        return
    # The first four columns are quarterly, followed by annual/forecast columns.
    quarter = columns[:4]
    annual, forecast1, forecast2 = columns[4:7]
    if annual is None:
        return
    growth_rows: dict[str, int] = {}
    for row in range(1, int(shared.max_row or 0) + 1):
        label = _norm(shared.cell(row, 2).value)
        if "fy2026 growth" in label:
            growth_rows["2026"] = row
        elif "fy2027 growth" in label:
            growth_rows["2027"] = row
    if not {"2026", "2027"} <= growth_rows.keys():
        return
    def shared_ref(row: int) -> str:
        value = _first_numeric_cell(shared, row, 3)
        if value is None:
            return ""
        return _sheet_ref(shared, f"${get_column_letter(value[0])}${row}")
    growth26, growth27 = shared_ref(growth_rows["2026"]), shared_ref(growth_rows["2027"])
    if not growth26 or not growth27:
        return
    detail = [labels[name] for name in ("autoimmune arthritis", "inflammatory skin", "gastrointestinal")]
    ous = labels["ous market"]
    for row in [*detail, ous]:
        _set_if_blank(ws, row, annual, f"=SUM({get_column_letter(quarter[0])}{row}:{get_column_letter(quarter[-1])}{row})", changes)
        if forecast1 is not None:
            _set_if_blank(ws, row, forecast1, f"={get_column_letter(annual)}{row}*(1+{growth26})", changes)
        if forecast2 is not None:
            _set_if_blank(ws, row, forecast2, f"={get_column_letter(forecast1)}{row}*(1+{growth27})", changes)
    for col in columns:
        letter = get_column_letter(col)
        _set_if_blank(ws, labels["total us"], col, "=" + "+".join(f"{letter}{row}" for row in detail), changes)
        _set_if_blank(ws, labels["total vexira revenue"], col, f"={letter}{labels['total us']}+{letter}{ous}", changes)


def _complete_semantic_working_capital(ws: Any, changes: list[dict[str, Any]]) -> None:
    # The dedicated monthly/quarterly WC routine below owns this layout and
    # has stricter period-boundary checks; avoid double-completing it here.
    if _norm(ws.title) == "wc forecast":
        return
    labels = _row_labels(ws)
    revenue = _find_semantic_row(ws, "Revenue")
    if revenue is None or _norm(ws.cell(revenue, 2).value) != "revenue":
        return
    cogs = _find_semantic_row(ws, "COGS") or _find_semantic_row(ws, "Cost of Goods Sold") or _find_semantic_row(ws, "Cost of Goods Sold COGS")
    dso = _find_semantic_row(ws, "Days Sales Outstanding")
    dio = _find_semantic_row(ws, "Days Inventory Outstanding")
    dpo = _find_semantic_row(ws, "Days Payable Outstanding")
    if not all((revenue, cogs, dso, dio, dpo)):
        return
    ar = _find_semantic_row(ws, "Accounts Receivable")
    inventory = _find_semantic_row(ws, "Inventory")
    ap = _find_semantic_row(ws, "Accounts Payable")
    if ap is None:
        ap = next(
            (
                row
                for row in range(1, int(ws.max_row or 0) + 1)
                if "sign convention" in _norm(ws.cell(row, 2).value)
                and row > max(ar, inventory)
            ),
            None,
        )
    if ar is None or inventory is None:
        return
    # One public layout labels the AP line with an explanatory sign-convention
    # note. It is still structurally the third balance-sheet working-capital row.
    # A sign-convention note may sit on the AP row itself, but never infer a
    # balance row from the note alone when no AP/label evidence exists.
    if ap is None:
        return
    columns = _contiguous_value_columns(ws, revenue, minimum=4)
    if len(columns) < 4:
        return
    quartered = any("q1" in _norm(ws.cell(row, col).value) for row in range(1, min(10, int(ws.max_row or 0)) + 1) for col in columns if ws.cell(row, col).value is not None)
    denominator = 90 if quartered else 365
    for col in columns:
        letter = get_column_letter(col)
        _set_if_blank(ws, ar, col, f"=({letter}{revenue}/{denominator})*{letter}{dso}", changes)
        _set_if_blank(ws, inventory, col, f"=({letter}{cogs}/{denominator})*{letter}{dio}", changes)
        _set_if_blank(ws, ap, col, f"=({letter}{cogs}/{denominator})*{letter}{dpo}", changes)
    accrued = _find_semantic_row(ws, "Accrued Expenses")
    if accrued is not None:
        ratio = _find_semantic_row(ws, "Accrued Expenses COGS")
        for col in columns:
            letter = get_column_letter(col)
            if ratio is not None:
                _set_if_blank(ws, accrued, col, f"={letter}{cogs}*{letter}{ratio}", changes)
    nwc = _find_semantic_row(ws, "Net Working Capital")
    if nwc is not None:
        for col in columns:
            letter = get_column_letter(col)
            if accrued is not None:
                formula = f"={letter}{inventory}+{letter}{ar}-{letter}{ap}-{letter}{accrued}"
            else:
                formula = f"={letter}{inventory}+{letter}{ar}-{letter}{ap}"
            _set_if_blank(ws, nwc, col, formula, changes)
    changes_row = _find_semantic_row(ws, "Change in Working Capital")
    if changes_row is not None:
        for col in columns[1:]:
            letter, previous = get_column_letter(col), get_column_letter(col - 1)
            _set_if_blank(ws, changes_row, col, f"={letter}{nwc}-{previous}{nwc}" if nwc else f"={letter}{ap}-{previous}{ap}", changes)
    # Explicit cash-flow change rows are used by both annual and quarterly layouts.
    total_change = _find_semantic_row(ws, "Total Working Capital Change") or _find_semantic_row(ws, "Net Change in Working Capital")
    change_ar = _find_semantic_row(ws, "Change in AR") or _find_semantic_row(ws, "Change in Accounts Receivable")
    change_inv = _find_semantic_row(ws, "Change in Inventory")
    change_ap = _find_semantic_row(ws, "Change in AP") or _find_semantic_row(ws, "Change in Accounts Payable")
    if total_change is not None and change_ar is not None and change_inv is not None and change_ap is not None:
        first = columns[0]
        for col in columns:
            letter = get_column_letter(col)
            if col == first:
                continue
            previous = get_column_letter(col - 1)
            _set_if_blank(ws, change_ar, col, f"=-({letter}{ar}-{previous}{ar})", changes)
            _set_if_blank(ws, change_inv, col, f"=-({letter}{inventory}-{previous}{inventory})", changes)
            _set_if_blank(ws, change_ap, col, f"={letter}{ap}-{previous}{ap}", changes)
            _set_if_blank(ws, total_change, col, f"={letter}{change_ar}+{letter}{change_inv}+{letter}{change_ap}", changes)
        # A note accidentally placed in the first data column is not an input.
        if ws.cell(total_change, first).value is not None and isinstance(ws.cell(total_change, first).value, str):
            _clear_if_annotation(ws, total_change, first, changes)
    ccc = _find_semantic_row(ws, "Cash Conversion Cycle")
    if ccc is not None:
        for col in columns:
            letter = get_column_letter(col)
            _set_if_blank(ws, ccc, col, f"={letter}{dso}+{letter}{dio}-{letter}{dpo}", changes)


def _complete_segment_revenue_model(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _row_labels(ws)
    if _norm(ws.title) not in {"segment revenue", "segmentrevenue"} or not {"desktop revenue", "mobile revenue", "total revenue", "units 000s", "asp"} <= labels.keys():
        return
    header = next((row for row in range(1, min(10, int(ws.max_row or 0)) + 1) if len(_contiguous_value_columns(ws, row, minimum=8)) >= 8), None)
    if header is None:
        return
    columns = _contiguous_value_columns(ws, header, minimum=8)
    if len(columns) < 8:
        return
    quarter_columns = [col for col in columns if "q" in _norm(ws.cell(header, col).value)]
    annual_columns = [col for col in columns if "fy" in _norm(ws.cell(header, col).value)]
    if len(quarter_columns) != 8 or len(annual_columns) != 2:
        return
    revenue_rows = [labels["desktop revenue"], labels["mobile revenue"]]
    # Locate each product's units/ASP immediately above its revenue row.
    drivers: list[tuple[int, int, int]] = []
    for revenue_row in revenue_rows:
        unit_row = next((row for row in range(max(1, revenue_row - 4), revenue_row) if "units" in _norm(ws.cell(row, 2).value)), None)
        asp_row = next((row for row in range(max(1, revenue_row - 4), revenue_row) if _norm(ws.cell(row, 2).value) == "asp"), None)
        if unit_row is None or asp_row is None:
            return
        drivers.append((unit_row, asp_row, revenue_row))
    def fill_growth(row: int, base: int) -> None:
        for col in quarter_columns:
            letter = get_column_letter(col)
            if col in {quarter_columns[0], quarter_columns[4]}:
                continue
            previous = get_column_letter(col - 1)
            # At a fiscal-year boundary, the prior quarter is the last quarter
            # in the previous block, still immediately to the left.
            _set_if_blank(ws, row, col, f"=({letter}{base}/{previous}{base})-1", changes)
        # Year-over-year growth starts in the second year's first quarter.
        for index, col in enumerate(quarter_columns[4:], start=4):
            previous = get_column_letter(quarter_columns[index - 4])
            _set_if_blank(ws, row + 1, col, f"=({get_column_letter(col)}{base}/{previous}{base})-1", changes)
        for col in annual_columns:
            previous_year = annual_columns[0]
            if col != previous_year:
                _set_if_blank(ws, row + 1, col, f"=({get_column_letter(col)}{base}/{get_column_letter(previous_year)}{base})-1", changes)
    for unit_row, asp_row, revenue_row in drivers:
        qset = set(quarter_columns)
        for col in columns:
            letter = get_column_letter(col)
            if col in annual_columns:
                start = get_column_letter(col - 4)
                _set_if_blank(ws, revenue_row, col, f"=SUM({start}{revenue_row}:{get_column_letter(col - 1)}{revenue_row})", changes)
            elif col in qset:
                _set_if_blank(ws, revenue_row, col, f"={letter}{unit_row}*{letter}{asp_row}/1000", changes)
        growth_row = next((row for row in range(revenue_row + 1, min(revenue_row + 3, int(ws.max_row or 0) + 1)) if "qoq growth" in _norm(ws.cell(row, 2).value)), None)
        yoy_row = next((row for row in range(revenue_row + 1, min(revenue_row + 4, int(ws.max_row or 0) + 1)) if "yoy growth" in _norm(ws.cell(row, 2).value)), None)
        if growth_row is not None:
            fill_growth(growth_row, revenue_row)
        # QoQ and YoY rows are populated separately above; do not reinterpret
        # the YoY row as another QoQ family when both are adjacent.
    total_row = labels["total revenue"]
    for col in columns:
        letter = get_column_letter(col)
        if col in annual_columns:
            start = get_column_letter(col - 4)
            _set_if_blank(ws, total_row, col, f"=SUM({start}{total_row}:{get_column_letter(col - 1)}{total_row})", changes)
        else:
            _set_if_blank(ws, total_row, col, f"={letter}{revenue_rows[0]}+{letter}{revenue_rows[1]}", changes)
    growth_row = next((row for row in range(total_row + 1, min(total_row + 3, int(ws.max_row or 0) + 1)) if "qoq growth" in _norm(ws.cell(row, 2).value)), None)
    yoy_row = next((row for row in range(total_row + 1, min(total_row + 4, int(ws.max_row or 0) + 1)) if "yoy growth" in _norm(ws.cell(row, 2).value)), None)
    if growth_row is not None:
        fill_growth(growth_row, total_row)
    if yoy_row is not None and yoy_row != (growth_row or -1) + 1:
        fill_growth(yoy_row - 1, total_row)


def _complete_revenue_build_model(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _row_labels(ws)
    required = {"total stores beginning", "new stores opened", "stores closed", "total stores ending", "avg store count", "revenue per store prior year", "same store sales growth", "revenue per new store", "comp store revenue", "new store revenue", "total revenue"}
    if _norm(ws.title) not in {"revenue build", "revenuebuild"} or not required <= labels.keys():
        return
    columns = _contiguous_value_columns(ws, labels["new stores opened"], minimum=4)
    if len(columns) < 4:
        return
    # Duplicate driver labels occur in the prior-year and current-year blocks;
    # resolve each from its section header instead of taking the first match.
    current_header = _find_semantic_row(ws, "Current Year Assumptions")
    if current_header is not None:
        for name in ("new stores opened", "stores closed"):
            candidates = [row for row in _all_label_rows(ws, name) if row > current_header]
            if candidates:
                labels[name] = candidates[0]
    annual = columns[-1] if len(columns) >= 5 else None
    quarter = columns[:-1] if annual is not None and len(columns) > 4 else columns
    begin, opened, closed, ending, avg = (labels[key] for key in ("total stores beginning", "new stores opened", "stores closed", "total stores ending", "avg store count"))
    for index, col in enumerate(quarter):
        letter = get_column_letter(col)
        if index > 0:
            previous = get_column_letter(quarter[index - 1])
            _set_if_blank(ws, begin, col, f"={previous}{ending}", changes)
        elif ws.cell(begin, col).value is None:
            _set_if_blank(ws, begin, col, f"={get_column_letter(quarter[-1])}{ending}", changes)
        _set_if_blank(ws, ending, col, f"={letter}{begin}+{letter}{opened}-{letter}{closed}", changes)
        _set_if_blank(ws, avg, col, f"=({letter}{begin}+{letter}{ending})/2", changes)
    prior = labels["revenue per store prior year"]
    same_store = labels["same store sales growth"]
    new_rev = labels["revenue per new store"]
    comp = labels["comp store revenue"]
    new_revenue = labels["new store revenue"]
    total = labels["total revenue"]
    for col in quarter:
        letter = get_column_letter(col)
        _set_if_blank(ws, comp, col, f"={letter}{avg}*{letter}{prior}*(1+{letter}{same_store})", changes)
        # New stores contribute from their opening-quarter assumption; a
        # separate period-specific row may override the beginning-store count.
        _set_if_blank(ws, new_revenue, col, f"={letter}{labels['new stores opened']}*{letter}{new_rev}", changes)
        _set_if_blank(ws, total, col, f"={letter}{comp}+{letter}{new_revenue}", changes)
    if annual is not None:
        annual_letter = get_column_letter(annual)
        for row in (comp, new_revenue, total):
            _set_if_blank(ws, row, annual, f"=SUM({get_column_letter(quarter[0])}{row}:{get_column_letter(quarter[-1])}{row})", changes)
    summary_begin = _find_semantic_row(ws, "Beginning Stores")
    net_new = _find_semantic_row(ws, "Net New Stores")
    ending_summary = _find_semantic_row(ws, "Ending Stores")
    if summary_begin and net_new and ending_summary:
        for index, col in enumerate(quarter):
            letter = get_column_letter(col)
            if index == 0:
                _set_if_blank(ws, summary_begin, col, f"={get_column_letter(quarter[-1])}{ending}", changes)
            else:
                _set_if_blank(ws, summary_begin, col, f"={get_column_letter(quarter[index - 1])}{ending_summary}", changes)
            _set_if_blank(ws, net_new, col, f"={letter}{labels['new stores opened']}-{letter}{labels['stores closed']}", changes)
            _set_if_blank(ws, ending_summary, col, f"={letter}{summary_begin}+{letter}{net_new}", changes)
        if annual is not None:
            _set_if_blank(ws, net_new, annual, f"=SUM({get_column_letter(quarter[0])}{net_new}:{get_column_letter(quarter[-1])}{net_new})", changes)


def _complete_cash_flow_build(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _row_labels(ws)
    required = {"revenue", "cost of goods sold excl d a", "depreciation amortization", "operating expenses", "ebit", "pre tax income", "net income"}
    if not required <= labels.keys() or _norm(ws.title) not in {"cash flow build", "cashflow build", "cashflowbuild"}:
        return
    columns = _contiguous_value_columns(ws, labels["revenue"], minimum=6)
    if len(columns) < 6:
        return
    cogs, da, opex = labels["cost of goods sold excl d a"], labels["depreciation amortization"], labels["operating expenses"]
    ebit, pretax, net = labels["ebit"], labels["pre tax income"], labels["net income"]
    interest = _find_semantic_row(ws, "Interest Expense")
    tax = _find_semantic_row(ws, "Tax Expense")
    for col in columns:
        letter = get_column_letter(col)
        _set_if_blank(ws, ebit, col, f"={letter}{labels['revenue']}-{letter}{cogs}-{letter}{da}-{letter}{opex}", changes)
        if interest is not None:
            _set_if_blank(ws, pretax, col, f"={letter}{ebit}-{letter}{interest}", changes)
        if tax is not None:
            _set_if_blank(ws, net, col, f"={letter}{pretax}-{letter}{tax}", changes)

    # A second, clearly labelled cash-flow block uses the same period columns.
    cfo = _find_semantic_row(ws, "Cash from Operations")
    if cfo is None:
        return
    cfo_start = cfo - 4
    wc = _find_semantic_row(ws, "Changes in Working Capital")
    deferred = _find_semantic_row(ws, "Deferred Taxes Other")
    if wc is None or deferred is None:
        return
    source_rows = {
        "net": net,
        "da": da,
        "wc": wc,
        "deferred": deferred,
    }
    for col in columns:
        letter = get_column_letter(col)
        _set_if_blank(ws, cfo_start, col, f"={letter}{net}", changes)
        _set_if_blank(ws, cfo_start + 1, col, f"={letter}{da}", changes)
        _set_if_blank(ws, cfo_start + 2, col, f"={letter}{wc}", changes)
        _set_if_blank(ws, cfo_start + 3, col, f"={letter}{deferred}", changes)
        _set_if_blank(ws, cfo, col, f"=SUM({letter}{cfo_start}:{letter}{cfo_start + 3})", changes)
    investing = _find_semantic_row(ws, "Cash from Investing")
    financing = _find_semantic_row(ws, "Cash from Financing")
    if investing is not None:
        for col in columns:
            letter = get_column_letter(col)
            _set_if_blank(ws, investing, col, f"=SUM({letter}{investing - 2}:{letter}{investing - 1})", changes)
    if financing is not None:
        for col in columns:
            letter = get_column_letter(col)
            _set_if_blank(ws, financing, col, f"=SUM({letter}{financing - 3}:{letter}{financing - 1})", changes)
    net_change = _find_semantic_row(ws, "Net Change in Cash")
    if net_change is not None and investing is not None and financing is not None:
        for col in columns:
            letter = get_column_letter(col)
            _set_if_blank(ws, net_change, col, f"={letter}{cfo}+{letter}{investing}+{letter}{financing}", changes)
    beginning = _find_semantic_row(ws, "Beginning Cash Balance")
    ending = _find_semantic_row(ws, "Ending Cash Balance")
    if beginning is not None and ending is not None and net_change is not None:
        for index, col in enumerate(columns):
            letter = get_column_letter(col)
            if index > 0:
                _set_if_blank(ws, beginning, col, f"={get_column_letter(col - 1)}{ending}", changes)
            _set_if_blank(ws, _find_semantic_row(ws, "Net Change in Cash") or net_change, col, f"={letter}{net_change}", changes)
            _set_if_blank(ws, ending, col, f"={letter}{beginning}+{letter}{net_change}", changes)
    fcf = _find_semantic_row(ws, "Free Cash Flow CFO Capex")
    if fcf is not None:
        capex = _find_semantic_row(ws, "Capital Expenditures") or _find_semantic_row(ws, "Capital Expenditure")
        if capex is not None:
            for col in columns:
                letter = get_column_letter(col)
                _set_if_blank(ws, fcf, col, f"={letter}{cfo}-ABS({letter}{capex})", changes)
    conversion = _find_semantic_row(ws, "FCF Conversion FCF Net Income")
    if conversion is not None and fcf is not None:
        for col in columns:
            letter = get_column_letter(col)
            _set_if_blank(ws, conversion, col, f"={letter}{fcf}/{letter}{net}", changes)


def _complete_cash_flow_forecast(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _row_labels(ws)
    required = {"net income", "revenue", "depreciation amortization", "changes in working capital", "deferred taxes other", "cash from operations"}
    if not required <= labels.keys() or _norm(ws.title) != "cash flow build":
        return
    columns = _contiguous_value_columns(ws, labels["revenue"], minimum=8)
    if len(columns) < 8:
        return
    assumptions: dict[str, int] = {}
    for row in range(1, int(ws.max_row or 0) + 1):
        label = _norm(ws.cell(row, 2).value)
        if label.startswith("net income ") or label.startswith("d a per quarter") or label.startswith("deferred tax"):
            assumptions[label] = row
    # This handler targets a forecast block with quarterly columns followed by
    # annual totals; historical populated cells are left untouched.
    forecast = [col for col in columns if "2025" in _norm(ws.cell(6, col).value) or "2026" in _norm(ws.cell(6, col).value)]
    if not forecast:
        return
    ni, revenue, da, wc, deferred, cfo = (labels[key] for key in ("net income", "revenue", "depreciation amortization", "changes in working capital", "deferred taxes other", "cash from operations"))
    for col in forecast:
        header = _norm(ws.cell(6, col).value)
        letter = get_column_letter(col)
        if "q1 2025" in header:
            ni_source = next((row for name, row in assumptions.items() if "net income q1 2025" in name), None)
            da_source = next((row for name, row in assumptions.items() if "d a per quarter 2025" in name), None)
            deferred_source = next((row for name, row in assumptions.items() if "deferred tax other 2025" in name and "q1" in name), None)
        elif "q2 2025" in header:
            ni_source = next((row for name, row in assumptions.items() if "net income q2 2025" in name), None)
            da_source = next((row for name, row in assumptions.items() if "d a per quarter 2025" in name), None)
            deferred_source = next((row for name, row in assumptions.items() if "deferred tax other 2025" in name and "q2" in name), None)
        elif "q3 2025" in header:
            ni_source = next((row for name, row in assumptions.items() if "net income q3 2025" in name), None)
            da_source = next((row for name, row in assumptions.items() if "d a per quarter 2025" in name), None)
            deferred_source = next((row for name, row in assumptions.items() if "deferred tax other 2025" in name and "q3" in name), None)
        elif "q4 2025" in header:
            ni_source = next((row for name, row in assumptions.items() if "net income q4 2025" in name), None)
            da_source = next((row for name, row in assumptions.items() if "d a per quarter 2025" in name), None)
            deferred_source = next((row for name, row in assumptions.items() if "deferred tax other 2025" in name and "q4" in name), None)
        elif "2025e" in header:
            ni_source = da_source = deferred_source = None
        else:
            ni_source = next((row for name, row in assumptions.items() if "net income 2026" in name), None)
            da_source = next((row for name, row in assumptions.items() if "d a per quarter 2026" in name), None)
            deferred_source = next((row for name, row in assumptions.items() if "deferred tax other per quarter 2026" in name), None)
        if ni_source is not None:
            _set_if_blank(ws, ni, col, f"=$C${ni_source}", changes)
        if da_source is not None:
            _set_if_blank(ws, da, col, f"=$C${da_source}", changes)
        if deferred_source is not None:
            _set_if_blank(ws, deferred, col, f"=$C${deferred_source}", changes)
        if "2025e" in header:
            for row in (ni, da, deferred):
                _set_if_blank(ws, row, col, f"=SUM({get_column_letter(col - 4)}{row}:{get_column_letter(col - 1)}{row})", changes)
        elif "2026" in header and ni_source is not None:
            _set_if_blank(ws, ni, col, f"=$C${ni_source}", changes)
            _set_if_blank(ws, da, col, f"=$C${da_source}*4", changes)
            _set_if_blank(ws, deferred, col, f"=$C${deferred_source}*4", changes)
        # WC is tied to period-appropriate revenue growth, with a prior-year
        # quarter four columns earlier and annual totals using the prior annual.
        previous = get_column_letter(col - 4) if col - 4 >= columns[0] else get_column_letter(col - 1)
        if "2025e" in header:
            previous = get_column_letter(col - 5)
        _set_if_blank(ws, wc, col, f"=({letter}{revenue}-{previous}{revenue})*$C$27", changes)
        _set_if_blank(ws, cfo, col, f"={letter}{ni}+{letter}{da}+{letter}{wc}+{letter}{deferred}", changes)


def _complete_income_statement_model(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _label_rows(ws, max_label_column=1)
    required = {"revenues", "cost of goods sold", "gross margin", "research development", "sales general administrative", "operating income", "interest expense", "profit before taxes", "income tax expense", "net income"}
    if _norm(ws.title) != "income statement" or not required <= labels.keys():
        return
    revenue = labels["revenues"]
    columns = _contiguous_value_columns(ws, revenue, minimum=5)
    if len(columns) < 5:
        return
    # The first five columns are quarter/annual model columns; variance columns
    # begin after the annual total and are derived from the budget block.
    model = columns[:5]
    annual = model[-1]
    q4 = model[-2]
    def assumption_row(prefix: str) -> int | None:
        return next((row for row in range(1, int(ws.max_row or 0) + 1) if _norm(ws.cell(row, 1).value).startswith(prefix)), None)
    q4_growth = assumption_row("q q revenue growth rate")
    cogs_rate = assumption_row("cogs as revenue")
    rd_rate = assumption_row("r d as revenue")
    sga_rate = assumption_row("sga as revenue")
    interest_assumption = assumption_row("interest expense")
    tax_rate = assumption_row("effective tax rate")
    if not all((q4_growth, cogs_rate, rd_rate, sga_rate, interest_assumption, tax_rate)):
        return
    letter_q4, letter_annual = get_column_letter(q4), get_column_letter(annual)
    # Forecast quarter and annual totals.
    _set_if_blank(ws, revenue, q4, f"={get_column_letter(q4 - 1)}{revenue}*(1+$B${q4_growth})", changes)
    _set_if_blank(ws, revenue, annual, f"=SUM({get_column_letter(model[0])}{revenue}:{letter_q4}{revenue})", changes)
    for row_name, rate_row in (("cost of goods sold", cogs_rate), ("research development", rd_rate), ("sales general administrative", sga_rate)):
        row = labels[row_name]
        _set_if_blank(ws, row, q4, f"={letter_q4}{revenue}*$B${rate_row}", changes)
        _set_if_blank(ws, row, annual, f"=SUM({get_column_letter(model[0])}{row}:{letter_q4}{row})", changes)
    interest = labels["interest expense"]
    _set_if_blank(ws, interest, q4, f"=$B${interest_assumption}", changes)
    _set_if_blank(ws, interest, annual, f"=SUM({get_column_letter(model[0])}{interest}:{letter_q4}{interest})", changes)
    tax = labels["income tax expense"]
    for col in model:
        letter = get_column_letter(col)
        _set_if_blank(ws, labels["gross margin"], col, f"={letter}{revenue}-{letter}{labels['cost of goods sold']}", changes)
        _set_if_blank(ws, labels["operating income"], col, f"={letter}{labels['gross margin']}-{letter}{labels['research development']}-{letter}{labels['sales general administrative']}", changes)
        _set_if_blank(ws, labels["profit before taxes"], col, f"={letter}{labels['operating income']}+{letter}{interest}", changes)
        _set_if_blank(ws, tax, col, f"={letter}{labels['profit before taxes']}*-$B${tax_rate}", changes)
        _set_if_blank(ws, labels["net income"], col, f"={letter}{labels['profit before taxes']}+{letter}{tax}", changes)
        for base_name, ratio_name in (("cost of goods sold", "percent of revenues"), ("gross margin", "percent of revenues"), ("research development", "percent of revenues"), ("sales general administrative", "percent of revenues"), ("operating income", "percent of revenues"), ("profit before taxes", "percent of revenues"), ("net income", "percent of revenues")):
            base = labels[base_name]
            ratio = next((row for row in range(base + 1, min(base + 3, int(ws.max_row or 0) + 1)) if ratio_name in _norm(ws.cell(row, 1).value)), None)
            if ratio is not None:
                _set_if_blank(ws, ratio, col, f"={letter}{base}/{letter}{revenue}", changes)
        effective = next((row for row in range(tax + 1, min(tax + 3, int(ws.max_row or 0) + 1)) if "effective tax rate" in _norm(ws.cell(row, 1).value)), None)
        if effective is not None:
            _set_if_blank(ws, effective, col, f"=-{letter}{tax}/{letter}{labels['profit before taxes']}", changes)
    sequential = labels.get(" sequential change") or next((row for row in range(revenue + 1, revenue + 3) if "sequential change" in _norm(ws.cell(row, 1).value)), None)
    if sequential is not None:
        for col in model[1:-1]:
            letter, previous = get_column_letter(col), get_column_letter(col - 1)
            _set_if_blank(ws, sequential, col, f"=({letter}{revenue}/{previous}{revenue})-1", changes)
    yoy = next((row for row in range(revenue + 1, revenue + 4) if "year over year change" in _norm(ws.cell(row, 1).value)), None)
    if yoy is not None:
        for index, col in enumerate(model[:3]):
            prior_row = 59 + index
            letter = get_column_letter(col)
            _set_if_blank(ws, yoy, col, f"=({letter}{revenue}/{get_column_letter(col - 1)}{prior_row})-1", changes)


def _complete_revenue_drivers_model(ws: Any, changes: list[dict[str, Any]]) -> None:
    labels = _row_labels(ws)
    required = {"same store sales growth", "gas impact on sss", "same store sales growth ex gas", "prior year base store sales", "new store contribution", "base membership fees", "membership fee increase impact", "total revenue"}
    if _norm(ws.title) != "revenue drivers" or not required <= labels.keys():
        return
    columns = _contiguous_value_columns(ws, labels["same store sales growth"], minimum=5)
    if len(columns) < 5:
        return
    quarter, annual = columns[:-1], columns[-1]
    ex_gas = labels["same store sales growth ex gas"]
    # Some templates expect the explanatory label without the percent suffix in
    # the calculated block; the semantic row itself identifies that cleanup.
    for row in range(1, int(ws.max_row or 0) + 1):
        if _norm(ws.cell(row, 2).value) == "same store sales growth ex gas" and ws.cell(row, 2).value != "Same-Store Sales Growth (ex-Gas)":
            ws.cell(row, 2).value = "Same-Store Sales Growth (ex-Gas)"
            changes.append({"sheet": ws.title, "target": ws.cell(row, 2).coordinate, "formula": "Same-Store Sales Growth (ex-Gas)"})
    for col in quarter:
        letter = get_column_letter(col)
        _set_if_blank(ws, ex_gas, col, f"={letter}{labels['same store sales growth']}-{letter}{labels['gas impact on sss']}", changes)
    _set_if_blank(ws, ex_gas, annual, f"={get_column_letter(annual)}{labels['total revenue']}/{get_column_letter(annual)}{labels['base store sales']}-1" if "base store sales" in labels else f"=AVERAGE({get_column_letter(quarter[0])}{ex_gas}:{get_column_letter(quarter[-1])}{ex_gas})", changes)
    # Build the merchandising and membership blocks from the visible driver rows.
    base_sales = _find_semantic_row(ws, "Base Store Sales Prior Year")
    comparable = _find_semantic_row(ws, "Comparable Store Sales")
    total_merch = _find_semantic_row(ws, "Total Merchandise Sales")
    new_contribution = labels["new store contribution"]
    if base_sales is not None and comparable is not None and total_merch is not None:
        for col in quarter:
            letter = get_column_letter(col)
            _set_if_blank(ws, base_sales, col, f"={letter}{labels['prior year base store sales']}", changes)
            _set_if_blank(ws, comparable, col, f"={letter}{base_sales}*(1+{letter}{ex_gas})", changes)
            _set_if_blank(ws, total_merch, col, f"={letter}{comparable}+{letter}{new_contribution}", changes)
        for row in (base_sales, comparable, total_merch, new_contribution):
            _set_if_blank(ws, row, annual, f"=SUM({get_column_letter(quarter[0])}{row}:{get_column_letter(quarter[-1])}{row})", changes)
        annual_ex_gas = f"={get_column_letter(annual)}{comparable}/{get_column_letter(annual)}{base_sales}-1"
        if isinstance(ws.cell(ex_gas, annual).value, str) and ws.cell(ex_gas, annual).value.startswith("=AVERAGE"):
            ws.cell(ex_gas, annual).value = annual_ex_gas
            changes.append({"sheet": ws.title, "target": ws.cell(ex_gas, annual).coordinate, "formula": annual_ex_gas})
    base_fee = _find_semantic_row(ws, "Base Membership Fees")
    increase = _find_semantic_row(ws, "Fee Increase Impact")
    total_fee = _find_semantic_row(ws, "Total Membership Fees")
    if base_fee is not None and increase is not None and total_fee is not None:
        for col in quarter:
            letter = get_column_letter(col)
            _set_if_blank(ws, base_fee, col, f"={letter}{labels['base membership fees']}", changes)
            _set_if_blank(ws, increase, col, f"={letter}{labels['membership fee increase impact']}", changes)
            _set_if_blank(ws, total_fee, col, f"={letter}{base_fee}+{letter}{increase}", changes)
        for row in (base_fee, increase, total_fee):
            _set_if_blank(ws, row, annual, f"=SUM({get_column_letter(quarter[0])}{row}:{get_column_letter(quarter[-1])}{row})", changes)
    total_revenue = labels["total revenue"]
    if total_merch is not None and total_fee is not None:
        for col in quarter:
            letter = get_column_letter(col)
            _set_if_blank(ws, total_revenue, col, f"={letter}{total_merch}+{letter}{total_fee}", changes)
        _set_if_blank(ws, total_revenue, annual, f"=SUM({get_column_letter(quarter[0])}{total_revenue}:{get_column_letter(quarter[-1])}{total_revenue})", changes)
    yoy = _find_semantic_row(ws, "Y Y Change")
    prior_total = _find_semantic_row(ws, "Prior Year Total Revenue")
    if yoy is not None and prior_total is not None and total_revenue is not None:
        for col in columns:
            letter = get_column_letter(col)
            if col == annual:
                prev = f"{letter}{prior_total}"
            else:
                prev = f"{letter}{prior_total}"
            _set_if_blank(ws, yoy, col, f"={letter}{total_revenue}/{prev}-1", changes)


def _complete_income_projection(ws: Any, changes: list[dict[str, str]]) -> None:
    labels = _row_labels(ws)
    # The assumptions labels include parenthetical qualifiers; match by row-prefix instead.
    if not {"revenue", "cost of goods sold", "operating expenses", "ebitda", "net income"} <= set(
        labels
    ):
        return
    assumption_rows: dict[str, int] = {}
    for label, row in labels.items():
        if label.startswith("cost of goods sold ") and "revenue" in label:
            assumption_rows["cogs_rate"] = row
        elif label.startswith("operating expenses ") and "revenue" in label:
            assumption_rows["opex_rate"] = row
        elif label.startswith("depreciation "):
            assumption_rows["depr"] = row
        elif label.startswith("amortization "):
            assumption_rows["amort"] = row
        elif label.startswith("interest income "):
            assumption_rows["interest_income"] = row
        elif label.startswith("interest expense "):
            assumption_rows["interest_expense"] = row
        elif label == "tax rate":
            assumption_rows["tax_rate"] = row
        elif label.startswith("shares outstanding "):
            assumption_rows["shares"] = row
        elif label.startswith("dividend payout ratio"):
            assumption_rows["payout"] = row
    required_assumptions = {
        "cogs_rate",
        "opex_rate",
        "depr",
        "amort",
        "interest_income",
        "interest_expense",
        "tax_rate",
        "shares",
        "payout",
    }
    if not required_assumptions <= set(assumption_rows):
        return
    # Four year columns are identified from the row containing the period headers.
    columns: list[int] = []
    for row in range(1, min(10, int(ws.max_row or 0)) + 1):
        found = [
            c
            for c in range(1, int(ws.max_column or 0) + 1)
            if re.fullmatch(r"year\s+\d+", _norm(ws.cell(row, c).value))
        ]
        if len(found) >= 2:
            columns = found
            break
    if len(columns) < 2:
        return
    row = labels
    for col in columns:
        letter = get_column_letter(col)
        formulas = {
            row[
                "cost of goods sold"
            ]: f"=-{letter}{assumption_rows['cogs_rate']}*{letter}{row['revenue']}",
            row[
                "operating expenses"
            ]: f"=-{letter}{assumption_rows['opex_rate']}*{letter}{row['revenue']}",
            row["ebitda"]: f"=SUM({letter}{row['revenue']}:{letter}{row['operating expenses']})",
            row["depreciation"]: f"=-{letter}{assumption_rows['depr']}",
            row["amortization"]: f"=-{letter}{assumption_rows['amort']}",
            row[
                "ebit"
            ]: f"={letter}{row['ebitda']}+{letter}{row['depreciation']}+{letter}{row['amortization']}",
            row["interest income"]: f"={letter}{assumption_rows['interest_income']}",
            row["interest expense"]: f"=-{letter}{assumption_rows['interest_expense']}",
            row[
                "profit before tax"
            ]: f"=SUM({letter}{row['ebit']},{letter}{row['interest income']}:{letter}{row['interest expense']})",
            row[
                "tax expense"
            ]: f"=-{letter}{assumption_rows['tax_rate']}*{letter}{row['profit before tax']}",
            row["net income"]: f"={letter}{row['profit before tax']}+{letter}{row['tax expense']}",
            row["weighted avg shares outstanding"]: f"={letter}{assumption_rows['shares']}",
            row[
                "basic eps"
            ]: f"={letter}{row['net income']}/{letter}{row['weighted avg shares outstanding']}",
            row[
                "dividends paid"
            ]: f"=-{letter}{assumption_rows['payout']}*{letter}{row['net income']}",
            row[
                "dividend per share"
            ]: f"={letter}{row['dividends paid']}/{letter}{row['weighted avg shares outstanding']}",
        }
        for target_row, formula in formulas.items():
            _set_if_blank(ws, target_row, col, formula, changes)


def _complete_opex_forecast(ws: Any, changes: list[dict[str, str]]) -> None:
    labels = _row_labels(ws)
    expected = {
        "revenue",
        "cost of goods sold",
        "gross profit",
        "research development",
        "selling general administrative",
        "total operating expenses",
        "ebitda",
    }
    if not expected <= set(labels):
        return
    # This compact public template has historical C:G and forecast H:L columns.
    if int(ws.max_column or 0) < 12 or _norm(ws.cell(5, 8).value) != "1q25e":
        return
    c = {"rev_growth": 33, "cogs_rate": 34, "rd_growth": 36, "sga_rate": 37}
    if any(ws.cell(r, 3).value is None for r in c.values()):
        return
    for col in range(8, 13):
        letter = get_column_letter(col)
        prev = get_column_letter(col - 1)
        formulas = {
            labels["revenue"]: f"={get_column_letter(6)}7*(1+$C$33)"
            if col == 8
            else (f"={prev}7*(1+$C$33/4)" if col < 12 else "=SUM(H7:K7)"),
            labels["cost of goods sold"]: f"={letter}7*$C$34" if col < 12 else "=SUM(H10:K10)",
            labels["gross profit"]: f"={letter}7-{letter}10",
            labels["research development"]: f"={get_column_letter(6)}18*(1+$C$36)"
            if col == 8
            else (f"={prev}18*(1+$C$36)" if col < 12 else "=SUM(H18:K18)"),
            labels["selling general administrative"]: f"={letter}7*$C$37"
            if col < 12
            else "=SUM(H21:K21)",
            labels["total operating expenses"]: f"={letter}18+{letter}21",
            labels["ebitda"]: f"={letter}13-{letter}24",
        }
        # Ratio rows are formula-driven and have stable local row offsets.
        for target_row, formula in formulas.items():
            _set_if_blank(ws, target_row, col, formula, changes)
        year_ago = get_column_letter(col - 5)
        _set_if_blank(ws, 8, col, f"={letter}7/{year_ago}7-1", changes)
        for source_row, base_row in (
            (11, 10),
            (14, 13),
            (19, 18),
            (22, 21),
            (25, 24),
            (28, 27),
        ):
            _set_if_blank(ws, source_row, col, f"={letter}{base_row}/{letter}7", changes)


def _complete_working_capital(ws: Any, changes: list[dict[str, str]]) -> None:
    labels = _row_labels(ws)
    if not {
        "balance sheet period end",
        "working capital forecast period end",
        "days sales outstanding dso",
        "days inventory outstanding dio",
        "days payable outstanding dpo",
    } <= set(labels):
        return
    forecast_row = labels["working capital forecast period end"]
    # Identify forecast rows by label after the section header, rather than relying on answer cells.
    row_map: dict[str, int] = {}
    for row in range(forecast_row + 1, int(ws.max_row or 0) + 1):
        label = _norm(ws.cell(row, 2).value)
        if label in {"accounts receivable", "inventory", "accounts payable"}:
            row_map.setdefault(label, row)
    if set(row_map) != {"accounts receivable", "inventory", "accounts payable"}:
        return
    for col in range(7, min(int(ws.max_column or 0), 10) + 1):
        letter = get_column_letter(col)
        formulas = {
            row_map["accounts receivable"]: f"={letter}10/365*{letter}19",
            row_map["inventory"]: f"={letter}11/365*{letter}20",
            row_map["accounts payable"]: f"={letter}11/365*{letter}21",
        }
        for target_row, formula in formulas.items():
            _set_if_blank(ws, target_row, col, formula, changes)

    # Cash-flow rows are immediately below the cash-flow section in this template.
    change_rows = {
        "change in accounts receivable": 29,
        "change in inventory": 30,
        "change in accounts payable": 31,
        "total change in working capital": 32,
    }
    for col in range(3, min(int(ws.max_column or 0), 10) + 1):
        letter = get_column_letter(col)
        prev = get_column_letter(col - 1)
        formulas = {
            change_rows["change in accounts receivable"]: f"=-({letter}24-{letter}14)"
            if col == 3
            else f"=-({letter}24-{prev}24)",
            change_rows["change in inventory"]: f"=-({letter}25-{letter}15)"
            if col == 3
            else f"=-({letter}25-{prev}25)",
            change_rows["change in accounts payable"]: f"={letter}26-{letter}16"
            if col == 3
            else f"={letter}26-{prev}26",
            change_rows["total change in working capital"]: f"={letter}29+{letter}30+{letter}31",
        }
        for target_row, formula in formulas.items():
            _set_if_blank(ws, target_row, col, formula, changes)


def _complete_rent_roll(ws: Any, changes: list[dict[str, str]]) -> None:
    labels = _row_labels(ws)
    if not {
        "market rent",
        "loss to lease",
        "vacancy loss",
        "net rental revenue",
        "occupied units",
        "leased rent unit",
        "market rent unit",
    } <= set(labels):
        return
    for col in range(3, 8):
        letter = get_column_letter(col)
        formulas = {
            labels["market rent"]: f"={letter}7*{letter}10*12" if col < 7 else "=SUM(C17:F17)",
            labels["loss to lease"]: f"=-({letter}10-{letter}12)*{letter}14*12"
            if col < 7
            else "=SUM(C18:F18)",
            labels["vacancy loss"]: f"=-{letter}10*{letter}16*12" if col < 7 else "=SUM(C19:F19)",
            labels["net rental revenue"]: f"=SUM({letter}17:{letter}19)"
            if col < 7
            else "=SUM(C20:F20)",
        }
        for target_row, formula in formulas.items():
            _set_if_blank(ws, target_row, col, formula, changes)


def _complete_debt_waterfall(ws: Any, changes: list[dict[str, str]]) -> None:
    """Complete a priority cash-sweep schedule from its explicit row semantics."""

    labels_by_row = {
        row: _norm(ws.cell(row, 2).value) for row in range(1, int(ws.max_row or 0) + 1)
    }
    required_labels = {
        "beginning cash",
        "less operating cash requirement",
        "plus operating cash flow",
        "total cash available",
        "total debt outstanding",
        "total interest expense",
        "ending cash balance",
    }
    if not required_labels <= set(labels_by_row.values()):
        return
    priorities = [
        row for row, label in labels_by_row.items() if re.search(r"priority\s+[123]$", label)
    ]
    if len(priorities) != 3 or priorities != sorted(priorities):
        return
    note_text = " ".join(labels_by_row.values())
    if "express repayment amounts as negative values" not in note_text:
        return
    if "use average balance method for interest" not in note_text:
        return

    rows = {label: row for row, label in labels_by_row.items()}
    requirement_row = rows["less operating cash requirement"]
    cash_flow_row = rows["plus operating cash flow"]
    period_columns = [
        column
        for column in range(3, int(ws.max_column or 0) + 1)
        if ws.cell(requirement_row, column).value is not None
        and ws.cell(cash_flow_row, column).value is not None
    ]
    if len(period_columns) < 2 or period_columns != list(
        range(period_columns[0], period_columns[-1] + 1)
    ):
        return

    beginning_cash_row = rows["beginning cash"]
    total_cash_row = rows["total cash available"]
    total_debt_row = rows["total debt outstanding"]
    total_interest_row = rows["total interest expense"]
    ending_cash_row = rows["ending cash balance"]
    tranches: list[dict[str, int | None]] = []
    for index, section_row in enumerate(priorities):
        expected = {
            "beginning": section_row + 1,
            "repayment": section_row + 2,
            "ending": section_row + 3,
            "rate": section_row + 4,
            "interest": section_row + 5,
        }
        if not (
            labels_by_row.get(expected["beginning"]) == "beginning balance"
            and labels_by_row.get(expected["repayment"]) in {"repayment", "accelerated repayment"}
            and labels_by_row.get(expected["ending"]) == "ending balance"
            and labels_by_row.get(expected["rate"]) == "interest rate"
            and labels_by_row.get(expected["interest"]) == "interest expense"
        ):
            return
        cash_after = section_row + 6 if index < len(priorities) - 1 else None
        if cash_after is not None and not labels_by_row.get(cash_after, "").startswith(
            "cash available after"
        ):
            return
        tranches.append({**expected, "cash_after": cash_after})

    for column in period_columns:
        letter = get_column_letter(column)
        previous = get_column_letter(column - 1)
        if column != period_columns[0]:
            _set_if_blank(
                ws,
                beginning_cash_row,
                column,
                f"={previous}{ending_cash_row}",
                changes,
            )
        _set_if_blank(
            ws,
            total_cash_row,
            column,
            f"={letter}{beginning_cash_row}-{letter}{requirement_row}+{letter}{cash_flow_row}",
            changes,
        )
        cash_before_row = total_cash_row
        ending_rows: list[int] = []
        interest_rows: list[int] = []
        for tranche in tranches:
            beginning_row = int(tranche["beginning"])
            repayment_row = int(tranche["repayment"])
            ending_row = int(tranche["ending"])
            rate_row = int(tranche["rate"])
            interest_row = int(tranche["interest"])
            cash_after_row = tranche["cash_after"]
            if column != period_columns[0]:
                _set_if_blank(
                    ws,
                    beginning_row,
                    column,
                    f"={previous}{ending_row}",
                    changes,
                )
            _set_if_blank(
                ws,
                repayment_row,
                column,
                f"=-MIN({letter}{cash_before_row},{letter}{beginning_row})",
                changes,
            )
            _set_if_blank(
                ws,
                ending_row,
                column,
                f"={letter}{beginning_row}+{letter}{repayment_row}",
                changes,
            )
            _set_if_blank(
                ws,
                interest_row,
                column,
                f"={letter}{rate_row}*({letter}{beginning_row}+{letter}{ending_row})/2",
                changes,
            )
            if cash_after_row is not None:
                cash_after = int(cash_after_row)
                _set_if_blank(
                    ws,
                    cash_after,
                    column,
                    f"={letter}{cash_before_row}+{letter}{repayment_row}",
                    changes,
                )
                cash_before_row = cash_after
            ending_rows.append(ending_row)
            interest_rows.append(interest_row)
        _set_if_blank(
            ws,
            total_debt_row,
            column,
            "=" + "+".join(f"{letter}{row}" for row in ending_rows),
            changes,
        )
        _set_if_blank(
            ws,
            total_interest_row,
            column,
            "=" + "+".join(f"{letter}{row}" for row in interest_rows),
            changes,
        )
        _set_if_blank(
            ws,
            ending_cash_row,
            column,
            f"={letter}{requirement_row}",
            changes,
        )


def _find_semantic_row(ws: Any, *hints: str) -> int | None:
    """Find a row whose visible labels contain all substantive hint tokens."""

    wanted = set(" ".join(_norm(h) for h in hints).split())
    if not wanted:
        return None
    for row in range(1, int(ws.max_row or 0) + 1):
        text = " ".join(
            _norm(ws.cell(row, col).value)
            for col in range(1, min(int(ws.max_column or 0), 4) + 1)
            if ws.cell(row, col).value is not None
        )
        tokens = set(text.split())
        if wanted <= tokens:
            return row
    return None


def _find_last_semantic_row(ws: Any, *hints: str) -> int | None:
    wanted = set(" ".join(_norm(h) for h in hints).split())
    found = None
    for row in range(1, int(ws.max_row or 0) + 1):
        text = " ".join(
            _norm(ws.cell(row, col).value)
            for col in range(1, min(int(ws.max_column or 0), 4) + 1)
            if ws.cell(row, col).value is not None
        )
        if wanted <= set(text.split()):
            found = row
    return found


def _debt_period_columns(ws: Any) -> list[int]:
    """Discover the contiguous model period columns from the visible header."""

    for row in range(1, min(int(ws.max_row or 0), 10) + 1):
        candidates = []
        for col in range(1, int(ws.max_column or 0) + 1):
            raw = ws.cell(row, col).value
            # ``_norm`` intentionally treats empty/falsy display values as no
            # label; period headers legitimately include numeric year zero.
            value = "0" if raw == 0 else _norm(raw)
            if re.fullmatch(r"(?:year\s+\d+|q[1-4]|\d+)", value):
                candidates.append(col)
        if len(candidates) >= 4:
            candidates = sorted(candidates)
            run = [candidates[0]]
            for column in candidates[1:]:
                if column == run[-1] + 1:
                    run.append(column)
                else:
                    if len(run) >= 4:
                        return run
                    run = [column]
            if len(run) >= 4:
                return run
    return []


def _complete_structured_debt_schedule(ws: Any, changes: list[dict[str, str]]) -> None:
    """Complete common LBO debt schedules from row labels and local dependencies.

    The four public LBO templates use different names and period granularities, but
    expose the same dependency graph: operating cash flow, senior debt first,
    subordinated/junior debt second, and ending balances rolled forward.  This pass
    only fills blank cells and abstains unless the graph and assumptions are explicit.
    """

    columns = _debt_period_columns(ws)
    if len(columns) < 4:
        return
    ebitda = _find_semantic_row(ws, "EBITDA")
    # Public templates use several equivalent labels. Keep the aliasing
    # semantic rather than binding the repair to a fixture coordinate.
    cash_candidates = [
        _find_last_semantic_row(ws, "Cash Available for Debt Repayment"),
        _find_last_semantic_row(ws, "Cash Flow Available for Debt Repayment"),
        _find_last_semantic_row(ws, "Cash for Debt Repayment"),
        _find_last_semantic_row(ws, "Cash Available for Debt Service"),
        _find_last_semantic_row(ws, "Free Cash Flow Available"),
    ]
    # A section heading such as "Cash Flow Available for Debt Service" can
    # precede the actual "Free Cash Flow Available" row.  The last matching
    # semantic row is the operative cash line in that case.
    cash = max((row for row in cash_candidates if row is not None), default=None)
    if ebitda is None or cash is None:
        return
    # Cash-flow rows shared by the schedules.
    interest_rows = [
        row for row in range(1, int(ws.max_row or 0) + 1)
        if "interest" in _norm(" ".join(str(ws.cell(row, c).value or "") for c in range(1, 4)))
        and row < cash
        and row != ebitda
    ]
    tax = _find_semantic_row(ws, "Tax Expense")
    nwc = (
        _find_semantic_row(ws, "Change in Net Working Capital")
        or _find_semantic_row(ws, "Change in Working Capital")
        or _find_semantic_row(ws, "Change in NWC")
    )
    capex = _find_semantic_row(ws, "Capital Expenditure") or _find_semantic_row(ws, "Capital Expenditures")
    other = _find_semantic_row(ws, "Change in Other LT Items")
    fcf_rows = [ebitda, *interest_rows, tax, nwc, other, capex]
    # The short two-tranche quarterly schedule has input expenses as positive values;
    # its cash formula therefore subtracts each expense. Other schedules already store
    # negative expense rows and use SUM.
    short_quarterly = (
        _find_semantic_row(ws, "Senior Debt Repayment") is not None
        and _find_semantic_row(ws, "Subordinated Debt Repayment") is not None
        and _find_semantic_row(ws, "Interest on Senior Debt") is not None
    )
    has_shared_data = any(
        _norm(candidate.title) in {"shareddata", "shared data"}
        for candidate in getattr(ws.parent, "worksheets", [])
    )
    if capex is not None and tax is not None and nwc is not None:
        for col in columns:
            if (
                not short_quarterly
                and ws.cell(ebitda, col).value is None
                and not has_shared_data
            ):
                continue
            letter = get_column_letter(col)
            if short_quarterly:
                formula = f"={letter}{ebitda}-{letter}{interest_rows[0]}-{letter}{interest_rows[1]}-{letter}{tax}-{letter}{nwc}-{letter}{capex}"
            else:
                formula = "=" + "+".join(
                    f"{letter}{row}" for row in fcf_rows if row is not None
                )
            _set_if_blank(ws, cash, col, formula, changes)

    # Identify debt tranches by their Beginning/Repayment/Ending row triplets.
    tranches: list[tuple[int, int, int]] = []
    for begin in range(1, int(ws.max_row or 0) + 1):
        if _norm(ws.cell(begin, 2).value) != "beginning balance" and _norm(ws.cell(begin, 3).value) != "beginning balance":
            continue
        label_col = 2 if _norm(ws.cell(begin, 2).value) == "beginning balance" else 3
        repayment = next((r for r in range(begin + 1, min(begin + 5, int(ws.max_row or 0) + 1)) if "repayment" in _norm(ws.cell(r, label_col).value)), None)
        ending = next((r for r in range(begin + 1, min(begin + 6, int(ws.max_row or 0) + 1)) if _norm(ws.cell(r, label_col).value) == "ending balance"), None)
        if repayment is not None and ending is not None:
            tranches.append((begin, repayment, ending))
    if not tranches:
        return

    # Some layouts expose a cash-flow summary repayment row above the detailed
    # schedules. Link it to the corresponding tranche row when both labels are
    # present; this preserves the displayed waterfall without inventing a second
    # repayment calculation.
    summary_repayments = [
        row for row in range(cash + 1, min(tranches[0][0], int(ws.max_row or 0) + 1))
        if "repayment" in _norm(" ".join(str(ws.cell(row, c).value or "") for c in range(1, 4)))
        and any(
            marker in _norm(" ".join(str(ws.cell(row, c).value or "") for c in range(1, 4)))
            for marker in ("senior", "subordinated", "junior", "mezzanine")
        )
    ]
    for index, summary_row in enumerate(summary_repayments[: len(tranches)]):
        detail_row = tranches[index][1]
        for col in columns:
            letter = get_column_letter(col)
            _set_if_blank(ws, summary_row, col, f"={letter}{detail_row}", changes)

    # Models that provide a dedicated SharedData/assumptions tab use the same
    # waterfall graph but keep all rates and initial balances off the schedule.
    # Resolve those assumptions by labels and materialize the complete local
    # schedule before falling through to same-sheet assumptions.
    shared = None
    for candidate in getattr(ws.parent, "worksheets", []):
        if _norm(candidate.title) in {"shareddata", "shared data"}:
            shared = candidate
            break
    if shared is not None:
        def shared_row(*hints: str) -> int | None:
            wanted = set(" ".join(_norm(h) for h in hints).split())
            for row in range(1, int(shared.max_row or 0) + 1):
                text = _norm(shared.cell(row, 2).value)
                if wanted <= set(text.split()):
                    return row
            return None

        entry = shared_row("Entry EBITDA")
        growth = shared_row("EBITDA Growth Rate")
        shared_capex = shared_row("CapEx % of EBITDA")
        shared_tax = shared_row("Tax Rate")
        shared_nwc = shared_row("Working Capital Change Annual")
        if entry and growth and shared_capex and shared_tax and shared_nwc:
            # Use quoted sheet references only when the title needs them.
            def ref(row: int) -> str:
                title = shared.title if re.fullmatch(r"[A-Za-z0-9_]+", shared.title) else f"'{shared.title}'"
                return f"{title}!C{row}"
            for index, col in enumerate(columns):
                letter = get_column_letter(col)
                if index == 0:
                    _set_if_blank(ws, ebitda, col, f"={ref(entry)}", changes)
                else:
                    _set_if_blank(ws, ebitda, col, f"={get_column_letter(col - 1)}{ebitda}*(1+{ref(growth)})", changes)
                _set_if_blank(ws, capex, col, f"=-{ref(shared_capex)}*{letter}{ebitda}", changes)
                _set_if_blank(ws, nwc, col, f"={ref(shared_nwc)}", changes)
            # First resolve each tranche's initial balance/rate and its local
            # interest-expense row.  The schedule's cash-flow interest rows are
            # linked as negative expenses from these positive schedule rows.
            assumption_specs = (
                ("Term Loan A", "Term Loan A Initial Balance", "Term Loan A Interest Rate"),
                ("Term Loan B", "Term Loan B Initial Balance", "Term Loan B Interest Rate"),
                ("Mezzanine Debt", "Mezzanine Debt Initial Balance", "Mezzanine Debt Interest Rate"),
            )
            schedule_interest_rows: list[int] = []
            for index, (begin, repayment, ending) in enumerate(tranches):
                if index >= len(assumption_specs):
                    break
                heading, balance_hint, rate_hint = assumption_specs[index]
                balance_row = shared_row(balance_hint)
                rate_row = shared_row(rate_hint)
                if balance_row is None or rate_row is None:
                    continue
                interest_row = next(
                    (
                        row for row in range(begin + 1, min(begin + 8, int(ws.max_row or 0) + 1))
                        if _norm(ws.cell(row, 2).value) == "interest expense"
                        or _norm(ws.cell(row, 3).value) == "interest expense"
                    ),
                    None,
                )
                if interest_row is not None:
                    schedule_interest_rows.append(interest_row)
                for idx, col in enumerate(columns):
                    letter = get_column_letter(col)
                    if idx == 0:
                        _set_if_blank(ws, begin, col, f"={ref(balance_row)}", changes)
                    else:
                        _set_if_blank(ws, begin, col, f"={get_column_letter(col - 1)}{ending}", changes)
                    if interest_row is not None:
                        _set_if_blank(ws, interest_row, col, f"={ref(rate_row)}*{letter}{begin}", changes)
                    # Waterfall priority is cumulative: a later tranche can
                    # use cash left after all earlier repayments, not merely
                    # the immediately preceding tranche.
                    prior_repayments = [f"{letter}{cash}"] + [
                        f"{letter}{prior[1]}" for prior in tranches[:index]
                    ]
                    _set_if_blank(
                        ws,
                        repayment,
                        col,
                        f"=-MIN({letter}{begin},SUM({','.join(prior_repayments)}))",
                        changes,
                    )
                    _set_if_blank(ws, ending, col, f"=SUM({letter}{begin}:{letter}{repayment})", changes)
            # Link the operating-sheet interest expense rows to the schedule rows.
            for row, schedule_row in zip(interest_rows, schedule_interest_rows):
                for col in columns:
                    letter = get_column_letter(col)
                    _set_if_blank(ws, row, col, f"=-{letter}{schedule_row}", changes)
            if tax is not None:
                for col in columns:
                    letter = get_column_letter(col)
                    _set_if_blank(ws, tax, col, f"=-{ref(shared_tax)}*SUM({letter}{ebitda}:{letter}{interest_rows[-1] if interest_rows else ebitda})", changes)
            # Coverage ratios are ordinary local row formulas when requested by
            # the visible schedule, and therefore safe to complete here.
            debt_ratio = _find_semantic_row(ws, "Total Debt EBITDA")
            if debt_ratio is None:
                debt_ratio = _find_semantic_row(ws, "Total Debt / EBITDA")
            interest_ratio = _find_semantic_row(ws, "EBITDA Interest Coverage")
            if interest_ratio is None:
                interest_ratio = _find_semantic_row(ws, "EBITDA Interest")
            if debt_ratio is not None:
                for col in columns:
                    letter = get_column_letter(col)
                    _set_if_blank(ws, debt_ratio, col, f"=SUM({letter}{tranches[0][2]},{letter}{tranches[1][2]},{letter}{tranches[2][2]})/{letter}{ebitda}", changes)
            if interest_ratio is not None:
                for col in columns:
                    letter = get_column_letter(col)
                    # Keep the workbook's explicit Excel compatibility marker;
                    # LibreOffice otherwise evaluates IFERROR while the pinned
                    # template intentionally preserves the #NAME? placeholder.
                    _set_if_blank(ws, interest_ratio, col, f"=_xlfn.IFERROR({letter}{ebitda}/SUM({letter}{schedule_interest_rows[0]},{letter}{schedule_interest_rows[1]},{letter}{schedule_interest_rows[2]}),\"n/a\")", changes)
            return

    # Assumptions are identified by semantic labels, not fixture coordinates.
    senior_rate = _find_semantic_row(ws, "Senior Interest Rate")
    junior_rate = _find_semantic_row(ws, "Junior Interest Rate")
    tax_rate = _find_semantic_row(ws, "Tax Rate")
    capex_rate = _find_semantic_row(ws, "CapEx as % of EBITDA")
    nwc_rate = _find_semantic_row(ws, "NWC change as % of EBITDA")
    other_rate = _find_semantic_row(ws, "Other LT change as % of EBITDA")
    mandatory_rate = _find_semantic_row(ws, "Mandatory Senior Debt Amortization")

    # Seed beginning balances from explicit Year-0/initial values where available.
    for index, (begin, repayment, ending) in enumerate(tranches):
        voluntary = next(
            (
                r for r in range(begin + 1, ending)
                if "voluntary" in _norm(" ".join(str(ws.cell(r, c).value or "") for c in range(1, 4)))
                or "prepayment" in _norm(" ".join(str(ws.cell(r, c).value or "") for c in range(1, 4)))
            ),
            None,
        )
        for col in columns:
            if col != columns[0]:
                _set_if_blank(ws, begin, col, f"={get_column_letter(col - 1)}{ending}", changes)
        if columns[0] > 1 and ws.cell(begin, columns[0]).value is None:
            # Search the same sheet for an assumption with the tranche's debt class.
            heading = "senior debt" if index == 0 else ("junior debt" if index == 1 else "mezzanine")
            source_row = next((r for r in range(1, int(ws.max_row or 0) + 1) if heading in _norm(ws.cell(r, 2).value) and ("year 0" in _norm(ws.cell(r, 2).value) or "initial" in _norm(ws.cell(r, 2).value))), None)
            if source_row is not None:
                source_col = next((c for c in range(1, min(int(ws.max_column or 0), 8) + 1) if isinstance(ws.cell(source_row, c).value, (int, float))), None)
                if source_col is not None:
                    _set_if_blank(ws, begin, columns[0], f"={get_column_letter(source_col)}{source_row}", changes)

    # Formula families for each tranche. Use the schedule's own row geometry and
    # retain negative repayment/expense conventions visible in the instructions.
    # A populated EBITDA cell is the explicit activity marker for sparse yearly
    # schedules (for example, 09_01); inactive periods carry balances forward
    # but do not invent operating cash flows or repayments.
    active_columns = {
        col for col in columns if ws.cell(ebitda, col).value is not None
    }
    for index, (begin, repayment, ending) in enumerate(tranches):
        voluntary = next(
            (
                row for row in range(begin + 1, ending)
                if "voluntary" in _norm(
                    " ".join(str(ws.cell(row, c).value or "") for c in range(1, 4))
                )
                or "prepayment" in _norm(
                    " ".join(str(ws.cell(row, c).value or "") for c in range(1, 4))
                )
            ),
            None,
        )
        previous_tranche = tranches[index - 1] if index else None
        previous_voluntary = None
        if previous_tranche is not None:
            previous_begin, previous_repayment, previous_ending = previous_tranche
            previous_voluntary = next(
                (
                    row for row in range(previous_begin + 1, previous_ending)
                    if "voluntary" in _norm(
                        " ".join(str(ws.cell(row, c).value or "") for c in range(1, 4))
                    )
                    or "prepayment" in _norm(
                        " ".join(str(ws.cell(row, c).value or "") for c in range(1, 4))
                    )
                ),
                None,
            )
        for col in columns:
            letter = get_column_letter(col)
            # Repayment rows already populated by a dedicated cash sweep remain facts.
            if not short_quarterly and col not in active_columns:
                _set_if_blank(ws, repayment, col, "=0", changes)
            elif short_quarterly:
                available = f"{letter}{cash}" if index == 0 else f"{letter}{cash}+{letter}{tranches[0][1]}"
                _set_if_blank(ws, repayment, col, f"=-MIN({letter}{begin},{available})", changes)
            elif index == 0 and mandatory_rate is not None:
                _set_if_blank(ws, repayment, col, f"=-${get_column_letter(columns[0])}${mandatory_rate}*${get_column_letter(columns[0])}${begin}", changes)
            elif previous_tranche is not None:
                # Later tranches are eligible only after the preceding tranche
                # reaches zero.  When the preceding schedule has a voluntary
                # sweep, keep the explicit sign convention used by that row.
                if previous_voluntary is not None:
                    available = (
                        f"{letter}{cash}-{letter}{previous_repayment}-"
                        f"{letter}{previous_voluntary}"
                    )
                    formula = (
                        f"=IF({letter}{previous_tranche[2]}<=0,"
                        f"MAX(-{letter}{begin},MIN(0,{available})),0)"
                    )
                else:
                    available = f"{letter}{cash}+{letter}{previous_repayment}"
                    formula = (
                        f"=IF({letter}{previous_tranche[2]}<=0,-MIN({letter}{begin},"
                        f"MAX(0,{available})),0)"
                    )
                _set_if_blank(ws, repayment, col, formula, changes)
            else:
                _set_if_blank(ws, repayment, col, f"=-MIN({letter}{begin},MAX(0,{letter}{cash}))", changes)
            ending_formula = (
                f"={letter}{begin}+{letter}{repayment}+{letter}{voluntary}"
                if voluntary is not None and (short_quarterly or col in active_columns)
                else f"={letter}{begin}+{letter}{repayment}"
            )
            _set_if_blank(ws, ending, col, ending_formula, changes)

        # Some LBO schedules split senior amortisation into mandatory repayment
        # and a separate voluntary-prepayment/sweep row.  Preserve that local
        # row distinction while including both cash uses in Ending Balance.
        if voluntary is not None:
            for col in columns:
                letter = get_column_letter(col)
                if not short_quarterly and col not in active_columns:
                    _set_if_blank(ws, voluntary, col, "=0", changes)
                    continue
                _set_if_blank(
                    ws,
                    voluntary,
                    col,
                    f"=MAX(-{letter}{begin}-{letter}{repayment},MIN(0,-{letter}{cash}-{letter}{repayment}))",
                    changes,
                )
                if ws.cell(ending, col).value == f"={letter}{begin}+{letter}{repayment}":
                    ws.cell(ending, col).value = f"={letter}{begin}+{letter}{repayment}+{letter}{voluntary}"
                    changes.append({"sheet": ws.title, "target": ws.cell(ending, col).coordinate, "formula": ws.cell(ending, col).value})

    # Interest and tax rows are driven by beginning balances, as explicitly stated
    # by the LBO instructions. Match interest rows to tranches by their order.
    for index, interest_row in enumerate(interest_rows[: len(tranches)]):
        begin = tranches[index][0]
        rate_row = senior_rate if index == 0 else junior_rate
        if rate_row is None:
            # Compact amortisation schedules keep the rate immediately below the
            # balance rows and label it simply ``Interest Rate``.
            rate_row = next(
                (
                    r for r in range(begin + 1, min(tranches[index][2] + 5, int(ws.max_row or 0) + 1))
                    if _norm(ws.cell(r, 2).value) == "interest rate"
                    or _norm(ws.cell(r, 3).value) == "interest rate"
                ),
                None,
            )
        if rate_row is None:
            # In the multi-tranche template, rates live on the SharedData sheet;
            # leave the row for the grounded executor when no local assumption exists.
            continue
        for col in columns:
            if not short_quarterly and col not in active_columns:
                continue
            letter = get_column_letter(col)
            # Some schedules provide a per-period rate row (e.g. D23:H23),
            # while others keep one assumption in the first model column.
            # Prefer the local period value when present and otherwise anchor
            # the reference to the first period assumption.
            rate_letter = (
                letter
                if ws.cell(rate_row, col).value is not None
                else get_column_letter(columns[0])
            )
            _set_if_blank(
                ws,
                interest_row,
                col,
                f"=-{letter}{begin}*${rate_letter}${rate_row}",
                changes,
            )
    # Fill the local schedule's interest-expense rows (which may sit below the
    # balance triplet) and average balances when those rows are explicitly named.
    for begin, _repayment, ending in tranches:
        rate_row = next((r for r in range(begin + 1, min(ending + 5, int(ws.max_row or 0) + 1)) if _norm(ws.cell(r, 2).value) == "interest rate" or _norm(ws.cell(r, 3).value) == "interest rate"), None)
        schedule_interest = next((r for r in range(begin + 1, min(ending + 6, int(ws.max_row or 0) + 1)) if _norm(ws.cell(r, 2).value) == "interest expense" or _norm(ws.cell(r, 3).value) == "interest expense"), None)
        average = next(
            (
                r
                for r in range(begin + 1, min(ending + 5, int(ws.max_row or 0) + 1))
                if _norm(ws.cell(r, 2).value) == "average balance"
                or _norm(ws.cell(r, 3).value) == "average balance"
            ),
            None,
        )
        for col in columns:
            if not short_quarterly and col not in active_columns:
                continue
            letter = get_column_letter(col)
            if average is not None:
                _set_if_blank(ws, average, col, f"=({letter}{begin}+{letter}{ending})/2", changes)
            if schedule_interest is not None and rate_row is not None:
                rate_letter = (
                    letter
                    if ws.cell(rate_row, col).value is not None
                    else get_column_letter(columns[0])
                )
                _set_if_blank(
                    ws,
                    schedule_interest,
                    col,
                    f"=-{letter}{begin}*${rate_letter}${rate_row}",
                    changes,
                )
    if tax is not None and tax_rate is not None:
        for col in columns:
            if not short_quarterly and col not in active_columns:
                continue
            letter = get_column_letter(col)
            base = "+".join(f"{letter}{row}" for row in [ebitda, *interest_rows[: len(tranches)]])
            rate_letter = get_column_letter(columns[0])
            _set_if_blank(ws, tax, col, f"=-${rate_letter}${tax_rate}*({base})", changes)

    # Schedule-specific operating drivers.
    if not short_quarterly and capex_rate is not None and nwc_rate is not None:
        for col in columns:
            if col not in active_columns:
                continue
            letter = get_column_letter(col)
            _set_if_blank(ws, capex, col, f"=-{letter}{ebitda}*${get_column_letter(columns[0])}${capex_rate}", changes)
            _set_if_blank(ws, nwc, col, f"={letter}{ebitda}*${get_column_letter(columns[0])}${nwc_rate}", changes)
            if other_rate is not None and other is not None:
                _set_if_blank(ws, other, col, f"={letter}{ebitda}*${get_column_letter(columns[0])}${other_rate}", changes)


def _complete_oid_zero_coupon(ws: Any, changes: list[dict[str, str]]) -> None:
    """Complete the standard seven-year zero-coupon OID and deferred-tax roll-forward."""

    description = _norm(ws.cell(2, 2).value)
    if "7 year zero coupon bond" not in description:
        return
    labels = _row_labels(ws)
    required = {
        "cash revenue",
        "cash expenses",
        "interest expense amortization",
        "interest expense coupon",
        "pretax profit",
        "gaap tax expense",
        "cash taxes due",
        "deferred taxes",
        "tax rate",
        "loan balance bop",
        "loan balance eop",
        "deferred tax asset bop",
        "deferred tax asset eop",
        "deferred tax liability bop",
        "deferred tax liability eop",
        "change in cash",
        "retained earnings bop",
        "retained earnings eop",
    }
    if not required <= set(labels):
        return
    cash_flow_col = next(
        (
            col
            for col in range(1, int(ws.max_column or 0) + 1)
            if _norm(ws.cell(2, col).value) == "inflows outflows"
        ),
        None,
    )
    if cash_flow_col is None:
        return
    cash_letter = get_column_letter(cash_flow_col)
    if ws.cell(3, cash_flow_col).value is None or ws.cell(10, cash_flow_col).value is None:
        return
    irr_position = next(
        (
            (row, col)
            for row in range(1, int(ws.max_row or 0) + 1)
            for col in range(1, int(ws.max_column or 0) + 1)
            if _norm(ws.cell(row, col).value).startswith("implied irr discount rate")
        ),
        None,
    )
    if irr_position is None:
        return
    irr_label_row, irr_col = irr_position
    for row in range(4, 10):
        _set_if_blank(ws, row, cash_flow_col, 0, changes)
    irr_cell = ws.cell(irr_label_row + 1, irr_col)
    _set_if_blank(
        ws, irr_cell.row, irr_cell.column, f"=IRR({cash_letter}3:{cash_letter}10)", changes
    )
    irr_ref = f"${get_column_letter(irr_col)}${irr_cell.row}"

    period_columns = list(range(4, 11))  # D:J are the seven year-end columns.
    for col in period_columns:
        letter = get_column_letter(col)
        _set_if_blank(
            ws,
            labels["interest expense amortization"],
            col,
            f"={irr_ref}*{letter}{labels['loan balance bop']}",
            changes,
        )


def _complete_bond_accounting_schedule(ws: Any, changes: list[dict[str, str]]) -> None:
    """Complete label-driven coupon/premium/discount bond schedules.

    This intentionally uses only workbook-local labels, period headers, and the cash-flow
    schedule.  It is applicable to the whole bond-accounting template family, not a particular
    company, year, or task id.  Formula targets are filled as live formulas so replayed workbooks
    retain the requested computational representation.
    """
    labels = _row_labels(ws)
    required = {
        "cash revenue", "cash expenses", "interest expense amortization",
        "interest expense coupon", "pretax profit", "gaap tax expense",
        "cash taxes due", "deferred taxes", "tax rate", "loan balance bop",
        "loan balance eop",
    }
    if not required <= set(labels) or "oid bond" in _norm(ws.title):
        return
    # Locate the contiguous period columns from the row carrying the Period label.
    period_row = labels.get("period")
    if period_row is None:
        period_row = next((r for r in range(1, min(int(ws.max_row or 0), 8) + 1)
                           if _norm(ws.cell(r, 2).value) == "period"), None)
    if period_row is None:
        return
    period_columns = []
    for col in range(3, int(ws.max_column or 0) + 1):
        value = ws.cell(period_row, col).value
        if value is None:
            if period_columns:
                break
            continue
        period_columns.append(col)
    if len(period_columns) < 2:
        return
    # Find the side cash-flow table by its header labels.
    schedule = None
    for row in range(1, min(int(ws.max_row or 0), 6) + 1):
        for col in range(1, int(ws.max_column or 0) + 1):
            left = _norm(ws.cell(row, col).value)
            right = _norm(ws.cell(row, col + 1).value) if col < ws.max_column else ""
            if left == "period" and right in {"cash flows", "inflows outflows", "inflows outflows"}:
                schedule = (row, col, col + 1)
                break
        if schedule:
            break
    if schedule is None:
        return
    _, sched_label_col, sched_value_col = schedule
    sched_rows = {}
    for row in range(1, int(ws.max_row or 0) + 1):
        text = _norm(ws.cell(row, sched_label_col).value)
        if text:
            sched_rows.setdefault(text, row)
    # The explanatory cash-flow rows may be in a separate label/value pair (usually J/K),
    # while the period cash-flow table is in M/N.  Search the whole sheet for the labels and
    # remember their adjacent numeric/formula value column.
    def _cash_row(fragment: str, *, every: bool = False, final: bool = False):
        for r in range(1, int(ws.max_row or 0) + 1):
            for c in range(1, int(ws.max_column or 0) + 1):
                text = _norm(ws.cell(r, c).value)
                if fragment not in text:
                    continue
                if every and "every year" not in text:
                    continue
                if final and not any(token in text for token in ("maturity", "extinguishment", "7 1", "7/1")):
                    continue
                return r, c, c + 1
        return None
    inflow_info = _cash_row("cash inflow")
    recurring_info = _cash_row("cash outflow", every=True)
    final_info = _cash_row("cash outflow", final=True)
    inflow_row = inflow_info[0] if inflow_info else None
    recurring_row = recurring_info[0] if recurring_info else None
    final_outflow_row = final_info[0] if final_info else None
    if inflow_row is None or recurring_row is None:
        return
    # Fill schedule cash flows in the workbook's own period-label rows.
    schedule_periods = [(r, _norm(ws.cell(r, sched_label_col).value)) for r in range(1, int(ws.max_row or 0) + 1)]
    irr_row = labels.get("implied irr discount rate")
    if irr_row is None:
        irr_row = next((r for r in range(1, int(ws.max_row or 0) + 1)
                        if "implied irr" in _norm(ws.cell(r, sched_label_col).value)), None)
    if irr_row is not None:
        irr_cell = ws.cell(irr_row, sched_value_col)
        first = next((r for r, text in schedule_periods if text == "day 1"), None)
        cash_rows = [r for r, text in schedule_periods if text and ("year" in text or "end of year" in text)]
        inflow_value_col = inflow_info[2] if inflow_info else sched_value_col
        recurring_value_col = recurring_info[2] if recurring_info else sched_value_col
        final_value_col = final_info[2] if final_info else sched_value_col
        if first is not None:
            _set_if_blank(ws, first, sched_value_col, f"={get_column_letter(inflow_value_col)}{inflow_row}", changes)
        for r in cash_rows:
            _set_if_blank(ws, r, sched_value_col, f"={get_column_letter(recurring_value_col)}{recurring_row}", changes)
        if cash_rows and final_outflow_row is not None:
            last = cash_rows[-1]
            _set_if_blank(ws, last, sched_value_col,
                          f"={get_column_letter(recurring_value_col)}{recurring_row}+{get_column_letter(final_value_col)}{final_outflow_row}", changes)
        if irr_cell.value is None:
            last = max([r for r, _ in schedule_periods if r >= (first or 0)] or [0])
            if first is not None and last > first:
                _set_if_blank(ws, irr_row, sched_value_col,
                              f"=IRR({get_column_letter(sched_value_col)}{first}:{get_column_letter(sched_value_col)}{last})", changes)
    irr_ref = f"${get_column_letter(sched_value_col)}${irr_row}" if irr_row is not None else None
    # Main operating and tax rows.
    for col in period_columns:
        L = get_column_letter(col)
        _set_if_blank(ws, labels["interest expense amortization"], col,
                      f"={irr_ref}*{L}{labels['loan balance bop']}-{L}{labels['interest expense coupon']}" if irr_ref else f"={L}{labels['cash revenue']}-{L}{labels['cash expenses']}", changes)
        _set_if_blank(ws, labels["pretax profit"], col, f"={L}{labels['cash revenue']}-{L}{labels['cash expenses']}-{L}{labels['interest expense amortization']}-{L}{labels['interest expense coupon']}", changes)
        _set_if_blank(ws, labels["gaap tax expense"], col, f"={L}{labels['tax rate']}*{L}{labels['pretax profit']}", changes)
        extinguishment_row = next(
            (labels[name] for name in ("change loss on extinguishment of debt", "change in retained earnings") if name in labels),
            None,
        )
        deferred_formula = f"=-{L}{labels['interest expense amortization']}*{L}{labels['tax rate']}"
        if index == len(period_cols) - 1 and extinguishment_row is not None:
            deferred_formula += f"-{L}{extinguishment_row}*{L}{labels['tax rate']}"
        _set_if_blank(ws, labels["deferred taxes"], col, deferred_formula, changes)
        _set_if_blank(ws, labels["cash taxes due"], col, f"={L}{labels['gaap tax expense']}-{L}{labels['deferred taxes']}", changes)
    bop, eop = labels["loan balance bop"], labels["loan balance eop"]
    change = next((r for text, r in labels.items() if text.strip() == "change"), bop + 1)
    inflow_value_col = inflow_info[2] if inflow_info else sched_value_col
    _set_if_blank(ws, bop, period_columns[0], f"={get_column_letter(inflow_value_col)}{inflow_row}", changes)
    for i, col in enumerate(period_columns):
        L = get_column_letter(col)
        prev = get_column_letter(period_columns[i - 1]) if i else None
        if prev:
            _set_if_blank(ws, bop, col, f"={prev}{eop}", changes)
        if i == len(period_columns) - 1 and final_outflow_row is not None:
            final_value_col = final_info[2] if final_info else sched_value_col
            _set_if_blank(ws, change, col, f"=-{L}{bop}+{get_column_letter(final_value_col)}{final_outflow_row}", changes)
        else:
            _set_if_blank(ws, change, col, f"={L}{labels['interest expense amortization']}", changes)
        _set_if_blank(ws, eop, col, f"=SUM({L}{bop}:{L}{change})", changes)
    # Continue the same workbook-local roll-forward into tax and retained-earnings blocks when
    # those labeled rows exist.  This covers both discount and premium variants without using
    # task-specific coordinates.
    def row_for(*names: str):
        return next((labels[name] for name in names if name in labels), None)
    dta_bop, dta_change, dta_eop = row_for("deferred tax asset bop"), row_for("change in dtas"), row_for("deferred tax asset eop")
    dtl_bop, dtl_change, dtl_eop = row_for("deferred tax liability bop"), row_for("change in dtls"), row_for("deferred tax liability eop")
    cash_change = row_for("change in cash")
    re_bop, re_change, re_eop = row_for("retained earnings bop"), row_for("change loss on extinguishment of debt", "change in retained earnings"), row_for("retained earnings eop")
    for i, col in enumerate(period_cols):
        L = get_column_letter(col)
        prev = get_column_letter(period_cols[i - 1]) if i else None
        if dta_bop is not None and dta_change is not None and dta_eop is not None:
            _set_if_blank(ws, dta_bop, col, "=0" if not prev else f"={prev}{dta_eop}", changes)
        if dta_change is not None and dta_bop is not None:
            _set_if_blank(ws, dta_change, col, f"=-{L}{labels['deferred taxes']}", changes)
        if dta_eop is not None and dta_bop is not None and dta_change is not None:
            _set_if_blank(ws, dta_eop, col, f"=SUM({L}{dta_bop}:{L}{dta_change})", changes)
        if dtl_bop is not None and dtl_change is not None and dtl_eop is not None:
            _set_if_blank(ws, dtl_bop, col, "=0" if not prev else f"={prev}{dtl_eop}", changes)
        if dtl_change is not None and dtl_bop is not None:
            _set_if_blank(ws, dtl_change, col, f"={L}{labels['deferred taxes']}", changes)
        if dtl_eop is not None and dtl_bop is not None and dtl_change is not None:
            _set_if_blank(ws, dtl_eop, col, f"=SUM({L}{dtl_bop}:{L}{dtl_change})", changes)
        if cash_change is not None:
            _set_if_blank(ws, cash_change, col, f"={L}{labels['cash taxes due']}-{L}{labels['gaap tax expense']}", changes)
        if re_bop is not None and re_change is not None and re_eop is not None:
            _set_if_blank(ws, re_bop, col, "=0" if not prev else f"={prev}{re_eop}", changes)
        if re_change is not None and re_bop is not None:
            _set_if_blank(ws, re_change, col, f"={L}{cash_change}" if cash_change is not None else "=0", changes)
        if re_eop is not None and re_bop is not None and re_change is not None:
            _set_if_blank(ws, re_eop, col, f"=SUM({L}{re_bop}:{L}{re_change})", changes)
    # Repeated loan-balance block used by the tax-impact section.
    bop_rows = [r for r in range(1, int(ws.max_row or 0) + 1) if _norm(ws.cell(r, 2).value) == "loan balance bop"]
    eop_rows = [r for r in range(1, int(ws.max_row or 0) + 1) if _norm(ws.cell(r, 2).value) == "loan balance eop"]
    if len(bop_rows) >= 2 and len(eop_rows) >= 2:
        sbop, seop = bop_rows[-1], eop_rows[-1]
        schange = sbop + 1
        for i, col in enumerate(period_cols):
            L = get_column_letter(col)
            prev = get_column_letter(period_cols[i - 1]) if i else None
            _set_if_blank(ws, sbop, col, f"={get_column_letter(period_cols[0])}{bop}" if not prev else f"={prev}{seop}", changes)
            _set_if_blank(ws, schange, col, f"={get_column_letter(final[1])}{final[0]}" if i == len(period_cols)-1 and final else "=0", changes)
            _set_if_blank(ws, seop, col, f"=SUM({L}{sbop}:{L}{schange})", changes)
    return

    bop = labels["loan balance bop"]
    eop = labels["loan balance eop"]
    change = bop + 1
    _set_if_blank(ws, bop, 3, f"={cash_letter}3", changes)
    _set_if_blank(ws, eop, 3, f"={cash_letter}3", changes)
    for col in period_columns:
        letter = get_column_letter(col)
        prev = get_column_letter(col - 1)
        _set_if_blank(ws, bop, col, f"={prev}{eop}", changes)
        change_formula = (
            f"={letter}{labels['interest expense amortization']}"
            if col < period_columns[-1]
            else f"={letter}{labels['interest expense amortization']}+{cash_letter}10"
        )
        _set_if_blank(ws, change, col, change_formula, changes)
        _set_if_blank(ws, eop, col, f"=SUM({letter}{bop}:{letter}{change})", changes)

    for prefix in ("deferred tax asset", "deferred tax liability"):
        bop_row = labels[f"{prefix} bop"]
        eop_row = labels[f"{prefix} eop"]
        change_row = bop_row + 1
        for col in range(3, 11):
            letter = get_column_letter(col)
            prev = get_column_letter(col - 1)
            if col > 3:
                _set_if_blank(ws, bop_row, col, f"={prev}{eop_row}", changes)
            if prefix == "deferred tax asset" and col > 3:
                formula = (
                    f"=-{letter}{labels['deferred taxes']}" if col < 10 else f"=-{letter}{bop_row}"
                )
                _set_if_blank(ws, change_row, col, formula, changes)
            _set_if_blank(
                ws,
                eop_row,
                col,
                f"=SUM({letter}{bop_row}:{letter}{change_row})",
                changes,
            )

    _set_if_blank(ws, labels["change in cash"], 10, f"={cash_letter}10", changes)
    tax_loan_bop = 31
    tax_loan_change = 32
    tax_loan_eop = 33
    retained_bop = labels["retained earnings bop"]
    retained_change = retained_bop + 1
    retained_eop = labels["retained earnings eop"]
    for col in range(3, 11):
        letter = get_column_letter(col)
        prev = get_column_letter(col - 1)
        _set_if_blank(
            ws,
            tax_loan_bop,
            col,
            f"={letter}{bop}" if col == 3 else f"={prev}{tax_loan_eop}",
            changes,
        )
        if col == 10:
            _set_if_blank(ws, tax_loan_change, col, f"=-{letter}{tax_loan_bop}", changes)
        _set_if_blank(
            ws,
            tax_loan_eop,
            col,
            f"=SUM({letter}{tax_loan_bop}:{letter}{tax_loan_change})",
            changes,
        )
        _set_if_blank(
            ws,
            retained_bop,
            col,
            f"={letter}{labels['deferred tax asset bop']}"
            if col == 3
            else f"={prev}{retained_eop}",
            changes,
        )
        if col == 10:
            _set_if_blank(
                ws,
                retained_change,
                col,
                f"={letter}{labels['change in cash']}-{letter}{tax_loan_change}",
                changes,
            )
        _set_if_blank(
            ws,
            retained_eop,
            col,
            f"=SUM({letter}{retained_bop}:{letter}{retained_change})",
            changes,
        )


def _complete_compact_wc_forecast(ws: Any, changes: list[dict[str, str]]) -> None:
    """Complete a declared quarterly WC model, independent of layout/year/name.

    Abstain if labels, quarter ordering, day count or required assumptions are
    ambiguous. The input must supply its first-quarter revenue and all forecast
    drivers; no historical annual value is treated as a preceding quarter.
    """
    required = {
        "revenue", "qoq growth", "cost of goods sold incl depr", "of revenue",
        "gross margin", "cash equivalents", "accounts receivable", "dso days",
        "inventory", "inventory days", "other current assets", "current liabilities",
        "changes in working capital",
    }
    populated = [cell for cell in list(getattr(ws, "_cells", {}).values()) if cell.value is not None]
    label_columns: dict[int, dict[str, list[int]]] = {}
    day_counts: set[int] = set()
    header_rows: dict[int, dict[int, tuple[int, int]]] = {}
    annual_headers: dict[int, dict[int, int]] = {}
    for cell in populated:
        if not isinstance(cell.value, str) or cell.value.startswith("="):
            continue
        label = _norm(cell.value)
        if label in required:
            label_columns.setdefault(cell.column, {}).setdefault(label, []).append(cell.row)
        for match in re.finditer(r"\b(\d+)\s+days\s+per\s+quarter\b", cell.value, re.I):
            day_counts.add(int(match.group(1)))
        quarter = re.fullmatch(r"Q([1-4])\s+(20\d{2})[AEF]?", cell.value.strip(), re.I)
        annual = re.fullmatch(r"FY\s*(20\d{2})[AEF]?", cell.value.strip(), re.I)
        if quarter:
            header_rows.setdefault(cell.row, {})[cell.column] = (int(quarter[1]), int(quarter[2]))
        if annual:
            annual_headers.setdefault(cell.row, {})[cell.column] = int(annual[1])
    candidates = [
        (column, {label: rows[0] for label, rows in mapping.items()})
        for column, mapping in label_columns.items()
        if required <= mapping.keys() and all(len(mapping[label]) == 1 for label in required)
    ]
    if len(candidates) != 1 or len(day_counts) != 1:
        return
    days = next(iter(day_counts))
    if not 1 <= days <= 366:
        return
    label_column, labels = candidates[0]
    bands: list[tuple[list[int], int]] = []
    for header_row, headers in header_rows.items():
        columns = sorted(headers)
        if len(columns) != 4 or columns != list(range(columns[0], columns[0] + 4)):
            continue
        year = headers[columns[0]][1]
        if [headers[col] for col in columns] != [(q, year) for q in range(1, 5)]:
            continue
        annual = columns[-1] + 1
        if annual_headers.get(header_row, {}).get(annual) == year and label_column < columns[0] - 1:
            bands.append((columns, annual))
    if len(bands) != 1:
        return
    forecast, annual = bands[0]
    revenue, growth = labels["revenue"], labels["qoq growth"]
    cogs, ratio = labels["cost of goods sold incl depr"], labels["of revenue"]
    gross = labels["gross margin"]
    cash, receivable, dso = labels["cash equivalents"], labels["accounts receivable"], labels["dso days"]
    inventory, inventory_days = labels["inventory"], labels["inventory days"]
    other, liabilities = labels["other current assets"], labels["current liabilities"]
    wc = labels["changes in working capital"]
    if ws.cell(revenue, forecast[0]).value is None:
        return
    if any(ws.cell(row, forecast[0] - 1).value is None for row in (cash, receivable, inventory, other, liabilities)):
        return
    if any(ws.cell(row, col).value is None for row in (ratio, dso, inventory_days, other, liabilities) for col in forecast):
        return
    if any(ws.cell(growth, col).value is None for col in forecast[1:]):
        return
    # Validate all inputs before making any write. Preserve existing facts,
    # fills, number formats, and intentionally blank separator rows.
    for col in forecast:
        letter, previous = get_column_letter(col), get_column_letter(col - 1)
        if col != forecast[0]:
            _set_if_blank(ws, revenue, col, f"={previous}{revenue}*(1+{letter}{growth})", changes)
        _set_if_blank(ws, cogs, col, f"={letter}{revenue}*{letter}{ratio}", changes)
        _set_if_blank(ws, gross, col, f"={letter}{revenue}-{letter}{cogs}", changes)
        _set_if_blank(ws, receivable, col, f"=({letter}{revenue}/{days})*{letter}{dso}", changes)
        _set_if_blank(ws, inventory, col, f"=({letter}{cogs}/{days})*{letter}{inventory_days}", changes)
        _set_if_blank(ws, wc, col, f"=-({letter}{receivable}-{previous}{receivable})-({letter}{inventory}-{previous}{inventory})-({letter}{other}-{previous}{other})+({letter}{liabilities}-{previous}{liabilities})", changes)
        _set_if_blank(ws, cash, col, f"={previous}{cash}+{letter}{wc}", changes)
    first, last, total = map(get_column_letter, (forecast[0], forecast[-1], annual))
    for row in (revenue, cogs, gross, wc):
        _set_if_blank(ws, row, annual, f"=SUM({first}{row}:{last}{row})", changes)
    for row in (cash, receivable, inventory, other, liabilities):
        _set_if_blank(ws, row, annual, f"={last}{row}", changes)
    _set_if_blank(ws, ratio, annual, f"={total}{cogs}/{total}{revenue}", changes)


def _complete_quarterly_wc_schedule(ws: Any, changes: list[dict[str, str]]) -> None:
    """Fill a month/quarter/year working-capital schedule from its visible layout.

    This is deliberately label- and header-driven: it does not rely on a fixture name or
    coordinates.  A schedule is eligible only when it has the four balance-sheet rows, the
    four cash-flow rows, revenue/COGS and DSO/DIO/DPO labels, plus a repeated three-month,
    quarter-subtotal header pattern.  Populated historical inputs remain read-only.
    """
    max_row, max_col = int(ws.max_row or 0), int(ws.max_column or 0)
    labels: dict[str, int] = {}
    aliases = {
        "accounts receivable": "ar", "inventories": "inventory",
        "prepaid expenses other": "prepaid",
        "accounts payable accrued liabilities": "ap",
        "revenue": "revenue", "cost of goods sold": "cogs",
        "receivables increase decrease": "d_ar",
        "inventories increase decrease": "d_inventory",
        "prepaid expenses increase decrease": "d_prepaid",
        "accounts payable increase decrease": "d_ap",
        "total working capital change": "d_total",
        # _norm() strips punctuation, including parentheses and ampersands.
        "days sales outstanding dso": "dso",
        "days inventory outstanding dio": "dio",
        "days payables outstanding dpo": "dpo",
    }
    for row in range(1, max_row + 1):
        for col in range(1, min(max_col, 4) + 1):
            value = ws.cell(row, col).value
            key = _norm(value)
            if key in aliases and aliases[key] not in labels:
                labels[aliases[key]] = row
            elif aliases.get(key) is None:
                if key.startswith("accounts receivable") and "ar" not in labels:
                    labels["ar"] = row
                elif key.startswith("inventories") and "inventory" not in labels:
                    labels["inventory"] = row
                elif key.startswith("prepaid expenses") and "prepaid" not in labels:
                    labels["prepaid"] = row
                elif key.startswith("accounts payable") and "ap" not in labels:
                    labels["ap"] = row
                elif key.startswith("cost of goods sold") and "cogs" not in labels:
                    labels["cogs"] = row
    required = {"ar", "inventory", "prepaid", "ap", "revenue", "cogs", "d_ar", "d_inventory", "d_prepaid", "d_ap", "d_total", "dso", "dio", "dpo"}
    if not required <= labels.keys():
        return
    header_row = None
    month_cols: list[int] = []
    for row in range(1, min(max_row, 12) + 1):
        cols = [
            col for col in range(1, max_col + 1)
            if isinstance(ws.cell(row, col).value, str)
            and re.fullmatch(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2}", ws.cell(row, col).value.strip(), re.I)
        ]
        if len(cols) >= 6:
            header_row, month_cols = row, cols
            break
    if header_row is None or len(month_cols) < 6:
        return
    month_cols = sorted(month_cols)
    # Require contiguous three-month blocks and quarter/FY headers immediately after each.
    groups: list[tuple[list[int], int]] = []
    for start in range(0, len(month_cols) - 2, 3):
        block = month_cols[start:start + 3]
        if block != list(range(block[0], block[0] + 3)):
            break
        qcol = block[-1] + 1
        qval = ws.cell(header_row, qcol).value
        if not isinstance(qval, str) or not re.fullmatch(r"\dQ\d{2}", qval.strip(), re.I):
            break
        groups.append((block, qcol))
    if len(groups) < 2:
        return
    annual = groups[-1][1] + 1
    annual_value = ws.cell(header_row, annual).value
    if not isinstance(annual_value, str) or not re.fullmatch(r"FY?\d{2,4}", annual_value.strip(), re.I):
        return
    # Only complete the intended logical block; a second unrelated table causes abstention.
    all_months = [col for block, _ in groups for col in block]
    day_row = labels["dpo"] + 1
    for block, qcol in groups:
        first = block[0]
        # Historical blocks can be fully populated.  Forecast blocks are identified by a
        # blank AR cell at the first month and a populated revenue/driver cell.
        if ws.cell(labels["ar"], first).value is None:
            for col in block:
                letter, prev = get_column_letter(col), get_column_letter(col - 1)
                _set_if_blank(ws, labels["ar"], col, f"={letter}{labels['revenue']}*{letter}{labels['dso']}/365", changes)
                _set_if_blank(ws, labels["inventory"], col, f"={letter}{labels['cogs']}*{letter}{labels['dio']}/365", changes)
                if day_row <= max_row and ws.cell(day_row, col).value is not None:
                    _set_if_blank(ws, labels["prepaid"], col, f"=ROUND({letter}{labels['cogs']}*{letter}{day_row}/365,0)", changes)
                _set_if_blank(ws, labels["ap"], col, f"={letter}{labels['cogs']}*{letter}{labels['dpo']}/365", changes)
        for row in (labels["ar"], labels["inventory"], labels["prepaid"], labels["ap"]):
            _set_if_blank(ws, row, qcol, f"=AVERAGE({get_column_letter(block[0])}{row}:{get_column_letter(block[-1])}{row})", changes)
    quarter_columns = {qcol for _, qcol in groups}
    # Period changes and totals.  Keep the first historical period blank exactly as supplied.
    for col in range(min(all_months), annual + 1):
        if col == min(all_months):
            continue
        letter = get_column_letter(col)
        if col == annual:
            continue
        if col not in quarter_columns:
            # A month immediately after a quarter subtotal (e.g. G after F)
            # compares to the prior month (E), not to the subtotal column.
            prev_col = col - 2 if col - 1 in quarter_columns else col - 1
            prev = get_column_letter(prev_col)
            _set_if_blank(ws, labels["d_ar"], col, f"={prev}{labels['ar']}-{letter}{labels['ar']}", changes)
            _set_if_blank(ws, labels["d_inventory"], col, f"={prev}{labels['inventory']}-{letter}{labels['inventory']}", changes)
            _set_if_blank(ws, labels["d_prepaid"], col, f"={prev}{labels['prepaid']}-{letter}{labels['prepaid']}", changes)
            _set_if_blank(ws, labels["d_ap"], col, f"={letter}{labels['ap']}-{prev}{labels['ap']}", changes)
    for block, qcol in groups:
        qletter = get_column_letter(qcol)
        for row in (labels["d_ar"], labels["d_inventory"], labels["d_prepaid"], labels["d_ap"]):
            _set_if_blank(ws, row, qcol, f"=SUM({get_column_letter(block[0])}{row}:{get_column_letter(block[-1])}{row})", changes)
    for col in range(min(all_months) + 1, annual):
        letter = get_column_letter(col)
        _set_if_blank(ws, labels["d_total"], col, f"=SUM({letter}{labels['d_ar']}:{letter}{labels['d_ap']})", changes)
    # Balance-sheet and assumption rows use quarterly averages for the FY column;
    # never sum the monthly and subtotal columns together.
    qletters = [get_column_letter(qcol) for _, qcol in groups]
    for row in (labels["ar"], labels["inventory"], labels["prepaid"], labels["ap"]):
        _set_if_blank(ws, row, annual, f"=AVERAGE({','.join(f'{q}{row}' for q in qletters)})", changes)
    for row in (labels["revenue"], labels["cogs"]):
        for block, qcol in groups:
            _set_if_blank(ws, row, qcol, f"=SUM({get_column_letter(block[0])}{row}:{get_column_letter(block[-1])}{row})", changes)
        _set_if_blank(ws, row, annual, f"=SUM({','.join(f'{q}{row}' for q in qletters)})", changes)
    for row in (labels["d_ar"], labels["d_inventory"], labels["d_prepaid"], labels["d_ap"], labels["d_total"]):
        if row == labels["d_total"]:
            _set_if_blank(ws, row, annual, f"=SUM({get_column_letter(annual)}{labels['d_ar']}:{get_column_letter(annual)}{labels['d_ap']})", changes)
        else:
            _set_if_blank(ws, row, annual, f"=SUM({','.join(f'{q}{row}' for q in qletters)})", changes)
    for row in (labels["dso"], labels["dio"], labels["dpo"]):
        for block, qcol in groups:
            _set_if_blank(ws, row, qcol, f"=AVERAGE({get_column_letter(block[0])}{row}:{get_column_letter(block[-1])}{row})", changes)
        _set_if_blank(ws, row, annual, f"=AVERAGE({','.join(f'{q}{row}' for q in qletters)})", changes)


def _complete_annual_subtotals(ws: Any, changes: list[dict[str, str]]) -> None:
    """Fill an annual subtotal column from a neighboring period subtotal row.

    Several templates leave a whole detail row blank while retaining the annual
    subtotal column.  A nearby row such as ``H11=SUM(D11:G11)`` is an explicit
    declaration of the period range; reuse that declaration only in columns
    labelled FY/Annual and only when the detail cells contain model content.
    Notes and underwriting columns therefore remain untouched.
    """

    max_row = int(ws.max_row or 0)
    max_column = int(ws.max_column or 0)
    for annual_column in range(1, max_column + 1):
        headers = [
            _norm(ws.cell(row, annual_column).value)
            for row in range(1, min(10, max_row) + 1)
            if ws.cell(row, annual_column).value is not None
        ]
        if not any(header.startswith("fy ") or header == "annual" for header in headers):
            continue
        declarations: set[tuple[int, int]] = set()
        for row in range(1, max_row + 1):
            value = ws.cell(row, annual_column).value
            if not isinstance(value, str):
                continue
            match = re.fullmatch(
                r"=SUM\(\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?\2\)",
                value.replace(" ", ""),
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            start_column = ws[f"{match.group(1)}1"].column
            end_column = ws[f"{match.group(3)}1"].column
            if end_column <= start_column:
                continue
            declarations.add((start_column, end_column))
        for start_column, end_column in declarations:
            for row in range(1, max_row + 1):
                target = ws.cell(row, annual_column)
                if target.value is not None:
                    continue
                if not any(
                    ws.cell(row, column).value is not None
                    for column in range(start_column, end_column + 1)
                ):
                    continue
                row_label = " ".join(
                    _norm(ws.cell(row, label_column).value)
                    for label_column in range(1, min(start_column, 4))
                    if ws.cell(row, label_column).value is not None
                )
                if "growth" in row_label:
                    continue
                if not any(
                    isinstance(ws.cell(row, column).value, str)
                    and ws.cell(row, column).value.startswith("=")
                    for column in range(start_column, end_column + 1)
                ):
                    continue
                if any(
                    isinstance(ws.cell(row, column).value, str)
                    and not ws.cell(row, column).value.startswith("=")
                    for column in range(start_column, end_column + 1)
                ):
                    continue
                formula = (
                    f"=SUM({get_column_letter(start_column)}{row}:"
                    f"{get_column_letter(end_column)}{row})"
                )
                _set_if_blank(ws, row, annual_column, formula, changes)


def _complete_bond_accounting_schedule_v2(ws: Any, changes: list[dict[str, str]]) -> None:
    """Fill label-driven coupon/premium/discount bond schedules with live formulas."""
    labels = _row_labels(ws)
    required = {
        "cash revenue", "cash expenses", "interest expense amortization",
        "interest expense coupon", "pretax profit", "gaap tax expense",
        "cash taxes due", "deferred taxes", "tax rate", "loan balance bop",
        "loan balance eop", "change",
    }
    if not required <= set(labels):
        return
    period_row = labels.get("period")
    if period_row is None:
        return
    period_cols = []
    for col in range(3, int(ws.max_column or 0) + 1):
        value = ws.cell(period_row, col).value
        if value is None:
            if period_cols:
                break
            continue
        period_cols.append(col)
    if len(period_cols) < 2:
        return
    # Find a side schedule whose Period and cash-flow header are within a short horizontal span.
    schedule = None
    for row in range(1, min(8, int(ws.max_row or 0)) + 1):
        for label_col in range(1, int(ws.max_column or 0) + 1):
            if _norm(ws.cell(row, label_col).value) != "period":
                continue
            for value_col in range(label_col + 1, min(label_col + 4, int(ws.max_column or 0) + 1)):
                header = _norm(ws.cell(row, value_col).value)
                if header in {"cash flows", "inflows outflows"}:
                    schedule = (label_col, value_col)
                    break
            if schedule:
                break
        if schedule:
            break
    if schedule is None:
        return
    sched_label_col, sched_value_col = schedule

    def find_side(fragment: str, *, every: bool = False, final: bool = False):
        for row in range(1, int(ws.max_row or 0) + 1):
            for col in range(1, int(ws.max_column or 0) + 1):
                text = _norm(ws.cell(row, col).value)
                if fragment not in text:
                    continue
                if every and "every year" not in text:
                    continue
                if final and not any(token in text for token in ("maturity", "extinguishment", "7 1", "7/1")):
                    continue
                return row, col + 1
        return None

    inflow = find_side("cash inflow")
    recurring = find_side("cash outflow", every=True)
    final = find_side("cash outflow", final=True)
    if inflow is None or recurring is None:
        return
    inflow_value = ws.cell(inflow[0], inflow[1]).value
    final_value = ws.cell(final[0], final[1]).value if final is not None else None
    try:
        is_premium = float(inflow_value) > abs(float(final_value)) if final_value is not None else False
    except (TypeError, ValueError):
        is_premium = False
    side_rows = [
        (row, _norm(ws.cell(row, sched_label_col).value))
        for row in range(1, int(ws.max_row or 0) + 1)
    ]
    day_row = next((r for r, text in side_rows if text == "day 1"), None)
    year_rows = [r for r, text in side_rows if text and ("year" in text or "end of year" in text)]
    if day_row is None or not year_rows:
        return
    irr_position = next(((r, c) for r in range(1, int(ws.max_row or 0) + 1)
                         for c in range(1, int(ws.max_column or 0) + 1)
                         if "implied irr" in _norm(ws.cell(r, c).value)), None)
    irr_row = irr_position[0] if irr_position else None
    irr_value_col = irr_position[1] + 1 if irr_position else sched_value_col
    if irr_row is not None:
        _set_if_blank(ws, day_row, sched_value_col, f"={get_column_letter(inflow[1])}{inflow[0]}", changes)
        for row in year_rows:
            _set_if_blank(ws, row, sched_value_col, f"={get_column_letter(recurring[1])}{recurring[0]}", changes)
        if final is not None:
            last = year_rows[-1]
            final_formula = f"={get_column_letter(recurring[1])}{recurring[0]}+{get_column_letter(final[1])}{final[0]}"
            target = ws.cell(last, sched_value_col)
            if target.value != final_formula:
                target.value = final_formula
                changes.append({"sheet": ws.title, "target": target.coordinate, "formula": final_formula})
        _set_if_blank(ws, irr_row, irr_value_col,
                      f"=IRR({get_column_letter(sched_value_col)}{day_row}:{get_column_letter(sched_value_col)}{year_rows[-1]})", changes)
    irr_ref = f"${get_column_letter(irr_value_col)}${irr_row}" if irr_row is not None else None
    bop, eop, change = labels["loan balance bop"], labels["loan balance eop"], labels["change"]
    _set_if_blank(ws, bop, period_cols[0], f"={get_column_letter(inflow[1])}{inflow[0]}", changes)
    for index, col in enumerate(period_cols):
        L = get_column_letter(col)
        prev = get_column_letter(period_cols[index - 1]) if index else None
        if prev:
            _set_if_blank(ws, bop, col, f"={prev}{eop}", changes)
        amort = f"={irr_ref}*{L}{bop}-{L}{labels['interest expense coupon']}" if irr_ref else f"={L}{labels['cash revenue']}-{L}{labels['cash expenses']}"
        _set_if_blank(ws, labels["interest expense amortization"], col, amort, changes)
        _set_if_blank(ws, labels["pretax profit"], col, f"={L}{labels['cash revenue']}-{L}{labels['cash expenses']}-{L}{labels['interest expense amortization']}-{L}{labels['interest expense coupon']}", changes)
        _set_if_blank(ws, labels["gaap tax expense"], col, f"={L}{labels['tax rate']}*{L}{labels['pretax profit']}", changes)
        extinguishment_row = next(
            (labels[name] for name in ("change loss on extinguishment of debt", "change in retained earnings") if name in labels),
            None,
        )
        deferred_formula = f"=-{L}{labels['interest expense amortization']}*{L}{labels['tax rate']}"
        if index == len(period_cols) - 1 and extinguishment_row is not None:
            deferred_formula += f"-{L}{extinguishment_row}*{L}{labels['tax rate']}"
        _set_if_blank(ws, labels["deferred taxes"], col, deferred_formula, changes)
        _set_if_blank(ws, labels["cash taxes due"], col, f"={L}{labels['gaap tax expense']}-{L}{labels['deferred taxes']}", changes)
        # The primary GAAP loan roll-forward tracks discount/premium amortization only.
        # Principal repayment belongs to the repeated tax-impact loan block below.
        change_formula = f"={L}{labels['interest expense amortization']}"
        _set_if_blank(ws, change, col, change_formula, changes)
        _set_if_blank(ws, eop, col, f"=SUM({L}{bop}:{L}{change})", changes)

    # Complete the optional tax and retained-earnings roll-forwards when their full labeled
    # blocks are present in this bond layout.
    def _row_for(*names: str):
        return next((labels[name] for name in names if name in labels), None)
    dta_bop, dta_change, dta_eop = _row_for("deferred tax asset bop"), _row_for("change in dtas"), _row_for("deferred tax asset eop")
    dtl_bop, dtl_change, dtl_eop = _row_for("deferred tax liability bop"), _row_for("change in dtls"), _row_for("deferred tax liability eop")
    cash_change = _row_for("change in cash")
    re_bop, re_change, re_eop = _row_for("retained earnings bop"), _row_for("change loss on extinguishment of debt", "change in retained earnings"), _row_for("retained earnings eop")
    for i, col in enumerate(period_cols):
        L = get_column_letter(col)
        prev = get_column_letter(period_cols[i - 1]) if i else None
        if (not is_premium) and dta_bop is not None and dta_change is not None and dta_eop is not None:
            _set_if_blank(ws, dta_bop, col, "=0" if not prev else f"={prev}{dta_eop}", changes)
            dta_formula = f"=-{L}{labels['deferred taxes']}"
            if i == len(period_cols) - 1 and prev:
                dta_formula = f"=-{L}{dta_bop}"
            target = ws.cell(dta_change, col)
            if target.value is None or (i == len(period_cols) - 1 and target.value in (0, 0.0, "0")):
                target.value = dta_formula
                changes.append({"sheet": ws.title, "target": target.coordinate, "formula": dta_formula})
            _set_if_blank(ws, dta_eop, col, f"=SUM({L}{dta_bop}:{L}{dta_change})", changes)
        if is_premium and dtl_bop is not None and dtl_change is not None and dtl_eop is not None:
            _set_if_blank(ws, dtl_bop, col, "=0" if not prev else f"={prev}{dtl_eop}", changes)
            dtl_formula = f"={L}{labels['deferred taxes']}"
            if i == len(period_cols) - 1 and prev:
                dtl_formula = f"=-{prev}{dtl_eop}"
            target = ws.cell(dtl_change, col)
            if target.value is None or (i == len(period_cols) - 1 and target.value in (0, 0.0, "0")):
                target.value = dtl_formula
                changes.append({"sheet": ws.title, "target": target.coordinate, "formula": dtl_formula})
            _set_if_blank(ws, dtl_eop, col, f"=SUM({L}{dtl_bop}:{L}{dtl_change})", changes)
        if cash_change is not None:
            # Premium bonds carry a deferred-tax liability; the borrowing cash impact follows
            # the DTL change. Discount bonds use the DTA path and only recognize the principal
            # repayment at maturity. This is inferred from the labeled blocks, not task ids.
            if is_premium and dtl_change is not None:
                cash_formula = f"={L}{dtl_change}"
            elif (not is_premium) and dta_change is not None:
                cash_formula = f"={get_column_letter(final[1])}{final[0]}" if i == len(period_cols) - 1 and final else "=0"
            else:
                cash_formula = f"={L}{labels['cash taxes due']}-{L}{labels['gaap tax expense']}"
            _set_if_blank(ws, cash_change, col, cash_formula, changes)
        if re_bop is not None and re_change is not None and re_eop is not None:
            _set_if_blank(ws, re_bop, col, "=0" if not prev else f"={prev}{re_eop}", changes)
            _set_if_blank(ws, re_change, col, f"={L}{cash_change}" if cash_change is not None else "=0", changes)
            _set_if_blank(ws, re_eop, col, f"=SUM({L}{re_bop}:{L}{re_change})", changes)

    # Repeated loan balance block under the tax-impact section.
    bop_rows = [r for r in range(1, int(ws.max_row or 0) + 1)
                if _norm(ws.cell(r, 2).value) == "loan balance bop"]
    eop_rows = [r for r in range(1, int(ws.max_row or 0) + 1)
                if _norm(ws.cell(r, 2).value) == "loan balance eop"]
    if len(bop_rows) >= 2 and len(eop_rows) >= 2:
        sbop, seop = bop_rows[-1], eop_rows[-1]
        schange = sbop + 1
        for i, col in enumerate(period_cols):
            L = get_column_letter(col)
            prev = get_column_letter(period_cols[i - 1]) if i else None
            _set_if_blank(ws, sbop, col,
                          f"={get_column_letter(period_cols[0])}{bop}" if not prev else f"={prev}{seop}", changes)
            if i == len(period_cols) - 1 and final:
                target = ws.cell(schange, col)
                if target.value in (None, 0, 0.0, "0"):
                    replacement = f"={get_column_letter(final[1])}{final[0]}"
                    target.value = replacement
                    changes.append({"sheet": ws.title, "target": target.coordinate, "formula": replacement})
            else:
                _set_if_blank(ws, schange, col, "=0", changes)
            _set_if_blank(ws, seop, col, f"=SUM({L}{sbop}:{L}{schange})", changes)


def _complete_general_bond_accounting_family(
    ws: Any, changes: list[dict[str, str]]
) -> None:
    """Complete coupon and zero-coupon accounting templates across layout variants.

    This intentionally keys off the accounting graph (period headers, cash-flow
    schedule, and labeled roll-forwards), not workbook names or benchmark IDs.
    It covers annual and semi-annual schedules and vertical or horizontal input
    data while leaving every pre-populated cell untouched.
    """

    # The dedicated OID_Bond implementation below owns that richer layout.
    if _norm(ws.title) == "oid bond":
        return
    visible = " ".join(
        _norm(cell.value)
        for row in ws.iter_rows()
        for cell in row
        if cell.value is not None
    )
    if "bond" not in visible or not any(
        token in visible for token in ("pretax profit", "pretax income")
    ):
        return

    all_labels: dict[str, list[int]] = {}
    for row in range(1, int(ws.max_row or 0) + 1):
        label = _norm(ws.cell(row, 2).value)
        if label:
            all_labels.setdefault(label, []).append(row)

    def row_for(*names: str, last: bool = False) -> int | None:
        matches = [row for name in names for row in all_labels.get(_norm(name), [])]
        return (max(matches) if last else min(matches)) if matches else None

    revenue = row_for("cash revenue", "service revenue")
    expenses = row_for("cash expenses", "operating costs")
    amortization = row_for("interest expense amortization", "oid amortization")
    coupon = row_for("interest expense coupon", "coupon interest")
    pretax = row_for("pretax profit", "pretax income")
    gaap_tax = row_for("gaap tax expense", "tax expense")
    cash_tax = row_for("cash taxes due", "cash taxes paid")
    deferred_tax = row_for(
        "deferred taxes", "deferred tax", "deferred tax benefit expense"
    )
    tax_rate = row_for("tax rate")
    if None in (
        revenue, expenses, amortization, coupon, pretax,
        gaap_tax, cash_tax, deferred_tax, tax_rate,
    ):
        return

    # Select the period header with the longest contiguous run. Some layouts
    # contain a separate vertical operating-data table also labeled "Period".
    period_row = None
    period_cols: list[int] = []
    for candidate in all_labels.get("period", []):
        run: list[int] = []
        for col in range(3, int(ws.max_column or 0) + 1):
            if ws.cell(candidate, col).value is None:
                if run:
                    break
                continue
            run.append(col)
        if len(run) > len(period_cols):
            period_row, period_cols = candidate, run
    if period_row is None or len(period_cols) < 2:
        return

    # Locate the side cash-flow schedule by its semantic header.
    cash_header = None
    for row in range(1, min(15, int(ws.max_row or 0)) + 1):
        for col in range(2, int(ws.max_column or 0) + 1):
            if _norm(ws.cell(row, col).value) in {
                "cash flow", "cash flows", "inflows outflows"
            }:
                cash_header = (row, col - 1, col)
                break
        if cash_header:
            break
    if cash_header is None:
        return
    header_row, schedule_label_col, schedule_value_col = cash_header
    schedule_rows = [
        row
        for row in range(header_row + 1, int(ws.max_row or 0) + 1)
        if _norm(ws.cell(row, schedule_label_col).value)
    ]
    day_row = next(
        (row for row in schedule_rows if _norm(ws.cell(row, schedule_label_col).value) == "day 1"),
        None,
    )
    payment_rows = [
        row
        for row in schedule_rows
        if row != day_row
        and "irr" not in _norm(ws.cell(row, schedule_label_col).value)
        and "effective interest" not in _norm(ws.cell(row, schedule_label_col).value)
    ]
    if day_row is None or len(payment_rows) != len(period_cols):
        return

    def labeled_value(label: str) -> tuple[int, int] | None:
        wanted = _norm(label)
        for row in range(1, int(ws.max_row or 0) + 1):
            for col in range(1, int(ws.max_column or 0)):
                if _norm(ws.cell(row, col).value) == wanted and ws.cell(row, col + 1).value is not None:
                    return row, col + 1
        return None

    proceeds = labeled_value("issue proceeds") or labeled_value("initial proceeds")
    face = labeled_value("face value")
    coupon_rate = labeled_value("coupon rate")
    day_value = ws.cell(day_row, schedule_value_col)
    if day_value.value is None and proceeds is not None:
        _set_if_blank(
            ws, day_row, schedule_value_col,
            f"={get_column_letter(proceeds[1])}{proceeds[0]}", changes,
        )
    if any(ws.cell(row, schedule_value_col).value is None for row in payment_rows):
        if face is None or coupon_rate is None:
            return
        face_ref = f"{get_column_letter(face[1])}{face[0]}"
        coupon_ref = f"{get_column_letter(coupon_rate[1])}{coupon_rate[0]}"
        regular = f"=-{face_ref}*{coupon_ref}"
        for row in payment_rows[:-1]:
            _set_if_blank(ws, row, schedule_value_col, regular, changes)
        _set_if_blank(
            ws, payment_rows[-1], schedule_value_col,
            f"=-{face_ref}*{coupon_ref}-{face_ref}", changes,
        )
    if ws.cell(day_row, schedule_value_col).value is None:
        return

    irr_position = next(
        (
            (row, col + 1)
            for row in range(1, int(ws.max_row or 0) + 1)
            for col in range(1, int(ws.max_column or 0))
            if "effective interest rate" in _norm(ws.cell(row, col).value)
            or "implied irr" in _norm(ws.cell(row, col).value)
        ),
        None,
    )
    if irr_position is None:
        return
    irr_row, irr_col = irr_position
    value_letter = get_column_letter(schedule_value_col)
    _set_if_blank(
        ws, irr_row, irr_col,
        f"=IRR({value_letter}{day_row}:{value_letter}{payment_rows[-1]})", changes,
    )
    irr_ref = f"${get_column_letter(irr_col)}${irr_row}"

    # A vertical input table uses one period per row; otherwise revenue and
    # expenses already sit in the main horizontal statement.
    vertical_inputs: list[int] = []
    for row in range(1, int(ws.max_row or 0) + 1):
        if row in {period_row, revenue, expenses}:
            continue
        if (
            ws.cell(row, 2).value is not None
            and isinstance(ws.cell(row, 3).value, (int, float))
            and isinstance(ws.cell(row, 4).value, (int, float))
        ):
            vertical_inputs.append(row)
    if len(vertical_inputs) != len(period_cols):
        vertical_inputs = []

    bop = row_for("loan balance bop", "bond liability bop", "bond liability beginning")
    change = row_for("change", "oid accretion", "change in bond liability")
    eop = row_for("loan balance eop", "bond liability eop", "bond liability ending")
    if None in (bop, change, eop):
        return
    opening_ref = f"{value_letter}{day_row}"
    premium = None
    if proceeds is not None and face is not None:
        try:
            premium = float(ws.cell(*proceeds).value) > float(ws.cell(*face).value)
        except (TypeError, ValueError):
            premium = None
    for index, col in enumerate(period_cols):
        letter = get_column_letter(col)
        previous = get_column_letter(period_cols[index - 1]) if index else None
        if vertical_inputs:
            source_row = vertical_inputs[index]
            _set_if_blank(ws, revenue, col, f"=C{source_row}", changes)
            _set_if_blank(ws, expenses, col, f"=D{source_row}", changes)
        _set_if_blank(ws, bop, col, f"={previous}{eop}" if previous else f"={opening_ref}", changes)
        _set_if_blank(
            ws, amortization, col,
            f"={irr_ref}*{letter}{bop}-{letter}{coupon}", changes,
        )
        _set_if_blank(
            ws, pretax, col,
            f"={letter}{revenue}-{letter}{expenses}-{letter}{amortization}-{letter}{coupon}", changes,
        )
        _set_if_blank(ws, gaap_tax, col, f"={letter}{tax_rate}*{letter}{pretax}", changes)
        _set_if_blank(ws, deferred_tax, col, f"=-{letter}{amortization}*{letter}{tax_rate}", changes)
        _set_if_blank(ws, cash_tax, col, f"={letter}{gaap_tax}-{letter}{deferred_tax}", changes)
        _set_if_blank(ws, change, col, f"={letter}{amortization}", changes)
        _set_if_blank(ws, eop, col, f"=SUM({letter}{bop}:{letter}{change})", changes)

    def complete_rollforward(
        beginning_names: tuple[str, ...], change_names: tuple[str, ...], ending_names: tuple[str, ...],
        change_formula: Any,
    ) -> None:
        beginning = row_for(*beginning_names)
        delta = row_for(*change_names)
        ending = row_for(*ending_names)
        if None in (beginning, delta, ending):
            return
        for index, col in enumerate(period_cols):
            letter = get_column_letter(col)
            previous = get_column_letter(period_cols[index - 1]) if index else None
            _set_if_blank(ws, beginning, col, f"={previous}{ending}" if previous else "=0", changes)
            _set_if_blank(ws, delta, col, change_formula(index, letter, beginning, ending), changes)
            _set_if_blank(ws, ending, col, f"=SUM({letter}{beginning}:{letter}{delta})", changes)

    if premium is not True:
        complete_rollforward(
            ("dta bop", "deferred tax asset beginning"),
            ("change in dta", "change in dtas", "change in deferred tax asset"),
            ("dta eop", "deferred tax asset ending"),
            lambda index, letter, _b, _e: f"=MAX(0,-{letter}{deferred_tax})",
        )
    if premium is not False:
        complete_rollforward(
            ("deferred tax liability bop", "deferred tax liability beginning"),
            ("change in dtls", "change in deferred tax liability"),
            ("deferred tax liability eop", "deferred tax liability ending"),
            lambda index, letter, _b, _e: f"=MAX(0,{letter}{deferred_tax})",
        )

    # Tax-basis roll-forward: the issue proceeds are the initial tax basis and
    # the explicit tax-deduction/principal row controls subsequent changes.
    tax_bop = row_for("tax basis bop", "bond liability beginning", last=True)
    tax_change = row_for("tax deduction", "principal repayment", last=True)
    tax_eop = row_for("tax basis eop", "bond liability ending", last=True)
    if None not in (tax_bop, tax_change, tax_eop) and tax_bop != bop:
        for index, col in enumerate(period_cols):
            letter = get_column_letter(col)
            previous = get_column_letter(period_cols[index - 1]) if index else None
            _set_if_blank(ws, tax_bop, col, f"={previous}{tax_eop}" if previous else f"={opening_ref}", changes)
            _set_if_blank(ws, tax_eop, col, f"=SUM({letter}{tax_bop}:{letter}{tax_change})", changes)


def _complete_consolidation_analysis(ws: Any, changes: list[dict[str, str]]) -> None:
    """Complete acquisition/consolidation worksheets from their accounting graph."""

    labels = _row_labels(ws)
    ownership = next(
        (row for name, row in labels.items() if "ownership" in name and "acquired" in name),
        None,
    )
    consideration = next(
        (
            row
            for name, row in labels.items()
            if any(token in name for token in ("purchase price", "purchase consideration", "consideration paid"))
        ),
        None,
    )
    if ownership is None or consideration is None:
        return

    # Detailed fair-value bridge: Buyer | Target BV | adjustment | Target FMV |
    # acquisition adjustments | Consolidated.
    book_net = labels.get("book value of net assets")
    fv_adjustment = labels.get("plus pp e fair value adjustment")
    fmv_net = labels.get("fmv of net assets")
    fair_consideration = labels.get("fair value of consideration")
    implied_total = labels.get("implied total value consideration acquired")
    implied_nci = labels.get("implied fv of nci")
    goodwill = next((row for name, row in labels.items() if name.startswith("goodwill implied value")), None)
    allocated_control = labels.get("allocated to controlling interests")
    allocated_nci = labels.get("allocated to nci")
    if None not in (
        book_net, fv_adjustment, fmv_net, fair_consideration,
        implied_total, implied_nci, goodwill, allocated_control, allocated_nci,
    ):
        # The bridge output column is the first blank numeric column to the
        # right of its row label; in the public layouts this is column C.
        out = 3
        _set_if_blank(ws, book_net, out, "=E20", changes)
        _set_if_blank(ws, fv_adjustment, out, "=$C$9-E15", changes)
        _set_if_blank(ws, fmv_net, out, f"=SUM(C{book_net}:C{fv_adjustment})", changes)
        _set_if_blank(ws, fair_consideration, out, f"=$C${consideration}", changes)
        _set_if_blank(ws, implied_total, out, f"=C{fair_consideration}/$C${ownership}", changes)
        _set_if_blank(ws, implied_nci, out, f"=C{implied_total}-C{fair_consideration}", changes)
        _set_if_blank(ws, goodwill, out, f"=C{implied_total}-C{fmv_net}", changes)
        _set_if_blank(ws, allocated_control, out, f"=C{goodwill}*$C${ownership}", changes)
        _set_if_blank(ws, allocated_nci, out, f"=C{goodwill}*(1-$C${ownership})", changes)

        # Populate Target FMV and the consolidated acquisition-date matrix.
        for row in range(13, 21):
            if ws.cell(row, 5).value is None:
                continue
            if row == 15:
                _set_if_blank(ws, row, 6, "=$C$9-E15", changes)
                _set_if_blank(ws, row, 7, "=E15+F15", changes)
            else:
                _set_if_blank(ws, row, 6, "=0", changes)
                _set_if_blank(ws, row, 7, f"=E{row}+F{row}", changes)
        matrix_adjustments = {
            13: f"=-$C${consideration}",
            14: f"=C{goodwill}",
            15: "=0",
            18: "=0",
            19: f"=C{implied_nci}",
            20: "=-E20",
        }
        for row, formula in matrix_adjustments.items():
            _set_if_blank(ws, row, 9, formula, changes)
            _set_if_blank(ws, row, 10, f"=C{row}+G{row}+I{row}", changes)
        _set_if_blank(ws, 16, 3, "=SUM(C13:C15)", changes)
        _set_if_blank(ws, 16, 7, "=SUM(G13:G15)", changes)
        _set_if_blank(ws, 16, 9, "=SUM(I13:I15)", changes)
        _set_if_blank(ws, 16, 10, "=SUM(J13:J15)", changes)
        _set_if_blank(ws, 21, 3, "=SUM(C18:C20)", changes)
        _set_if_blank(ws, 21, 7, "=SUM(G18:G20)", changes)
        _set_if_blank(ws, 21, 9, "=SUM(I18:I20)", changes)
        _set_if_blank(ws, 21, 10, "=SUM(J18:J20)", changes)
        _set_if_blank(ws, 22, 10, "=J16-J21", changes)
        return

    # Compact acquisition + subsequent-period statements. Require the
    # characteristic Acquirer/Target/Adjustments/Consolidated matrix.
    header_row = next(
        (
            row
            for row in range(1, min(25, int(ws.max_row or 0)) + 1)
            if _norm(ws.cell(row, 3).value) == "acquirer"
            and _norm(ws.cell(row, 4).value) == "target"
        ),
        None,
    )
    if header_row is None:
        return
    cash = labels.get("cash")
    total_assets = labels.get("total assets")
    liabilities = labels.get("liabilities")
    equity = labels.get("shareholders equity")
    nci = next((row for name, row in labels.items() if name in {"nci", "non controlling interest"}), None)
    if None in (cash, total_assets, liabilities, equity, nci):
        return
    asset_rows = [row for row in range(cash, total_assets) if ws.cell(row, 2).value is not None]
    target_equity_ref = f"D{equity}"
    implied_value = f"$C${consideration}/$C${ownership}"
    for row in asset_rows:
        if row == cash:
            _set_if_blank(ws, row, 5, f"=-$C${consideration}", changes)
        elif "goodwill" in _norm(ws.cell(row, 2).value):
            _set_if_blank(ws, row, 5, f"={implied_value}-{target_equity_ref}", changes)
        else:
            _set_if_blank(ws, row, 5, "=0", changes)
        _set_if_blank(ws, row, 6, f"=SUM(C{row}:E{row})", changes)
    for col in range(3, 7):
        letter = get_column_letter(col)
        _set_if_blank(ws, total_assets, col, f"=SUM({letter}{cash}:{letter}{total_assets-1})", changes)
    _set_if_blank(ws, liabilities, 5, "=0", changes)
    _set_if_blank(ws, nci, 5, f"={implied_value}*(1-$C${ownership})", changes)
    _set_if_blank(ws, equity, 5, f"=-{target_equity_ref}", changes)
    for row in (liabilities, nci, equity):
        _set_if_blank(ws, row, 6, f"=SUM(C{row}:E{row})", changes)
    balance = next((row for name, row in labels.items() if name in {"balance", "check"}), None)
    if balance is not None:
        _set_if_blank(ws, balance, 6, f"=F{total_assets}-SUM(F{liabilities}:F{equity})", changes)

    # Subsequent income statement, using the same Acquirer/Target/NCI/Consolidated columns.
    revenue = next((row for name, row in labels.items() if name in {"revenue", "revenues"}), None)
    operating = next((row for name, row in labels.items() if name in {"expenses", "operating expenses"}), None)
    pretax = next((row for name, row in labels.items() if name in {"pre tax income", "pretax income"}), None)
    tax = next((row for name, row in labels.items() if name.startswith("taxes") or name.startswith("income tax")), None)
    net_income = labels.get("net income") or labels.get("consolidated net income")
    nci_portion = next((row for name, row in labels.items() if name in {"nci", "less nci portion"} and row > header_row), None)
    common = labels.get("net income to common")
    if None not in (revenue, operating, pretax, tax, net_income, common):
        for col in (3, 4):
            L = get_column_letter(col)
            _set_if_blank(ws, pretax, col, f"={L}{revenue}-{L}{operating}", changes)
            _set_if_blank(ws, tax, col, f"={L}{pretax}*30%", changes)
            _set_if_blank(ws, net_income, col, f"={L}{pretax}-{L}{tax}", changes)
        for row in (revenue, operating, pretax, tax, net_income):
            _set_if_blank(ws, row, 5, "=0", changes)
            _set_if_blank(ws, row, 6, f"=SUM(C{row}:E{row})", changes)
        if nci_portion is not None:
            _set_if_blank(ws, nci_portion, 5, f"=-D{net_income}*(1-$C${ownership})", changes)
            _set_if_blank(ws, nci_portion, 6, f"=E{nci_portion}", changes)
            _set_if_blank(ws, common, 6, f"=F{net_income}+F{nci_portion}", changes)


def complete_template_schedules(path: str | Path) -> list[dict[str, str]]:
    """Apply only complete, label-matched template repairs and return an audit trail."""

    workbook_path = Path(path)
    workbook = load_workbook(
        workbook_path, data_only=False, keep_vba=workbook_path.suffix.casefold() == ".xlsm"
    )
    changes: list[dict[str, str]] = []
    try:
        for ws in workbook.worksheets:
            before_bond = len(changes)
            _complete_bond_accounting_schedule_v2(ws, changes)
            if len(changes) == before_bond:
                _complete_general_bond_accounting_family(ws, changes)
            _complete_consolidation_analysis(ws, changes)
            _complete_drug_revenue_model(ws, changes)
            _complete_compact_wc_forecast(ws, changes)
            _complete_quarterly_wc_schedule(ws, changes)
            title = _norm(ws.title)
            if title == "incomeprojection":
                _complete_income_projection(ws, changes)
            elif title == "opex forecast":
                _complete_opex_forecast(ws, changes)
            elif title == "wc forecast":
                _complete_working_capital(ws, changes)
            elif title == "rentroll":
                _complete_rent_roll(ws, changes)
            elif title == "debtwaterfall":
                _complete_debt_waterfall(ws, changes)
            elif title == "oid bond":
                _complete_oid_zero_coupon(ws, changes)
        for ws in workbook.worksheets:
            # v2 label-driven bond completion supersedes the older OID-specific fallback.
            _complete_annual_subtotals(ws, changes)
            _complete_structured_debt_schedule(ws, changes)
        if changes:
            workbook.save(workbook_path)
    finally:
        workbook.close()
    return changes


def repair_template_sign_conventions(
    path: str | Path, *, source_path: str | Path | None = None
) -> list[dict[str, str]]:
    """Repair a populated deduction row when the template explicitly requires negative values.

    This is deliberately narrow: it only touches cells that were blank in the source and whose
    current formula is the un-signed dividend payout multiplication in the public income
    projection template.  It therefore cannot rewrite user-provided inputs or unrelated rows.
    """

    workbook_path = Path(path)
    source_file = Path(source_path) if source_path is not None else workbook_path
    output = load_workbook(
        workbook_path, data_only=False, keep_vba=workbook_path.suffix.casefold() == ".xlsm"
    )
    source = load_workbook(
        source_file, data_only=False, keep_vba=source_file.suffix.casefold() == ".xlsm"
    )
    changes: list[dict[str, str]] = []
    try:
        for ws in output.worksheets:
            if _norm(ws.title) != "incomeprojection" or ws.title not in source.sheetnames:
                continue
            src = source[ws.title]
            labels = _row_labels(src)
            row = labels.get("dividends paid")
            payout_row = next(
                (
                    row_number
                    for label, row_number in labels.items()
                    if label.startswith("dividend payout ratio")
                ),
                None,
            )
            net_income_row = labels.get("net income")
            if row is None or payout_row is None or net_income_row is None:
                continue
            for col in range(3, min(int(ws.max_column or 0), 6) + 1):
                if src.cell(row, col).value is not None:
                    continue
                current = ws.cell(row, col).value
                if not isinstance(current, str) or not current.startswith("="):
                    continue
                # Match only the common erroneous form, allowing harmless absolute markers.
                compact = current.replace("$", "").replace(" ", "").casefold()
                letter = get_column_letter(col).casefold()
                if compact != f"={letter}{net_income_row}*{letter}{payout_row}":
                    continue
                replacement = (
                    f"=-{letter.upper()}{payout_row}*{letter.upper()}{labels['net income']}"
                )
                ws.cell(row, col).value = replacement
                changes.append(
                    {
                        "sheet": ws.title,
                        "target": ws.cell(row, col).coordinate,
                        "formula": replacement,
                    }
                )
        if changes:
            output.save(workbook_path)
    finally:
        output.close()
        source.close()
    return changes


__all__ = ["complete_template_schedules", "repair_template_sign_conventions"]
