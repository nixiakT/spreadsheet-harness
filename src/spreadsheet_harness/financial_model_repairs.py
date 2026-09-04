"""High-confidence semantic completions for common financial-model schedules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openpyxl.cell.cell import MergedCell
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string, get_column_letter

from .openpyxl_compat import load_workbook, repair_workbook_archive_in_place

_SAME_COLUMN_SUM_FORMULA = re.compile(
    r"=SUM\(\$?(?P<column>[A-Z]{1,3})\$?(?P<start>\d+):"
    r"\$?(?P=column)\$?(?P<end>\d+)\)",
    flags=re.IGNORECASE,
)
_DIRECT_SHEET_REFERENCE = re.compile(
    r"^=\+?(?P<quote>'?)(?P<sheet>[^'!]+)(?P=quote)!"
    r"\$?(?P<column>[A-Z]{1,3})\$?(?P<row>\d+)$",
    flags=re.IGNORECASE,
)
_SUMIF_SELF_ROW_RANGE = re.compile(
    r"=SUMIF\([^,]+,[^,]+,\$?(?P<start>[A-Z]{1,3})\$?(?P<row>\d+):"
    r"\$?(?P<end>[A-Z]{1,3})\$?(?P=row)\)",
    flags=re.IGNORECASE,
)


def _label(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _label_tokens(value: Any) -> tuple[str, ...]:
    stopwords = {
        "a",
        "an",
        "and",
        "at",
        "by",
        "cumulative",
        "deal",
        "deals",
        "first",
        "for",
        "from",
        "in",
        "into",
        "of",
        "per",
        "sheet",
        "tab",
        "the",
        "to",
    }
    return tuple(
        token.removesuffix("s")
        for token in _label(value).split()
        if token not in stopwords and len(token) >= 2
    )


def _specific_label_match(reference: Any, candidate: Any, *, minimum_overlap: int = 1) -> bool:
    normalized_reference = _label(reference)
    normalized_candidate = _label(candidate)
    reference_words = set(normalized_reference.split())
    candidate_words = set(normalized_candidate.split())
    for ordinal in ("first", "second", "third"):
        if ordinal in reference_words and ordinal not in candidate_words:
            return False
    return _labels_match(reference, candidate, minimum_overlap=minimum_overlap)


def _labels_match(reference: Any, candidate: Any, *, minimum_overlap: int = 1) -> bool:
    reference_tokens = set(_label_tokens(reference))
    candidate_tokens = set(_label_tokens(candidate))
    if not reference_tokens or not candidate_tokens:
        return False
    overlap = len(reference_tokens & candidate_tokens)
    return overlap >= min(minimum_overlap, len(reference_tokens), len(candidate_tokens))


def _raw_row_label(worksheet: Any, row: int, *, before_column: int = 8) -> str:
    for column in range(min(before_column, worksheet.max_column), 0, -1):
        value = worksheet.cell(row, column).value
        if value is not None and not (isinstance(value, str) and value.startswith("=")):
            return str(value).strip()
    return ""


def _cell_is_writable(cell: Any) -> bool:
    return not isinstance(cell, MergedCell)


def _row_has_text_label(
    worksheet: Any,
    row: int,
    hint: str,
    *,
    minimum_overlap: int = 1,
    max_column: int = 8,
) -> bool:
    return any(
        isinstance((value := worksheet.cell(row, column).value), str)
        and not value.startswith("=")
        and _labels_match(hint, value, minimum_overlap=minimum_overlap)
        for column in range(1, min(int(worksheet.max_column or 0), max_column) + 1)
    )


def _find_sheet(workbook: Any, hint: str) -> Any | None:
    normalized_hint = _label(hint).removesuffix(" sheet").removesuffix(" tab")
    candidates = [
        worksheet
        for worksheet in workbook.worksheets
        if _label(worksheet.title) == normalized_hint
        or _label(worksheet.title).removesuffix(" sheet") == normalized_hint
        or normalized_hint in _label(worksheet.title)
    ]
    return candidates[0] if candidates else None


def _find_row_by_label(worksheet: Any, hint: str) -> int | None:
    best: tuple[int, int] | None = None
    hint_tokens = set(_label_tokens(hint))
    if not hint_tokens:
        return None
    for row in range(1, int(worksheet.max_row or 0) + 1):
        text_candidates = [
            str(value).strip()
            for column in range(1, min(int(worksheet.max_column or 0), 8) + 1)
            if isinstance((value := worksheet.cell(row, column).value), str)
            and not value.startswith("=")
            and str(value).strip()
        ]
        if not text_candidates:
            label = _raw_row_label(worksheet, row)
            text_candidates = [label] if label else []
        for label in dict.fromkeys(text_candidates):
            candidate_tokens = set(_label_tokens(label))
            score = len(hint_tokens & candidate_tokens)
            if score <= 0:
                continue
            bonus = 1 if _label(label) == _label(hint) else 0
            ranked = (score + bonus, -row)
            if best is None or ranked > best:
                best = ranked
    return -best[1] if best is not None else None


def _instruction_sheet_clauses(instruction: str) -> tuple[tuple[str, str], ...]:
    compact_instruction = re.sub(r"\s+", " ", instruction.strip())
    clauses: list[tuple[str, str]] = []
    for fragment in re.split(r"(?i)\bin the\b", compact_instruction):
        fragment = fragment.strip(" .")
        if not fragment or "," not in fragment:
            continue
        raw_sheet, body = fragment.split(",", 1)
        sheet = re.sub(r"\bsheet\b$", "", raw_sheet, flags=re.IGNORECASE).strip(" .")
        body = body.strip(" .")
        if sheet and body:
            clauses.append((sheet, body))
    return tuple(clauses)


def _instruction_ticket_size_hints(
    instruction: str,
) -> tuple[tuple[str, tuple[tuple[str, float], ...]], ...]:
    hints: list[tuple[str, tuple[tuple[str, float], ...]]] = []
    tranche_labels = ("first tranche", "second tranche", "third tranche")
    for sheet, body in _instruction_sheet_clauses(instruction):
        if "ticket size per investment" not in _label(body):
            continue
        values = [float(raw) for raw in re.findall(r"\d+(?:\.\d+)?", body)]
        if len(values) < len(tranche_labels):
            continue
        hints.append((sheet, tuple(zip(tranche_labels, values[: len(tranche_labels)], strict=True))))
    return tuple(hints)


def _sheet_literal(sheet_name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_]+", sheet_name):
        return sheet_name
    return f"'{sheet_name}'"


def _is_subtotal_like_label(label: Any) -> bool:
    normalized = _label(label)
    return any(
        marker in normalized
        for marker in ("subtotal", "total", "ebit", "income", "profit", "cash flow", "expense")
    )


def _next_blank_value_column(
    worksheet: Any,
    source_sheet: Any,
    *,
    start_row: int,
    count: int,
    anchor_column: int,
) -> int | None:
    for column in range(anchor_column + 1, min(anchor_column + 5, worksheet.max_column) + 1):
        if any(
            worksheet.cell(start_row + offset, column).value is not None
            or source_sheet.cell(start_row + offset, column).value is not None
            for offset in range(count)
        ):
            continue
        support_column = column - 1
        support_values = [
            worksheet.cell(start_row + offset, support_column).value for offset in range(count)
        ]
        if sum(value is not None for value in support_values) >= max(1, count - 1):
            return column
    return None


def _matches_year_run(worksheet: Any, start_row: int, year_column: int, years: list[int]) -> bool:
    for index, year in enumerate(years):
        cell = worksheet.cell(start_row + index, year_column)
        value = cell.value
        if isinstance(value, (int, float)) and int(value) == year:
            continue
        if isinstance(value, str):
            compact = re.sub(r"\s+", "", value).upper()
            previous = f"{get_column_letter(year_column)}{start_row + index - 1}"
            if index > 0 and compact == f"={previous.upper()}+1":
                continue
            if compact == str(year):
                continue
        return False
    return True


def _matches_numbered_label_run(
    worksheet: Any,
    *,
    start_row: int,
    label_column: int,
    count: int,
    label_hint: str,
) -> bool:
    for index in range(count):
        label = worksheet.cell(start_row + index, label_column).value
        if not _labels_match(label_hint, label, minimum_overlap=2):
            return False
        if not re.search(rf"(?:^|[^0-9]){index + 1}(?:[^0-9]|$)", str(label or "")):
            return False
    return True


def _fill_constant_series_from_instruction(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    percentage_clauses = re.finditer(
        r"insert\s+(?P<label>[^,.]+?)\s+at\s+(?P<value>\d+(?:\.\d+)?)%\s+for\s+"
        r"(?P<start>\d{4})\s*[–-]\s*(?P<end>\d{4})",
        instruction,
        flags=re.IGNORECASE,
    )
    for match in percentage_clauses:
        label_hint = match.group("label")
        years = list(range(int(match.group("start")), int(match.group("end")) + 1))
        literal = float(match.group("value")) / 100.0
        for worksheet in output.worksheets:
            if worksheet.title not in source.sheetnames:
                continue
            source_sheet = source[worksheet.title]
            for row in range(2, max(int(worksheet.max_row or 0) - len(years) + 2, 2)):
                for column in range(1, int(worksheet.max_column or 0) + 1):
                    if not _matches_year_run(worksheet, row, column, years):
                        continue
                    nearby_labels = [
                        worksheet.cell(row - 1, candidate_column).value
                        for candidate_column in range(max(1, column - 1), min(column + 2, worksheet.max_column) + 1)
                    ]
                    if not any(_labels_match(label_hint, candidate, minimum_overlap=2) for candidate in nearby_labels):
                        continue
                    target_column = _next_blank_value_column(
                        worksheet,
                        source_sheet,
                        start_row=row,
                        count=len(years),
                        anchor_column=column,
                    )
                    if target_column is None:
                        continue
                    if any(
                        worksheet.cell(row + offset, target_column).value is not None
                        or source_sheet.cell(row + offset, target_column).value is not None
                        for offset in range(len(years))
                    ):
                        continue
                    for offset in range(len(years)):
                        target = worksheet.cell(row + offset, target_column)
                        if not _cell_is_writable(target):
                            continue
                        target.value = literal
                        changes.append(
                            {
                                "sheet": worksheet.title,
                                "target": target.coordinate,
                                "value": str(literal),
                            }
                        )
                    break
                else:
                    continue
                break

    counted_clauses = re.finditer(
        r"insert\s+(?P<label>[^,.]+?)\s+at\s+(?P<value>\d+(?:\.\d+)?)\s+per\s+\w+\s+for\s+"
        r"(?P<count>\d+)\s+(?:cumulative\s+)?(?:deals?|items?|investments?)",
        instruction,
        flags=re.IGNORECASE,
    )
    for match in counted_clauses:
        label_hint = match.group("label")
        literal = float(match.group("value"))
        count = int(match.group("count"))
        for worksheet in output.worksheets:
            if worksheet.title not in source.sheetnames:
                continue
            source_sheet = source[worksheet.title]
            for row in range(1, max(int(worksheet.max_row or 0) - count + 2, 1)):
                for column in range(1, min(int(worksheet.max_column or 0), 8) + 1):
                    if not _matches_numbered_label_run(
                        worksheet,
                        start_row=row,
                        label_column=column,
                        count=count,
                        label_hint=label_hint,
                    ):
                        continue
                    target_column = _next_blank_value_column(
                        worksheet,
                        source_sheet,
                        start_row=row,
                        count=count,
                        anchor_column=column,
                    )
                    if target_column is None:
                        continue
                    for offset in range(count):
                        target = worksheet.cell(row + offset, target_column)
                        if not _cell_is_writable(target):
                            continue
                        target.value = int(literal) if literal.is_integer() else literal
                        changes.append(
                            {
                                "sheet": worksheet.title,
                                "target": target.coordinate,
                                "value": str(target.value),
                            }
                        )
                    break
                else:
                    continue
                break
    return changes


def _instruction_sheet_metric_hints(instruction: str) -> tuple[tuple[str, str], ...]:
    hints: list[tuple[str, str]] = []
    for sheet, body in _instruction_sheet_clauses(instruction):
        match = re.search(
            r"(?:compute|calculate)\s+(?P<label>.+?)(?: from| at| for| using| based|\.|,|$)",
            body,
            flags=re.IGNORECASE,
        )
        if match is not None:
            label = match.group("label").strip(" ,.")
            if label:
                hints.append((sheet, label))
            continue
        match = re.search(r"link\s+(?P<label>.+?)\s+from", body, flags=re.IGNORECASE)
        if match is not None:
            label = match.group("label").strip(" ,.")
            if label:
                hints.append((sheet, label))
            continue
        match = re.search(r"link\s+(?P<label>.+?)(?: using|,|\.|$)", body, flags=re.IGNORECASE)
        if match is not None:
            label = match.group("label").strip(" ,.")
            if label:
                hints.append((sheet, label))
    return tuple(dict.fromkeys(hints))


def _instruction_link_hints(instruction: str) -> tuple[tuple[str, str, str], ...]:
    hints: list[tuple[str, str, str]] = []
    for sheet, body in _instruction_sheet_clauses(instruction):
        matched = re.search(
            r"link\s+(?P<label>.+?)\s+from\s+(?P<source>.+?)(?: including|,|\.|$)",
            body,
            flags=re.IGNORECASE,
        )
        if matched is None:
            continue
        hints.append(
            (
                sheet,
                matched.group("label").strip(" ,."),
                matched.group("source").strip(" ,."),
            )
        )
    return tuple(dict.fromkeys(hints))


def _continue_financial_metric_formula_runs(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    metric_hints = _instruction_sheet_metric_hints(instruction)
    if not metric_hints:
        return changes
    for sheet_hint, label_hint in metric_hints:
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        source_sheet = source[worksheet.title]
        minimum_overlap = 2 if len(set(_label_tokens(label_hint))) > 1 else 1
        for row in range(1, int(worksheet.max_row or 0) + 1):
            if not _row_has_text_label(
                worksheet,
                row,
                label_hint,
                minimum_overlap=minimum_overlap,
                max_column=8,
            ):
                row_label = _raw_row_label(worksheet, row)
                if not row_label or not _labels_match(
                    label_hint,
                    row_label,
                    minimum_overlap=minimum_overlap,
                ):
                    continue
            seed_column = None
            seed_formula = None
            for column in range(1, int(worksheet.max_column or 0) + 1):
                value = worksheet.cell(row, column).value
                if not (isinstance(value, str) and value.startswith("=")):
                    continue
                if str(row) in re.findall(r"\d+", value):
                    continue
                if (
                    worksheet.cell(row, column + 1).value is None
                    and source_sheet.cell(row, column + 1).value is None
                ):
                    try:
                        translated = Translator(
                            value,
                            origin=f"{get_column_letter(column)}{row}",
                        ).translate_formula(f"{get_column_letter(column + 1)}{row}")
                    except (TypeError, ValueError):
                        continue
                    if f"{get_column_letter(column + 1)}{row}" in translated:
                        continue
                    seed_column = column
                    seed_formula = value
            if seed_column is None or seed_formula is None:
                continue
            started = False
            stalled = 0
            for column in range(seed_column + 1, int(worksheet.max_column or 0) + 1):
                target = worksheet.cell(row, column)
                if target.value is not None or source_sheet.cell(row, column).value is not None:
                    if started:
                        stalled += 1
                    if started and stalled >= 3:
                        break
                    continue
                if not _cell_is_writable(target):
                    if started:
                        stalled += 1
                    if started and stalled >= 3:
                        break
                    continue
                try:
                    translated = Translator(
                        seed_formula,
                        origin=f"{get_column_letter(seed_column)}{row}",
                    ).translate_formula(target.coordinate)
                except (TypeError, ValueError):
                    break
                if target.coordinate in translated:
                    if started:
                        break
                    continue
                support_refs = {
                    (
                        column_index_from_string(match.group("column")),
                        int(match.group("row")),
                    )
                    for match in re.finditer(
                        r"(?:'[^']+'!)?\$?(?P<column>[A-Z]{1,3})\$?(?P<row>\d+)",
                        translated,
                        flags=re.IGNORECASE,
                    )
                }
                local_support = [
                    (ref_column, ref_row)
                    for ref_column, ref_row in support_refs
                    if 1 <= ref_row <= int(worksheet.max_row or 0)
                    and 1 <= ref_column <= int(worksheet.max_column or 0)
                ]
                required_support = 1 if len(local_support) <= 1 else 2
                populated_support = sum(
                    worksheet.cell(ref_row, ref_column).value is not None
                    for ref_column, ref_row in local_support
                )
                nearby_support = sum(
                    worksheet.cell(reference_row, column).value is not None
                    for reference_row in range(
                        max(1, row - 60),
                        min(int(worksheet.max_row or 0), row + 60) + 1,
                    )
                    if reference_row != row
                )
                allow_nearby_support = len(local_support) <= 2
                if populated_support < required_support and (
                    not allow_nearby_support or nearby_support < 2
                ):
                    if started:
                        stalled += 1
                    if started and stalled >= 3:
                        break
                    continue
                target.value = translated
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                        "formula": translated,
                    }
                )
                started = True
                stalled = 0
    return changes


def _fill_instruction_ticket_size_and_exit_value_formulas(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    if "investment exit value" not in _label(instruction):
        return changes
    for sheet_hint, tranche_hints in _instruction_ticket_size_hints(instruction):
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        source_sheet = source[worksheet.title]
        ticket_heading_row = _find_row_by_label(worksheet, "Ticket Size per Investment")
        ticket_cells: list[tuple[int, int]] = []
        for label_hint, literal in tranche_hints:
            row = next(
                (
                    candidate_row
                    for candidate_row in range(
                        (ticket_heading_row or 0) + 1,
                        min(int(worksheet.max_row or 0), (ticket_heading_row or 0) + 6) + 1,
                    )
                    if any(
                        isinstance((value := worksheet.cell(candidate_row, column).value), str)
                        and not value.startswith("=")
                        and _specific_label_match(label_hint, value, minimum_overlap=2)
                        for column in range(1, min(int(worksheet.max_column or 0), 8) + 1)
                    )
                ),
                None,
            ) if ticket_heading_row is not None else None
            if row is None:
                row = _find_row_by_label(worksheet, label_hint)
            if row is None:
                ticket_cells = []
                break
            label_column = next(
                (
                    column
                    for column in range(1, int(worksheet.max_column or 0) + 1)
                    if _specific_label_match(
                        label_hint,
                        worksheet.cell(row, column).value,
                        minimum_overlap=2,
                    )
                ),
                None,
            )
            if label_column is None:
                ticket_cells = []
                break
            target = None
            for candidate_column in range(
                label_column + 1, min(label_column + 4, int(worksheet.max_column or 0)) + 1
            ):
                candidate = worksheet.cell(row, candidate_column)
                if not _cell_is_writable(candidate):
                    continue
                if (
                    candidate.value is None
                    and source_sheet.cell(row, candidate_column).value is None
                ):
                    target = candidate
                    break
                if target is None and candidate.value is not None:
                    target = candidate
            if target is None:
                ticket_cells = []
                break
            ticket_cells.append((row, int(target.column)))
            if target.value is None and source_sheet.cell(row, int(target.column)).value is None:
                target.value = int(literal) if literal.is_integer() else literal
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                        "value": str(target.value),
                    }
                )
        if len(ticket_cells) != len(tranche_hints):
            continue

        exit_header_row = _find_row_by_label(worksheet, "Investment Exit Value")
        if exit_header_row is None or exit_header_row + 2 > int(worksheet.max_row or 0):
            continue
        header_row = exit_header_row + 1
        tranche_pairs = [
            (column, column + 1)
            for column in range(1, int(worksheet.max_column or 0))
            if _label(worksheet.cell(header_row, column).value) == "month"
            and _label(worksheet.cell(header_row, column + 1).value) in {"number", "numbers"}
        ]
        if len(tranche_pairs) < len(ticket_cells):
            continue
        count_start_row = next(
            (
                row
                for row in range(1, exit_header_row)
                if _row_has_text_label(
                    worksheet,
                    row,
                    "Investment Number 1",
                    minimum_overlap=2,
                    max_column=4,
                )
            ),
            None,
        )
        target_start_row = next(
            (
                row
                for row in range(exit_header_row + 1, int(worksheet.max_row or 0) + 1)
                if _row_has_text_label(
                    worksheet,
                    row,
                    "Investment Number 1",
                    minimum_overlap=2,
                    max_column=4,
                )
            ),
            None,
        )
        if count_start_row is None or target_start_row is None:
            continue
        first_month_value = worksheet.cell(target_start_row, tranche_pairs[0][0]).value
        if not isinstance(first_month_value, str):
            continue
        first_month_match = re.fullmatch(
            r"=\+?\$?(?P<column>[A-Z]{1,3})\$?(?P<row>\d+)",
            re.sub(r"\s+", "", first_month_value),
            flags=re.IGNORECASE,
        )
        if first_month_match is None:
            continue
        source_start_row = int(first_month_match.group("row"))
        if (
            column_index_from_string(first_month_match.group("column"))
            != tranche_pairs[0][0]
        ):
            continue

        for offset in range(0, int(worksheet.max_row or 0) - target_start_row + 1):
            target_row = target_start_row + offset
            if not any(
                isinstance(worksheet.cell(target_row, column).value, str)
                and not str(worksheet.cell(target_row, column).value).startswith("=")
                for column in range(1, min(int(worksheet.max_column or 0), 4) + 1)
            ):
                if offset > 0:
                    break
                continue
            if not _row_has_text_label(
                worksheet,
                target_row,
                "Investment Number",
                minimum_overlap=2,
                max_column=4,
            ):
                break
            count_row = count_start_row + offset
            source_row = source_start_row + offset
            for (ticket_row, ticket_col), (_month_col, value_col) in zip(
                ticket_cells,
                tranche_pairs,
                strict=True,
            ):
                target = worksheet.cell(target_row, value_col)
                if (
                    target.value is not None
                    or source_sheet.cell(target_row, value_col).value is not None
                    or not _cell_is_writable(target)
                ):
                    continue
                if (
                    worksheet.cell(count_row, value_col).value is None
                    or worksheet.cell(source_row, value_col).value is None
                ):
                    continue
                formula = (
                    f"={get_column_letter(value_col)}{source_row}"
                    f"*{get_column_letter(value_col)}{count_row}"
                    f"*${get_column_letter(ticket_col)}${ticket_row}"
                )
                target.value = formula
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                        "formula": formula,
                    }
                )
    return changes


def _fill_instruction_cumulative_position_rows(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for sheet_hint, label_hint in _instruction_sheet_metric_hints(instruction):
        if "cumulative position" not in _label(label_hint):
            continue
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        source_sheet = source[worksheet.title]
        for row in range(1, int(worksheet.max_row or 0) + 1):
            monthly_bounds: tuple[int, int] | None = None
            anchor_columns: list[int] = []
            for column in range(1, min(int(worksheet.max_column or 0), 24) + 1):
                value = worksheet.cell(row, column).value
                if not isinstance(value, str):
                    continue
                matched = _SUMIF_SELF_ROW_RANGE.fullmatch(re.sub(r"\s+", "", value))
                if matched is None or int(matched.group("row")) != row:
                    continue
                bounds = (
                    column_index_from_string(matched.group("start")),
                    column_index_from_string(matched.group("end")),
                )
                if monthly_bounds is None:
                    monthly_bounds = bounds
                elif monthly_bounds != bounds:
                    monthly_bounds = None
                    break
                anchor_columns.append(column)
            if monthly_bounds is None or len(anchor_columns) < 3:
                continue
            monthly_start, monthly_end = monthly_bounds
            detail_end = row - 1
            detail_start = detail_end
            while detail_start >= 1 and any(
                worksheet.cell(detail_start, column).value is not None
                for column in range(monthly_start, min(monthly_end, monthly_start + 12) + 1)
            ):
                detail_start -= 1
            detail_start += 1
            if not (2 <= detail_end - detail_start + 1 <= 16):
                continue
            for column in range(monthly_start, monthly_end + 1):
                target = worksheet.cell(row, column)
                if target.value is not None or source_sheet.cell(row, column).value is not None:
                    continue
                if not _cell_is_writable(target):
                    continue
                populated = sum(
                    worksheet.cell(detail_row, column).value is not None
                    for detail_row in range(detail_start, detail_end + 1)
                )
                if populated < 2:
                    continue
                formula = (
                    f"=SUM({get_column_letter(column)}{detail_start}:"
                    f"{get_column_letter(column)}{detail_end})"
                )
                target.value = formula
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                        "formula": formula,
                    }
                )
    return changes


def _fill_instruction_annual_rollup_columns(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for sheet_hint, label_hint in _instruction_sheet_metric_hints(instruction):
        normalized_hint = _label(label_hint)
        if "y1" not in normalized_hint and "total" not in normalized_hint:
            continue
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        source_sheet = source[worksheet.title]
        header_row = None
        annual_columns: list[tuple[int, int, int]] = []
        for candidate_row in range(1, min(int(worksheet.max_row or 0), 12) + 1):
            matches: list[tuple[int, int, int]] = []
            for column in range(2, int(worksheet.max_column or 0) + 1):
                header = _label(worksheet.cell(candidate_row, column).value)
                if not re.fullmatch(r"y\d+", header):
                    continue
                left_column = column - 1
                left_header = _label(worksheet.cell(candidate_row, left_column).value)
                if not re.fullmatch(r"[mq]\d+", left_header):
                    continue
                prefix = left_header[0]
                start_column = left_column
                while start_column > 1 and re.fullmatch(
                    rf"{prefix}\d+",
                    _label(worksheet.cell(candidate_row, start_column - 1).value),
                ):
                    start_column -= 1
                span = left_column - start_column + 1
                if span in {4, 12, 13}:
                    matches.append((column, start_column, left_column))
            if matches:
                header_row = candidate_row
                annual_columns = matches
                break
        if header_row is None:
            continue
        detail_bounds_by_column: dict[int, tuple[int, int]] = {}
        shared_detail_bounds: tuple[int, int] | None = None
        for annual_column, _block_start, _block_end in annual_columns:
            for row in range(header_row + 1, int(worksheet.max_row or 0) + 1):
                value = worksheet.cell(row, annual_column).value
                if not isinstance(value, str):
                    continue
                matched = _SAME_COLUMN_SUM_FORMULA.fullmatch(re.sub(r"\s+", "", value))
                if matched is None:
                    continue
                if column_index_from_string(matched.group("column")) != annual_column:
                    continue
                detail_bounds = (int(matched.group("start")), int(matched.group("end")))
                detail_bounds_by_column[annual_column] = detail_bounds
                if shared_detail_bounds is None:
                    shared_detail_bounds = detail_bounds
                break
        if shared_detail_bounds is None:
            continue
        for annual_column, block_start, block_end in annual_columns:
            detail_bounds = detail_bounds_by_column.get(annual_column, shared_detail_bounds)
            detail_start, detail_end = detail_bounds
            for row in range(detail_start, detail_end + 1):
                target = worksheet.cell(row, annual_column)
                if target.value is not None or source_sheet.cell(row, annual_column).value is not None:
                    continue
                if not _cell_is_writable(target):
                    continue
                row_label = _raw_row_label(worksheet, row, before_column=max(3, block_start - 1))
                if (
                    not row_label
                    and not any(
                        worksheet.cell(row, component_column).value is not None
                        for component_column in range(block_start, block_end + 1)
                    )
                    and not any(
                        worksheet.cell(row, peer_annual_column).value is not None
                        for peer_annual_column, _peer_start, _peer_end in annual_columns
                        if peer_annual_column != annual_column
                    )
                ):
                    continue
                formula = (
                    f"=SUM({get_column_letter(block_start)}{row}:"
                    f"{get_column_letter(block_end)}{row})"
                )
                target.value = formula
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                        "formula": formula,
                    }
                )
            for row in range(detail_end + 1, min(detail_end + 3, int(worksheet.max_row or 0)) + 1):
                target = worksheet.cell(row, annual_column)
                if target.value is not None or source_sheet.cell(row, annual_column).value is not None:
                    continue
                if not _cell_is_writable(target):
                    continue
                row_label = _raw_row_label(worksheet, row, before_column=max(3, block_start - 1))
                if not row_label or not _is_subtotal_like_label(row_label):
                    continue
                formula = (
                    f"=SUM({get_column_letter(annual_column)}{detail_start}:"
                    f"{get_column_letter(annual_column)}{detail_end})"
                )
                target.value = formula
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                        "formula": formula,
                    }
                )
    return changes


def _link_instruction_metric_rows(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for destination_hint, label_hint, source_hint in _instruction_link_hints(instruction):
        destination_sheet = _find_sheet(output, destination_hint)
        source_sheet = _find_sheet(output, source_hint)
        if destination_sheet is None or source_sheet is None:
            continue
        source_original = source[source_sheet.title]
        destination_original = source[destination_sheet.title]
        source_row = _find_row_by_label(source_sheet, label_hint)
        destination_row = _find_row_by_label(destination_sheet, label_hint)
        if source_row is None or destination_row is None:
            continue
        mapped_columns: list[tuple[int, int]] = []
        for peer_row in range(max(1, destination_row - 3), min(destination_sheet.max_row, destination_row + 3) + 1):
            if peer_row == destination_row:
                continue
            direct_refs: list[tuple[int, int]] = []
            for column in range(1, int(destination_sheet.max_column or 0) + 1):
                formula = destination_sheet.cell(peer_row, column).value
                if not isinstance(formula, str):
                    continue
                matched = _DIRECT_SHEET_REFERENCE.match(formula.strip())
                if matched is None or _label(matched.group("sheet")) != _label(source_sheet.title):
                    continue
                direct_refs.append(
                    (column, column_index_from_string(matched.group("column")))
                )
            if len(direct_refs) < 3:
                continue
            offsets = {source_column - destination_column for destination_column, source_column in direct_refs}
            if len(offsets) != 1:
                continue
            direct_refs.sort()
            if any(
                right_destination != left_destination + 1 or right_source != left_source + 1
                for (left_destination, left_source), (right_destination, right_source) in zip(
                    direct_refs,
                    direct_refs[1:],
                )
            ):
                continue
            mapped_columns = direct_refs
            break
        if not mapped_columns:
            continue
        for destination_column, source_column in mapped_columns:
            target = destination_sheet.cell(destination_row, destination_column)
            if target.value is not None or destination_original.cell(destination_row, destination_column).value is not None:
                continue
            if not _cell_is_writable(target):
                continue
            if source_original.cell(source_row, source_column).value is None and source_sheet.cell(source_row, source_column).value is None:
                continue
            target.value = f"={_sheet_literal(source_sheet.title)}!{get_column_letter(source_column)}{source_row}"
            changes.append(
                {
                    "sheet": destination_sheet.title,
                    "target": target.coordinate,
                    "formula": str(target.value),
                }
            )
    return changes


def _link_instruction_header_columns(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for destination_hint, label_hint, source_hint in _instruction_link_hints(instruction):
        destination_sheet = _find_sheet(output, destination_hint)
        source_sheet = _find_sheet(output, source_hint)
        if destination_sheet is None or source_sheet is None:
            continue
        destination_original = source[destination_sheet.title]
        source_row = _find_row_by_label(source_sheet, label_hint)
        if source_row is None:
            continue
        source_label_column = next(
            (
                column
                for column in range(1, int(source_sheet.max_column or 0) + 1)
                if _labels_match(label_hint, source_sheet.cell(source_row, column).value, minimum_overlap=2)
            ),
            None,
        )
        if source_label_column is None:
            continue
        source_value_column = next(
            (
                column
                for column in range(
                    source_label_column + 1,
                    min(source_label_column + 4, int(source_sheet.max_column or 0)) + 1,
                )
                if source_sheet.cell(source_row, column).value is not None
            ),
            None,
        )
        if source_value_column is None:
            continue
        destination_header = next(
            (
                (row, column)
                for row in range(1, min(int(destination_sheet.max_row or 0), 12) + 1)
                for column in range(1, int(destination_sheet.max_column or 0) + 1)
                if _labels_match(
                    label_hint,
                    destination_sheet.cell(row, column).value,
                    minimum_overlap=2,
                )
            ),
            None,
        )
        if destination_header is None:
            continue
        header_row, header_column = destination_header
        formula = (
            f"={_sheet_literal(source_sheet.title)}!"
            f"${get_column_letter(source_value_column)}${source_row}"
        )
        for row in range(header_row + 1, int(destination_sheet.max_row or 0) + 1):
            target = destination_sheet.cell(row, header_column)
            if (
                target.value is not None
                or destination_original.cell(row, header_column).value is not None
                or not _cell_is_writable(target)
            ):
                continue
            row_label = _raw_row_label(destination_sheet, row, before_column=max(4, header_column - 1))
            row_signal_count = sum(
                destination_sheet.cell(row, column).value is not None
                for column in range(1, int(destination_sheet.max_column or 0) + 1)
                if column != header_column
            )
            leading_signal = any(
                destination_sheet.cell(row, column).value is not None
                for column in range(1, max(2, header_column - 3))
            )
            if not row_label and (row_signal_count < 2 or not leading_signal):
                continue
            if not any(
                destination_sheet.cell(row, column).value is not None
                for column in range(
                    max(1, header_column - 2),
                    min(int(destination_sheet.max_column or 0), header_column + 2) + 1,
                )
                if column != header_column
            ):
                continue
            target.value = formula
            changes.append(
                {
                    "sheet": destination_sheet.title,
                    "target": target.coordinate,
                    "formula": formula,
                }
            )
    return changes


def _apply_instruction_freeze_panes(output: Any, instruction: str) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for sheet, body in _instruction_sheet_clauses(instruction):
        match = re.search(
            r"freeze rows (?P<row_start>\d+)\s*[–-]\s*(?P<row_end>\d+)\s+and columns "
            r"(?P<column_start>[A-Z]{1,3})\s*[–-]\s*(?P<column_end>[A-Z]{1,3})",
            body,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        worksheet = _find_sheet(output, sheet)
        if worksheet is None:
            continue
        anchor_column = get_column_letter(column_index_from_string(match.group("column_end")) + 1)
        anchor_row = int(match.group("row_end")) + 1
        anchor = f"{anchor_column}{anchor_row}"
        if worksheet.freeze_panes == anchor:
            continue
        if worksheet.max_row < anchor_row or worksheet.max_column < column_index_from_string(
            match.group("column_end")
        ):
            worksheet[anchor] = worksheet[anchor].value
        worksheet.freeze_panes = anchor
        changes.append(
            {
                "sheet": worksheet.title,
                "target": anchor,
                "value": "freeze_panes",
            }
        )
    return changes


def complete_revenue_growth_schedule(path: str | Path) -> list[dict[str, str]]:
    """Fill a blank revenue-growth calculation block from explicit assumption rows."""

    workbook_path = Path(path)
    repair_workbook_archive_in_place(workbook_path)
    workbook = load_workbook(
        workbook_path,
        data_only=False,
        keep_vba=workbook_path.suffix.casefold() == ".xlsm",
    )
    changes: list[dict[str, str]] = []
    try:
        required = {
            "cpi growth rate": "cpi",
            "price growth over cpi": "spread",
            "population growth": "population",
            "market share beginning": "share_begin",
            "market share target growth": "share_target",
            "historical revenue year 0": "historical_revenue",
            "total price growth rate": "total_price",
            "market share ending": "share_end",
            "share growth rate": "share_growth",
            "volume growth rate": "volume_growth",
            "total revenue growth rate": "total_revenue_growth",
            "projected revenue": "projected_revenue",
        }
        for worksheet in workbook.worksheets:
            rows: dict[str, int] = {}
            for row in worksheet.iter_rows():
                for cell in row[:4]:
                    normalized = _label(cell.value)
                    for phrase, key in required.items():
                        if key not in rows and phrase in normalized:
                            rows[key] = cell.row
            if set(rows) != set(required.values()):
                continue

            year_columns: list[int] = []
            for row in worksheet.iter_rows(min_row=1, max_row=min(12, worksheet.max_row)):
                candidates = [
                    cell.column for cell in row if re.fullmatch(r"year\s+\d+", _label(cell.value))
                ]
                if len(candidates) >= 2:
                    year_columns = candidates
                    break
            if len(year_columns) < 2 or year_columns != list(
                range(year_columns[0], year_columns[-1] + 1)
            ):
                continue

            calculation_keys = (
                "total_price",
                "share_end",
                "share_growth",
                "volume_growth",
                "total_revenue_growth",
                "projected_revenue",
            )
            targets = [
                worksheet.cell(rows[key], column)
                for key in calculation_keys
                for column in year_columns
            ]
            targets.extend(
                worksheet.cell(rows["share_begin"], column) for column in year_columns[1:]
            )
            if any(cell.value is not None for cell in targets):
                continue
            first_column = year_columns[0]
            if worksheet.cell(rows["historical_revenue"], first_column).value is None:
                continue
            if worksheet.cell(rows["share_begin"], first_column).value is None:
                continue
            if any(
                worksheet.cell(rows[key], column).value is None
                for key in ("cpi", "spread", "population", "share_target")
                for column in year_columns
            ):
                continue

            for column in year_columns:
                letter = get_column_letter(column)
                previous_letter = get_column_letter(column - 1)
                if column != first_column:
                    beginning = worksheet.cell(rows["share_begin"], column)
                    if not _cell_is_writable(beginning):
                        continue
                    beginning.value = f"={previous_letter}{rows['share_end']}"
                    changes.append(
                        {
                            "sheet": worksheet.title,
                            "target": beginning.coordinate,
                            "formula": str(beginning.value),
                        }
                    )
                formulas = {
                    "total_price": f"={letter}{rows['cpi']}+{letter}{rows['spread']}",
                    "share_end": (
                        f"={letter}{rows['share_begin']}*(1+{letter}{rows['share_target']})"
                        if column == first_column
                        else f"={letter}{rows['share_begin']}*(1+{letter}{rows['share_target']})"
                    ),
                    "share_growth": (
                        f"={letter}{rows['share_end']}/{letter}{rows['share_begin']}-1"
                        if column == first_column
                        else f"={letter}{rows['share_end']}/{letter}{rows['share_begin']}-1"
                    ),
                    "volume_growth": f"=(1+{letter}{rows['population']})*(1+{letter}{rows['share_growth']})-1",
                    "total_revenue_growth": f"=(1+{letter}{rows['total_price']})*(1+{letter}{rows['volume_growth']})-1",
                    "projected_revenue": (
                        f"={letter}{rows['historical_revenue']}*(1+{letter}{rows['total_revenue_growth']})"
                        if column == first_column
                        else f"={previous_letter}{rows['projected_revenue']}*(1+{letter}{rows['total_revenue_growth']})"
                    ),
                }
                for key, formula in formulas.items():
                    cell = worksheet.cell(rows[key], column)
                    if not _cell_is_writable(cell):
                        continue
                    cell.value = formula
                    changes.append(
                        {"sheet": worksheet.title, "target": cell.coordinate, "formula": formula}
                    )
            break
        if changes:
            workbook.save(workbook_path)
    finally:
        workbook.close()
    return changes


def complete_isolated_formula_holes(
    path: str | Path,
    *,
    source_path: str | Path,
) -> list[dict[str, str]]:
    """Fill a blank bracketed by formulas that translate to one exact expression.

    This is deliberately narrow: the source and output must both be blank, immediately adjacent
    formulas must translate to the same target formula, all three cells must share a style, and
    the target row must contain other model content. It cannot invent a new schedule or fill a
    visual separator row.
    """

    workbook_path = Path(path)
    original_path = Path(source_path)
    repair_workbook_archive_in_place(workbook_path)
    repair_workbook_archive_in_place(original_path)
    output = load_workbook(
        workbook_path,
        data_only=False,
        keep_vba=workbook_path.suffix.casefold() == ".xlsm",
    )
    source = load_workbook(
        original_path,
        data_only=False,
        keep_vba=original_path.suffix.casefold() == ".xlsm",
    )
    changes: list[dict[str, str]] = []
    try:
        for worksheet in output.worksheets:
            if worksheet.title not in source.sheetnames or worksheet.max_row < 3:
                continue
            source_sheet = source[worksheet.title]
            proposals: list[tuple[Any, str]] = []
            for row in range(2, worksheet.max_row + 1):
                formula_count_in_row = sum(
                    isinstance(worksheet.cell(row, column).value, str)
                    and worksheet.cell(row, column).value.startswith("=")
                    for column in range(1, worksheet.max_column + 1)
                )
                if formula_count_in_row < 3:
                    continue
                for column in range(1, worksheet.max_column + 1):
                    target = worksheet.cell(row, column)
                    if target.value is not None or source_sheet.cell(row, column).value is not None:
                        continue
                    translated: set[str] = set()
                    above_row = row - 1
                    if above_row > 1 and all(
                        worksheet.cell(above_row, peer_column).value is None
                        for peer_column in range(1, worksheet.max_column + 1)
                    ):
                        above_row -= 1
                    if row < worksheet.max_row:
                        above = worksheet.cell(above_row, column)
                        below = worksheet.cell(row + 1, column)
                    else:
                        above = below = None
                    if (
                        above is not None
                        and below is not None
                        and isinstance(above.value, str)
                        and above.value.startswith("=")
                        and isinstance(below.value, str)
                        and below.value.startswith("=")
                        and above.style_id == target.style_id == below.style_id
                    ):
                        try:
                            from_above = Translator(
                                above.value, origin=above.coordinate
                            ).translate_formula(target.coordinate)
                            from_below = Translator(
                                below.value, origin=below.coordinate
                            ).translate_formula(target.coordinate)
                        except (TypeError, ValueError):
                            pass
                        else:
                            if from_above == from_below:
                                translated.add(from_above)
                    if 1 < column < worksheet.max_column:
                        left = worksheet.cell(row, column - 1)
                        right = worksheet.cell(row, column + 1)
                        if (
                            isinstance(left.value, str)
                            and left.value.startswith("=")
                            and isinstance(right.value, str)
                            and right.value.startswith("=")
                            and left.style_id == target.style_id == right.style_id
                        ):
                            try:
                                from_left = Translator(
                                    left.value, origin=left.coordinate
                                ).translate_formula(target.coordinate)
                                from_right = Translator(
                                    right.value, origin=right.coordinate
                                ).translate_formula(target.coordinate)
                            except (TypeError, ValueError):
                                pass
                            else:
                                if from_left == from_right:
                                    compact = re.sub(r"\s+", "", from_left)
                                    if (
                                        _SAME_COLUMN_SUM_FORMULA.fullmatch(compact)
                                        and not _is_subtotal_like_label(_raw_row_label(worksheet, row))
                                    ):
                                        continue
                                    translated.add(from_left)
                    if len(translated) == 1:
                        proposals.append((target, translated.pop()))
            for target, formula in proposals:
                if not _cell_is_writable(target):
                    continue
                target.value = formula
                changes.append(
                    {
                        "sheet": worksheet.title,
                        "target": target.coordinate,
                            "formula": formula,
                        }
                    )
            # Forecast schedules often leave a subtotal row blank even though the same row already
            # contains same-column SUM formulas for a parallel yearly block.  Reuse the declared
            # row bounds only where the source workbook is also blank and the candidate detail
            # column already contains model content.
            for row in range(1, int(worksheet.max_row or 0) + 1):
                sum_patterns: dict[tuple[int, int], set[int]] = {}
                for column in range(1, int(worksheet.max_column or 0) + 1):
                    value = worksheet.cell(row, column).value
                    if not isinstance(value, str):
                        continue
                    matched = _SAME_COLUMN_SUM_FORMULA.fullmatch(re.sub(r"\s+", "", value))
                    if matched is None:
                        continue
                    formula_column = column_index_from_string(matched.group("column"))
                    start_row = int(matched.group("start"))
                    end_row = int(matched.group("end"))
                    if formula_column != column or not (1 <= start_row <= end_row < row):
                        continue
                    sum_patterns.setdefault((start_row, end_row), set()).add(column)
                if not sum_patterns:
                    continue
                for (detail_start, detail_end), anchor_columns in sum_patterns.items():
                    if len(anchor_columns) < 2:
                        continue
                    for column in range(max(anchor_columns) + 1, int(worksheet.max_column or 0) + 1):
                        target = worksheet.cell(row, column)
                        if (
                            target.value is not None
                            or source_sheet.cell(row, column).value is not None
                            or not any(
                                worksheet.cell(detail_row, column).value is not None
                                for detail_row in range(detail_start, detail_end + 1)
                            )
                        ):
                            continue
                        if not _cell_is_writable(target):
                            continue
                        formula = (
                            f"=SUM({get_column_letter(column)}{detail_start}:"
                            f"{get_column_letter(column)}{detail_end})"
                        )
                        target.value = formula
                        changes.append(
                            {
                                "sheet": worksheet.title,
                                "target": target.coordinate,
                                "formula": formula,
                            }
                        )

            # Annual columns are often anchored by a subtotal such as
            # ``V30=SUM(R30:U30)`` while each quarterly component subtotal declares the detail
            # row bounds (for example ``R30=SUM(R6:R29)``). Use those two independent formula
            # shapes to complete omitted annual detail sums without relying on fixed columns.
            for annual_total in list(worksheet._cells.values()):
                annual_formula = annual_total.value
                if not isinstance(annual_formula, str):
                    continue
                annual_match = re.fullmatch(
                    r"=SUM\(\$?(?P<start>[A-Z]{1,3})\$?(?P<row>\d+):"
                    r"\$?(?P<end>[A-Z]{1,3})\$?(?P=row)\)",
                    annual_formula,
                    flags=re.IGNORECASE,
                )
                if annual_match is None or int(annual_match.group("row")) != annual_total.row:
                    continue
                start_column = worksheet[annual_match.group("start") + "1"].column
                end_column = worksheet[annual_match.group("end") + "1"].column
                if end_column - start_column + 1 not in {4, 12, 13}:
                    continue
                detail_bounds: set[tuple[int, int]] = set()
                for component_column in range(start_column, end_column + 1):
                    component_formula = worksheet.cell(annual_total.row, component_column).value
                    component_letter = get_column_letter(component_column)
                    if not isinstance(component_formula, str):
                        continue
                    component_match = re.fullmatch(
                        rf"=SUM\(\$?{component_letter}\$?(?P<start>\d+):"
                        rf"\$?{component_letter}\$?(?P<end>\d+)\)",
                        component_formula,
                        flags=re.IGNORECASE,
                    )
                    if component_match is not None:
                        detail_bounds.add(
                            (
                                int(component_match.group("start")),
                                int(component_match.group("end")),
                            )
                        )
                if len(detail_bounds) != 1:
                    continue
                detail_start, detail_end = next(iter(detail_bounds))
                if not (1 <= detail_start <= detail_end < annual_total.row):
                    continue
                start_letter = get_column_letter(start_column)
                end_letter = get_column_letter(end_column)
                for detail_row in range(detail_start, detail_end + 1):
                    target = worksheet.cell(detail_row, annual_total.column)
                    if (
                        target.value is not None
                        or source_sheet.cell(detail_row, annual_total.column).value is not None
                        or not any(
                            worksheet.cell(detail_row, component_column).value is not None
                            for component_column in range(start_column, end_column + 1)
                        )
                    ):
                        continue
                    if not _cell_is_writable(target):
                        continue
                    formula = f"=SUM({start_letter}{detail_row}:{end_letter}{detail_row})"
                    target.value = formula
                    changes.append(
                        {
                            "sheet": worksheet.title,
                            "target": target.coordinate,
                            "formula": formula,
                        }
                    )
        if changes:
            output.save(workbook_path)
    finally:
        source.close()
        output.close()
    return changes


def complete_financial_model_runtime_actions(
    path: str | Path,
    *,
    source_path: str | Path,
    instruction: str,
) -> list[dict[str, str]]:
    """Apply instruction-grounded Financial_Model repairs before the agent spends turns.

    These actions are intentionally narrower than free-form planning: they only fire when both
    the public instruction and the workbook structure agree on an exact constant series, a
    translated forecast-row continuation, a neighboring cross-sheet link pattern, or a freeze-pane
    target.
    """

    workbook_path = Path(path)
    original_path = Path(source_path)
    repair_workbook_archive_in_place(workbook_path)
    repair_workbook_archive_in_place(original_path)
    output = load_workbook(
        workbook_path,
        data_only=False,
        keep_vba=workbook_path.suffix.casefold() == ".xlsm",
    )
    source = load_workbook(
        original_path,
        data_only=False,
        keep_vba=original_path.suffix.casefold() == ".xlsm",
    )
    changes: list[dict[str, str]] = []
    try:
        changes.extend(_fill_constant_series_from_instruction(output, source, instruction))
        changes.extend(_fill_instruction_ticket_size_and_exit_value_formulas(output, source, instruction))
        changes.extend(_fill_instruction_cumulative_position_rows(output, source, instruction))
        changes.extend(_fill_instruction_annual_rollup_columns(output, source, instruction))
        changes.extend(_continue_financial_metric_formula_runs(output, source, instruction))
        changes.extend(_link_instruction_header_columns(output, source, instruction))
        changes.extend(_link_instruction_metric_rows(output, source, instruction))
        changes.extend(_apply_instruction_freeze_panes(output, instruction))
        if changes:
            output.save(workbook_path)
    finally:
        source.close()
        output.close()
    return changes


__all__ = [
    "complete_financial_model_runtime_actions",
    "complete_isolated_formula_holes",
    "complete_revenue_growth_schedule",
]
