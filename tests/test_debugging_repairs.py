from __future__ import annotations

from openpyxl import Workbook
from openpyxl.worksheet.formula import ArrayFormula

from spreadsheet_harness.debugging_repairs import detect_debugging_repair_candidates


def test_incorrect_average_candidates_are_exact_and_task_gated() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["C3"] = "=AVERAGE(C1:C2)"
    worksheet["D3"] = "=SUM(360,365)"

    assert detect_debugging_repair_candidates(workbook, task_hint="other.xlsx") == []
    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx"
    )

    assert any(item.cell == "D3" and item.replacement == "=AVERAGE(360,365)" for item in candidates)
    assert any(item.cell == "C3" and item.replacement == "=AVERAGE(C1:C3)" for item in candidates)
    assert all(item.target.startswith("'Model'!") for item in candidates)


def test_average_candidates_cover_whole_range_shift_and_neighbor_translation() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["I7"] = "=SUM($I7,I$1)*AVERAGE(I4,I6)"
    worksheet["J7"] = "=SUM($I7,J$1)*AVERAGE(J6)"
    worksheet["D9"] = "=AVERAGE(D3:E3)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "J7"
        and item.kind == "average_neighbor_translation"
        and item.replacement == "=SUM($I7,J$1)*AVERAGE(J4,J6)"
        for item in candidates
    )
    assert any(
        item.cell == "D9"
        and item.kind == "aggregate_range_shift"
        and item.replacement == "=AVERAGE(C3:D3)"
        for item in candidates
    )


def test_average_context_excludes_blank_and_subject_company() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "WACC"
    source = workbook.create_sheet("Exhibit 6")
    worksheet["C6"] = "Anadarko"
    worksheet["D6"] = "Comparables"
    worksheet["D7"] = "=AVERAGE('Exhibit 6'!D36:J36)"
    source["D5"] = "Anadarko"
    source["E5"] = "Chevron"
    source["D36"] = 0.4
    source["E36"] = 0.1
    source["J36"] = 0.2
    worksheet["I15"] = "=AVERAGE($F$15:$H$15)"
    worksheet["G15"] = 0.2
    worksheet["H15"] = 0.3

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )
    assert any(
        item.cell == "D7"
        and item.kind == "average_exclude_subject"
        and item.replacement == "=AVERAGE('Exhibit 6'!E36:J36)"
        for item in candidates
    )
    assert any(
        item.cell == "I15"
        and item.kind == "average_exclude_blank_endpoint"
        and item.replacement == "=AVERAGE($G$15:$H$15)"
        for item in candidates
    )


def test_average_range_shift_exposes_duplicate_neighbor_for_execution_guard() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "WACC"
    worksheet["D9"] = "=AVERAGE(H10:M10)"
    worksheet["D10"] = "=AVERAGE(H9:M9)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )
    assert any(
        item.cell == "D10"
        and item.replacement == worksheet["D9"].value
        and item.kind in {"aggregate_range_shift", "aggregate_boundary"}
        for item in candidates
    )


def test_average_array_formula_stops_before_summary_rows_after_blank_gap() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "WACC"
    source = workbook.create_sheet("Exhibit 9")
    worksheet["D14"] = ArrayFormula(
        ref="D14", text="=AVERAGE('Exhibit 9'!M8:M26/100)"
    )
    for row in range(8, 23):
        source.cell(row, 13).value = float(row)
    source["M25"] = 7.3
    source["M26"] = 8.7

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )
    assert any(
        item.cell == "D14"
        and item.kind == "average_trim_summary_after_gap"
        and item.replacement == "=AVERAGE('Exhibit 9'!M8:M22/100)"
        for item in candidates
    )


def test_average_context_excludes_self_and_uses_ending_balance_for_interest() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "LBO"
    worksheet["B12"] = "Average"
    worksheet["E12"] = "=AVERAGE(E7:E12)"
    worksheet["B132"] = "Beginning Balance"
    worksheet["C132"] = "Cap:"
    worksheet["B134"] = "Paydown"
    worksheet["B135"] = "Ending Balance"
    worksheet["B136"] = "Interest"
    worksheet["G136"] = "=AVERAGE(G132,G134)*G127"
    worksheet["B180"] = "Beginning Balance"
    worksheet["B181"] = "Increase / (Decrease)"
    worksheet["B182"] = "Ending Balance"
    worksheet["B183"] = "Interest on Cash"
    worksheet["I183"] = "=H183*AVERAGE(I180:I182)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )
    assert any(
        item.cell == "E12"
        and item.kind == "average_exclude_self_reference"
        and item.replacement == "=AVERAGE(E7:E11)"
        for item in candidates
    )
    assert any(
        item.cell == "I183"
        and item.kind == "average_balance_endpoints"
        and item.replacement == "=H183*AVERAGE(I180,I182)"
        for item in candidates
    )
    assert any(
        item.cell == "G136"
        and item.kind == "average_beginning_ending_balance"
        and item.replacement == "=AVERAGE(G132,G135)*G127"
        for item in candidates
    )


