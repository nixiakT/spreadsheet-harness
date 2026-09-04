import io
import zipfile
from pathlib import Path

from openpyxl import Workbook, load_workbook

from spreadsheet_harness.financial_model_repairs import (
    complete_financial_model_runtime_actions,
    complete_isolated_formula_holes,
    complete_revenue_growth_schedule,
)


def _rewrite_zip_member(path: Path, member_name: str, callback) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(path) as source:
        with zipfile.ZipFile(buffer, "w") as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == member_name:
                    payload = callback(payload)
                target.writestr(info, payload)
    path.write_bytes(buffer.getvalue())


def _schedule(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Revenue"
    worksheet.append([None, "Metric", "Year 1", "Year 2"])
    rows = [
        ("CPI Growth Rate (%)", 0.02, 0.03),
        ("Price Growth Over CPI (%)", 0.01, 0.01),
        ("Population Growth (%)", 0.005, 0.005),
        ("Market Share - Beginning", 0.3, None),
        ("Market Share - Target Growth", 0.02, 0.01),
        ("Historical Revenue (Year 0)", 100, None),
        ("Total Price Growth Rate (%)", None, None),
        ("Market Share - Ending", None, None),
        ("Share Growth Rate (%)", None, None),
        ("Volume Growth Rate (%)", None, None),
        ("Total Revenue Growth Rate (%)", None, None),
        ("Projected Revenue", None, None),
    ]
    for label, first, second in rows:
        worksheet.append([None, label, first, second])
    workbook.save(path)
    workbook.close()


def test_complete_revenue_growth_schedule_fills_only_calculation_rows(tmp_path: Path) -> None:
    path = tmp_path / "revenue.xlsx"
    _schedule(path)

    changes = complete_revenue_growth_schedule(path)

    workbook = load_workbook(path, data_only=False)
    worksheet = workbook["Revenue"]
    assert len(changes) == 13
    assert worksheet["C8"].value == "=C2+C3"
    assert worksheet["C9"].value == "=C5*(1+C6)"
    assert worksheet["D5"].value == "=C9"
    assert worksheet["D9"].value == "=D5*(1+D6)"
    assert worksheet["C10"].value == "=C9/C5-1"
    assert worksheet["D10"].value == "=D9/D5-1"
    assert worksheet["C11"].value == "=(1+C4)*(1+C10)-1"
    assert worksheet["C12"].value == "=(1+C8)*(1+C11)-1"
    assert worksheet["C13"].value == "=C7*(1+C12)"
    assert worksheet["D13"].value == "=C13*(1+D12)"
    assert worksheet["C5"].value == 0.3
    workbook.close()


def test_complete_revenue_growth_schedule_fails_closed_on_populated_target(tmp_path: Path) -> None:
    path = tmp_path / "revenue.xlsx"
    _schedule(path)
    workbook = load_workbook(path)
    workbook["Revenue"]["C8"] = 123
    workbook.save(path)
    workbook.close()

    assert complete_revenue_growth_schedule(path) == []
    unchanged = load_workbook(path, data_only=False)
    assert unchanged["Revenue"]["C8"].value == 123
    assert unchanged["Revenue"]["C9"].value is None
    unchanged.close()


def test_complete_revenue_growth_schedule_repairs_unbound_prefix_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "revenue.xlsx"
    _schedule(path)

    _rewrite_zip_member(
        path,
        "docProps/core.xml",
        lambda payload: payload.decode("utf-8").replace(
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"', "", 1
        ).encode("utf-8"),
    )

    changes = complete_revenue_growth_schedule(path)

    workbook = load_workbook(path, data_only=False)
    assert len(changes) == 13
    assert workbook["Revenue"]["C8"].value == "=C2+C3"
    workbook.close()


def test_complete_isolated_formula_holes_uses_bidirectional_translation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Distribution"
    for row in range(2, 7):
        worksheet.cell(row, 2, f"Tier {row}")
        worksheet.cell(row, 3, row)
    workbook.save(source)
    worksheet["D2"] = "=Dashboard!$H$9"
    worksheet["D3"] = "=Dashboard!$H$9"
    worksheet["D5"] = "=Dashboard!$H$9"
    worksheet["D6"] = "=Dashboard!$H$9"
    worksheet["E4"] = "=1"
    worksheet["F4"] = "=1"
    worksheet["G4"] = "=1"
    workbook.save(output)
    workbook.close()

    changes = complete_isolated_formula_holes(output, source_path=source)

    repaired = load_workbook(output, data_only=False)
    assert changes == [
        {
            "sheet": "Distribution",
            "target": "D4",
            "formula": "=Dashboard!$H$9",
        }
    ]
    assert repaired["Distribution"]["D4"].value == "=Dashboard!$H$9"
    repaired.close()


def test_complete_isolated_formula_holes_repairs_unbound_prefix_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Distribution"
    for row in range(2, 7):
        worksheet.cell(row, 2, f"Tier {row}")
        worksheet.cell(row, 3, row)
    workbook.save(source)
    worksheet["D2"] = "=Dashboard!$H$9"
    worksheet["D3"] = "=Dashboard!$H$9"
    worksheet["D5"] = "=Dashboard!$H$9"
    worksheet["D6"] = "=Dashboard!$H$9"
    worksheet["E4"] = "=1"
    worksheet["F4"] = "=1"
    worksheet["G4"] = "=1"
    workbook.save(output)
    workbook.close()

    def _break_core(payload: bytes) -> bytes:
        return payload.decode("utf-8").replace(
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"', "", 1
        ).encode("utf-8")

    _rewrite_zip_member(source, "docProps/core.xml", _break_core)
    _rewrite_zip_member(output, "docProps/core.xml", _break_core)

    changes = complete_isolated_formula_holes(output, source_path=source)

    repaired = load_workbook(output, data_only=False)
    assert changes == [
        {
            "sheet": "Distribution",
            "target": "D4",
            "formula": "=Dashboard!$H$9",
        }
    ]
    assert repaired["Distribution"]["D4"].value == "=Dashboard!$H$9"
    repaired.close()


def test_complete_isolated_formula_holes_fills_horizontal_subtotal_gap(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Annuals"
    worksheet["B4"] = "Subtotal"
    worksheet["D4"] = "=SUM(D1:D3)"
    worksheet["F4"] = "=SUM(F1:F3)"
    worksheet["H4"] = "=SUM(H1:H3)"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    changes = complete_isolated_formula_holes(output, source_path=source)

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "Annuals", "target": "E4", "formula": "=SUM(E1:E3)"} in changes
    assert repaired["Annuals"]["E4"].value == "=SUM(E1:E3)"
    repaired.close()


def test_complete_isolated_formula_holes_extends_rightward_same_row_subtotals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Forecast"
    for row in range(2, 5):
        for column in range(4, 11):
            worksheet.cell(row, column, row * column)
    worksheet["D5"] = "=SUM(D2:D4)"
    worksheet["E5"] = "=SUM(E2:E4)"
    worksheet["F5"] = "=SUM(F2:F4)"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    changes = complete_isolated_formula_holes(output, source_path=source)

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "Forecast", "target": "G5", "formula": "=SUM(G2:G4)"} in changes
    assert {"sheet": "Forecast", "target": "J5", "formula": "=SUM(J2:J4)"} in changes
    assert repaired["Forecast"]["G5"].value == "=SUM(G2:G4)"
    assert repaired["Forecast"]["J5"].value == "=SUM(J2:J4)"
    repaired.close()


