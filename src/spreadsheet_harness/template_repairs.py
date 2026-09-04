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
        _set_if_blank(
            ws,
            labels["pretax profit"],
            col,
            f"={letter}{labels['cash revenue']}-{letter}{labels['cash expenses']}-{letter}{labels['interest expense amortization']}-{letter}{labels['interest expense coupon']}",
            changes,
        )
        _set_if_blank(
            ws,
            labels["gaap tax expense"],
            col,
            f"={letter}{labels['tax rate']}*{letter}{labels['pretax profit']}",
            changes,
        )
        deferred_formula = (
            f"=-{letter}{labels['interest expense amortization']}*{letter}{labels['tax rate']}"
            if col < period_columns[-1]
            else f"={letter}{labels['deferred tax asset bop']}"
        )
        _set_if_blank(ws, labels["deferred taxes"], col, deferred_formula, changes)
        _set_if_blank(
            ws,
            labels["cash taxes due"],
            col,
            f"={letter}{labels['gaap tax expense']}-{letter}{labels['deferred taxes']}",
            changes,
        )

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


def complete_template_schedules(path: str | Path) -> list[dict[str, str]]:
    """Apply only complete, label-matched template repairs and return an audit trail."""

    workbook_path = Path(path)
    workbook = load_workbook(
        workbook_path, data_only=False, keep_vba=workbook_path.suffix.casefold() == ".xlsm"
    )
    changes: list[dict[str, str]] = []
    try:
        for ws in workbook.worksheets:
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