def test_incorrect_average_repairs_cagr_semantics_and_header_period() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "P&L Summary"
    worksheet["M5"] = "'23A-'25A"
    worksheet["N5"] = "'26E-'30E"
    worksheet["M10"] = "=(E10-C10)/(2*C10)"
    worksheet["N13"] = "=(J13/F13)^(1/5)-1"
    worksheet["P12"] = '=+IFERROR((G12/E12-1)/(YEAR(G$7)-YEAR(E$7)),"NA")'
    worksheet["Q12"] = "=_xludf.RRI(5,W12,AA12)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )
    expected = {
        ("M10", "cagr_fixed_period", "=(E10/C10)^(1/2)-1"),
        ("N13", "cagr_period_alignment", "=(J13/F13)^(1/4)-1"),
        (
            "P12",
            "cagr_compound_years",
            '=+IFERROR((G12/E12)^(1/(YEAR(G$7)-YEAR(E$7)))-1,"NA")',
        ),
        ("Q12", "cagr_rri_native_function", "=rri(5,W12,AA12)"),
    }
    observed = {(item.cell, item.kind, item.replacement) for item in candidates}
    assert expected <= observed


def test_incorrect_average_uses_financial_table_semantics() -> None:
    workbook = Workbook()
    comps = workbook.active
    comps.title = "Comps + Beta"
    comps["B29"] = "Relevered Beta"
    comps["C30"] = "=AVERAGE(I22:I26)"
    comps["I20"] = "3yr Levered Beta"
    comps["K20"] = "Unlevered Beta"
    comps["H5"] = "N/A"
    comps["H6"] = "=E6/K6"
    comps["H7"] = "=E7/K7"
    comps["H15"] = "=MEDIAN(H7:H10)"
    dcf = workbook.create_sheet("DCF Model")
    dcf["G7"] = "Projected"
    dcf["D15"] = 0.1
    dcf["E15"] = 0.2
    dcf["F15"] = 0.3
    dcf["G15"] = "=AVERAGE(D15:E15)"
    dcf["Q94"] = 100
    dcf["R94"] = 120
    dcf["Q95"] = 0.9
    dcf["R95"] = 0.8
    lbo = workbook.create_sheet("LBO & Lenders")
    lbo["B36"] = "Exit Proceeds (Average of Exit Low / High DCF Values)"
    lbo["X36"] = "=AVERAGE('DCF Model'!Q94:R95)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Average_input.xlsx", max_candidates=500
    )
    expected = {
        ("Comps + Beta", "C30", "=AVERAGE(K22:K26)"),
        ("Comps + Beta", "H15", "=MEDIAN(H6:H10)"),
        ("DCF Model", "G15", "=AVERAGE(D15:F15)"),
        ("LBO & Lenders", "X36", "=AVERAGE('DCF Model'!Q94:R94)"),
    }
    observed = {(item.sheet, item.cell, item.replacement) for item in candidates}
    assert expected <= observed


def test_sign_candidates_cover_two_operator_flip_and_row_peer_translation() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "LBO"
    worksheet["B4"] = "Enterprise Value"
    worksheet["C4"] = "=C1-C2+C3"
    worksheet["H7"] = "=SUM(H2:H4)"
    worksheet["I7"] = "=I2-I3-I4"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Sign Conventions_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "C4" and item.kind == "sign_operator_flip" and item.replacement == "=C1+C2-C3"
        for item in candidates
    )
    assert any(
        item.cell == "I7"
        and item.kind == "row_peer_translation"
        and item.replacement == "=SUM(I2:I4)"
        for item in candidates
    )
    assert any(
        item.cell == "I7"
        and item.kind == "sign_contiguous_sum"
        and item.replacement == "=SUM(I2:I4)"
        for item in candidates
    )