def test_complete_financial_model_runtime_actions_applies_constants_and_freeze_panes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    dashboard = workbook.active
    dashboard.title = "Dashboard"
    dashboard["B12"] = "Management Fee"
    for row, year in enumerate(range(2025, 2028), start=13):
        dashboard[f"B{row}"] = year if row == 13 else f"=B{row - 1}+1"
        dashboard[f"C{row}"] = "%age"
    dashboard["B23"] = "Portfolio"
    for index, row in enumerate(range(24, 27), start=1):
        dashboard[f"B{row}"] = f"Investment Number {index}"
        dashboard[f"C{row}"] = "Number "
        dashboard[f"D{row}"] = 100 + index
    dashboard["E23"] = "Numbers"
    revenue = workbook.create_sheet("Revenue")
    revenue["A1"] = "Revenue"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = (
        "In the Dashboard, insert management fees at 1.75% for 2025-2027, "
        "and insert first tranche investment numbers at 1 per deal for 3 cumulative deals. "
        "In the Revenue sheet, freeze rows 1-4 and columns A-C."
    )
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "Dashboard", "target": "D13", "value": "0.0175"} in changes
    assert {"sheet": "Dashboard", "target": "E24", "value": "1"} in changes
    assert repaired["Dashboard"]["D13"].value == 0.0175
    assert repaired["Dashboard"]["E24"].value == 1
    assert repaired["Revenue"].freeze_panes == "D5"
    repaired.close()


