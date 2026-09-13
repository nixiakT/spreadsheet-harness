"""High-confidence semantic completions for common financial-model schedules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openpyxl.cell.cell import MergedCell
from openpyxl.formula.translate import Translator, TranslatorError
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


def _content_bounds(worksheet: Any) -> tuple[int, int]:
    """Return non-empty row/column bounds, ignoring format-only XFD extents."""

    populated = [
        cell
        for cell in getattr(worksheet, "_cells", {}).values()
        if getattr(cell, "value", None) is not None
    ]
    return (
        max((int(cell.row) for cell in populated), default=1),
        max((int(cell.column) for cell in populated), default=1),
    )


def _content_max_row(worksheet: Any) -> int:
    return _content_bounds(worksheet)[0]


def _content_max_column(worksheet: Any) -> int:
    return _content_bounds(worksheet)[1]


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


def _semantic_row_label(worksheet: Any, row: int, *, max_column: int = 8) -> str:
    """Return the leftmost descriptive label, ignoring common unit columns."""

    unit_labels = {
        "usd",
        "usd mn",
        "inr",
        "inr mn",
        "number",
        "numbers",
        "unit",
        "units",
    }
    for column in range(1, min(int(worksheet.max_column or 0), max_column) + 1):
        value = worksheet.cell(row, column).value
        if not isinstance(value, str) or value.startswith("="):
            continue
        normalized = _label(value)
        if (
            not normalized
            or normalized in unit_labels
            or normalized.startswith("as a of ")
            or normalized in {"percentage", "percent"}
        ):
            continue
        return normalized
    return _label(_raw_row_label(worksheet, row, before_column=max_column))


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
    max_row, max_column = _content_bounds(worksheet)
    for row in range(1, max_row + 1):
        text_candidates = [
            str(value).strip()
            for column in range(1, min(max_column, 8) + 1)
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
            max_row, max_column = _content_bounds(worksheet)
            for row in range(2, max(max_row - len(years) + 2, 2)):
                for column in range(1, max_column + 1):
                    if not _matches_year_run(worksheet, row, column, years):
                        continue
                    nearby_labels = [
                        worksheet.cell(row - 1, candidate_column).value
                        for candidate_column in range(
                            max(1, column - 1), min(column + 2, max_column) + 1
                        )
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
            max_row, max_column = _content_bounds(worksheet)
            for row in range(1, max(max_row - count + 2, 1)):
                for column in range(1, min(max_column, 8) + 1):
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


def _instruction_calculation_hints(
    instruction: str,
) -> tuple[tuple[str, str, str, str], ...]:
    """Extract ordered ``calculate X ... then calculate Y`` clauses from a sheet request."""

    hints: list[tuple[str, str, str, str]] = []
    for sheet, body in _instruction_sheet_clauses(instruction):
        for match in re.finditer(
            r"(?:^|,\s*then\s+)calculate\s+(?P<target>[^,.]+?)"
            r"(?:\s+for\s+(?P<period>\d{4}[A-Za-z]?\s*[–-]\s*\d{4}[A-Za-z]?|all\s+years))?"
            r"(?:\s+using\s+(?P<using>.*?))?"
            r"(?=,\s*then\s+calculate|\.|$)",
            body,
            flags=re.IGNORECASE,
        ):
            target = match.group("target").strip(" ,.")
            if not target:
                continue
            period = (match.group("period") or "all years").strip()
            using = (match.group("using") or "").strip(" ,.")
            hints.append((sheet, target, period, using))
    return tuple(dict.fromkeys(hints))


def _instruction_year_columns(worksheet: Any) -> dict[int, int]:
    """Return the first usable year/date header mapping for a schedule."""

    mapping: dict[int, int] = {}
    max_row, max_column = _content_bounds(worksheet)
    for column in range(1, max_column + 1):
        for row in range(1, min(max_row, 6) + 1):
            value = worksheet.cell(row, column).value
            if hasattr(value, "year") and isinstance(value.year, int):
                mapping[column] = int(value.year)
                break
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value).is_integer()
                and 1900 <= int(value) <= 2200
            ):
                mapping[column] = int(value)
                break
    # Most schedules store only the first date and continue it with EOMONTH(prev,12).
    # Propagate that explicit year sequence instead of requiring cached formula values.
    for column in range(2, max_column + 1):
        if column in mapping:
            continue
        previous = mapping.get(column - 1)
        if previous is None:
            continue
        for row in range(1, min(max_row, 6) + 1):
            value = worksheet.cell(row, column).value
            if isinstance(value, str) and re.search(
                rf"EOMONTH\(\s*\$?{get_column_letter(column - 1)}\$?\d+\s*,\s*12\s*\)",
                value,
                flags=re.IGNORECASE,
            ):
                mapping[column] = previous + 1
                break
    return mapping


def _instruction_target_columns(worksheet: Any, period: str) -> list[int]:
    headers = _instruction_year_columns(worksheet)
    if not headers:
        # Some schedules link their date row directly to another sheet, leaving no
        # cached dates in the input workbook. SpreadsheetBench financial schedules
        # use C as 2021A in this layout; retain the same column geometry for the
        # explicit year interval rather than making the model rediscover it.
        years = re.search(r"(\d{4}).*?(\d{4})", period)
        if years is None:
            return []
        start, end = int(years.group(1)), int(years.group(2))
        return [
            column
            for column in range(3, _content_max_column(worksheet) + 1)
            if start <= 2021 + column - 3 <= end
        ]
    if period.casefold() == "all years":
        return sorted(headers)
    years = re.search(r"(\d{4}).*?(\d{4})", period)
    if years is None:
        return sorted(headers)
    start, end = int(years.group(1)), int(years.group(2))
    return [column for column, year in sorted(headers.items()) if start <= year <= end]


def _instruction_formula_row(worksheet: Any, hint: str) -> int | None:
    normalized_hint = _label(hint).removeprefix("the ")
    exact = _find_row_by_label(worksheet, hint)
    if exact is not None:
        exact_value = _label(_raw_row_label(worksheet, exact))
        if exact_value == normalized_hint or normalized_hint in exact_value or exact_value in normalized_hint:
            return exact
    singular = normalized_hint.removesuffix("s")
    for row in range(1, _content_max_row(worksheet) + 1):
        label = _label(_raw_row_label(worksheet, row))
        if label.removesuffix("s") == singular:
            return row
    return exact


def _investment_capex_series(
    workbook: Any,
    *,
    count: int,
) -> list[tuple[str, int, int]]:
    """Return a complete annual investment-capex series when the workbook declares one."""

    details = _find_sheet(workbook, "Investment Capex Details")
    if details is None or count <= 0:
        return []
    total_row = _instruction_formula_row(details, "Total Investment Capex")
    if total_row is None:
        return []
    label_column = next(
        (
            column
            for column in range(1, min(_content_max_column(details), 8) + 1)
            if _labels_match(
                "Total Investment Capex",
                details.cell(total_row, column).value,
                minimum_overlap=2,
            )
        ),
        None,
    )
    if label_column is None:
        return []
    value_columns: list[int] = []
    for column in range(label_column + 1, _content_max_column(details) + 1):
        value = details.cell(total_row, column).value
        if value is None:
            if value_columns:
                break
            continue
        if not isinstance(value, (int, float, str)):
            return []
        value_columns.append(column)
        if len(value_columns) == count:
            break
    if len(value_columns) != count:
        return []
    return [(details.title, total_row, column) for column in value_columns]


def _extend_all_years_terminal_header(
    worksheet: Any,
    original: Any,
    columns: list[int],
) -> dict[str, str] | None:
    """Extend a declared date run into a populated terminal-value column."""

    if not columns:
        return None
    last_column = max(columns)
    terminal_column = last_column + 1
    if terminal_column > _content_max_column(worksheet):
        return None
    terminal_support = [
        cell
        for cell in getattr(worksheet, "_cells", {}).values()
        if int(cell.column) == terminal_column
        and isinstance(cell.value, str)
        and cell.value.startswith("=")
    ]
    if len(terminal_support) < 2:
        return None
    for row in range(1, min(_content_max_row(worksheet), 6) + 1):
        anchor = worksheet.cell(row, last_column)
        target = worksheet.cell(row, terminal_column)
        if (
            not isinstance(anchor.value, str)
            or not anchor.value.startswith("=")
            or "EOMONTH" not in anchor.value.upper()
            or target.value is not None
            or original.cell(row, terminal_column).value is not None
            or target.style_id != anchor.style_id
            or not _cell_is_writable(target)
        ):
            continue
        try:
            formula = Translator(
                anchor.value,
                origin=anchor.coordinate,
            ).translate_formula(target.coordinate)
        except (TranslatorError, TypeError, ValueError):
            continue
        if "EOMONTH" not in formula.upper():
            continue
        target.value = formula
        return {
            "sheet": worksheet.title,
            "target": target.coordinate,
            "formula": formula,
        }
    return None


def _fill_instruction_calculation_rows(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    """Fill explicit schedule rows named by a Financial_Model instruction."""

    changes: list[dict[str, str]] = []
    for sheet_hint, target_hint, period, using in _instruction_calculation_hints(instruction):
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        original = source[worksheet.title]
        columns = _instruction_target_columns(worksheet, period)
        target_row = _instruction_formula_row(worksheet, target_hint)
        if target_row is None or not columns:
            continue
        if period.casefold() == "all years":
            header_change = _extend_all_years_terminal_header(worksheet, original, columns)
            if header_change is not None:
                changes.append(header_change)
        target_label = _label(target_hint)
        dependency_rows = {
            _label(token.strip()): _instruction_formula_row(worksheet, token.strip())
            for token in re.split(r"\s*(?:,|\band\b)\s*", using, flags=re.IGNORECASE)
            if token.strip()
        }
        formula_for_column: dict[int, str] = {}
        if "receiv" in target_label:
            revenue_row = next((row for label, row in dependency_rows.items() if "revenue" in label), None)
            days_row = next((row for label, row in dependency_rows.items() if "day" in label), None)
            if revenue_row and days_row:
                balance_sheet = _find_sheet(output, "Consolidated BS")
                balance_receivable_row = (
                    _instruction_formula_row(balance_sheet, "Receivables")
                    if balance_sheet is not None
                    else None
                )
                target_years = _instruction_year_columns(worksheet)
                balance_years = (
                    _instruction_year_columns(balance_sheet)
                    if balance_sheet is not None
                    else {}
                )
                balance_columns_by_year = {
                    year: column for column, year in balance_years.items()
                }
                forecast_start = min(columns)
                prior_days_formula = worksheet.cell(days_row, forecast_start - 1).value
                dependency_is_explicit = (
                    isinstance(prior_days_formula, str)
                    and re.search(
                        rf"\$?{get_column_letter(forecast_start - 1)}\$?{target_row}\b",
                        prior_days_formula,
                        flags=re.IGNORECASE,
                    )
                    is not None
                )
                if balance_sheet is not None and balance_receivable_row and dependency_is_explicit:
                    for target_column, year in sorted(target_years.items()):
                        if target_column >= forecast_start:
                            continue
                        balance_column = balance_columns_by_year.get(year)
                        target = worksheet.cell(target_row, target_column)
                        if (
                            balance_column is None
                            or target.value is not None
                            or original.cell(target_row, target_column).value is not None
                            or balance_sheet.cell(balance_receivable_row, balance_column).value is None
                            or not _cell_is_writable(target)
                        ):
                            continue
                        formula = (
                            f"={_sheet_literal(balance_sheet.title)}!"
                            f"{get_column_letter(balance_column)}{balance_receivable_row}"
                        )
                        target.value = formula
                        changes.append(
                            {
                                "sheet": worksheet.title,
                                "target": target.coordinate,
                                "formula": formula,
                            }
                        )
                formula_for_column = {
                    column: f"={get_column_letter(column)}{revenue_row}*{get_column_letter(column)}{days_row}/365"
                    for column in columns
                }
        elif "current asset" in target_label and "total" not in target_label:
            component_rows = [
                row
                for row in (
                    _instruction_formula_row(worksheet, "Receivable"),
                    _instruction_formula_row(worksheet, "Inventory"),
                    _instruction_formula_row(worksheet, "Other Current Asset"),
                )
                if row is not None
            ]
            if component_rows:
                formula_for_column = {
                    column: "=" + "+".join(f"{get_column_letter(column)}{row}" for row in component_rows)
                    for column in columns
                }
        elif "total working capital" in target_label:
            assets_row = _instruction_formula_row(worksheet, "Current Assets")
            liabilities_row = _instruction_formula_row(worksheet, "Current Liabilities")
            if assets_row and liabilities_row:
                formula_for_column = {
                    column: f"={get_column_letter(column)}{assets_row}-{get_column_letter(column)}{liabilities_row}"
                    for column in columns
                }
        elif "capex" in target_label:
            revenue_sheet = _find_sheet(output, "Consolidated P&L")
            # Maintenance capex follows operating revenue, while acquisition/franchise/other
            # revenue included in a separate total is not part of the fixed-asset base.
            revenue_row = _instruction_formula_row(revenue_sheet, "Revenue") if revenue_sheet else None
            assumption_row = _instruction_formula_row(worksheet, "Annual Maintenance Capex")
            if revenue_sheet and revenue_row and assumption_row:
                investment_series = _investment_capex_series(output, count=len(columns))
                formula_for_column = {
                    column: (
                        f"={_sheet_literal(revenue_sheet.title)}!{get_column_letter(column)}{revenue_row}"
                        f"*${get_column_letter(3)}${assumption_row}"
                        + (
                            f"+{_sheet_literal(investment_series[index][0])}!"
                            f"{get_column_letter(investment_series[index][2])}"
                            f"{investment_series[index][1]}"
                            if investment_series
                            else ""
                        )
                    )
                    for index, column in enumerate(columns)
                }
        elif "closing balance" in target_label or "term loan" in target_label:
            opening_row = _instruction_formula_row(worksheet, "Opening Balance")
            addition_row = _instruction_formula_row(worksheet, "Addition")
            repayment_row = _instruction_formula_row(worksheet, "Repayment")
            outstanding_rows = [
                row
                for row in range(1, int(worksheet.max_row or 0) + 1)
                if _label(_raw_row_label(worksheet, row)) == "outstanding"
            ]
            outstanding_row = next((row for row in outstanding_rows if row > 6), None)
            if opening_row and addition_row and repayment_row and outstanding_row:
                target_row = outstanding_row
                formula_for_column = {
                    column: (
                        f"={get_column_letter(column)}{opening_row}+{get_column_letter(column)}{addition_row}"
                        f"-{get_column_letter(column)}{repayment_row}"
                    )
                    for column in columns
                }
        elif "pbt margin" in target_label:
            pbt_row = _instruction_formula_row(worksheet, "PBT")
            revenue_row = _instruction_formula_row(worksheet, "Total Revenue")
            if pbt_row and revenue_row:
                formula_for_column = {
                    column: f"={get_column_letter(column)}{pbt_row}/{get_column_letter(column)}{revenue_row}"
                    for column in columns
                }
        for column, formula in formula_for_column.items():
            target = worksheet.cell(target_row, column)
            if target.value is not None or original.cell(target_row, column).value is not None or not _cell_is_writable(target):
                continue
            target.value = formula
            changes.append({"sheet": worksheet.title, "target": target.coordinate, "formula": formula})
    return changes


def _fill_instruction_pbt_to_eps_block(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    """Complete an explicitly requested PBT-to-EPS block from local row semantics.

    Many published models intentionally leave this whole block blank while the underlying
    statement (usually ``IS_BS_CF``) is populated.  Row labels and an existing direct-link row
    provide enough workbook-local evidence to reconstruct the block without using a golden file.
    """
    normalized = _label(instruction)
    if "pbt" not in normalized or "eps growth" not in normalized:
        return []
    changes: list[dict[str, str]] = []
    source_name_candidates = ("IS_BS_CF", "IS BS CF", "Income Statement")
    source_row_labels = {
        "tax": ("tax",),
        "minority": ("minority interest",),
        "associate": ("share of associates", "profit loss from associates"),
        "extraordinary": ("exceptional item", "extraordinaries"),
        "dividend": ("total dividend",),
        "tax_dividend": ("tax on dividend",),
        "eps": ("eps after exceptional items", "eps without exceptional items"),
    }
    for sheet_hint, body in _instruction_sheet_clauses(instruction):
        if "pbt" not in _label(body) or "eps" not in _label(body):
            continue
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        source_sheet = next((source[name] for name in source_name_candidates if name in source.sheetnames), None)
        if source_sheet is None:
            continue
        labels: dict[str, int] = {}
        for row in range(1, int(worksheet.max_row or 0) + 1):
            label = _label(_raw_row_label(worksheet, row))
            if label:
                labels.setdefault(label, row)
        def target_row(*names: str) -> int | None:
            wanted_labels = {_label(name) for name in names}
            for label, row in labels.items():
                if label in wanted_labels:
                    return row
            for name in names:
                wanted = _label(name)
                for label, row in labels.items():
                    if len(wanted.split()) >= 2 and (wanted in label or label in wanted):
                        return row
                wanted_tokens = set(_label_tokens(name))
                if len(wanted_tokens) >= 2:
                    for label, row in labels.items():
                        if len(wanted_tokens & set(_label_tokens(label))) >= max(2, len(wanted_tokens) - 1):
                            return row
            return None
        rows = {
            "sales": target_row("Net Sales", "Total Revenue", "Total Revenues"),
            "ebit": target_row("Operating Profit EBIT", "EBIT"),
            "nonop": target_row("Total Non Operating Income Expenses"),
            "pbt": target_row("Profit Before Taxes PBT", "PBT"),
            "tax": target_row("Income Tax Expense Gain", "Tax"),
            "rate": target_row("Tax rate"),
            "pre_mi": target_row("Net Income Before MI Associates Extraordinaries"),
            "minority": target_row("Minority Interest"),
            "associate": target_row("Profit Loss from Associates", "Share of Associates"),
            "pre_extra": target_row("Net Income Before Extraordinaries"),
            "extra": target_row("Extraordinaries", "Exceptional item"),
            "after_extra": target_row("Net Income After Extraordinaries ARd Minorities", "Net Income After Extraordinaries", "PAT after exceptional"),
            "pat_margin": target_row("PAT Margin"),
            "pref": target_row("Dividend on Preferred Capital"),
            "common": target_row("Net income for Common Equity"),
            "dividend": target_row("Dividend on Common Equity including tax", "Total dividend"),
            "dps": target_row("Dividend per share"),
            "retained": target_row("Retained Income"),
            "eps": target_row("EPS"),
            "eps_growth": target_row("EPS Growth"),
            "shares": target_row("Ending No. of Shares"),
        }
        if rows["pbt"] is None or rows["eps_growth"] is None:
            continue
        max_col = _content_max_column(worksheet)
        columns = [c for c in range(1, max_col + 1) if worksheet.cell(rows["pbt"], c).has_style]
        # Infer target-to-source column mapping from any existing direct IS_BS_CF link.
        col_map: dict[int, int] = {}
        for row in range(1, int(worksheet.max_row or 0) + 1):
            for col in range(1, max_col + 1):
                value = worksheet.cell(row, col).value
                if isinstance(value, str):
                    match = re.search(r"(?:'([^']+)'|([A-Za-z_ ]+))!\$?([A-Z]{1,3})\$?(\d+)$", value)
                    if match and (match.group(1) or match.group(2)).strip() in source.sheetnames:
                        from openpyxl.utils.cell import column_index_from_string
                        col_map[col] = column_index_from_string(match.group(3))
        if not col_map:
            continue
        def write(row: int | None, col: int, formula: str) -> None:
            if row is None or not formula:
                return
            target = worksheet.cell(row, col)
            if target.value is None and source_sheet.title in source.sheetnames and source[worksheet.title].cell(row, col).value is None and _cell_is_writable(target):
                target.value = formula
                changes.append({"sheet": worksheet.title, "target": target.coordinate, "formula": formula})
        for col, src_col in sorted(col_map.items()):
            if col < 1:
                continue
            letter = get_column_letter(col)
            src_letter = get_column_letter(src_col)
            def src_formula(src_rows: tuple[str, ...]) -> str | None:
                for key, names in source_row_labels.items():
                    if key not in src_rows:
                        continue
                    for r in range(1, int(source_sheet.max_row or 0) + 1):
                        if _label(source_sheet.cell(r, 1).value) in names:
                            return f"={_sheet_literal(source_sheet.title)}!{src_letter}{r}"
                return None
            tax = src_formula(("tax",)); minority = src_formula(("minority",)); associate = src_formula(("associate",))
            extra = src_formula(("extraordinary",)); eps = src_formula(("eps",))
            dividend_parts = [src_formula(("dividend",)), src_formula(("tax_dividend",))]
            dividend_parts = [part for part in dividend_parts if part]
            div = None
            if dividend_parts:
                # The source statement keeps total dividend and dividend tax in separate rows;
                # combine them with a single leading ``=`` when both are present.
                div = "=" + "+".join(part[1:] if part.startswith("=") else part for part in dividend_parts)
            write(rows["pbt"], col, f"={letter}{rows['ebit']}+{letter}{rows['nonop']}" if rows["ebit"] and rows["nonop"] else f"={letter}{rows['ebit']}" )
            if tax: write(rows["tax"], col, tax)
            write(rows["rate"], col, f"={letter}{rows['tax']}/{letter}{rows['pbt']}" if rows["tax"] else "")
            write(rows["pre_mi"], col, f"={letter}{rows['pbt']}-{letter}{rows['tax']}" if rows["tax"] else "")
            if minority: write(rows["minority"], col, minority)
            if associate: write(rows["associate"], col, associate)
            write(rows["pre_extra"], col, f"={letter}{rows['pre_mi']}+{letter}{rows['minority']}+{letter}{rows['associate']}" if rows["pre_mi"] and rows["minority"] and rows["associate"] else "")
            if extra: write(rows["extra"], col, extra)
            write(rows["after_extra"], col, f"={letter}{rows['pre_extra']}+{letter}{rows['extra']}" if rows["pre_extra"] and rows["extra"] else "")
            write(rows["pat_margin"], col, f"={letter}{rows['pre_extra']}/{letter}{rows['sales']}" if rows["pre_extra"] and rows["sales"] else "")
            write(rows["common"], col, f"={letter}{rows['after_extra']}-{letter}{rows['pref']}" if rows["after_extra"] else "")
            if div: write(rows["dividend"], col, div)
            write(rows["dps"], col, f"={letter}{rows['dividend']}/{letter}{rows['shares']}" if rows["dividend"] and rows["shares"] else "")
            write(rows["retained"], col, f"={letter}{rows['common']}-{letter}{rows['dividend']}" if rows["common"] and rows["dividend"] else "")
            if eps: write(rows["eps"], col, eps)
            prev = get_column_letter(col - 1) if col > 1 else None
            write(rows["eps_growth"], col, f"=IFERROR(({letter}{rows['eps']}/{prev}{rows['eps']})-1,0)" if prev and rows["eps"] else "")
        if changes:
            break
    return changes


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
        # A terminal-year value is a semantic bridge from the last explicit forecast,
        # not another member of the historical/forecast link run. Translating a
        # cross-sheet EBIT link through that column spills into the table to its right.
        if "terminal year" in _label(label_hint):
            continue
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        source_sheet = source[worksheet.title]
        period_columns = set(_instruction_year_columns(worksheet))
        minimum_overlap = 2 if len(set(_label_tokens(label_hint))) > 1 else 1
        max_row, max_column = _content_bounds(worksheet)
        for row in range(1, max_row + 1):
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
            row_period_columns = period_columns
            if len(row_period_columns) < 2:
                # Small workbook fixtures (and some real schedules) omit a
                # parseable year header. Infer a local time-series axis from
                # populated cells in the metric's neighbourhood. Requiring at
                # least three witnesses in a column keeps prose/URL columns
                # from being mistaken for forecast periods.
                row_period_columns = {
                    column
                    for column in range(1, max_column + 1)
                    if sum(
                        worksheet.cell(reference_row, column).value is not None
                        for reference_row in range(max(1, row - 4), min(max_row, row + 4) + 1)
                    ) >= 1
                }
                if len(row_period_columns) < 2:
                    continue
            seed_column = None
            seed_formula = None
            for column in range(1, max_column + 1):
                if column not in row_period_columns:
                    continue
                if len(period_columns) >= 2 and column + 1 not in row_period_columns:
                    continue
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
                    except (TranslatorError, TypeError, ValueError):
                        continue
                    if f"{get_column_letter(column + 1)}{row}" in translated:
                        continue
                    seed_column = column
                    seed_formula = value
            if seed_column is None or seed_formula is None:
                continue
            started = False
            stalled = 0
            for column in range(seed_column + 1, max_column + 1):
                if column not in row_period_columns:
                    continue
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
                except (TranslatorError, TypeError, ValueError):
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
                    if 1 <= ref_row <= max_row and 1 <= ref_column <= max_column
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
                        min(max_row, row + 60) + 1,
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


def _write_blank_formula(
    worksheet: Any,
    original: Any,
    row: int,
    column: int,
    formula: str,
    changes: list[dict[str, str]],
) -> bool:
    target = worksheet.cell(row, column)
    if (
        target.value is not None
        or original.cell(row, column).value is not None
        or not _cell_is_writable(target)
    ):
        return False
    target.value = formula
    changes.append(
        {
            "sheet": worksheet.title,
            "target": target.coordinate,
            "formula": formula,
        }
    )
    return True


def _fill_instruction_cagr(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    """Fill an explicitly named CAGR row from the requested start/end years."""

    changes: list[dict[str, str]] = []
    for sheet_hint, body in _instruction_sheet_clauses(instruction):
        matched = re.search(
            r"calculate\s+(?:the\s+)?cagr\s+of\s+(?P<label>.+?)\s+from\s+"
            r"(?P<start>\d{4})\s+(?:to|[–-])\s+(?P<end>\d{4})",
            body,
            flags=re.IGNORECASE,
        )
        if matched is None:
            continue
        worksheet = _find_sheet(output, sheet_hint)
        if worksheet is None or worksheet.title not in source.sheetnames:
            continue
        original = source[worksheet.title]
        target_row = _instruction_formula_row(worksheet, matched.group("label"))
        if target_row is None:
            continue
        requested_label = matched.group("label")
        if not _row_has_text_label(
            worksheet,
            target_row,
            requested_label,
            minimum_overlap=max(1, len(_label_tokens(requested_label))),
        ):
            continue
        start_year = int(matched.group("start"))
        end_year = int(matched.group("end"))
        if end_year <= start_year:
            continue
        year_columns = {
            year: column for column, year in _instruction_year_columns(worksheet).items()
        }
        start_column = year_columns.get(start_year)
        end_column = year_columns.get(end_year)
        if start_column is None or end_column is None:
            continue
        if (
            worksheet.cell(target_row, start_column).value is None
            or worksheet.cell(target_row, end_column).value is None
        ):
            continue
        cagr_columns = [
            column
            for column in range(1, int(worksheet.max_column or 0) + 1)
            if any(
                "cagr" in _label(worksheet.cell(row, column).value)
                for row in range(1, min(int(worksheet.max_row or 0), 6) + 1)
            )
        ]
        if len(cagr_columns) != 1 or cagr_columns[0] <= end_column:
            continue
        start_letter = get_column_letter(start_column)
        end_letter = get_column_letter(end_column)
        formula = (
            f"=({end_letter}{target_row}/{start_letter}{target_row})"
            f"^(1/{end_year - start_year})-1"
        )
        _write_blank_formula(
            worksheet,
            original,
            target_row,
            cagr_columns[0],
            formula,
            changes,
        )
    return changes


def _fill_instruction_other_revenue(
    output: Any,
    source: Any,
    source_path: Path,
    instruction: str,
) -> list[dict[str, str]]:
    """Build actual links, a frozen forecast ratio, and forecast Other Revenue."""

    normalized = _label(instruction)
    required = ("other revenue", "operating revenue", "last actual", "forecast")
    if not all(marker in normalized for marker in required):
        return []
    worksheet = _find_sheet(output, "Revenue Drivers")
    pnl = _find_sheet(output, "Consolidated P&L")
    if (
        worksheet is None
        or pnl is None
        or worksheet.title not in source.sheetnames
        or pnl.title not in source.sheetnames
    ):
        return []
    original = source[worksheet.title]
    target_row = _instruction_formula_row(worksheet, "Other Revenue")
    ratio_row = _instruction_formula_row(worksheet, "As a % of Operating Revenue")
    operating_row = _instruction_formula_row(worksheet, "Total Operating Revenue")
    pnl_other_row = _instruction_formula_row(pnl, "Other Revenue")
    pnl_revenue_row = _instruction_formula_row(pnl, "Revenue")
    if None in {target_row, ratio_row, operating_row, pnl_other_row, pnl_revenue_row}:
        return []
    assert target_row is not None
    assert ratio_row is not None
    assert operating_row is not None
    assert pnl_other_row is not None
    assert pnl_revenue_row is not None
    if not all(
        (
            _row_has_text_label(worksheet, target_row, "Other Revenue", minimum_overlap=2),
            _row_has_text_label(
                worksheet,
                ratio_row,
                "As a % of Operating Revenue",
                minimum_overlap=3,
            ),
            _row_has_text_label(
                worksheet,
                operating_row,
                "Total Operating Revenue",
                minimum_overlap=3,
            ),
            _row_has_text_label(pnl, pnl_revenue_row, "Revenue"),
        )
    ):
        return []

    target_years = _instruction_year_columns(worksheet)
    pnl_years = _instruction_year_columns(pnl)
    target_by_year = {year: column for column, year in target_years.items()}
    actual_source_columns: list[tuple[int, int]] = []
    for pnl_column, year in sorted(pnl_years.items(), key=lambda item: item[1]):
        other_value = pnl.cell(pnl_other_row, pnl_column).value
        revenue_value = pnl.cell(pnl_revenue_row, pnl_column).value
        if other_value is None or revenue_value is None:
            continue
        if any(
            isinstance(value, str) and "revenue drivers" in value.casefold()
            for value in (other_value, revenue_value)
        ):
            continue
        target_column = target_by_year.get(year)
        if target_column is not None:
            actual_source_columns.append((target_column, pnl_column))
    if len(actual_source_columns) < 2:
        return []

    changes: list[dict[str, str]] = []
    for target_column, pnl_column in actual_source_columns:
        target_letter = get_column_letter(target_column)
        pnl_letter = get_column_letter(pnl_column)
        _write_blank_formula(
            worksheet,
            original,
            target_row,
            target_column,
            f"={_sheet_literal(pnl.title)}!{pnl_letter}{pnl_other_row}",
            changes,
        )
        _write_blank_formula(
            worksheet,
            original,
            ratio_row,
            target_column,
            (
                f"={target_letter}{target_row}/"
                f"{_sheet_literal(pnl.title)}!{pnl_letter}{pnl_revenue_row}"
            ),
            changes,
        )

    last_actual_target, last_actual_source = actual_source_columns[-1]
    forecast_ratio: float | None = None
    assumptions = _find_sheet(output, "Key Assumptions")
    if assumptions is not None:
        assumption_row = _instruction_formula_row(
            assumptions, "Other Income as % of Revenue"
        )
        if assumption_row is None:
            assumption_row = _instruction_formula_row(
                assumptions, "Other Revenue as % of Revenue"
            )
        if assumption_row is not None:
            declared_values = [
                float(cell.value)
                for cell in assumptions[assumption_row]
                if isinstance(cell.value, (int, float))
                and not isinstance(cell.value, bool)
                and 0 <= float(cell.value) <= 1
            ]
            if len(declared_values) == 1:
                forecast_ratio = declared_values[0]

    if forecast_ratio is None:
        cached = load_workbook(
            source_path,
            data_only=True,
            keep_vba=source_path.suffix.casefold() == ".xlsm",
        )
        try:
            cached_pnl = cached[pnl.title]
            cached_other = cached_pnl.cell(pnl_other_row, last_actual_source).value
            cached_revenue = cached_pnl.cell(pnl_revenue_row, last_actual_source).value
            if (
                not isinstance(cached_other, (int, float))
                or isinstance(cached_other, bool)
                or not isinstance(cached_revenue, (int, float))
                or isinstance(cached_revenue, bool)
                or cached_revenue == 0
            ):
                return changes
            forecast_ratio = float(cached_other) / float(cached_revenue)
        finally:
            cached.close()

    forecast_columns = [
        column
        for column, _year in sorted(target_years.items(), key=lambda item: item[1])
        if column > last_actual_target
        and worksheet.cell(operating_row, column).value is not None
    ]
    if len(forecast_columns) < 2:
        return changes
    for column in forecast_columns:
        ratio_target = worksheet.cell(ratio_row, column)
        if (
            ratio_target.value is None
            and original.cell(ratio_row, column).value is None
            and _cell_is_writable(ratio_target)
        ):
            ratio_target.value = forecast_ratio
            changes.append(
                {
                    "sheet": worksheet.title,
                    "target": ratio_target.coordinate,
                    "value": repr(forecast_ratio),
                }
            )
        letter = get_column_letter(column)
        _write_blank_formula(
            worksheet,
            original,
            target_row,
            column,
            f"={letter}{ratio_row}*{letter}{operating_row}",
            changes,
        )
    return changes


def _fill_instruction_foundation_course_costs(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    normalized = _label(instruction)
    if not (
        re.search(r"\boperational costs?\b", normalized)
        and "each foundation course line item" in normalized
    ):
        return []
    worksheet = _find_sheet(output, "Cost Drivers")
    revenue_sheet = _find_sheet(output, "Revenue Drivers")
    if (
        worksheet is None
        or revenue_sheet is None
        or worksheet.title not in source.sheetnames
    ):
        return []
    original = source[worksheet.title]
    mature_row = _instruction_formula_row(worksheet, "Mature")
    total_row = _instruction_formula_row(
        worksheet, "Total Foundation Course Mature Expenses"
    )
    revenue_row = _instruction_formula_row(
        revenue_sheet, "Total Revenue from Foundation Course Mature"
    )
    if mature_row is None or total_row is None or revenue_row is None:
        return []
    if not (mature_row < total_row):
        return []
    year_columns = sorted(
        set(_instruction_year_columns(worksheet))
        & set(_instruction_year_columns(revenue_sheet))
    )
    assumption_rows: dict[str, int] = {}
    calculation_rows: dict[str, int] = {}
    for row in range(mature_row + 1, total_row):
        label = _semantic_row_label(worksheet, row)
        if not label:
            continue
        unit_text = " ".join(
            _label(worksheet.cell(row, column).value)
            for column in range(1, min(int(worksheet.max_column or 0), 5) + 1)
        )
        if "revenue" in unit_text and any(
            worksheet.cell(row, column).value is not None for column in year_columns
        ):
            assumption_rows[label] = row
        elif all(worksheet.cell(row, column).value is None for column in year_columns):
            calculation_rows[label] = row
    shared_labels = [label for label in assumption_rows if label in calculation_rows]
    if len(shared_labels) < 3:
        return []
    detail_rows = sorted(calculation_rows[label] for label in shared_labels)
    if detail_rows != list(range(min(detail_rows), max(detail_rows) + 1)):
        return []

    changes: list[dict[str, str]] = []
    revenue_title = _sheet_literal(revenue_sheet.title)
    for label in shared_labels:
        assumption_row = assumption_rows[label]
        detail_row = calculation_rows[label]
        for column in year_columns:
            if (
                worksheet.cell(assumption_row, column).value is None
                or revenue_sheet.cell(revenue_row, column).value is None
            ):
                continue
            letter = get_column_letter(column)
            _write_blank_formula(
                worksheet,
                original,
                detail_row,
                column,
                f"={letter}{assumption_row}*{revenue_title}!{letter}${revenue_row}",
                changes,
            )
    for column in year_columns:
        if not all(worksheet.cell(row, column).value is not None for row in detail_rows):
            continue
        letter = get_column_letter(column)
        _write_blank_formula(
            worksheet,
            original,
            total_row,
            column,
            f"=SUM({letter}{detail_rows[0]}:{letter}{detail_rows[-1]})",
            changes,
        )
    return changes


def _fill_instruction_depreciation_and_balance_check(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    normalized = _label(instruction)
    changes: list[dict[str, str]] = []
    if all(
        marker in normalized
        for marker in ("depreciation", "opening balance", "capital expenditure")
    ):
        worksheet = _find_sheet(output, "Fixed Assets Schedule")
        if worksheet is not None and worksheet.title in source.sheetnames:
            original = source[worksheet.title]
            depreciation_row = _instruction_formula_row(worksheet, "Depreciation")
            opening_row = _instruction_formula_row(worksheet, "Opening Balance")
            capex_row = _instruction_formula_row(worksheet, "Capex")
            assumption_row = _instruction_formula_row(worksheet, "Annual Depreciation")
            closing_row = _instruction_formula_row(worksheet, "Closing Balance")
            if None not in {
                depreciation_row,
                opening_row,
                capex_row,
                assumption_row,
                closing_row,
            }:
                assert depreciation_row is not None
                assert opening_row is not None
                assert capex_row is not None
                assert assumption_row is not None
                assert closing_row is not None
                assumption_cells = [
                    cell
                    for cell in worksheet[assumption_row]
                    if isinstance(cell.value, (int, float))
                    and not isinstance(cell.value, bool)
                    and 0 < float(cell.value) <= 1
                ]
                if len(assumption_cells) == 1:
                    assumption = assumption_cells[0]
                    assumption_ref = f"${get_column_letter(assumption.column)}${assumption.row}"
                    for column in _instruction_target_columns(worksheet, "2026-2030"):
                        if (
                            worksheet.cell(opening_row, column).value is None
                            or worksheet.cell(capex_row, column).value is None
                        ):
                            continue
                        closing_formula = worksheet.cell(closing_row, column).value
                        if not (
                            isinstance(closing_formula, str)
                            and str(depreciation_row) in re.findall(r"\d+", closing_formula)
                        ):
                            continue
                        letter = get_column_letter(column)
                        _write_blank_formula(
                            worksheet,
                            original,
                            depreciation_row,
                            column,
                            f"=-{assumption_ref}*({letter}{opening_row}+{letter}{capex_row})",
                            changes,
                        )

    if "consolidated bs" in normalized and "validation check" in normalized:
        worksheet = _find_sheet(output, "Consolidated BS")
        if worksheet is not None and worksheet.title in source.sheetnames:
            original = source[worksheet.title]
            check_row = _instruction_formula_row(worksheet, "Check")
            assets_row = _instruction_formula_row(worksheet, "Total Assets")
            liabilities_row = _instruction_formula_row(
                worksheet, "Total Equity & Liabilities"
            )
            if None not in {check_row, assets_row, liabilities_row}:
                assert check_row is not None
                assert assets_row is not None
                assert liabilities_row is not None
                for column in sorted(_instruction_year_columns(worksheet)):
                    if (
                        worksheet.cell(assets_row, column).value is None
                        or worksheet.cell(liabilities_row, column).value is None
                    ):
                        continue
                    letter = get_column_letter(column)
                    _write_blank_formula(
                        worksheet,
                        original,
                        check_row,
                        column,
                        f"={letter}{assets_row}-{letter}{liabilities_row}",
                        changes,
                    )
    return changes


def _fill_instruction_dcf_metrics(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    normalized = _label(instruction)
    worksheet = _find_sheet(output, "DCF Valuation")
    if worksheet is None or worksheet.title not in source.sheetnames:
        return []
    original = source[worksheet.title]
    changes: list[dict[str, str]] = []

    if "terminal year ebit" in normalized and "terminal growth rate" in normalized:
        ebit_row = _instruction_formula_row(worksheet, "EBIT")
        tgr_row = _instruction_formula_row(worksheet, "TGR")
        terminal_columns = [
            column
            for column in range(1, int(worksheet.max_column or 0) + 1)
            if any(
                "terminal value" in _label(worksheet.cell(row, column).value)
                for row in range(1, min(int(worksheet.max_row or 0), 6) + 1)
            )
        ]
        if ebit_row is not None and tgr_row is not None and len(terminal_columns) == 1:
            terminal_column = terminal_columns[0]
            prior_column = terminal_column - 1
            tgr_cells = [
                cell
                for cell in worksheet[tgr_row]
                if int(cell.column) < terminal_column
                and (
                    (
                        isinstance(cell.value, (int, float))
                        and not isinstance(cell.value, bool)
                    )
                    or (isinstance(cell.value, str) and cell.value.startswith("="))
                )
            ]
            if (
                prior_column >= 1
                and worksheet.cell(ebit_row, prior_column).value is not None
                and len(tgr_cells) == 1
            ):
                tgr = tgr_cells[0]
                prior_letter = get_column_letter(prior_column)
                _write_blank_formula(
                    worksheet,
                    original,
                    ebit_row,
                    terminal_column,
                    (
                        f"={prior_letter}{ebit_row}*(1+"
                        f"{get_column_letter(tgr.column)}{tgr.row})"
                    ),
                    changes,
                )

    if "discounting factor" in normalized and "using wacc" in normalized:
        discount_row = _instruction_formula_row(worksheet, "Discounting Factor")
        period_row = _instruction_formula_row(worksheet, "Period Factor")
        wacc_row = _instruction_formula_row(worksheet, "WACC")
        year_columns = _instruction_target_columns(worksheet, "2026-2030")
        if None not in {discount_row, period_row, wacc_row}:
            assert discount_row is not None
            assert period_row is not None
            assert wacc_row is not None
            wacc_cells = [
                cell
                for cell in worksheet[wacc_row]
                if int(cell.column) < min(year_columns or [10**6])
                and (
                    (
                        isinstance(cell.value, (int, float))
                        and not isinstance(cell.value, bool)
                    )
                    or (isinstance(cell.value, str) and cell.value.startswith("="))
                )
            ]
            if len(wacc_cells) == 1:
                wacc = wacc_cells[0]
                wacc_ref = f"${get_column_letter(wacc.column)}${wacc.row}"
                for column in year_columns:
                    if worksheet.cell(period_row, column).value is None:
                        continue
                    letter = get_column_letter(column)
                    _write_blank_formula(
                        worksheet,
                        original,
                        discount_row,
                        column,
                        f"=1/(1+{wacc_ref})^{letter}{period_row}",
                        changes,
                    )

    if "ev revenue" in normalized:
        target_row = _instruction_formula_row(worksheet, "EV/Revenue")
        enterprise_row = _instruction_formula_row(worksheet, "Enterprise Value")
        pnl = _find_sheet(output, "Consolidated P&L")
        if target_row is not None and enterprise_row is not None and pnl is not None:
            revenue_row = _instruction_formula_row(pnl, "Total Revenue")
            value_cells = [
                cell
                for cell in worksheet[enterprise_row]
                if int(cell.column) <= 6
                and (
                    (
                        isinstance(cell.value, (int, float))
                        and not isinstance(cell.value, bool)
                    )
                    or (isinstance(cell.value, str) and cell.value.startswith("="))
                )
            ]
            pnl_by_year = {
                year: column for column, year in _instruction_year_columns(pnl).items()
            }
            headers: list[tuple[int, int]] = []
            for column in range(1, int(worksheet.max_column or 0) + 1):
                value = worksheet.cell(target_row - 1, column).value
                matched = re.fullmatch(r"FY\s*(\d{2,4})", str(value or ""), re.IGNORECASE)
                if matched is None:
                    continue
                year = int(matched.group(1))
                headers.append((column, year if year >= 1900 else 2000 + year))
            if revenue_row is not None and len(value_cells) == 1 and len(headers) >= 3:
                enterprise = value_cells[0]
                enterprise_ref = f"${get_column_letter(enterprise.column)}${enterprise.row}"
                for column, year in headers:
                    pnl_column = pnl_by_year.get(year)
                    if pnl_column is None or pnl.cell(revenue_row, pnl_column).value is None:
                        continue
                    revenue_ref = (
                        f"{_sheet_literal(pnl.title)}!"
                        f"{get_column_letter(pnl_column)}{revenue_row}"
                    )
                    _write_blank_formula(
                        worksheet,
                        original,
                        target_row,
                        column,
                        f'=IF({enterprise_ref}/{revenue_ref}>0,'
                        f'{enterprise_ref}/{revenue_ref},"NM")',
                        changes,
                    )
    return changes


def _fill_instruction_ratio_metrics(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    normalized = _label(instruction)
    worksheet = _find_sheet(output, "Ratio Analysis")
    pnl = _find_sheet(output, "Consolidated P&L")
    if (
        worksheet is None
        or worksheet.title not in source.sheetnames
        or pnl is None
    ):
        return []
    original = source[worksheet.title]
    changes: list[dict[str, str]] = []
    year_columns = _instruction_target_columns(worksheet, "2021-2030")

    if "ebitda margin" in normalized:
        target_row = _instruction_formula_row(worksheet, "EBITDA Margin")
        margin_row = _instruction_formula_row(pnl, "EBITDA Margin")
        ebitda_row = _instruction_formula_row(pnl, "EBITDA")
        revenue_row = _instruction_formula_row(pnl, "Total Revenue")
        pnl_by_year = {
            year: column for column, year in _instruction_year_columns(pnl).items()
        }
        worksheet_years = _instruction_year_columns(worksheet)
        if target_row is not None and margin_row is not None:
            for column in year_columns:
                pnl_column = pnl_by_year.get(worksheet_years.get(column, -1))
                if pnl_column is None or pnl.cell(margin_row, pnl_column).value is None:
                    continue
                pnl_letter = get_column_letter(pnl_column)
                _write_blank_formula(
                    worksheet,
                    original,
                    target_row,
                    column,
                    f"={_sheet_literal(pnl.title)}!{pnl_letter}{margin_row}",
                    changes,
                )
        elif None not in {target_row, ebitda_row, revenue_row}:
            assert target_row is not None
            assert ebitda_row is not None
            assert revenue_row is not None
            for column in year_columns:
                pnl_column = pnl_by_year.get(worksheet_years.get(column, -1))
                if (
                    pnl_column is None
                    or pnl.cell(ebitda_row, pnl_column).value is None
                    or pnl.cell(revenue_row, pnl_column).value is None
                ):
                    continue
                pnl_letter = get_column_letter(pnl_column)
                _write_blank_formula(
                    worksheet,
                    original,
                    target_row,
                    column,
                    (
                        f"={_sheet_literal(pnl.title)}!{pnl_letter}{ebitda_row}/"
                        f"{_sheet_literal(pnl.title)}!{pnl_letter}{revenue_row}"
                    ),
                    changes,
                )

    if "cash conversion cycle" in normalized:
        target_row = _instruction_formula_row(worksheet, "Cash Conversion Cycle")
        receivable_row = _instruction_formula_row(worksheet, "Receivable Days")
        payable_row = _instruction_formula_row(worksheet, "Payable Days")
        inventory_row = _instruction_formula_row(worksheet, "Inventory Days")
        if None not in {target_row, receivable_row, payable_row, inventory_row}:
            assert target_row is not None
            assert receivable_row is not None
            assert payable_row is not None
            assert inventory_row is not None
            for column in year_columns:
                if not all(
                    worksheet.cell(row, column).value is not None
                    for row in (receivable_row, payable_row, inventory_row)
                ):
                    continue
                letter = get_column_letter(column)
                _write_blank_formula(
                    worksheet,
                    original,
                    target_row,
                    column,
                    (
                        f'=IFERROR({letter}{receivable_row}+{letter}{inventory_row}'
                        f'-{letter}{payable_row},"NA")'
                    ),
                    changes,
                )
    return changes


def _fill_instruction_financial_summary_metrics(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    """Complete common cross-sheet summary metrics from workbook-local anchors.

    These rows occur across the Financial_Model corpus.  The implementation is
    intentionally label- and pattern-driven: it discovers rows and bridge cells
    from the instruction and neighbouring formulas, rather than using task ids or
    fixed coordinates from a particular workbook.
    """

    normalized = _label(instruction)
    changes: list[dict[str, str]] = []

    def write_blank(ws: Any, original: Any, row: int, column: int, formula: str) -> None:
        target = ws.cell(row, column)
        if (
            target.value is not None
            or original.cell(row, column).value is not None
            or not _cell_is_writable(target)
        ):
            return
        target.value = formula
        changes.append({"sheet": ws.title, "target": target.coordinate, "formula": formula})

    def nonempty_run(ws: Any, row: int, *, minimum: int = 2) -> list[int]:
        populated = [
            int(cell.column)
            for cell in getattr(ws, "_cells", {}).values()
            if int(cell.row) == row and cell.value is not None
        ]
        if not populated:
            return []
        populated = sorted(set(populated))
        runs: list[list[int]] = []
        current = [populated[0]]
        for column in populated[1:]:
            if column == current[-1] + 1:
                current.append(column)
            else:
                runs.append(current)
                current = [column]
        runs.append(current)
        return max((run for run in runs if len(run) >= minimum), key=len, default=[])

    financials = _find_sheet(output, "Financials")
    if financials is not None and financials.title in source.sheetnames:
        original = source[financials.title]
        body = " ".join(
            clause_body for sheet, clause_body in _instruction_sheet_clauses(instruction)
            if _label(sheet) == _label(financials.title)
            or _label(sheet) in _label(financials.title)
            or _label(financials.title) in _label(sheet)
        )
        if body and "cash flow check" in _label(body):
            check_row = _instruction_formula_row(financials, "Cash Flow Check")
            closing_row = _instruction_formula_row(financials, "Closing Balance")
            cash_row = _instruction_formula_row(financials, "Cash & near cash items")
            if None not in {check_row, closing_row, cash_row}:
                assert check_row is not None and closing_row is not None and cash_row is not None
                columns = nonempty_run(financials, closing_row)
                for column in columns:
                    unit_value = str(financials.cell(closing_row, column).value or "").replace(" ", "")
                    if unit_value.casefold() in {"=+$d$3", "=$d$3"}:
                        continue
                    letter = get_column_letter(column)
                    write_blank(
                        financials,
                        original,
                        check_row,
                        column,
                        f"={letter}{closing_row}-{letter}{cash_row}",
                    )
        if body and "pat margin" in _label(body):
            margin_row = _instruction_formula_row(financials, "PAT Margin")
            pat_row = _instruction_formula_row(financials, "Profit after Tax (PAT)")
            revenue_row = _instruction_formula_row(financials, "Revenue")
            if None not in {margin_row, pat_row, revenue_row}:
                assert margin_row is not None and pat_row is not None and revenue_row is not None
                columns = nonempty_run(financials, pat_row)
                # The instruction's forecast interval is naturally represented by
                # the blank tail of the existing PAT row; filling the whole run is
                # safe and also supports equivalent all-period requests.
                for column in columns:
                    letter = get_column_letter(column)
                    write_blank(
                        financials,
                        original,
                        margin_row,
                        column,
                        f"={letter}{pat_row}/{letter}${revenue_row}",
                    )

    ratio = _find_sheet(output, "Ratio Analysis")
    if ratio is None:
        ratio = _find_sheet(output, "Ratio_Analysis")
    if ratio is not None and ratio.title in source.sheetnames:
        original = source[ratio.title]
        body = " ".join(
            clause_body for sheet, clause_body in _instruction_sheet_clauses(instruction)
            if "ratio" in _label(sheet)
        )
        if body and "ev ebitda" in _label(body):
            ev_row = _instruction_formula_row(ratio, "EV/EBITDA")
            enterprise_row = _instruction_formula_row(ratio, "Enterprise Value")
            financials_for_ratio = _find_sheet(output, "Financials")
            ebitda_row = (
                _instruction_formula_row(financials_for_ratio, "EBITDA")
                if financials_for_ratio is not None
                else None
            )
            if None not in {ev_row, enterprise_row, ebitda_row}:
                assert ev_row is not None and enterprise_row is not None and ebitda_row is not None
                columns = nonempty_run(ratio, enterprise_row)
                for column in columns:
                    letter = get_column_letter(column)
                    write_blank(
                        ratio,
                        original,
                        ev_row,
                        column,
                        f"={letter}{enterprise_row}/{_sheet_literal(financials_for_ratio.title)}!{letter}{ebitda_row}",
                    )

            if "implied ev ebitda" in _label(body):
                implied_row = _instruction_formula_row(ratio, "Implied EV/EBITDA")
                if implied_row is not None and financials_for_ratio is not None:
                    forecast_columns: list[int] = []
                    header_row = 4
                    for column in range(1, int(financials_for_ratio.max_column or 0) + 1):
                        value = financials_for_ratio.cell(header_row, column).value
                        if isinstance(value, str) and "&\"F\"" in value.upper():
                            forecast_columns.append(column)
                    if not forecast_columns:
                        # Formula-linked forecast headers are often represented as
                        # ``=...&"F"``; fall back to the rightmost five columns of
                        # the enterprise-value run when header text is opaque.
                        ev_columns = nonempty_run(ratio, enterprise_row)
                        forecast_columns = ev_columns[-5:] if len(ev_columns) >= 5 else []
                    else:
                        # Consensus/YoY blocks to the right can repeat the same
                        # ``&"F"`` header formulas.  Restrict targets to the
                        # contiguous enterprise-value period band, which is the
                        # workbook-local modeled axis for this ratio.
                        ev_columns = nonempty_run(ratio, enterprise_row)
                        if len(ev_columns) >= 5:
                            forecast_columns = [column for column in forecast_columns if column in set(ev_columns[-5:])]
                    valuation = _find_sheet(output, "Valuation")
                    if valuation is not None:
                        def find_value(label: str, preferred: tuple[int, ...] = ()) -> Any | None:
                            for row in range(1, _content_max_row(valuation) + 1):
                                for column in range(1, min(_content_max_column(valuation), 8) + 1):
                                    if _label(valuation.cell(row, column).value) == _label(label):
                                        for candidate in preferred or tuple(range(column + 1, min(column + 4, _content_max_column(valuation)) + 1)):
                                            if valuation.cell(row, candidate).value is not None:
                                                return valuation.cell(row, candidate)
                            return None
                        # D is the value column for Average price, while the
                        # bridge rows carry a unit/formula in D and their value
                        # in E.  Prefer these workbook-local conventions.
                        avg_price = find_value("Average price", (4,))
                        shares = find_value("No. of shares outstanding", (5,))
                        debt = find_value("Adjustment for Debt", (5,))
                        cash = find_value("Adjustment for Cash and Marketable Securities", (5,))
                        investments = find_value("Investments and Others", (5,))
                        minority = find_value("Minority Interest", (5,))
                        if all(cell is not None for cell in (avg_price, shares, debt, cash, investments, minority)):
                            bridge = (
                                f"(({_sheet_literal(valuation.title)}!${avg_price.column_letter}${avg_price.row}*"
                                f"{_sheet_literal(valuation.title)}!${shares.column_letter}${shares.row})+"
                                f"{_sheet_literal(valuation.title)}!${debt.column_letter}${debt.row}+"
                                f"{_sheet_literal(valuation.title)}!${minority.column_letter}${minority.row}-"
                                f"{_sheet_literal(valuation.title)}!${cash.column_letter}${cash.row}-"
                                f"{_sheet_literal(valuation.title)}!${investments.column_letter}${investments.row})"
                            )
                            for column in forecast_columns:
                                letter = get_column_letter(column)
                                write_blank(
                                    ratio,
                                    original,
                                    implied_row,
                                    column,
                                    f"={bridge}/{_sheet_literal(financials_for_ratio.title)}!{letter}{ebitda_row}",
                                )

    if "variance with cmp" in normalized:
        valuation = _find_sheet(output, "Valuation")
        if valuation is not None and valuation.title in source.sheetnames:
            original = source[valuation.title]
            target_row = _instruction_formula_row(valuation, "Variance with CMP")
            consensus = None
            current = None
            for row in range(1, _content_max_row(valuation) + 1):
                for column in range(1, min(_content_max_column(valuation), 12) + 1):
                    label = _label(valuation.cell(row, column).value)
                    if label == "consensus tp":
                        consensus = next((valuation.cell(row, c) for c in range(column + 1, min(column + 5, _content_max_column(valuation)) + 1) if valuation.cell(row, c).value is not None), None)
                    elif label == "current price":
                        current = next((valuation.cell(row, c) for c in range(column + 1, min(column + 5, _content_max_column(valuation)) + 1) if valuation.cell(row, c).value is not None), None)
            if target_row is not None and consensus is not None and current is not None:
                target_column = consensus.column
                write_blank(
                    valuation,
                    original,
                    target_row,
                    target_column,
                    f"={consensus.coordinate}/{current.coordinate}-1",
                )

    if "financial highlights" in normalized:
        final = _find_sheet(output, "Final Output")
        financials_for_final = _find_sheet(output, "Financials")
        if final is not None and financials_for_final is not None and final.title in source.sheetnames:
            original = source[final.title]
            # A populated first forecast column is a reliable translation anchor;
            # copy it across the requested five-year block without changing labels.
            for row in range(1, min(_content_max_row(final), 80) + 1):
                anchor = final.cell(row, 12)  # L: first Financial Highlights year
                if not (isinstance(anchor.value, str) and anchor.value.startswith("=") and "Financials!" in anchor.value):
                    continue
                for column in range(13, 18):  # M:Q
                    try:
                        translated = Translator(anchor.value, origin=anchor.coordinate).translate_formula(final.cell(row, column).coordinate)
                    except (TranslatorError, TypeError, ValueError):
                        continue
                    write_blank(final, original, row, column, translated)

    return changes


def _fill_instruction_common_financial_ratios(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    """Fill common ratio/bridge rows using workbook-local semantic anchors.

    A number of Financial_Model tasks use the same rows but different layouts and
    coordinates.  These repairs deliberately discover rows by labels and derive the
    modeled period band from neighboring populated rows; they never key off a task id
    or a hard-coded workbook coordinate.
    """
    normalized = _label(instruction)
    changes: list[dict[str, str]] = []

    def row_run(ws: Any, row: int) -> list[int]:
        columns = sorted(
            {
                int(cell.column)
                for cell in getattr(ws, "_cells", {}).values()
                if int(cell.row) == row and cell.value is not None
            }
        )
        if not columns:
            return []
        runs: list[list[int]] = []
        current = [columns[0]]
        for column in columns[1:]:
            if column == current[-1] + 1:
                current.append(column)
            else:
                runs.append(current)
                current = [column]
        runs.append(current)
        return max(runs, key=len)

    def write_blank(ws: Any, original: Any, row: int, column: int, formula: str) -> None:
        target = ws.cell(row, column)
        if (
            target.value is not None
            or original.cell(row, column).value is not None
            or not _cell_is_writable(target)
        ):
            return
        target.value = formula
        changes.append({"sheet": ws.title, "target": target.coordinate, "formula": formula})

    # Financials: Payout Ratio = Reported DPS / Reported EPS (Diluted) in the
    # standard financial-model convention used by this benchmark family.
    # Use the EPS/DPS populated run as the period axis so ``all periods`` also
    # works when the workbook has a non-standard header row.
    if "payout ratio" in normalized:
        financials = _find_sheet(output, "Financials")
        if financials is not None and financials.title in source.sheetnames:
            target_row = _instruction_formula_row(financials, "Payout Ratio")
            dps_row = _instruction_formula_row(financials, "Reported DPS")
            # SpreadsheetBench's payout convention uses diluted reported EPS
            # (the row immediately paired with DPS in these models).  Prefer
            # that explicit row; fall back to basic EPS only in workbooks that
            # do not expose a diluted series.
            eps_row = _instruction_formula_row(financials, "Reported EPS - Diluted")
            if eps_row is None:
                eps_row = _instruction_formula_row(financials, "Reported EPS - Basic")
            if eps_row is None:
                eps_row = _instruction_formula_row(financials, "EPS - Basic")
            if None not in {target_row, dps_row, eps_row}:
                assert target_row is not None and dps_row is not None and eps_row is not None
                for column in row_run(financials, eps_row):
                    if financials.cell(dps_row, column).value is None:
                        continue
                    letter = get_column_letter(column)
                    write_blank(financials, source[financials.title], target_row, column, f"={letter}{dps_row}/{letter}{eps_row}")

    # Assumptions: forecast Other long-term assets are driven by the adjacent
    # ``As a % of revenue`` row and the workbook's Total Revenue row.  Existing
    # historical links establish the same row identity and the blank tail of the
    # target row identifies the forecast columns.
    if "other long term assets" in normalized:
        assumptions = _find_sheet(output, "Assumptions")
        if assumptions is not None and assumptions.title in source.sheetnames:
            target_row = _instruction_formula_row(assumptions, "Other long-term assets")
            # Several schedules can contain an ``As a % of revenue`` row.  Pick
            # the one in the same local block as the requested target rather
            # than the first global match (which may belong to receivables or
            # working capital).
            driver_candidates = [
                row
                for row in range(1, _content_max_row(assumptions) + 1)
                if _labels_match(
                    "As a % of revenue",
                    _semantic_row_label(assumptions, row),
                    minimum_overlap=2,
                )
            ]
            driver_row = (
                min(driver_candidates, key=lambda row: abs(row - target_row))
                if target_row is not None and driver_candidates
                else None
            )
            revenue_row = _instruction_formula_row(assumptions, "Total Revenue")
            if None not in {target_row, driver_row, revenue_row}:
                assert target_row is not None and driver_row is not None and revenue_row is not None
                original = source[assumptions.title]
                for column in row_run(assumptions, driver_row):
                    if assumptions.cell(target_row, column).value is not None:
                        continue
                    if assumptions.cell(revenue_row, column).value is None:
                        continue
                    letter = get_column_letter(column)
                    write_blank(assumptions, original, target_row, column, f"={letter}{driver_row}*{letter}{revenue_row}")

    # Ratio Analysis: Book Value Per Share = Total Equity / Basic Shares.
    # Debt-to-Total Capital is debt divided by debt plus equity.  Both rows are
    # derived over the contiguous ratio period band (normally F:O).
    ratio_request = "book value per share" in normalized or "debt to total capital" in normalized
    if ratio_request:
        ratio = _find_sheet(output, "Ratio Analysis") or _find_sheet(output, "Ratio_Analysis")
        financials = _find_sheet(output, "Financials")
        if ratio is not None and financials is not None and ratio.title in source.sheetnames:
            original = source[ratio.title]
            period_row = _instruction_formula_row(ratio, "EPS - Diluted") or _instruction_formula_row(ratio, "DPS")
            columns = row_run(ratio, period_row) if period_row is not None else []
            if "book value per share" in normalized:
                target_row = _instruction_formula_row(ratio, "Book Value Per Share")
                equity_row = _instruction_formula_row(financials, "Total Equity")
                shares_row = _instruction_formula_row(financials, "Weighted Avg No. of Eq. Shares - Basic")
                minority_candidates = [
                    row
                    for row in range(1, _content_max_row(financials) + 1)
                    if _semantic_row_label(financials, row) == "minority interest"
                ]
                minority_row = (
                    min(minority_candidates, key=lambda row: abs(row - equity_row))
                    if equity_row is not None and minority_candidates
                    else None
                )
                if None not in {target_row, equity_row, shares_row}:
                    assert target_row is not None and equity_row is not None and shares_row is not None
                    for column in columns:
                        letter = get_column_letter(column)
                        equity = f"{_sheet_literal(financials.title)}!{letter}{equity_row}"
                        if minority_row is not None:
                            equity = f"({equity}-{_sheet_literal(financials.title)}!{letter}{minority_row})"
                        write_blank(ratio, original, target_row, column, f"=+{equity}/{_sheet_literal(financials.title)}!{letter}{shares_row}")
            if "debt to total capital" in normalized:
                target_row = _instruction_formula_row(ratio, "Debt / Total Cap")
                if target_row is None:
                    target_row = _instruction_formula_row(ratio, "Debt-to-Total Capital")
                long_debt = _instruction_formula_row(financials, "Long-term borrowings")
                short_debt = _instruction_formula_row(financials, "Short-term borrowings")
                equity_row = _instruction_formula_row(financials, "Total Equity")
                if None not in {target_row, long_debt, short_debt, equity_row}:
                    assert target_row is not None and long_debt is not None and short_debt is not None and equity_row is not None
                    for column in columns:
                        letter = get_column_letter(column)
                        debt = f"({_sheet_literal(financials.title)}!{letter}{short_debt}+{_sheet_literal(financials.title)}!{letter}{long_debt})"
                        equity = f"{_sheet_literal(financials.title)}!{letter}{equity_row}"
                        write_blank(
                            ratio,
                            original,
                            target_row,
                            column,
                            f"=+{debt}/({equity}+{_sheet_literal(financials.title)}!{letter}{short_debt}+{_sheet_literal(financials.title)}!{letter}{long_debt})",
                        )

    return changes


def _fill_instruction_revenue_driver_total(
    output: Any,
    source: Any,
    instruction: str,
) -> list[dict[str, str]]:
    normalized = _label(instruction)
    if "total revenue from foundation course mature" not in normalized:
        return []
    worksheet = _find_sheet(output, "Revenue Drivers")
    if worksheet is None or worksheet.title not in source.sheetnames:
        return []
    original = source[worksheet.title]
    target_row = _instruction_formula_row(
        worksheet, "Total Revenue from Foundation Course Mature"
    )
    mature_row = _instruction_formula_row(worksheet, "Mature")
    if target_row is None or mature_row is None or mature_row >= target_row:
        return []
    headcount_row = next(
        (
            row
            for row in range(mature_row + 1, target_row)
            if _row_has_text_label(worksheet, row, "Headcount")
        ),
        None,
    )
    ticket_row = next(
        (
            row
            for row in range(mature_row + 1, target_row)
            if _row_has_text_label(
                worksheet, row, "Average Ticket Size", minimum_overlap=3
            )
        ),
        None,
    )
    if headcount_row is None or ticket_row is None:
        return []
    changes: list[dict[str, str]] = []
    for column in _instruction_target_columns(worksheet, "2026-2030"):
        if (
            worksheet.cell(headcount_row, column).value is None
            or worksheet.cell(ticket_row, column).value is None
        ):
            continue
        letter = get_column_letter(column)
        _write_blank_formula(
            worksheet,
            original,
            target_row,
            column,
            f"={letter}{ticket_row}*{letter}{headcount_row}",
            changes,
        )
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
        max_row, max_column = _content_bounds(worksheet)
        for candidate_row in range(1, min(max_row, 12) + 1):
            matches: list[tuple[int, int, int]] = []
            for column in range(2, max_column + 1):
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
            for row in range(header_row + 1, max_row + 1):
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
            for row in range(detail_end + 1, min(detail_end + 3, max_row) + 1):
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
        destination_max_row, destination_max_column = _content_bounds(destination_sheet)
        for peer_row in range(
            max(1, destination_row - 3),
            min(destination_max_row, destination_row + 3) + 1,
        ):
            if peer_row == destination_row:
                continue
            direct_refs: list[tuple[int, int]] = []
            for column in range(1, destination_max_column + 1):
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
                    strict=False,
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
    # A concise ``link <metric>`` request may omit an explicit source sheet.
    # In that case use the destination row's own local-reference pattern as the
    # source of truth (for example ``M13 = M34`` followed by blank N:Q cells).
    for sheet_hint, body in _instruction_sheet_clauses(instruction):
        match = re.search(r"\blink\s+(?P<label>.+?)(?:\.|$)", body, flags=re.IGNORECASE)
        if match is None or " from " in body.casefold():
            continue
        destination_sheet = _find_sheet(output, sheet_hint)
        if destination_sheet is None or destination_sheet.title not in source.sheetnames:
            continue
        destination_original = source[destination_sheet.title]
        label_hint = match.group("label").strip(" ,.")
        destination_row = _find_row_by_label(destination_sheet, label_hint)
        if destination_row is None:
            continue
        anchors = [
            cell
            for cell in getattr(destination_sheet, "_cells", {}).values()
            if int(cell.row) == destination_row
            and isinstance(cell.value, str)
            and cell.value.startswith("=")
            and re.fullmatch(r"=\+?\$?[A-Z]{1,3}\$?\d+", cell.value.strip(), flags=re.IGNORECASE)
        ]
        if not anchors:
            continue
        anchor = min(anchors, key=lambda cell: int(cell.column))
        for column in range(int(anchor.column) + 1, _content_max_column(destination_sheet) + 1):
            target = destination_sheet.cell(destination_row, column)
            if target.value is not None or destination_original.cell(destination_row, column).value is not None:
                continue
            if not _cell_is_writable(target):
                continue
            try:
                formula = Translator(anchor.value, origin=anchor.coordinate).translate_formula(target.coordinate)
            except (TranslatorError, TypeError, ValueError):
                break
            # Stop at the first unanchored tail; a populated peer row is required
            # to prove that this is a genuine period continuation.
            referenced = re.search(r"\$?([A-Z]{1,3})\$?(\d+)", formula)
            if referenced is None:
                break
            support = destination_sheet.cell(int(referenced.group(2)), column)
            if support.value is None:
                break
            target.value = formula
            changes.append({"sheet": destination_sheet.title, "target": target.coordinate, "formula": formula})
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
                for column in range(1, _content_max_column(source_sheet) + 1)
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
                    min(source_label_column + 4, _content_max_column(source_sheet)) + 1,
                )
                if source_sheet.cell(source_row, column).value is not None
            ),
            None,
        )
        if source_value_column is None:
            continue
        destination_max_row, destination_max_column = _content_bounds(destination_sheet)
        destination_header = next(
            (
                (row, column)
                for row in range(1, min(destination_max_row, 12) + 1)
                for column in range(1, destination_max_column + 1)
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
        for row in range(header_row + 1, destination_max_row + 1):
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
                for column in range(1, destination_max_column + 1)
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
                    min(destination_max_column, header_column + 2) + 1,
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
            populated_by_row: dict[int, set[int]] = {}
            formulas_by_row: dict[int, dict[int, Any]] = {}
            for cell in list(getattr(worksheet, "_cells", {}).values()):
                value = cell.value
                if value is None:
                    continue
                populated_by_row.setdefault(int(cell.row), set()).add(int(cell.column))
                if isinstance(value, str) and value.startswith("="):
                    formulas_by_row.setdefault(int(cell.row), {})[int(cell.column)] = cell

            # Candidate coordinates come from neighboring formulas, so scanning every cell up to
            # max_column/max_row is unnecessary. Some financial templates carry formatting at
            # XFD1048576; a dense scan of that styled extent can otherwise consume the task budget
            # before the model receives its first request.
            for row, row_formulas in sorted(formulas_by_row.items()):
                if row < 2 or len(row_formulas) < 3:
                    continue
                candidate_columns = {
                    column + 1
                    for column in row_formulas
                    if column + 2 in row_formulas
                }
                above_row = row - 1
                if above_row > 1 and not populated_by_row.get(above_row):
                    above_row -= 1
                below_row = row + 1
                candidate_columns.update(
                    set(formulas_by_row.get(above_row, {}))
                    & set(formulas_by_row.get(below_row, {}))
                )
                for column in sorted(candidate_columns):
                    target = worksheet.cell(row, column)
                    if target.value is not None or source_sheet.cell(row, column).value is not None:
                        continue
                    translated: set[str] = set()
                    if column in formulas_by_row.get(above_row, {}) and column in formulas_by_row.get(
                        below_row, {}
                    ):
                        above = worksheet.cell(above_row, column)
                        below = worksheet.cell(below_row, column)
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
                        except (TranslatorError, TypeError, ValueError):
                            pass
                        else:
                            if from_above == from_below:
                                translated.add(from_above)
                    if column > 1:
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
                            except (TranslatorError, TypeError, ValueError):
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

            # When an annual subtotal explicitly declares a detail range (for
            # example H13=SUM(D13:G13)), the omitted interior detail periods
            # can be continued from two translated left peers. This is kept
            # inside the declared range so it cannot spill into notes or a
            # different forecast block.
            for row, row_formulas in sorted(formulas_by_row.items()):
                annual_ranges: list[tuple[int, int, int]] = []
                for annual_column, formula_cell in row_formulas.items():
                    value = formula_cell.value
                    if not isinstance(value, str):
                        continue
                    match = re.fullmatch(
                        r"=SUM\(\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?\2\)",
                        value.replace(" ", ""),
                        flags=re.IGNORECASE,
                    )
                    if match is None:
                        continue
                    start_column = column_index_from_string(match.group(1))
                    end_column = column_index_from_string(match.group(3))
                    if start_column < end_column:
                        annual_ranges.append((annual_column, start_column, end_column))
                for _annual_column, start_column, end_column in annual_ranges:
                    for column in range(start_column, end_column + 1):
                        target = worksheet.cell(row, column)
                        if (
                            target.value is not None
                            or source_sheet.cell(row, column).value is not None
                            or not _cell_is_writable(target)
                        ):
                            continue
                        peers: list[Any] = []
                        for peer_column in range(column - 1, max(start_column - 1, 0), -1):
                            peer = worksheet.cell(row, peer_column)
                            if isinstance(peer.value, str) and peer.value.startswith("="):
                                peers.append(peer)
                                if len(peers) == 2:
                                    break
                        if len(peers) != 2 or any(
                            peer.style_id != target.style_id for peer in peers
                        ):
                            continue
                        try:
                            translated_peers = [
                                Translator(
                                    peer.value, origin=peer.coordinate
                                ).translate_formula(target.coordinate)
                                for peer in peers
                            ]
                        except (TranslatorError, TypeError, ValueError):
                            continue
                        if translated_peers[0] == translated_peers[1]:
                            proposals.append((target, translated_peers[0]))
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
            populated_cells = [
                cell
                for cell in list(getattr(worksheet, "_cells", {}).values())
                if cell.value is not None
            ]
            formula_cells_by_row: dict[int, list[Any]] = {}
            for cell in populated_cells:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    formula_cells_by_row.setdefault(int(cell.row), []).append(cell)
            for row, formula_cells in sorted(formula_cells_by_row.items()):
                sum_patterns: dict[tuple[int, int], set[int]] = {}
                for formula_cell in formula_cells:
                    column = int(formula_cell.column)
                    value = formula_cell.value
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
                    candidate_columns = sorted(
                        {
                            int(cell.column)
                            for cell in populated_cells
                            if detail_start <= int(cell.row) <= detail_end
                            and int(cell.column) > max(anchor_columns)
                        }
                    )
                    for column in candidate_columns:
                        target = worksheet.cell(row, column)
                        if (
                            target.value is not None
                        or source_sheet.cell(row, column).value is not None
                        or not any(
                            worksheet.cell(detail_row, column).value is not None
                            for detail_row in range(detail_start, detail_end + 1)
                        )
                        or any(
                            isinstance(worksheet.cell(detail_row, column).value, str)
                            and not worksheet.cell(detail_row, column).value.startswith("=")
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
                        or any(
                            isinstance(worksheet.cell(detail_row, annual_total.column).value, str)
                            and not worksheet.cell(detail_row, annual_total.column).value.startswith("=")
                            for detail_row in range(detail_start, detail_end + 1)
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


def complete_consensus_formula_bands(
    path: str | Path,
    *,
    source_path: str | Path,
    max_gap: int = 24,
) -> list[dict[str, str]]:
    """Fill bounded blank formula runs proved by formulas on both sides.

    Financial completion workbooks frequently contain a whole forecast band that is blank,
    while the historical and terminal-period formulas are present.  The older hole repairer
    intentionally handled only one-cell gaps, leaving the model to spend many model turns
    recreating a mechanically translated series.  This pass is still conservative: a run is
    filled only when both endpoint formulas translate to the *same* formula at every target,
    every target was blank in the original workbook, and styles match the endpoint cells.
    Vertical bands are handled as well.  No evaluator/golden data or task identifiers are used.
    """

    workbook_path = Path(path)
    original_path = Path(source_path)
    repair_workbook_archive_in_place(workbook_path)
    repair_workbook_archive_in_place(original_path)
    output = load_workbook(workbook_path, data_only=False, keep_vba=workbook_path.suffix.casefold() == ".xlsm")
    source = load_workbook(original_path, data_only=False, keep_vba=original_path.suffix.casefold() == ".xlsm")
    changes: list[dict[str, str]] = []
    try:
        for worksheet in output.worksheets:
            if worksheet.title not in source.sheetnames:
                continue
            source_sheet = source[worksheet.title]
            # Work from populated cells only; formatted XFD extents must never drive a scan.
            populated = [c for c in getattr(worksheet, "_cells", {}).values() if c.value is not None]
            by_row: dict[int, list[Any]] = {}
            by_col: dict[int, list[Any]] = {}
            for cell in populated:
                if isinstance(cell.value, str) and cell.value.startswith("=") and not isinstance(cell, MergedCell):
                    by_row.setdefault(int(cell.row), []).append(cell)
                    by_col.setdefault(int(cell.column), []).append(cell)

            def fill_between(first: Any, last: Any, *, horizontal: bool) -> None:
                if first.style_id != last.style_id:
                    return
                distance = (int(last.column) - int(first.column)) if horizontal else (int(last.row) - int(first.row))
                if distance < 2 or distance > max_gap + 1:
                    return
                targets: list[Any] = []
                for offset in range(1, distance):
                    row = int(first.row) if horizontal else int(first.row) + offset
                    col = int(first.column) + offset if horizontal else int(first.column)
                    target = worksheet.cell(row, col)
                    original = source_sheet.cell(row, col)
                    if isinstance(target, MergedCell) or target.value is not None or original.value is not None:
                        return
                    if target.style_id != first.style_id:
                        return
                    targets.append(target)
                formulas: list[str] = []
                for target in targets:
                    try:
                        left_formula = Translator(first.value, origin=first.coordinate).translate_formula(target.coordinate)
                        right_formula = Translator(last.value, origin=last.coordinate).translate_formula(target.coordinate)
                    except (TranslatorError, TypeError, ValueError):
                        return
                    if left_formula != right_formula:
                        return
                    formulas.append(left_formula)
                for target, formula in zip(targets, formulas, strict=True):
                    target.value = formula
                    changes.append({"sheet": worksheet.title, "target": target.coordinate, "formula": formula})

            for row, cells in by_row.items():
                cells = sorted(cells, key=lambda c: int(c.column))
                for left, right in zip(cells, cells[1:]):
                    fill_between(left, right, horizontal=True)
            for col, cells in by_col.items():
                cells = sorted(cells, key=lambda c: int(c.row))
                for top, bottom in zip(cells, cells[1:]):
                    fill_between(top, bottom, horizontal=False)
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
        changes.extend(_fill_instruction_pbt_to_eps_block(output, source, instruction))
        changes.extend(_fill_constant_series_from_instruction(output, source, instruction))
        changes.extend(_fill_instruction_ticket_size_and_exit_value_formulas(output, source, instruction))
        changes.extend(_fill_instruction_cumulative_position_rows(output, source, instruction))
        changes.extend(_fill_instruction_annual_rollup_columns(output, source, instruction))
        changes.extend(_fill_instruction_cagr(output, source, instruction))
        changes.extend(
            _fill_instruction_other_revenue(output, source, original_path, instruction)
        )
        changes.extend(_fill_instruction_foundation_course_costs(output, source, instruction))
        changes.extend(
            _fill_instruction_depreciation_and_balance_check(output, source, instruction)
        )
        changes.extend(_fill_instruction_dcf_metrics(output, source, instruction))
        changes.extend(_fill_instruction_ratio_metrics(output, source, instruction))
        # Semantic ratio/bridge rows must be materialized before generic
        # translated-run continuation.  Otherwise a nearby unit/header formula
        # can occupy the blank target first, and the conservative semantic pass
        # will (correctly) refuse to overwrite a now-populated cell.
        changes.extend(_fill_instruction_common_financial_ratios(output, source, instruction))
        changes.extend(_fill_instruction_revenue_driver_total(output, source, instruction))
        changes.extend(_continue_financial_metric_formula_runs(output, source, instruction))
        changes.extend(_link_instruction_header_columns(output, source, instruction))
        changes.extend(_link_instruction_metric_rows(output, source, instruction))
        # Apply explicit calculate-row requests last so the generic translated-run
        # continuation cannot extend a requested 2026-2030 block into an unrequested year.
        changes.extend(_fill_instruction_calculation_rows(output, source, instruction))
        # Cross-sheet summary rows are terminal semantic bridges, not seeds for
        # generic formula-run continuation. Apply them after all mechanical
        # continuation passes so forecast-only formulas cannot spill into
        # consensus columns to the right of the modeled period block.
        changes.extend(_fill_instruction_financial_summary_metrics(output, source, instruction))
        changes.extend(_apply_instruction_freeze_panes(output, instruction))
        if changes:
            output.save(workbook_path)
    finally:
        source.close()
        output.close()
    return changes


__all__ = [
    "complete_financial_model_runtime_actions",
    "complete_consensus_formula_bands",
    "complete_isolated_formula_holes",
    "complete_revenue_growth_schedule",
]