def test_sign_candidate_uses_explicit_plus_minus_line_item_labels() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "LBO"
    worksheet["B10"] = "Equity Value"
    worksheet["B11"] = "(+) Debt"
    worksheet["B12"] = "(-) Cash"
    worksheet["B13"] = "Enterprise Value"
    worksheet["C13"] = "=+C10-C11+C12"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Sign Conventions_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "C13"
        and item.kind == "label_sign_alignment"
        and item.replacement == "=+C10+C11-C12"
        for item in candidates
    )


def test_embedded_hardcode_candidate_uses_bidirectional_formula_consensus() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "DCF"
    worksheet["H5"] = "=H2*$C$1"
    worksheet["I5"] = 12.4
    worksheet["J5"] = "=J2*$C$1"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Embedded Hardcodes_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "I5"
        and item.kind == "embedded_hardcode_consensus"
        and item.current == 12.4
        and item.replacement == "=I2*$C$1"
        for item in candidates
    )


def test_embedded_hardcode_candidate_uses_two_same_direction_row_peers() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Cases"
    worksheet["F11"] = 0.12
    worksheet["G11"] = "=Financials!G10/Financials!F10-1"
    worksheet["H11"] = "=Financials!H10/Financials!G10-1"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Embedded Hardcodes_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "F11"
        and item.kind == "embedded_row_consensus"
        and item.replacement == "=Financials!F10/Financials!E10-1"
        for item in candidates
    )


def test_embedded_hardcode_candidate_restores_flat_forecast_chain() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "DCF"
    worksheet["I15"] = "=AVERAGE($G$15:$H$15)"
    for coordinate in ("J15", "K15", "L15", "M15"):
        worksheet[coordinate] = 0.124

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Embedded Hardcodes_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "J15" and item.kind == "embedded_flat_run_chain" and item.replacement == "=I15"
        for item in candidates
    )
    assert any(
        item.cell == "M15" and item.kind == "embedded_flat_run_chain" and item.replacement == "=L15"
        for item in candidates
    )


def test_embedded_formula_literal_candidate_links_same_column_assumption() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["J45"] = "=-0.25*J44"
    worksheet["J46"] = 0.25

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Embedded Hardcodes_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "J45"
        and item.kind == "embedded_literal_same_column"
        and item.replacement == "=-J46*J44"
        for item in candidates
    )


def test_embedded_hardcode_candidate_extrapolates_nonunit_row_stride() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "LBO"
    worksheet["Q41"] = 0.04
    worksheet["R41"] = 0.05
    worksheet["Q42"] = "=+I22"
    worksheet["Q43"] = "=+I27"
    worksheet["R42"] = "=+J22"
    worksheet["R43"] = "=+J27"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Embedded Hardcode_input.xlsx", max_candidates=500
    )
    assert any(
        item.cell == "Q41"
        and item.kind == "embedded_matching_sequence"
        and item.replacement == "=+I17"
        for item in candidates
    )


def test_embedded_hardcode_candidate_links_lbo_multiple_and_cross_sheet_driver() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "LBO"
    worksheet["B18"] = "Entry EBITDA Multiple"
    worksheet["C18"] = "=C38"
    worksheet["B20"] = "Exit EBITDA Multiple"
    worksheet["C20"] = "=C38"
    worksheet["W18"] = "(x) Exit Multiple"
    worksheet["X17"] = "=P23"
    worksheet["Y17"] = "=Q23"
    worksheet["Z17"] = "=R23"
    worksheet["X18"] = 14.2
    worksheet["Y18"] = 14.2
    worksheet["Z18"] = 14.2
    worksheet["B78"] = "Senior Secured TLB Interest Expense"
    worksheet["B47"] = "Senior Debt Rate"
    worksheet["C47"] = 0.04
    worksheet["Q78"] = "=Q73*(0.04+Q52)"
    worksheet["R78"] = "=R73*(0.04+R52)"
    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Embedded Hardcode_input.xlsx", max_candidates=500
    )
    assert any(
        item.cell == "X18"
        and item.kind == "embedded_exit_multiple_anchor"
        and item.replacement == "=C18"
        for item in candidates
    )
    assert any(
        item.cell == "Y18"
        and item.kind == "embedded_exit_multiple_anchor"
        and item.replacement == "=$C$20"
        for item in candidates
    )
    assert any(
        item.cell == "Q78"
        and item.kind == "embedded_literal_assumption_match"
        and item.replacement == "=Q73*($C$47+Q52)"
        for item in candidates
    )