def test_complete_financial_model_runtime_actions_continues_metric_rows_and_links_peer_row(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    income = workbook.active
    income.title = "IS - Mgmt Co."
    income["B57"] = "EBITDA"
    income["N57"] = "=N13-N55"
    income["B59"] = "Depreciation"
    income["N59"] = 5
    income["O59"] = 6
    income["P59"] = 7
    income["Q59"] = 8
    income["B66"] = "Net Income"
    income["N66"] = "=N61-N63-N64"
    income["O66"] = "=O61-O63-O64"
    income["P66"] = "=P61-P63-P64"
    income["Q66"] = "=Q61-Q63-Q64"
    cashflow = workbook.create_sheet("CF")
    cashflow["B10"] = "+ Net Income"
    cashflow["M11"] = "=+'IS - Mgmt Co.'!N59"
    cashflow["N11"] = "=+'IS - Mgmt Co.'!O59"
    cashflow["O11"] = "=+'IS - Mgmt Co.'!P59"
    cashflow["P11"] = "=+'IS - Mgmt Co.'!Q59"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = (
        "In the CF sheet, link Net Income from IS - Mgmt Co. including Pre-Op Cost. "
        "In the IS - Mgmt Co. sheet, compute EBITDA."
    )
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "IS - Mgmt Co.", "target": "O57", "formula": "=O13-O55"} in changes
    assert {"sheet": "CF", "target": "M10", "formula": "='IS - Mgmt Co.'!N66"} in changes
    assert repaired["IS - Mgmt Co."]["Q57"].value == "=Q13-Q55"
    assert repaired["CF"]["P10"].value == "='IS - Mgmt Co.'!Q66"
    repaired.close()


def test_complete_financial_model_runtime_actions_skips_merged_metric_targets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    dashboard = workbook.active
    dashboard.title = "Dashboard"
    dashboard["A55"] = "Investment Exit Value"
    dashboard["H55"] = "=H40"
    dashboard.merge_cells("H55:I55")
    dashboard["I40"] = 123
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = "In the Dashboard, compute investment exit value for each tranche."
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert changes == []
    assert repaired["Dashboard"]["H55"].value == "=H40"
    repaired.close()


def test_complete_financial_model_runtime_actions_fills_ticket_sizes_and_exit_values(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    dashboard = workbook.active
    dashboard.title = "Dashboard"
    dashboard["G14"] = "Ticket Size per Investment (Unit Economics) (USD Mn)"
    dashboard["G15"] = "First Tranche"
    dashboard["G16"] = "Second Tranche"
    dashboard["G17"] = "Third Tranche"
    dashboard["D22"] = "First Tranche"
    dashboard["F22"] = "Second Tranche"
    dashboard["H22"] = "Third Tranche"
    dashboard["B23"] = "Portfolio Building MoM"
    dashboard["C23"] = "UOM"
    dashboard["D23"] = "Month"
    dashboard["E23"] = "Numbers"
    dashboard["F23"] = "Month"
    dashboard["G23"] = "Numbers"
    dashboard["H23"] = "Month"
    dashboard["I23"] = "Numbers"
    dashboard["B24"] = "Investment Number 1"
    dashboard["E24"] = 1
    dashboard["G24"] = 1
    dashboard["I24"] = 1
    dashboard["B43"] = "Investment 1 - NCLT Case"
    dashboard["E43"] = 2.5
    dashboard["G43"] = 2
    dashboard["I43"] = 2
    dashboard["B55"] = "Investment Exit Value"
    dashboard["C56"] = "UOM"
    dashboard["D56"] = "Month"
    dashboard["E56"] = "Numbers"
    dashboard["F56"] = "Month"
    dashboard["G56"] = "Numbers"
    dashboard["H56"] = "Month"
    dashboard["I56"] = "Numbers"
    dashboard["B57"] = "Investment Number 1"
    dashboard["D57"] = "=D43"
    dashboard["F57"] = "=F43"
    dashboard["H57"] = "=H43"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = (
        "In the Dashboard, update ticket size per investment for first, second, and third "
        "tranches at 20, 10, and 10 in light blue, and compute investment exit value for "
        "each tranche."
    )
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "Dashboard", "target": "H15", "value": "20"} in changes
    assert {"sheet": "Dashboard", "target": "H16", "value": "10"} in changes
    assert {"sheet": "Dashboard", "target": "H17", "value": "10"} in changes
    assert {"sheet": "Dashboard", "target": "E57", "formula": "=E43*E24*$H$15"} in changes
    assert {"sheet": "Dashboard", "target": "G57", "formula": "=G43*G24*$H$16"} in changes
    assert {"sheet": "Dashboard", "target": "I57", "formula": "=I43*I24*$H$17"} in changes
    assert repaired["Dashboard"]["H15"].value == 20
    assert repaired["Dashboard"]["E57"].value == "=E43*E24*$H$15"
    assert repaired["Dashboard"]["G57"].value == "=G43*G24*$H$16"
    assert repaired["Dashboard"]["I57"].value == "=I43*I24*$H$17"
    repaired.close()


def test_complete_financial_model_runtime_actions_links_header_column_from_dashboard(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    dashboard = workbook.active
    dashboard.title = "Dashboard"
    dashboard["G9"] = "Hurdle Rate"
    dashboard["H9"] = 0.12
    distribution = workbook.create_sheet("Distribution Sheet-Deal By Deal")
    distribution["I4"] = "Hurdle Rate"
    distribution["C5"] = "Investment Number 1"
    distribution["H5"] = "=Dashboard!E57"
    distribution["J5"] = "=G5"
    distribution["C6"] = "Investment Number 2"
    distribution["H6"] = "=Dashboard!E58"
    distribution["J6"] = "=G6"
    distribution["C8"] = "=Dashboard!F22"
    distribution["J8"] = "=G8"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = "In the Distribution Sheet-Deal By Deal, link hurdle rate from Dashboard."
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {
        "sheet": "Distribution Sheet-Deal By Deal",
        "target": "I5",
        "formula": "=Dashboard!$H$9",
    } in changes
    assert {
        "sheet": "Distribution Sheet-Deal By Deal",
        "target": "I6",
        "formula": "=Dashboard!$H$9",
    } in changes
    assert {
        "sheet": "Distribution Sheet-Deal By Deal",
        "target": "I8",
        "formula": "=Dashboard!$H$9",
    } in changes
    assert repaired["Distribution Sheet-Deal By Deal"]["I5"].value == "=Dashboard!$H$9"
    assert repaired["Distribution Sheet-Deal By Deal"]["I6"].value == "=Dashboard!$H$9"
    assert repaired["Distribution Sheet-Deal By Deal"]["I8"].value == "=Dashboard!$H$9"
    repaired.close()


def test_complete_financial_model_runtime_actions_continues_metric_rows_across_spacer_gap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    workings = workbook.active
    workings.title = "Workings Cost Sheet"
    workings["B204"] = "Opening"
    workings["B205"] = "Capex"
    workings["B206"] = "Renewal Capex"
    workings["B207"] = "Depreciation"
    workings["B208"] = "Ending PP&E"
    for column in "DEFGHIJKL":
        workings[f"{column}208"] = f"={column}204+{column}205+{column}206-{column}207"
    workings["N207"] = 0.125
    workings["O204"] = 0
    workings["P204"] = "=O208"
    workings["Q204"] = "=P208"
    workings["O205"] = 10
    workings["P205"] = 20
    workings["Q205"] = 30
    workings["O206"] = 0
    workings["P206"] = 0
    workings["Q206"] = 0
    workings["O207"] = "=SUM(O204:O206)*$N$207"
    workings["P207"] = "=SUM(P204:P206)*$N$207"
    workings["Q207"] = "=SUM(Q204:Q206)*$N$207"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = "In the Workings Cost sheet, compute ending PP&E from Apr-25 to Jun-33."
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "Workings Cost Sheet", "target": "O208", "formula": "=O204+O205+O206-O207"} in changes
    assert {"sheet": "Workings Cost Sheet", "target": "Q208", "formula": "=Q204+Q205+Q206-Q207"} in changes
    assert repaired["Workings Cost Sheet"]["M208"].value is None
    assert repaired["Workings Cost Sheet"]["N208"].value is None
    assert repaired["Workings Cost Sheet"]["O208"].value == "=O204+O205+O206-O207"
    assert repaired["Workings Cost Sheet"]["Q208"].value == "=Q204+Q205+Q206-Q207"
    repaired.close()