def test_double_counting_candidates_remove_direct_duplicates() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["M88"] = "=SUM(M83:M87)+M84"
    worksheet["I8"] = "=SUM(I4,I6,I4)"
    worksheet["C83"] = "=C71+C74+C71-SUM(C77,C80)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Double Counting_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "M88"
        and item.kind == "double_count_range_member"
        and item.replacement == "=SUM(M83:M87)"
        for item in candidates
    )
    assert any(
        item.cell == "C83"
        and item.kind == "double_count_direct_term"
        and item.replacement == "=C71+C74-SUM(C77,C80)"
        for item in candidates
    )
    assert any(
        item.cell == "I8"
        and item.kind == "double_count_duplicate"
        and item.replacement == "=SUM(I6,I4)"
        for item in candidates
    )


def test_double_counting_candidates_use_financial_subtotal_labels() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "LBO"
    worksheet["B64"] = "Core EBIT"
    worksheet["B57"] = "Adj. EBITDA"
    worksheet["B66"] = "EBIT"
    worksheet["B69"] = "Net Income"
    worksheet["B158"] = "Cash Interest"
    worksheet["B175"] = "Total Interest"
    worksheet["G64"] = "=G57-G62"
    worksheet["G66"] = "=+G57+G75"
    worksheet["G69"] = "=+SUM(G64:G68)"
    worksheet["G175"] = "=+G136+G137+G145+G152+G158"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Double Counting_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "G66"
        and item.kind == "double_count_subtotal_chain"
        and item.replacement == "=+G64+G75"
        for item in candidates
    )
    assert any(
        item.cell == "G69"
        and item.kind == "double_count_subtotal_chain"
        and item.replacement == "=+SUM(G66:G68)"
        for item in candidates
    )
    assert any(
        item.cell == "G175"
        and item.kind == "double_count_derived_interest"
        and item.replacement == "=+G136+G137+G145+G152"
        for item in candidates
    )


def test_double_counting_candidate_expands_verified_cross_sheet_total() -> None:
    workbook = Workbook()
    target = workbook.active
    target.title = "DCF"
    source = workbook.create_sheet("Balance Sheet")
    source["A20"] = "Accounts payable"
    source["A21"] = "Short-term debt"
    source["A22"] = "Other current liabilities"
    source["A23"] = "Total current liabilities"
    source["C20"] = 2164
    source["C21"] = 947
    source["C22"] = 1547
    source["C23"] = 4658
    source["C24"] = 15470
    source["C9"] = 1295
    target["R32"] = (
        "=('Balance Sheet'!C23+'Balance Sheet'!C24+'Balance Sheet'!C21"
        "-'Balance Sheet'!C20-'Balance Sheet'!C22)-'Balance Sheet'!C9"
    )
    comparables = workbook.create_sheet("WACC")
    comparables["C6"] = "Subject Co"
    comparables["D6"] = "Comparables"
    for column, company in zip(
        range(7, 14),
        ("Subject Co", "Peer A", "Peer B", "Peer C", "Peer D", "Peer E", "Peer F"),
        strict=True,
    ):
        comparables.cell(6, column).value = company
        comparables.cell(9, column).value = column / 10
    comparables["D10"] = "=AVERAGE(G9:M9)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Double Counting_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "R32"
        and item.kind == "double_count_total_component"
        and item.replacement
        == "=('Balance Sheet'!C23+'Balance Sheet'!C24-'Balance Sheet'!C20"
        "-'Balance Sheet'!C22)-'Balance Sheet'!C9"
        for item in candidates
    )
    assert any(
        item.target == "'WACC'!D10"
        and item.kind == "average_exclude_subject"
        and item.replacement == "=AVERAGE(H9:M9)"
        for item in candidates
    )
    source["C23"] = 9999
    assert not any(
        item.kind == "double_count_total_component"
        for item in detect_debugging_repair_candidates(
            workbook, task_hint="Double Counting_input.xlsx", max_candidates=500
        )
    )


def test_double_counting_candidates_align_parallel_and_repeated_blocks() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Merger Model"
    worksheet["B21"] = "Premium Paid"
    worksheet["G21"] = "Premium Paid"
    worksheet["D21"] = "=D20-D18"
    worksheet["I21"] = "=I20-I18"
    worksheet["J21"] = "=H21+I21"
    worksheet["B25"] = "Break-up Fees"
    worksheet["D25"] = 1000
    worksheet["B29"] = "Value of NewCo equity"
    worksheet["G29"] = "Value of NewCo equity"
    worksheet["E29"] = "=+E18-E21+E23-E25-E26"
    worksheet["J29"] = "=J18+J23-J26"
    worksheet["B49"] = "Premium Paid"
    worksheet["D49"] = "=D48-D46"
    worksheet["B53"] = "Break-up Fees"
    worksheet["D53"] = 1000
    worksheet["B57"] = "Value of NewCo equity"
    worksheet["E57"] = "=+E46-E49+E51-E53-E54"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Double Counting_input.xlsx", max_candidates=500
    )
    observed = {
        (item.cell, item.kind, item.replacement)
        for item in candidates
        if item.kind == "double_count_parallel_block_term"
    }

    assert (
        "E29",
        "double_count_parallel_block_term",
        "=+E18+E23-E25-E26",
    ) in observed
    assert (
        "E57",
        "double_count_parallel_block_term",
        "=+E46+E51-E53-E54",
    ) in observed