def test_complete_financial_model_runtime_actions_links_metric_rows_without_explicit_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    cashflow = workbook.active
    cashflow.title = "CF"
    cashflow["B13"] = "- Change in Working Capital"
    cashflow["M13"] = "=M34"
    cashflow["N34"] = 10
    cashflow["O34"] = 11
    cashflow["P34"] = 12
    cashflow["Q34"] = 13
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = "In the CF sheet, link Change in Working Capital."
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "CF", "target": "N13", "formula": "=N34"} in changes
    assert {"sheet": "CF", "target": "Q13", "formula": "=Q34"} in changes
    assert repaired["CF"]["N13"].value == "=N34"
    assert repaired["CF"]["Q13"].value == "=Q34"
    repaired.close()


def test_complete_financial_model_runtime_actions_fills_later_annual_blocks_and_subtotals(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    expenses = workbook.active
    expenses.title = "Other Expenses"
    expenses["B5"] = "Expenses"
    for coordinate, value in {
        "D5": "M0",
        "E5": "M1",
        "F5": "M2",
        "G5": "M3",
        "H5": "M4",
        "I5": "M5",
        "J5": "M6",
        "K5": "M7",
        "L5": "M8",
        "M5": "M9",
        "N5": "M10",
        "O5": "M11",
        "P5": "M12",
        "Q5": "Y1",
        "R5": "Q1",
        "S5": "Q2",
        "T5": "Q3",
        "U5": "Q4",
        "V5": "Y2",
        "W5": "Q1",
        "X5": "Q2",
        "Y5": "Q3",
        "Z5": "Q4",
        "AA5": "Y3",
    }.items():
        expenses[coordinate] = value
    expenses["B6"] = "Rent"
    expenses["Q6"] = "=SUM(D6:P6)"
    expenses["R6"] = 1
    expenses["S6"] = 2
    expenses["T6"] = 3
    expenses["U6"] = 4
    expenses["W6"] = 5
    expenses["X6"] = 6
    expenses["Y6"] = 7
    expenses["Z6"] = 8
    expenses["B7"] = "Subtotal"
    expenses["Q7"] = "=SUM(Q6:Q6)"
    workbook.save(source)
    workbook.save(output)
    workbook.close()

    instruction = "In the Other Expenses sheet, compute total expenses for Y2 and Y3."
    changes = complete_financial_model_runtime_actions(
        output,
        source_path=source,
        instruction=instruction,
    )

    repaired = load_workbook(output, data_only=False)
    assert {"sheet": "Other Expenses", "target": "V6", "formula": "=SUM(R6:U6)"} in changes
    assert {"sheet": "Other Expenses", "target": "AA6", "formula": "=SUM(W6:Z6)"} in changes
    assert {"sheet": "Other Expenses", "target": "V7", "formula": "=SUM(V6:V6)"} in changes
    assert {"sheet": "Other Expenses", "target": "AA7", "formula": "=SUM(AA6:AA6)"} in changes
    assert repaired["Other Expenses"]["V6"].value == "=SUM(R6:U6)"
    assert repaired["Other Expenses"]["AA6"].value == "=SUM(W6:Z6)"
    assert repaired["Other Expenses"]["V7"].value == "=SUM(V6:V6)"
    assert repaired["Other Expenses"]["AA7"].value == "=SUM(AA6:AA6)"
    repaired.close()