def test_cross_sheet_candidates_use_matching_source_row_label() -> None:
    workbook = Workbook()
    target = workbook.active
    target.title = "DCF"
    source = workbook.create_sheet("Income Statement")
    target["B15"] = "EBITDA"
    target["C15"] = "='Income Statement'!J23"
    source["B23"] = "Revenue"
    source["B25"] = "EBITDA"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Cross Sheet References_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "C15"
        and item.kind == "cross_sheet_label_alignment"
        and item.replacement == "='Income Statement'!J25"
        for item in candidates
    )


def test_index_match_candidates_cover_exact_mode_and_source_whitespace() -> None:
    workbook = Workbook()
    model = workbook.active
    model.title = "DCF"
    source = workbook.create_sheet("Exhibit 5")
    source["A8"] = "  10 Years"
    model["C7"] = "=INDEX('Exhibit 5'!B:B,MATCH(\"10 Years\",'Exhibit 5'!A:A,1))"
    model["C8"] = "=INDEX('Exhibit 5'!B1:B12,MATCH(\"10 Years\",'Exhibit 5'!A1:A12,1))"
    model["C9"] = "=INDEX('Exhibit 5'!B1:B12,OpCase-1)"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Incorrect Index Match_input.xlsx", max_candidates=500
    )

    assert any(
        item.kind == "index_match_exact_mode" and item.replacement.endswith(",0))")
        for item in candidates
    )
    assert any(
        item.cell == "C8"
        and item.kind == "index_match_exact_mode"
        and item.replacement.endswith(",0))")
        for item in candidates
    )
    assert any(
        item.cell == "C9"
        and item.kind == "index_selector_offset"
        and item.replacement == "=INDEX('Exhibit 5'!B1:B12,OpCase)"
        for item in candidates
    )
    assert any(
        item.kind == "index_match_exact_label" and 'MATCH("  10 Years"' in item.replacement
        for item in candidates
    )


def test_relative_reference_candidates_anchor_drifting_series_to_first_peer() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "DCF"
    worksheet["G18"] = "=-G17*C8"
    worksheet["H18"] = "=-H17*D8"
    worksheet["I18"] = "=-I17*E8"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Relative vs Absolute References_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "I18"
        and item.kind == "relative_series_anchor"
        and item.replacement == "=-I17*$C$8"
        for item in candidates
    )


def test_relative_reference_candidates_release_overanchored_parallel_series() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "DCF"
    for column, year in zip("CDEFGH", range(1, 7), strict=True):
        worksheet[f"{column}97"] = year
        worksheet[f"{column}98"] = 0.1
        worksheet[f"{column}99"] = f"=(1+{column}98)^$C$97"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Relative vs Absolute References_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "D99"
        and item.kind == "relative_release_series_anchor"
        and item.replacement == "=(1+D98)^D$97"
        for item in candidates
    )


def test_unit_mismatch_candidates_cover_percent_days_and_growth() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Model"
    worksheet["B4"] = "Tax Rate"
    worksheet["C4"] = 5.3
    worksheet["C4"].number_format = "0.0%"
    worksheet["B8"] = "Accounts Receivable Days"
    worksheet["J8"] = "=(J7*J6)/12"
    worksheet["B14"] = "Revenue Growth"
    worksheet["D14"] = "=D13/C13"

    candidates = detect_debugging_repair_candidates(
        workbook, task_hint="Unit Mismatch_input.xlsx", max_candidates=500
    )

    assert any(
        item.cell == "C4" and item.kind == "unit_percent_scale" and item.replacement == "=5.3/100"
        for item in candidates
    )
    assert any(
        item.cell == "J8" and item.kind == "unit_days_per_year" and "/365" in item.replacement
        for item in candidates
    )
    assert any(
        item.cell == "D14" and item.kind == "unit_growth_rate" and item.replacement.endswith("-1")
        for item in candidates
    )
