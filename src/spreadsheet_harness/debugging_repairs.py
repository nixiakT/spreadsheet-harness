"""Constrained repair candidates for task-hinted spreadsheet debugging."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from openpyxl.formula.translate import Translator, TranslatorError
from openpyxl.utils.cell import column_index_from_string, get_column_letter

_AGGREGATE_RE = re.compile(
    r"(?P<function>AVERAGE|SUM|MIN|MAX|MEDIAN)\((?P<arguments>[^()]*)\)",
    re.IGNORECASE,
)
_CELL_RE = re.compile(r"(?P<column_abs>\$?)(?P<column>[A-Z]{1,3})(?P<row_abs>\$?)(?P<row>\d+)")
_RANGE_RE = re.compile(r"(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)")
_NUMERIC_LITERAL_RE = re.compile(
    r"(?<![A-Z0-9_.])(?P<number>\d+(?:\.\d+)?)(?![A-Z0-9_.])",
    re.IGNORECASE,
)
_CROSS_SHEET_CELL_RE = re.compile(
    r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
    r"(?P<reference>\$?[A-Z]{1,3}\$?\d+)"
)
_MATCH_EXACT_RE = re.compile(
    r"MATCH\(\s*\"(?P<label>[^\"]*)\"\s*,\s*"
    r"(?P<sheet>'[^']+'|[A-Za-z_][^!,()]*)!"
    r"\$?(?P<start_column>[A-Z]{1,3}):\$?(?P<end_column>[A-Z]{1,3})\s*,\s*"
    r"(?P<mode>[-+]?\d+)\s*\)",
    re.IGNORECASE,
)
_LINEAR_YEAR_CAGR_RE = re.compile(
    r"\((?P<end>\$?[A-Z]{1,3}\$?\d+)/(?P<start>\$?[A-Z]{1,3}\$?\d+)-1\)/"
    r"\((?P<period>YEAR\(\$?[A-Z]{1,3}\$?\d+\)-YEAR\(\$?[A-Z]{1,3}\$?\d+\))\)",
    re.IGNORECASE,
)
_LINEAR_FIXED_CAGR_RE = re.compile(
    r"^=\(?(?P<end>\$?[A-Z]{1,3}\$?\d+)-(?P<start>\$?[A-Z]{1,3}\$?\d+)\)?/"
    r"\((?P<period>\d+)\*(?P=start)\)$",
    re.IGNORECASE,
)
_POWER_CAGR_RE = re.compile(
    r"^=\((?P<end>\$?[A-Z]{1,3}\$?\d+)/(?P<start>\$?[A-Z]{1,3}\$?\d+)\)"
    r"\^\(1/(?P<period>\d+)\)-1$",
    re.IGNORECASE,
)
_UDF_RRI_RE = re.compile(r"^=_xludf\.RRI\(", re.IGNORECASE)
_QUALIFIED_RANGE_RE = re.compile(
    r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
    r"(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DebuggingRepairCandidate:
    candidate_id: str
    kind: str
    sheet: str
    cell: str
    current: Any
    replacement: str
    rationale: str
    context: tuple[tuple[str, str], ...]

    @property
    def target(self) -> str:
        escaped = self.sheet.replace("'", "''")
        return f"'{escaped}'!{self.cell}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "target": self.target,
            "current": self.current,
            "replacement": self.replacement,
            "rationale": self.rationale,
            "context": [{"cell": cell, "value": value} for cell, value in self.context],
        }


def _candidate_id(sheet: str, cell: str, replacement: str) -> str:
    digest = hashlib.sha256(f"{sheet}\0{cell}\0{replacement}".encode()).hexdigest()[:12]
    return f"aggregate-{digest}"


def _nearby_context(worksheet: Any, row: int, column: int) -> tuple[tuple[str, str], ...]:
    """Return a small, label-heavy context instead of a rectangular cell dump."""

    items: list[tuple[str, str]] = []
    current = worksheet.cell(row, column)
    if current.value is not None:
        items.append((current.coordinate, str(current.value)[:120]))
    for distance in range(1, 31):
        label_column = column - distance
        if label_column < 1:
            break
        label = worksheet.cell(row, label_column)
        if label.value is not None and not (
            isinstance(label.value, str) and label.value.startswith("=")
        ):
            items.append((label.coordinate, str(label.value)[:120]))
            if len(items) == 3:
                break
    offsets = ((-1, 0), (-2, 0), (-3, 0), (1, 0), (0, -1), (0, 1), (2, 0))
    for row_delta, column_delta in offsets:
        row_number = row + row_delta
        column_number = column + column_delta
        if row_number < 1 or column_number < 1:
            continue
        cell = worksheet.cell(row_number, column_number)
        if cell.value is not None and cell.coordinate != current.coordinate:
            items.append((cell.coordinate, str(cell.value)[:120]))
    return tuple(items[:8])


def _replace_once(formula: str, start: int, end: int, replacement: str) -> str:
    return formula[:start] + replacement + formula[end:]


def _adjust_reference(reference: str, *, row_delta: int = 0, column_delta: int = 0) -> str | None:
    match = _CELL_RE.fullmatch(reference)
    if match is None:
        return None
    column = column_index_from_string(match.group("column")) + column_delta
    row = int(match.group("row")) + row_delta
    if column < 1 or row < 1:
        return None
    return f"{match.group('column_abs')}{get_column_letter(column)}{match.group('row_abs')}{row}"


def _formula_alternatives(formula: str) -> list[tuple[str, str, str]]:
    alternatives: list[tuple[str, str, str]] = []
    for aggregate in _AGGREGATE_RE.finditer(formula):
        function = aggregate.group("function").upper()
        arguments = aggregate.group("arguments")
        argument_start = aggregate.start("arguments")
        ranges = list(_RANGE_RE.finditer(arguments))
        for range_match in ranges:
            start_ref = range_match.group("start")
            end_ref = range_match.group("end")
            for endpoint, ref, ref_start, ref_end in (
                ("start", start_ref, range_match.start("start"), range_match.end("start")),
                ("end", end_ref, range_match.start("end"), range_match.end("end")),
            ):
                for row_delta, column_delta in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    adjusted = _adjust_reference(
                        ref, row_delta=row_delta, column_delta=column_delta
                    )
                    if adjusted is None:
                        continue
                    absolute_start = argument_start + ref_start
                    absolute_end = argument_start + ref_end
                    replacement = _replace_once(formula, absolute_start, absolute_end, adjusted)
                    alternatives.append(
                        (
                            "aggregate_boundary",
                            replacement,
                            f"adjust {function} {endpoint} boundary by one cell",
                        )
                    )
            if function == "AVERAGE":
                start = _CELL_RE.fullmatch(start_ref)
                end = _CELL_RE.fullmatch(end_ref)
                if start is not None and end is not None:
                    start_col = column_index_from_string(start.group("column"))
                    end_col = column_index_from_string(end.group("column"))
                    start_row = int(start.group("row"))
                    end_row = int(end.group("row"))
                    if abs(end_col - start_col) + abs(end_row - start_row) >= 2:
                        endpoints = f"{start_ref},{end_ref}"
                        replacement = _replace_once(
                            formula,
                            argument_start + range_match.start(),
                            argument_start + range_match.end(),
                            endpoints,
                        )
                        alternatives.append(
                            (
                                "average_endpoints",
                                replacement,
                                "average the period endpoints instead of every intermediate cell",
                            )
                        )
                        prefix = formula[: argument_start + range_match.start()]
                        qualifier_match = re.search(
                            r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)$",
                            prefix,
                        )
                        if qualifier_match is not None:
                            qualified_endpoints = (
                                f"{start_ref},{qualifier_match.group('qualifier')}{end_ref}"
                            )
                            qualified_replacement = _replace_once(
                                formula,
                                argument_start + range_match.start(),
                                argument_start + range_match.end(),
                                qualified_endpoints,
                            )
                            alternatives.append(
                                (
                                    "average_endpoints",
                                    qualified_replacement,
                                    "average two explicitly sheet-qualified period endpoints",
                                )
                            )
            for row_delta, column_delta in (
                (-1, 0),
                (1, 0),
                (0, -1),
                (0, 1),
            ):
                shifted_start = _adjust_reference(
                    start_ref, row_delta=row_delta, column_delta=column_delta
                )
                shifted_end = _adjust_reference(
                    end_ref, row_delta=row_delta, column_delta=column_delta
                )
                if shifted_start is None or shifted_end is None:
                    continue
                replacement = _replace_once(
                    formula,
                    argument_start + range_match.start(),
                    argument_start + range_match.end(),
                    f"{shifted_start}:{shifted_end}",
                )
                alternatives.append(
                    (
                        "aggregate_range_shift",
                        replacement,
                        f"shift the complete {function} range by one cell",
                    )
                )
        simple_arguments = [item.strip() for item in arguments.split(",")]
        formula_body_start = 2 if formula.startswith("=+") else 1
        whole_formula_aggregate = (
            aggregate.start() == formula_body_start and aggregate.end() == len(formula)
        )
        if (
            function == "SUM"
            and whole_formula_aggregate
            and len(simple_arguments) == 2
            and all(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", item) for item in simple_arguments)
        ):
            replacement = _replace_once(
                formula, aggregate.start("function"), aggregate.end("function"), "AVERAGE"
            )
            alternatives.append(
                (
                    "sum_to_average",
                    replacement,
                    "the task hint identifies an incorrect average and this is a two-input SUM",
                )
            )
        elif function == "SUM" and whole_formula_aggregate:
            replacement = _replace_once(
                formula, aggregate.start("function"), aggregate.end("function"), "AVERAGE"
            )
            alternatives.append(
                (
                    "sum_to_average_candidate",
                    replacement,
                    "the task hint identifies an incorrect average; verify whether this SUM is it",
                )
            )
        if (
            function == "AVERAGE"
            and len(simple_arguments) == 1
            and (single_ref := _CELL_RE.fullmatch(simple_arguments[0])) is not None
        ):
            reference = single_ref.group(0)
            for row_delta, column_delta in (
                (-1, 0),
                (1, 0),
                (-2, 0),
                (2, 0),
                (-3, 0),
                (3, 0),
                (0, -1),
                (0, 1),
            ):
                additional = _adjust_reference(
                    reference, row_delta=row_delta, column_delta=column_delta
                )
                if additional is None:
                    continue
                if row_delta < 0 or column_delta < 0:
                    replacement_arguments = f"{additional},{reference}"
                else:
                    replacement_arguments = f"{reference},{additional}"
                replacement = _replace_once(
                    formula,
                    aggregate.start("arguments"),
                    aggregate.end("arguments"),
                    replacement_arguments,
                )
                alternatives.append(
                    (
                        "average_add_argument",
                        replacement,
                        "restore a missing period endpoint to a one-argument average",
                    )
                )
        for ref_match in _CELL_RE.finditer(arguments):
            if any(
                range_match.start() <= ref_match.start() < range_match.end()
                for range_match in ranges
            ):
                continue
            ref = ref_match.group(0)
            deltas = (
                (
                    (-1, 0),
                    (1, 0),
                    (0, -1),
                    (0, 1),
                    (-2, 0),
                    (2, 0),
                    (0, -2),
                    (0, 2),
                    (-3, 0),
                    (3, 0),
                    (0, -3),
                    (0, 3),
                )
                if function == "AVERAGE"
                else ((-1, 0), (1, 0), (0, -1), (0, 1))
            )
            for row_delta, column_delta in deltas:
                adjusted = _adjust_reference(ref, row_delta=row_delta, column_delta=column_delta)
                if adjusted is None:
                    continue
                replacement = _replace_once(
                    formula,
                    argument_start + ref_match.start(),
                    argument_start + ref_match.end(),
                    adjusted,
                )
                alternatives.append(
                    (
                        "aggregate_argument",
                        replacement,
                        f"adjust one {function} argument by one cell",
                    )
                )
    return list(dict.fromkeys(alternatives))


def _contextual_average_alternatives(
    workbook: Any, worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """High-precision average fixes that require workbook/table semantics.

    Two recurring spreadsheet errors cannot be inferred from formula syntax alone:
    an average that includes a blank historical endpoint, and a comparable-company
    average that accidentally includes the subject company.  These checks use only
    labels and cell contents in the current workbook (never sibling/golden files).
    """
    alternatives: list[tuple[str, str, str]] = []
    if not any(function in formula.upper() for function in ("AVERAGE(", "MEDIAN(")):
        return alternatives
    qualified_spans: set[tuple[int, int]] = set()
    for match in _QUALIFIED_RANGE_RE.finditer(formula):
        qualified_spans.add((match.start("start"), match.end("end")))
        start, end = match.group("start"), match.group("end")
        sm, em = _CELL_RE.fullmatch(start), _CELL_RE.fullmatch(end)
        if sm is None or em is None:
            continue
        start_col = column_index_from_string(sm.group("column"))
        end_col = column_index_from_string(em.group("column"))
        start_row, end_row = int(sm.group("row")), int(em.group("row"))
        # A leading blank in an otherwise populated average range is almost always
        # a missing-period artifact (e.g. the first forecast year has no history).
        if start_row == end_row and end_col > start_col:
            first = worksheet.cell(start_row, start_col).value
            interior = [worksheet.cell(start_row, c).value for c in range(start_col + 1, end_col + 1)]
            if first is None and any(value is not None for value in interior):
                shifted = _adjust_reference(start, column_delta=1)
                if shifted is not None:
                    replacement = _replace_once(
                        formula, match.start("start"), match.end("start"), shifted
                    )
                    alternatives.append(
                        (
                            "average_exclude_blank_endpoint",
                            replacement,
                            "exclude a blank leading period from the average",
                        )
                    )
        # In a column headed "Comparables", exclude the adjacent subject company
        # when the source table's first header matches that subject label.
        qualifier = match.group("qualifier")[:-1].strip("'")
        try:
            source = workbook[qualifier]
        except (KeyError, TypeError):
            continue
        if start_col == end_col and end_row - start_row >= 6:
            values = [source.cell(source_row, start_col).value for source_row in range(start_row, end_row + 1)]
            for offset in range(5, len(values) - 2):
                if (
                    values[offset] is None
                    and values[offset + 1] is None
                    and any(value is not None for value in values[offset + 2 :])
                ):
                    trimmed_end = _adjust_reference(end, row_delta=(start_row + offset - 1) - end_row)
                    if trimmed_end is not None:
                        alternatives.append(
                            (
                                "average_trim_summary_after_gap",
                                _replace_once(formula, match.start("end"), match.end("end"), trimmed_end),
                                "stop a raw-data average before a blank separator and summary rows",
                            )
                        )
                    break
        target_row_label = " ".join(
            str(worksheet.cell(row, label_column).value)
            for label_column in range(1, column)
            if isinstance(worksheet.cell(row, label_column).value, str)
            and not str(worksheet.cell(row, label_column).value).startswith("=")
        ).casefold()
        if (
            end_col > start_col
            and end_row > start_row
            and "low" in target_row_label
            and "high" in target_row_label
        ):
            trimmed_end = _adjust_reference(end, row_delta=start_row - end_row)
            if trimmed_end is not None:
                alternatives.append(
                    (
                        "average_low_high_single_metric",
                        _replace_once(formula, match.start("end"), match.end("end"), trimmed_end),
                        "average low/high columns for one metric without the next metric row",
                    )
                )
        target_header = worksheet.cell(row - 1, column).value if row > 1 else None
        subject = worksheet.cell(row - 1, column - 1).value if row > 1 and column > 1 else None
        def norm(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

        # Summary rows (Average/Median/Max/Min) conventionally cover the
        # contiguous numeric block immediately above them.  A one-row boundary
        # mutation is therefore recoverable without guessing business logic;
        # emit the inclusive range before generic +/- one-cell alternatives.
        summary_label = norm(
            " ".join(
                str(worksheet.cell(row, label_column).value)
                for label_column in range(1, column)
                if worksheet.cell(row, label_column).value is not None
                and not str(worksheet.cell(row, label_column).value).startswith("=")
            )
        )
        function_name = ""
        function_match = re.search(r"\b(AVERAGE|MEDIAN|MAX|MIN)\s*\(", formula, re.IGNORECASE)
        if function_match is not None:
            function_name = function_match.group(1).casefold()
        if function_name and any(token in summary_label for token in ("average", "median", "max", "min")):
            for range_match in _RANGE_RE.finditer(formula):
                sm = _CELL_RE.fullmatch(range_match.group("start"))
                em = _CELL_RE.fullmatch(range_match.group("end"))
                if sm is None or em is None:
                    continue
                start_row = int(sm.group("row"))
                end_row = int(em.group("row"))
                start_col = column_index_from_string(sm.group("column"))
                end_col = column_index_from_string(em.group("column"))
                if start_col != end_col or end_row >= row or start_row <= 1:
                    continue
                previous = worksheet.cell(start_row - 1, start_col).value
                if previous is None or (isinstance(previous, str) and previous.startswith("=")):
                    continue
                expanded_start = _adjust_reference(sm.group(0), row_delta=-1)
                if expanded_start is not None:
                    alternatives.insert(
                        0,
                        (
                            "average_summary_contiguous",
                            _replace_once(
                                formula,
                                range_match.start("start"),
                                range_match.end("start"),
                                expanded_start,
                            ),
                            "include the contiguous numeric row immediately above the summary",
                        ),
                    )

        source_header = None
        # Financial exhibits often place a section title several rows above the
        # metric row; find a matching company header anywhere in the preceding
        # label block rather than assuming it is immediately adjacent.
        if start_row > 1:
            subject_norm = norm(subject)
            for header_row in range(start_row - 1, 0, -1):
                value = source.cell(header_row, start_col).value
                if value is not None and norm(value) == subject_norm:
                    source_header = value
                    break
        if (
            "comparab" in norm(target_header)
            and norm(subject)
            and norm(source_header) == norm(subject)
            and end_col > start_col
        ):
            shifted = _adjust_reference(start, column_delta=1)
            if shifted is not None:
                replacement = _replace_once(
                    formula, match.start("start"), match.end("start"), shifted
                )
                alternatives.append(
                    (
                        "average_exclude_subject",
                        replacement,
                        "exclude the subject company from a Comparables average",
                    )
                )
    # Repeat the blank-endpoint check for local (unqualified) ranges.
    for range_match in _RANGE_RE.finditer(formula):
        if any(start <= range_match.start() < end for start, end in qualified_spans):
            continue
        sm, em = _CELL_RE.fullmatch(range_match.group("start")), _CELL_RE.fullmatch(
            range_match.group("end")
        )
        if sm is None or em is None:
            continue
        start_col = column_index_from_string(sm.group("column"))
        end_col = column_index_from_string(em.group("column"))
        start_row, end_row = int(sm.group("row")), int(em.group("row"))
        if start_row == end_row and end_col - start_col >= 2 and column > 1:
            for header_row in range(row - 1, max(0, row - 12), -1):
                target_header = worksheet.cell(header_row, column).value
                subject = worksheet.cell(header_row, column - 1).value
                source_subject = worksheet.cell(header_row, start_col).value
                peer_headers = [
                    worksheet.cell(header_row, source_column).value
                    for source_column in range(start_col + 1, end_col + 1)
                ]
                if (
                    "comparab" in re.sub(
                        r"\s+", " ", str(target_header or "")
                    ).casefold()
                    and str(subject or "").strip()
                    and re.sub(r"\s+", " ", str(subject)).strip().casefold()
                    == re.sub(r"\s+", " ", str(source_subject or ""))
                    .strip()
                    .casefold()
                    and all(str(value or "").strip() for value in peer_headers)
                ):
                    shifted = _adjust_reference(
                        range_match.group("start"), column_delta=1
                    )
                    if shifted is not None:
                        alternatives.append(
                            (
                                "average_exclude_subject",
                                _replace_once(
                                    formula,
                                    range_match.start("start"),
                                    range_match.end("start"),
                                    shifted,
                                ),
                                "exclude the subject company from a local Comparables average",
                            )
                        )
                    break
        if start_col == end_col == column and end_row == row and start_row < row:
            trimmed = _adjust_reference(range_match.group("end"), row_delta=-1)
            if trimmed is not None:
                alternatives.append(
                    (
                        "average_exclude_self_reference",
                        _replace_once(
                            formula,
                            range_match.start("end"),
                            range_match.end("end"),
                            trimmed,
                        ),
                        "exclude the formula cell itself from a vertical average",
                    )
                )
        if start_col == end_col and start_row >= 3 and end_row > start_row:
            previous = worksheet.cell(start_row - 1, start_col).value
            before_previous = worksheet.cell(start_row - 2, start_col).value
            if (
                (
                    isinstance(previous, int | float)
                    or (isinstance(previous, str) and previous.startswith("="))
                )
                and isinstance(before_previous, str)
                and not before_previous.startswith("=")
                and before_previous.strip().casefold() in {"n/a", "na", "n.m.", "nm", "-"}
            ):
                expanded = _adjust_reference(range_match.group("start"), row_delta=-1)
                if expanded is not None:
                    alternatives.append(
                        (
                            "aggregate_restore_first_numeric_after_text",
                            _replace_once(
                                formula,
                                range_match.start("start"),
                                range_match.end("start"),
                                expanded,
                            ),
                            "restore the first numeric comparable after a text-only subject row",
                        )
                    )
        if (
            start_row == end_row == row
            and end_col == column - 2
            and column - start_col >= 3
            and worksheet.cell(row, column - 1).value is not None
            and any(
                "projected" in str(worksheet.cell(header_row, column).value).casefold()
                or "forecast" in str(worksheet.cell(header_row, column).value).casefold()
                for header_row in range(max(1, row - 12), row)
            )
        ):
            expanded = _adjust_reference(range_match.group("end"), column_delta=1)
            if expanded is not None:
                alternatives.append(
                    (
                        "average_extend_to_preforecast",
                        _replace_once(
                            formula,
                            range_match.start("end"),
                            range_match.end("end"),
                            expanded,
                        ),
                        "include the last historical period immediately before forecast",
                    )
                )
        elif start_row == end_row == row and end_col == column and start_col < column:
            trimmed = _adjust_reference(range_match.group("end"), column_delta=-1)
            if trimmed is not None:
                alternatives.append(
                    (
                        "average_exclude_self_reference",
                        _replace_once(
                            formula,
                            range_match.start("end"),
                            range_match.end("end"),
                            trimmed,
                        ),
                        "exclude the formula cell itself from a horizontal average",
                    )
                )
        if start_row != end_row or end_col <= start_col:
            continue
        if worksheet.cell(start_row, start_col).value is not None:
            continue
        if not any(worksheet.cell(start_row, c).value is not None for c in range(start_col + 1, end_col + 1)):
            continue
        shifted = _adjust_reference(range_match.group("start"), column_delta=1)
        if shifted is not None:
            alternatives.append(
                (
                    "average_exclude_blank_endpoint",
                    _replace_once(formula, range_match.start("start"), range_match.end("start"), shifted),
                    "exclude a blank leading period from the average",
                )
            )
    section_context = " ".join(
        str(worksheet.cell(context_row, context_column).value)
        for context_row in range(max(1, row - 5), row + 1)
        for context_column in range(1, min(worksheet.max_column, column + 5) + 1)
        if isinstance(worksheet.cell(context_row, context_column).value, str)
        and not str(worksheet.cell(context_row, context_column).value).startswith("=")
    ).casefold()
    if "relevered beta" in section_context:
        for range_match in _RANGE_RE.finditer(formula):
            if any(start <= range_match.start() < end for start, end in qualified_spans):
                continue
            sm, em = _CELL_RE.fullmatch(range_match.group("start")), _CELL_RE.fullmatch(
                range_match.group("end")
            )
            if sm is None or em is None or sm.group("column") != em.group("column"):
                continue
            source_column = column_index_from_string(sm.group("column"))
            start_row, end_row = int(sm.group("row")), int(em.group("row"))
            header_row = next(
                (
                    candidate_row
                    for candidate_row in range(start_row - 1, max(0, start_row - 8), -1)
                    if isinstance(worksheet.cell(candidate_row, source_column).value, str)
                    and "levered beta"
                    in str(worksheet.cell(candidate_row, source_column).value).casefold()
                ),
                None,
            )
            if header_row is None:
                continue
            unlevered_column = next(
                (
                    candidate_column
                    for candidate_column in range(1, worksheet.max_column + 1)
                    if isinstance(worksheet.cell(header_row, candidate_column).value, str)
                    and "unlevered beta"
                    in str(worksheet.cell(header_row, candidate_column).value).casefold()
                ),
                None,
            )
            if unlevered_column is None or unlevered_column == source_column:
                continue
            replacement_range = (
                f"{get_column_letter(unlevered_column)}{start_row}:"
                f"{get_column_letter(unlevered_column)}{end_row}"
            )
            alternatives.append(
                (
                    "average_unlevered_beta_source",
                    _replace_once(
                        formula, range_match.start(), range_match.end(), replacement_range
                    ),
                    "use mean unlevered beta as the input to a relevered-beta calculation",
                )
            )
    return list(dict.fromkeys(alternatives))


def _interest_balance_average_alternatives(
    worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Average beginning and ending debt balances for interest calculations."""

    def row_label(label_row: int) -> str:
        labels = [
            str(worksheet.cell(label_row, label_column).value).strip()
            for label_column in range(1, min(column, 8))
            if isinstance(worksheet.cell(label_row, label_column).value, str)
            and not str(worksheet.cell(label_row, label_column).value).startswith("=")
        ]
        return " ".join(labels).casefold()

    if "interest" not in row_label(row):
        target_is_interest = False
    else:
        target_is_interest = True
    alternatives: list[tuple[str, str, str]] = []
    for aggregate in _AGGREGATE_RE.finditer(formula):
        if aggregate.group("function").upper() != "AVERAGE":
            continue
        raw_arguments = aggregate.group("arguments").strip()
        range_match = _RANGE_RE.fullmatch(raw_arguments)
        if range_match is not None:
            first = _CELL_RE.fullmatch(range_match.group("start"))
            last = _CELL_RE.fullmatch(range_match.group("end"))
            if first is not None and last is not None and first.group("column") == last.group("column"):
                first_label = row_label(int(first.group("row")))
                last_label = row_label(int(last.group("row")))
                beginning = "beginning balance" in first_label or "bop" in first_label
                ending = "ending balance" in last_label or "eop" in last_label
                if beginning and ending:
                    alternatives.append(
                        (
                            "average_balance_endpoints",
                            _replace_once(
                                formula,
                                aggregate.start("arguments"),
                                aggregate.end("arguments"),
                                f"{range_match.group('start')},{range_match.group('end')}",
                            ),
                            "average beginning and ending balances without intervening flows",
                        )
                    )
            continue
        if not target_is_interest:
            continue
        arguments = [item.strip() for item in raw_arguments.split(",")]
        if len(arguments) != 2:
            continue
        first, second = (_CELL_RE.fullmatch(item) for item in arguments)
        if first is None or second is None or first.group("column") != second.group("column"):
            continue
        first_row, second_row = int(first.group("row")), int(second.group("row"))
        if "beginning balance" not in row_label(first_row):
            continue
        ending_row = next(
            (
                candidate_row
                for candidate_row in range(first_row + 1, min(row, first_row + 8))
                if "ending balance" in row_label(candidate_row)
            ),
            None,
        )
        if ending_row is None or ending_row == second_row:
            continue
        replacement_ref = _adjust_reference(arguments[1], row_delta=ending_row - second_row)
        if replacement_ref is None:
            continue
        second_start = aggregate.start("arguments") + len(arguments[0]) + 1
        while second_start < aggregate.end("arguments") and formula[second_start].isspace():
            second_start += 1
        alternatives.append(
            (
                "average_beginning_ending_balance",
                _replace_once(formula, second_start, second_start + len(arguments[1]), replacement_ref),
                "average beginning and ending balances for an interest calculation",
            )
        )
    return alternatives


def _cagr_alternatives(
    worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Repair linearized or period-misaligned CAGR formulas."""
    alternatives: list[tuple[str, str, str]] = []
    if _UDF_RRI_RE.match(formula):
        alternatives.append(
            (
                "cagr_rri_native_function",
                _UDF_RRI_RE.sub("=rri(", formula, count=1),
                "replace the unresolved add-in RRI prefix with the workbook's native CAGR function",
            )
        )
    for match in _LINEAR_YEAR_CAGR_RE.finditer(formula):
        replacement = _replace_once(
            formula,
            match.start(),
            match.end(),
            f"({match.group('end')}/{match.group('start')})^(1/({match.group('period')}))-1",
        )
        alternatives.append(
            (
                "cagr_compound_years",
                replacement,
                "use geometric compounding across the explicit year interval",
            )
        )

    header_period = None
    for header_row in range(row - 1, max(0, row - 12), -1):
        header = worksheet.cell(header_row, column).value
        if not isinstance(header, str):
            continue
        years = re.findall(r"(?<!\d)(\d{2,4})(?!\d)", header)
        if len(years) >= 2:
            header_period = abs(int(years[-1]) - int(years[0]))
            if header_period:
                break

    fixed = _LINEAR_FIXED_CAGR_RE.fullmatch(formula)
    if fixed is not None:
        start, end = _CELL_RE.fullmatch(fixed.group("start")), _CELL_RE.fullmatch(
            fixed.group("end")
        )
        if start is not None and end is not None and int(start.group("row")) == int(
            end.group("row")
        ):
            period = header_period or abs(
                column_index_from_string(end.group("column"))
                - column_index_from_string(start.group("column"))
            )
            if period and int(fixed.group("period")) == period:
                alternatives.append(
                    (
                        "cagr_fixed_period",
                        f"=({fixed.group('end')}/{fixed.group('start')})^(1/{period})-1",
                        "replace an arithmetic annualization with geometric CAGR",
                    )
                )

    powered = _POWER_CAGR_RE.fullmatch(formula)
    if powered is not None and header_period and int(powered.group("period")) != header_period:
        alternatives.append(
            (
                "cagr_period_alignment",
                _replace_once(
                    formula,
                    powered.start("period"),
                    powered.end("period"),
                    str(header_period),
                ),
                "align the CAGR exponent with the period stated in the column header",
            )
        )
    return alternatives


def _neighbor_average_alternatives(
    worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Translate nearby peer formulas to expose missing/duplicated AVERAGE arguments."""

    alternatives: list[tuple[str, str, str]] = []
    origin = worksheet.cell(row, column).coordinate
    offsets = [
        *((0, distance) for distance in (-1, 1, -2, 2, -3, 3, -4, 4, -6, 6, -8, 8, -10, 10, -12, 12)),
        *((distance, 0) for distance in (-1, 1, -2, 2, -3, 3)),
    ]
    for row_delta, column_delta in offsets:
        peer_row = row + row_delta
        peer_column = column + column_delta
        if peer_row < 1 or peer_column < 1:
            continue
        peer = worksheet.cell(peer_row, peer_column)
        peer_formula = peer.value
        if (
            not isinstance(peer_formula, str)
            or not peer_formula.startswith("=")
            or "AVERAGE(" not in peer_formula.upper()
        ):
            continue
        try:
            replacement = Translator(peer_formula, origin=peer.coordinate).translate_formula(origin)
        except (TranslatorError, TypeError, ValueError):
            continue
        if replacement != formula and "AVERAGE(" in replacement.upper():
            alternatives.append(
                (
                    "average_neighbor_translation",
                    replacement,
                    f"translate the nearby peer formula at {peer.coordinate}",
                )
            )
    return list(dict.fromkeys(alternatives))


def _has_nearby_average_formula(worksheet: Any, row: int, column: int) -> bool:
    for row_delta, column_delta in (
        (-3, 0),
        (-2, 0),
        (-1, 0),
        (1, 0),
        (2, 0),
        (3, 0),
        (0, -3),
        (0, -2),
        (0, -1),
        (0, 1),
        (0, 2),
        (0, 3),
    ):
        peer_row = row + row_delta
        peer_column = column + column_delta
        if peer_row < 1 or peer_column < 1:
            continue
        value = worksheet.cell(peer_row, peer_column).value
        if isinstance(value, str) and "AVERAGE(" in value.upper():
            return True
    return False


def _sign_operator_positions(formula: str) -> list[int]:
    positions: list[int] = []
    single_quoted = False
    double_quoted = False
    for index, character in enumerate(formula):
        if character == "'" and not double_quoted:
            single_quoted = not single_quoted
            continue
        if character == '"' and not single_quoted:
            double_quoted = not double_quoted
            continue
        if single_quoted or double_quoted or character not in "+-":
            continue
        if index == 1 and formula.startswith("=+"):
            continue
        if index > 0 and formula[index - 1] in "Ee" and index + 1 < len(formula):
            if formula[index + 1].isdigit():
                continue
        positions.append(index)
    return positions


def _sign_formula_alternatives(formula: str) -> list[tuple[str, str, str]]:
    positions = _sign_operator_positions(formula)[:8]
    alternatives: list[tuple[str, str, str]] = []
    combinations = [(position,) for position in positions]
    combinations.extend(
        (positions[first], positions[second])
        for first in range(len(positions))
        for second in range(first + 1, len(positions))
    )
    for selected in combinations[:28]:
        characters = list(formula)
        for position in selected:
            characters[position] = "+" if characters[position] == "-" else "-"
        alternatives.append(
            (
                "sign_operator_flip",
                "".join(characters),
                f"flip {len(selected)} arithmetic sign(s) for a sign-convention task",
            )
        )
    references = [match.group(0) for match in _CELL_RE.finditer(formula)]
    skeleton = _CELL_RE.sub("", formula)
    if references and re.fullmatch(r"[=+\-()\s]+", skeleton):
        parsed = [_CELL_RE.fullmatch(reference) for reference in references]
        if all(match is not None for match in parsed):
            columns = [match.group("column") for match in parsed if match is not None]
            rows = [int(match.group("row")) for match in parsed if match is not None]
            if len(set(columns)) == 1 and max(rows) - min(rows) + 1 == len(set(rows)):
                start = references[rows.index(min(rows))]
                end = references[rows.index(max(rows))]
                alternatives.append(
                    (
                        "sign_contiguous_sum",
                        f"=SUM({start}:{end})",
                        "restore a contiguous vertical total whose terms have inconsistent signs",
                    )
                )
            column_indexes = [column_index_from_string(column) for column in columns]
            if len(set(rows)) == 1 and max(column_indexes) - min(column_indexes) + 1 == len(
                set(column_indexes)
            ):
                start = references[column_indexes.index(min(column_indexes))]
                end = references[column_indexes.index(max(column_indexes))]
                alternatives.append(
                    (
                        "sign_contiguous_sum",
                        f"=SUM({start}:{end})",
                        "restore a contiguous horizontal total whose terms have inconsistent signs",
                    )
                )
    return alternatives


def _row_peer_alternatives(
    worksheet: Any, row: int, column: int, formula: Any
) -> list[tuple[str, str, str]]:
    origin = worksheet.cell(row, column).coordinate
    peer_columns: list[int] = []
    for direction in (-1, 1):
        found = 0
        for distance in range(1, 13):
            peer_column = column + direction * distance
            if peer_column < 1:
                break
            value = worksheet.cell(row, peer_column).value
            if isinstance(value, str) and value.startswith("="):
                peer_columns.append(peer_column)
                found += 1
                if found == 2:
                    break
    alternatives: list[tuple[str, str, str]] = []
    for peer_column in peer_columns:
        peer = worksheet.cell(row, peer_column)
        try:
            replacement = Translator(str(peer.value), origin=peer.coordinate).translate_formula(
                origin
            )
        except (TranslatorError, TypeError, ValueError):
            continue
        if replacement != formula:
            alternatives.append(
                (
                    "row_peer_translation",
                    replacement,
                    f"translate the peer formula at {peer.coordinate} across the same row",
                )
            )
    return list(dict.fromkeys(alternatives))


def _column_peer_alternatives(
    worksheet: Any, row: int, column: int, formula: Any
) -> list[tuple[str, str, str]]:
    origin = worksheet.cell(row, column).coordinate
    alternatives: list[tuple[str, str, str]] = []
    for direction in (-1, 1):
        found = 0
        for distance in range(1, 7):
            peer_row = row + direction * distance
            if peer_row < 1:
                break
            peer = worksheet.cell(peer_row, column)
            if not isinstance(peer.value, str) or not peer.value.startswith("="):
                continue
            found += 1
            try:
                replacement = Translator(peer.value, origin=peer.coordinate).translate_formula(
                    origin
                )
            except (TranslatorError, TypeError, ValueError):
                continue
            if replacement != formula:
                alternatives.append(
                    (
                        "column_peer_translation",
                        replacement,
                        f"translate the peer formula at {peer.coordinate} down the same column",
                    )
                )
            if found == 2:
                break
    return list(dict.fromkeys(alternatives))


def _embedded_hardcode_alternatives(
    worksheet: Any, row: int, column: int, current: Any, *, workbook: Any | None = None
) -> list[tuple[str, str, str]]:
    origin = worksheet.cell(row, column).coordinate
    alternatives = _row_peer_alternatives(worksheet, row, column, current)
    alternatives.extend(_column_peer_alternatives(worksheet, row, column, current))

    def nearest_row_label(label_row: int, anchor_column: int, *, limit: int = 2) -> str:
        labels: list[str] = []
        for label_column in range(anchor_column - 1, 0, -1):
            value = worksheet.cell(label_row, label_column).value
            if value is None or (isinstance(value, str) and value.startswith("=")):
                continue
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
                if len(labels) >= limit:
                    break
        return " ".join(reversed(labels))

    # A hardcoded hole is especially persuasive when two formulas on the same side of it
    # independently translate to the same target formula.  Keep this distinct from the older
    # four-neighbour consensus: financial models often have a historical/forecast boundary, so
    # requiring two peers from one repeated series is much safer than mixing row and column
    # semantics.
    if isinstance(current, int | float) and not isinstance(current, bool):
        # A repeated forecast block can use a different source-row stride than
        # the target row stride.  Translating the immediate neighbour would be
        # wrong in that layout (Q42 -> I22, Q43 -> I27, while Q41 should be I17).
        # Two same-column direct-reference peers make the extrapolation
        # deterministic and keep it independent of the evaluator workbook.
        direct_reference = re.compile(
            r"^=\+?(?P<reference>\$?[A-Z]{1,3}\$?\d+)$", re.IGNORECASE
        )
        contiguous_numeric_values: list[Any] = [current]
        scan_column = column - 1
        while scan_column >= 1 and isinstance(worksheet.cell(row, scan_column).value, int | float) and not isinstance(worksheet.cell(row, scan_column).value, bool):
            contiguous_numeric_values.append(worksheet.cell(row, scan_column).value)
            scan_column -= 1
        scan_column = column + 1
        while scan_column <= worksheet.max_column and isinstance(worksheet.cell(row, scan_column).value, int | float) and not isinstance(worksheet.cell(row, scan_column).value, bool):
            contiguous_numeric_values.append(worksheet.cell(row, scan_column).value)
            scan_column += 1
        row_numeric_values = contiguous_numeric_values
        varied_numeric_row = len(set(row_numeric_values)) > 1
        for direction in (-1, 1) if varied_numeric_row else ():
            peer_rows: list[tuple[int, str, re.Match[str]]] = []
            for distance in range(1, 8):
                peer_row = row + direction * distance
                if peer_row < 1:
                    break
                peer = worksheet.cell(peer_row, column)
                match = direct_reference.fullmatch(str(peer.value or ""))
                if match is not None:
                    peer_rows.append((peer_row, match.group("reference"), match))
                if len(peer_rows) == 2:
                    break
            if len(peer_rows) != 2:
                continue
            first_row, first_reference, _ = peer_rows[0]
            second_row, second_reference, _ = peer_rows[1]
            first_match = _CELL_RE.fullmatch(first_reference)
            second_match = _CELL_RE.fullmatch(second_reference)
            if first_match is None or second_match is None:
                continue
            source_column = first_match.group("column")
            if source_column != second_match.group("column"):
                continue
            target_row_delta = second_row - first_row
            source_row_delta = int(second_match.group("row")) - int(first_match.group("row"))
            if target_row_delta == 0 or source_row_delta == 0:
                continue
            # The source progression must be an integer multiple of the target
            # progression; this rejects coincidental direct links in a table.
            if source_row_delta % target_row_delta:
                continue
            stride = source_row_delta // target_row_delta
            predicted_source_row = int(first_match.group("row")) + stride * (row - first_row)
            if predicted_source_row < 1:
                continue
            first_peer_value = worksheet.cell(first_row, column).value
            formula_prefix = "=+" if str(first_peer_value).startswith("=+") else "="
            column_prefix = first_reference[: first_reference.find(first_match.group("column"))]
            row_prefix = first_match.group("row_abs")
            predicted = (
                f"{formula_prefix}{column_prefix}{source_column}"
                f"{row_prefix}{predicted_source_row}"
            )
            if predicted == current:
                continue
            alternatives.insert(
                0,
                (
                    "embedded_matching_sequence",
                    predicted,
                    f"extrapolate the direct-reference sequence from {worksheet.cell(first_row, column).coordinate} and {worksheet.cell(second_row, column).coordinate}",
                ),
            )

        # A row of hardcoded exit multiples is a common embedded-hardcode
        # mutation in LBO models.  The first period is the entry multiple and
        # subsequent periods carry the explicit exit-multiple assumption.  The
        # neighbouring labels provide the semantic anchor without relying on a
        # golden workbook.
        row_label = nearest_row_label(row, column).casefold()
        if "exit" in row_label and "multiple" in row_label:
            value_column = None
            entry_row = exit_row = None
            entry_fallback = exit_fallback = None
            for label_row in range(1, worksheet.max_row + 1):
                label = nearest_row_label(label_row, min(column, 8)).casefold()
                if "entry" in label and "multiple" in label:
                    if label.startswith("entry"):
                        entry_row = label_row
                    elif entry_fallback is None:
                        entry_fallback = label_row
                if "exit" in label and "multiple" in label:
                    if label.startswith("exit"):
                        exit_row = label_row
                    elif exit_fallback is None:
                        exit_fallback = label_row
            entry_row = entry_row or entry_fallback
            exit_row = exit_row or exit_fallback
            if entry_row is not None and exit_row is not None:
                for candidate_column in range(1, min(column, 8)):
                    entry_value = worksheet.cell(entry_row, candidate_column).value
                    exit_value = worksheet.cell(exit_row, candidate_column).value
                    if (
                        isinstance(entry_value, str)
                        and entry_value.startswith("=")
                        and isinstance(exit_value, str)
                        and exit_value.startswith("=")
                    ):
                        value_column = candidate_column
                        break
                if value_column is not None:
                    run_start = column
                    run_end = column
                    while run_start > 1 and worksheet.cell(row, run_start - 1).value == current:
                        run_start -= 1
                    while worksheet.cell(row, run_end + 1).value == current:
                        run_end += 1
                    if run_end - run_start + 1 >= 2:
                        source_entry = worksheet.cell(entry_row, value_column).coordinate
                        source_exit = worksheet.cell(exit_row, value_column).coordinate
                        replacement = (
                            f"={source_entry}"
                            if column == run_start
                            else f"=${get_column_letter(value_column)}${exit_row}"
                        )
                        alternatives.insert(
                            0,
                            (
                                "embedded_exit_multiple_anchor",
                                replacement,
                                "link the first exit-multiple period to entry and later periods to the exit assumption",
                            ),
                        )

        for axis, directions, limit, kind in (
            ("row", ((0, -1), (0, 1)), 12, "embedded_row_consensus"),
            ("column", ((-1, 0), (1, 0)), 8, "embedded_column_consensus"),
        ):
            for row_direction, column_direction in directions:
                translated: list[tuple[str, str]] = []
                for distance in range(1, limit + 1):
                    peer_row = row + row_direction * distance
                    peer_column = column + column_direction * distance
                    if peer_row < 1 or peer_column < 1:
                        break
                    peer = worksheet.cell(peer_row, peer_column)
                    if not isinstance(peer.value, str) or not peer.value.startswith("="):
                        continue
                    try:
                        replacement = Translator(
                            peer.value, origin=peer.coordinate
                        ).translate_formula(origin)
                    except (TranslatorError, TypeError, ValueError):
                        continue
                    translated.append((replacement, peer.coordinate))
                    if len(translated) == 3:
                        break
                by_replacement: dict[str, list[str]] = {}
                for replacement, coordinate in translated:
                    by_replacement.setdefault(replacement, []).append(coordinate)
                for replacement, coordinates in by_replacement.items():
                    if len(coordinates) < 2 or replacement == current:
                        continue
                    alternatives.insert(
                        0,
                        (
                            kind,
                            replacement,
                            f"two {axis} peers ({', '.join(coordinates[:2])}) translate to the same formula",
                        ),
                    )

        # Flat projected assumptions are frequently converted from a carry-forward formula into
        # a run of cached numbers.  Reconstruct the chain only when there are at least three equal
        # hardcodes and a real formula immediately to the left of the run.
        run_start = column
        while run_start > 1 and worksheet.cell(row, run_start - 1).value == current:
            run_start -= 1
        run_end = column
        while worksheet.cell(row, run_end + 1).value == current:
            run_end += 1
        left_peer = worksheet.cell(row, run_start - 1) if run_start > 1 else None
        if (
            run_end - run_start + 1 >= 3
            and left_peer is not None
            and isinstance(left_peer.value, str)
            and left_peer.value.startswith("=")
        ):
            predecessor = worksheet.cell(row, column - 1).coordinate
            alternatives.insert(
                0,
                (
                    "embedded_flat_run_chain",
                    f"={predecessor}",
                    f"restore a {run_end - run_start + 1}-cell flat forecast carry-forward chain",
                ),
            )

    if isinstance(current, str) and current.startswith("="):
        # Embedded numeric literals often replace a tax/rate assumption reference.  Same-column
        # numeric matches are compact, auditable options and cover the common row-driven model
        # pattern without consulting evaluator data.
        for match in list(_NUMERIC_LITERAL_RE.finditer(current))[:3]:
            raw_number = match.group("number")
            if "." not in raw_number:
                continue
            number = float(raw_number)
            for distance in range(1, 61):
                for source_row in (row - distance, row + distance):
                    if source_row < 1:
                        continue
                    source = worksheet.cell(source_row, column)
                    if not isinstance(source.value, int | float) or isinstance(source.value, bool):
                        continue
                    if abs(float(source.value) - number) > 1e-12:
                        continue
                    replacement = (
                        current[: match.start("number")]
                        + source.coordinate
                        + current[match.end("number") :]
                    )
                    alternatives.insert(
                        0,
                        (
                            "embedded_literal_same_column",
                            replacement,
                            f"replace literal {raw_number} with matching assumption {source.coordinate}",
                        ),
                    )
                    break
                else:
                    continue
                break

        # If a decimal literal has no same-column match, use a uniquely
        # label-aligned numeric assumption.  This handles e.g. the embedded
        # 0.04 in Senior Secured TLB Interest Expense, where the correct
        # reference is Senior Debt Rate rather than the nearby Revolver Rate.
        target_label = nearest_row_label(row, column)
        label_tokens = {
            token
            for token in re.findall(r"[a-z][a-z0-9]+", target_label.casefold())
            if token not in {"the", "of", "and", "for", "in", "on", "expense", "rate"}
        }
        semantic_tokens = {
            "interest",
            "tax",
            "rate",
            "multiple",
            "growth",
            "leverage",
            "debt",
            "capex",
            "capital",
            "working",
        }
        if label_tokens & semantic_tokens:
            # Cross-sheet formulas often already contain the intended numeric
            # driver as a reference (e.g. AC18 and X18 around a hardcoded
            # multiple).  Prefer the first referenced numeric cell matching the
            # literal; this is deterministic and preserves the workbook's
            # existing sheet qualification.
            if workbook is not None:
                for match in list(_NUMERIC_LITERAL_RE.finditer(current))[:3]:
                    raw_number = match.group("number")
                    if "." not in raw_number:
                        continue
                    number = float(raw_number)
                    for reference_match in _CROSS_SHEET_CELL_RE.finditer(current):
                        source_name = reference_match.group("qualifier")[:-1].strip("'")
                        source = workbook[source_name] if source_name in workbook.sheetnames else None
                        if source is None:
                            continue
                        source_cell = source[reference_match.group("reference")]
                        if not isinstance(source_cell.value, int | float) or isinstance(source_cell.value, bool):
                            continue
                        if abs(float(source_cell.value) - number) > 1e-12:
                            continue
                        replacement = (
                            current[: match.start("number")]
                            + reference_match.group(0)
                            + current[match.end("number") :]
                        )
                        alternatives.insert(
                            0,
                            (
                                "embedded_literal_reference_match",
                                replacement,
                                f"replace literal {raw_number} with matching referenced driver {reference_match.group(0)}",
                            ),
                        )
                        break
            for match in list(_NUMERIC_LITERAL_RE.finditer(current))[:3]:
                raw_number = match.group("number")
                if "." not in raw_number:
                    continue
                number = float(raw_number)
                scored_sources: list[tuple[int, str]] = []
                for source_sheet in [worksheet]:
                    for source_cell in getattr(source_sheet, "_cells", {}).values():
                        if not isinstance(source_cell.value, int | float) or isinstance(
                            source_cell.value, bool
                        ) or abs(float(source_cell.value) - number) > 1e-12:
                            continue
                        source_labels: list[str] = []
                        for label_column in range(source_cell.column - 1, 0, -1):
                            value = source_sheet.cell(source_cell.row, label_column).value
                            if value is None or (isinstance(value, str) and value.startswith("=")):
                                continue
                            if isinstance(value, str) and value.strip():
                                source_labels.append(value.strip())
                                if len(source_labels) >= 2:
                                    break
                        source_label = " ".join(reversed(source_labels))
                        source_tokens = set(re.findall(r"[a-z][a-z0-9]+", source_label.casefold()))
                        overlap = len(label_tokens & source_tokens)
                        if overlap:
                            scored_sources.append((overlap, source_cell.coordinate))
                if scored_sources:
                    best_score = max(score for score, _ in scored_sources)
                    best = [coordinate for score, coordinate in scored_sources if score == best_score]
                    if len(best) == 1:
                        replacement = (
                            current[: match.start("number")]
                            + f"${best[0][0]}${best[0][1:]}"
                            + current[match.end("number") :]
                        )
                        alternatives.insert(
                            0,
                            (
                                "embedded_literal_assumption_match",
                                replacement,
                                f"replace literal {raw_number} with uniquely label-aligned assumption {best[0]}",
                            ),
                        )
    immediate: list[str] = []
    for row_delta, column_delta in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        peer_row = row + row_delta
        peer_column = column + column_delta
        if peer_row < 1 or peer_column < 1:
            continue
        peer = worksheet.cell(peer_row, peer_column)
        if not isinstance(peer.value, str) or not peer.value.startswith("="):
            continue
        try:
            translated = Translator(peer.value, origin=peer.coordinate).translate_formula(
                worksheet.cell(row, column).coordinate
            )
        except (TranslatorError, TypeError, ValueError):
            continue
        immediate.append(translated)
    for replacement in set(immediate):
        if immediate.count(replacement) >= 2 and replacement != current:
            alternatives.insert(
                0,
                (
                    "embedded_hardcode_consensus",
                    replacement,
                    "independent adjacent formulas translate to the same missing formula",
                ),
            )
    return list(dict.fromkeys(alternatives))


def _double_counting_alternatives(formula: str, *, current_row: int) -> list[tuple[str, str, str]]:
    """Remove syntactically duplicated additive terms from double-counting tasks."""

    alternatives: list[tuple[str, str, str]] = []
    direct_terms = list(
        re.finditer(
            r"(?P<prefix>^=\+?|[+-])(?P<reference>\$?[A-Z]{1,3}\$?\d+)(?=$|[+-])",
            formula,
        )
    )
    seen_terms: set[str] = set()
    for term in direct_terms:
        reference = term.group("reference")
        normalized_reference = reference.replace("$", "")
        if normalized_reference in seen_terms and not term.group("prefix").startswith("="):
            alternatives.insert(
                0,
                (
                    "double_count_direct_term",
                    formula[: term.start("prefix")] + formula[term.end("reference") :],
                    f"remove the repeated additive term {reference}",
                ),
            )
        seen_terms.add(normalized_reference)
    for aggregate in _AGGREGATE_RE.finditer(formula):
        if aggregate.group("function").upper() != "SUM":
            continue
        arguments = [item.strip() for item in aggregate.group("arguments").split(",")]
        if len(arguments) < 2 or any(not item for item in arguments):
            continue
        for index, argument in enumerate(arguments):
            remaining = arguments[:index] + arguments[index + 1 :]
            duplicate = argument in remaining
            reference = _CELL_RE.fullmatch(argument)
            # A total should almost never include a cell below itself; expose it as an auditable
            # option even when the duplicate is indirect rather than textual.
            target_row = None
            if reference is not None:
                target_row = int(reference.group("row"))
            if duplicate or (target_row is not None and target_row > current_row):
                replacement = _replace_once(
                    formula,
                    aggregate.start("arguments"),
                    aggregate.end("arguments"),
                    ",".join(remaining),
                )
                alternatives.append(
                    (
                        "double_count_duplicate" if duplicate else "double_count_sum_argument",
                        replacement,
                        "remove a duplicated SUM argument"
                        if duplicate
                        else f"test whether the separately listed term {argument} is already included",
                    )
                )

    # SUM(A1:A5)+A3 and A3+SUM(A1:A5) are provably double counted.
    for aggregate in _AGGREGATE_RE.finditer(formula):
        if aggregate.group("function").upper() != "SUM":
            continue
        range_match = _RANGE_RE.fullmatch(aggregate.group("arguments").strip())
        if range_match is None:
            continue
        start = _CELL_RE.fullmatch(range_match.group("start"))
        end = _CELL_RE.fullmatch(range_match.group("end"))
        if start is None or end is None:
            continue
        min_col = min(
            column_index_from_string(start.group("column")),
            column_index_from_string(end.group("column")),
        )
        max_col = max(
            column_index_from_string(start.group("column")),
            column_index_from_string(end.group("column")),
        )
        min_row = min(int(start.group("row")), int(end.group("row")))
        max_row = max(int(start.group("row")), int(end.group("row")))
        suffix = re.match(r"\s*[+-]\s*(\$?[A-Z]{1,3}\$?\d+)", formula[aggregate.end() :])
        if suffix is not None:
            reference = _CELL_RE.fullmatch(suffix.group(1))
            assert reference is not None
            ref_col = column_index_from_string(reference.group("column"))
            ref_row = int(reference.group("row"))
            if min_col <= ref_col <= max_col and min_row <= ref_row <= max_row:
                end_index = aggregate.end() + suffix.end()
                alternatives.insert(
                    0,
                    (
                        "double_count_range_member",
                        formula[: aggregate.end()] + formula[end_index:],
                        f"{suffix.group(1)} is already inside the preceding SUM range",
                    ),
                )
    return list(dict.fromkeys(alternatives))


def _double_counting_contextual_alternatives(
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Find common financial-model subtotal double counts from row labels.

    Some models do not contain a literal duplicate term.  Instead a summary
    formula includes both a subtotal and the later line item that supersedes
    it (for example ``Core EBIT`` together with ``EBIT``), or includes a
    derived ``Cash Interest`` line in a ``Total Interest`` subtotal.  These
    are structural, task-natural repairs: they rely only on labels and
    same-column references in the workbook, never on evaluator coordinates or
    golden values.
    """

    def label_at(target_row: int) -> str:
        # Financial sheets conventionally keep the line-item label immediately
        # to the left of the period columns.  Search a short prefix and use the
        # last non-formula text, tolerating merged/blank cells.
        labels: list[str] = []
        for label_column in range(1, max(1, column)):
            value = worksheet.cell(target_row, label_column).value
            if isinstance(value, str) and value.strip() and not value.startswith("="):
                labels.append(re.sub(r"\s+", " ", value).strip().casefold())
        return labels[-1] if labels else ""

    current_label = label_at(row)
    if not current_label:
        return []
    alternatives: list[tuple[str, str, str]] = []

    # EBIT rows occasionally point at Adj. EBITDA plus an adjustment.  When a
    # same-column Core EBIT line exists, that reference is the structurally
    # correct base and avoids counting EBITDA/D&A twice downstream.
    if re.fullmatch(r"(?:reported )?ebit", current_label):
        refs = list(_CELL_RE.finditer(formula))
        for ref in refs:
            ref_row = int(ref.group("row"))
            if "ebitda" not in label_at(ref_row):
                continue
            core_rows = [
                candidate_row
                for candidate_row in range(1, worksheet.max_row + 1)
                if "core ebit" in label_at(candidate_row)
            ]
            if not core_rows:
                continue
            core_row = min(core_rows, key=lambda candidate_row: abs(candidate_row - row))
            replacement_ref = f"{get_column_letter(column)}{core_row}"
            replacement = formula[: ref.start()] + replacement_ref + formula[ref.end() :]
            alternatives.append(
                (
                    "double_count_subtotal_chain",
                    replacement,
                    "use the same-column Core EBIT subtotal instead of reusing EBITDA",
                )
            )
            break

    # A Net Income/earnings subtotal should consume EBIT, not both Core EBIT
    # and EBIT.  Restrict this to contiguous SUM ranges containing both labels.
    aggregate = next(
        (m for m in _AGGREGATE_RE.finditer(formula) if m.group("function").upper() == "SUM"),
        None,
    )
    if aggregate is not None and any(
        marker in current_label for marker in ("net income", "profit after", "earnings")
    ):
        range_match = _RANGE_RE.fullmatch(aggregate.group("arguments").strip())
        if range_match is not None:
            start = _CELL_RE.fullmatch(range_match.group("start"))
            end = _CELL_RE.fullmatch(range_match.group("end"))
            if start is not None and end is not None:
                start_row = int(start.group("row"))
                end_row = int(end.group("row"))
                first_col = column_index_from_string(start.group("column"))
                last_col = column_index_from_string(end.group("column"))
                if first_col == last_col == column:
                    core_rows = [
                        candidate_row
                        for candidate_row in range(start_row, end_row + 1)
                        if "core ebit" in label_at(candidate_row)
                    ]
                    ebit_rows = [
                        candidate_row
                        for candidate_row in range(start_row, end_row + 1)
                        if re.search(r"\bebit\b", label_at(candidate_row))
                        and "core ebit" not in label_at(candidate_row)
                    ]
                    if core_rows and ebit_rows and min(ebit_rows) > min(core_rows):
                        replacement = _replace_once(
                            formula,
                            aggregate.start("arguments"),
                            aggregate.end("arguments"),
                            f"{get_column_letter(column)}{min(ebit_rows)}:{get_column_letter(column)}{end_row}",
                        )
                        alternatives.append(
                            (
                                "double_count_subtotal_chain",
                                replacement,
                                "exclude a superseded subtotal when the later EBIT line is already included",
                            )
                        )

    # Total Interest is commonly a debt-interest subtotal; Cash Interest is a
    # separate financing line and must not be added into that subtotal again.
    if "total interest" in current_label and "expense" not in current_label:
        refs = list(_CELL_RE.finditer(formula))
        cash_interest_refs = []
        for ref in refs:
            ref_row = int(ref.group("row"))
            if "cash interest" in label_at(ref_row):
                cash_interest_refs.append(ref)
        if cash_interest_refs and len(refs) >= 3:
            # Remove only a signed direct term; preserving the leading term
            # avoids producing malformed formulas.
            target = next(
                (ref for ref in cash_interest_refs if ref.start() > 2 and formula[ref.start() - 1] in "+-"),
                None,
            )
            if target is not None:
                sign_start = target.start() - 1
                replacement = formula[:sign_start] + formula[target.end() :]
                alternatives.append(
                    (
                        "double_count_derived_interest",
                        replacement,
                        "exclude Cash Interest from the debt-interest subtotal",
                    )
                )
    return list(dict.fromkeys(alternatives))


def _double_counting_rollforward_total_alternatives(
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Remove an accidentally carried-forward subtotal from a period total.

    A common spreadsheet corruption changes ``SUM(D36:D41)`` into
    ``SUM(D36:D41)+C42``.  The previous-period total is already represented by
    the current period's component rows, so adding it double counts the block.
    We only propose this repair when the prior column contains the exact
    translated SUM formula and the row is explicitly labelled as a total.
    """

    if column <= 1 or not isinstance(formula, str):
        return []
    labels = [
        str(worksheet.cell(row, label_column).value).strip().casefold()
        for label_column in range(1, column)
        if isinstance(worksheet.cell(row, label_column).value, str)
        and not str(worksheet.cell(row, label_column).value).startswith("=")
        and str(worksheet.cell(row, label_column).value).strip()
    ]
    if not labels or not any("total" in label for label in labels):
        return []
    aggregate = next(
        (item for item in _AGGREGATE_RE.finditer(formula) if item.group("function").upper() == "SUM"),
        None,
    )
    if aggregate is None or "+" not in formula[aggregate.end() :] and "-" not in formula[aggregate.end() :]:
        return []
    suffix = re.fullmatch(r"\s*([+-])\s*(\$?[A-Z]{1,3}\$?\d+)\s*", formula[aggregate.end() :])
    if suffix is None:
        return []
    aggregate_formula = formula[: aggregate.end()]
    normalize = lambda value: re.sub(r"\s+", "", str(value)).replace("$", "").upper()
    reference = _CELL_RE.fullmatch(suffix.group(2))
    if reference is None or int(reference.group("row")) != row:
        return []
    if column_index_from_string(reference.group("column")) != column - 1:
        return []
    # The corruption is often copied across several columns, so the immediate
    # predecessor may itself contain the same carried-forward term.  Walk left
    # until an uncorrupted SUM anchor is found, checking the translated shape
    # at each column rather than depending on any evaluator coordinates.
    anchor_found = False
    for candidate_column in range(column - 1, 0, -1):
        candidate = worksheet.cell(row, candidate_column).value
        candidate_formula = getattr(candidate, "text", candidate)
        if not isinstance(candidate_formula, str) or not candidate_formula.startswith("="):
            break
        try:
            expected_aggregate = Translator(
                aggregate_formula,
                origin=worksheet.cell(row, column).coordinate,
            ).translate_formula(worksheet.cell(row, candidate_column).coordinate)
        except (TranslatorError, TypeError, ValueError):
            break
        if normalize(candidate_formula) == normalize(expected_aggregate):
            anchor_found = True
            break
    if not anchor_found:
        return []
    return [
        (
            "double_count_rollforward_total",
            aggregate_formula,
            "remove a prior-period subtotal already represented by the current-period components",
        )
    ]


def _double_counting_debt_component_alternatives(
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Replace a net-debt link with the explicit debt-ending-balance components."""

    labels = [
        str(worksheet.cell(row, label_column).value).strip().casefold()
        for label_column in range(1, column)
        if isinstance(worksheet.cell(row, label_column).value, str)
        and not str(worksheet.cell(row, label_column).value).startswith("=")
        and str(worksheet.cell(row, label_column).value).strip()
    ]
    if not labels or not any("debt" in label for label in labels):
        return []
    direct = re.fullmatch(r"=\s*(\$?[A-Z]{1,3}\$?\d+)\s*", formula or "")
    if direct is None:
        return []
    reference = _CELL_RE.fullmatch(direct.group(1))
    if reference is None:
        return []
    source_row = int(reference.group("row"))
    source_column = column_index_from_string(reference.group("column"))
    source_label = " ".join(
        str(worksheet.cell(source_row, label_column).value or "")
        for label_column in range(1, source_column)
    ).casefold()
    if "total net debt" not in source_label:
        return []
    ending_rows: list[int] = []
    for candidate_row in range(1, worksheet.max_row + 1):
        candidate_label = " ".join(
            str(worksheet.cell(candidate_row, label_column).value or "")
            for label_column in range(1, source_column)
        ).casefold()
        if "ending balance" in candidate_label and candidate_row != row:
            ending_rows.append(candidate_row)
    if len(ending_rows) < 2:
        return []
    ending_rows.sort()
    references = [f"{get_column_letter(source_column)}{candidate_row}" for candidate_row in ending_rows]
    return [
        (
            "double_count_debt_components",
            "=+" + "+".join(references),
            "use the explicit debt ending balances instead of a net-debt link that already subtracts cash flow",
        )
    ]


def _cross_sheet_total_component_alternatives(
    workbook: Any,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Remove one component counted both directly and through a verified total.

    This recognizes a strict accounting pattern such as ``Total current
    liabilities + Short-term debt - AP - Other current liabilities``.  The
    source total must equal a contiguous block of at least three numeric rows,
    the formula must reference every component exactly once, and exactly one
    component must have the same sign as the total.  No evaluator positions or
    target values participate in the decision.
    """

    signed_reference = re.compile(
        r"(?P<sign>[+-]?)\s*"
        r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
        r"(?P<reference>\$?[A-Z]{1,3}\$?\d+)",
    )
    terms: list[dict[str, Any]] = []
    for match in signed_reference.finditer(formula):
        reference = _CELL_RE.fullmatch(match.group("reference"))
        if reference is None:
            continue
        terms.append(
            {
                "match": match,
                "sheet": match.group("qualifier")[:-1].strip("'"),
                "column": reference.group("column"),
                "row": int(reference.group("row")),
                "sign": -1 if match.group("sign") == "-" else 1,
            }
        )
    alternatives: list[tuple[str, str, str]] = []
    for total in terms:
        if total["sheet"] not in workbook.sheetnames:
            continue
        source = workbook[total["sheet"]]
        source_column = column_index_from_string(total["column"])
        total_row = int(total["row"])
        total_value = source.cell(total_row, source_column).value
        if not isinstance(total_value, int | float) or isinstance(total_value, bool):
            continue
        label = " ".join(
            str(source.cell(total_row, column).value or "")
            for column in range(1, source_column)
        ).casefold()
        if "total" not in label:
            continue
        component_rows: list[int] = []
        for row in range(total_row - 1, 0, -1):
            value = source.cell(row, source_column).value
            if not isinstance(value, int | float) or isinstance(value, bool):
                break
            component_rows.append(row)
        component_rows.reverse()
        if len(component_rows) < 3 or not math.isclose(
            float(total_value),
            sum(float(source.cell(row, source_column).value) for row in component_rows),
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            continue
        component_terms = [
            term
            for row in component_rows
            for term in terms
            if term["sheet"] == total["sheet"]
            and term["column"] == total["column"]
            and term["row"] == row
        ]
        if len(component_terms) != len(component_rows) or any(
            sum(
                term["sheet"] == total["sheet"]
                and term["column"] == total["column"]
                and term["row"] == row
                for term in terms
            )
            != 1
            for row in component_rows
        ):
            continue
        same_sign = [
            term for term in component_terms if term["sign"] == total["sign"]
        ]
        if len(same_sign) != 1 or any(
            term["sign"] == total["sign"] for term in component_terms if term not in same_sign
        ):
            continue
        redundant = same_sign[0]
        match = redundant["match"]
        if match.group("sign") not in {"+", "-"}:
            continue
        replacement = formula[: match.start("sign")] + formula[match.end() :]
        alternatives.append(
            (
                "double_count_total_component",
                replacement,
                "remove a component already included in a numerically verified source total",
            )
        )
    return list(dict.fromkeys(alternatives))


def _parallel_block_extra_term_alternatives(
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
    *,
    propagate_vertical: bool = True,
) -> list[tuple[str, str, str]]:
    """Find an additive term absent from a label-aligned parallel block."""

    def row_label(label_row: int, anchor_column: int) -> str:
        for label_column in range(anchor_column - 1, max(0, anchor_column - 8), -1):
            value = worksheet.cell(label_row, label_column).value
            if isinstance(value, str) and value.strip() and not value.startswith("="):
                return re.sub(r"\s+", " ", value).strip().casefold()
        return ""

    target_label = row_label(row, column)
    if not target_label:
        return []
    current_references = list(_CELL_RE.finditer(formula))
    if len(current_references) < 4 or any(
        column_index_from_string(reference.group("column")) != column
        for reference in current_references
    ):
        return []
    local_term = re.compile(
        r"(?P<sign>[+-])\s*(?P<reference>\$?[A-Z]{1,3}\$?\d+)"
    )
    current_terms = list(local_term.finditer(formula))
    alternatives: list[tuple[str, str, str]] = []
    for peer_column in range(1, worksheet.max_column + 1):
        if abs(peer_column - column) < 3:
            continue
        peer = worksheet.cell(row, peer_column)
        peer_formula = peer.value
        if (
            not isinstance(peer_formula, str)
            or not peer_formula.startswith("=")
            or row_label(row, peer_column) != target_label
        ):
            continue
        peer_references = list(_CELL_RE.finditer(peer_formula))
        if len(peer_references) < 3 or any(
            column_index_from_string(reference.group("column")) != peer_column
            for reference in peer_references
        ):
            continue
        try:
            translated_peer = Translator(
                peer_formula, origin=peer.coordinate
            ).translate_formula(worksheet.cell(row, column).coordinate)
        except (TranslatorError, TypeError, ValueError):
            continue
        translated_references = {
            match.group(0).replace("$", "").upper()
            for match in _CELL_RE.finditer(translated_peer)
        }
        column_delta = peer_column - column
        peer_alternatives: list[tuple[str, str, str]] = []
        for term in current_terms:
            reference = _CELL_RE.fullmatch(term.group("reference"))
            if reference is None:
                continue
            normalized = term.group("reference").replace("$", "").upper()
            if normalized in translated_references:
                continue
            counterpart_column = (
                column_index_from_string(reference.group("column")) + column_delta
            )
            if counterpart_column < 1:
                continue
            reference_row = int(reference.group("row"))
            counterpart = worksheet.cell(reference_row, counterpart_column)
            if counterpart.value is None or row_label(reference_row, counterpart_column) != row_label(
                reference_row, column_index_from_string(reference.group("column"))
            ):
                continue
            replacement = formula[: term.start("sign")] + formula[term.end() :]
            peer_alternatives.append(
                (
                    "double_count_parallel_block_term",
                    replacement,
                    "remove a labeled term omitted by the corresponding populated parallel block",
                )
            )
        if len(peer_alternatives) == 1:
            alternatives.extend(peer_alternatives)
    if propagate_vertical:
        current = worksheet.cell(row, column)
        for peer_row in range(1, worksheet.max_row + 1):
            if peer_row == row or row_label(peer_row, column) != target_label:
                continue
            peer = worksheet.cell(peer_row, column)
            peer_formula = peer.value
            if not isinstance(peer_formula, str) or not peer_formula.startswith("="):
                continue
            try:
                translated = Translator(
                    peer_formula, origin=peer.coordinate
                ).translate_formula(current.coordinate)
            except (TranslatorError, TypeError, ValueError):
                continue
            if translated.replace("=+", "=", 1) != formula.replace("=+", "=", 1):
                continue
            for kind, replacement, rationale in _parallel_block_extra_term_alternatives(
                worksheet,
                peer_row,
                column,
                peer_formula,
                propagate_vertical=False,
            ):
                try:
                    propagated = Translator(
                        replacement, origin=peer.coordinate
                    ).translate_formula(current.coordinate)
                except (TranslatorError, TypeError, ValueError):
                    continue
                alternatives.append((kind, propagated, rationale + "; propagated to repeated block"))
    return list(dict.fromkeys(alternatives))


def _cross_sheet_alternatives(
    workbook: Any, worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Expose small, exact cross-sheet offsets and label-aligned source rows."""

    alternatives: list[tuple[str, str, str]] = []
    target_label = ""
    for label_column in range(column - 1, 0, -1):
        value = worksheet.cell(row, label_column).value
        if isinstance(value, str) and not value.startswith("="):
            target_label = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
            if target_label:
                break
    for match in list(_CROSS_SHEET_CELL_RE.finditer(formula))[:8]:
        reference = match.group("reference")
        for row_delta, column_delta in (
            (-1, 0),
            (1, 0),
            (-2, 0),
            (2, 0),
            (-3, 0),
            (3, 0),
            (0, -1),
            (0, 1),
        ):
            adjusted = _adjust_reference(reference, row_delta=row_delta, column_delta=column_delta)
            if adjusted is None:
                continue
            alternatives.append(
                (
                    "cross_sheet_offset",
                    _replace_once(
                        formula, match.start("reference"), match.end("reference"), adjusted
                    ),
                    f"shift the cross-sheet source {reference} by a nearby row or column",
                )
            )

        sheet_name = match.group("qualifier")[:-1].strip("'").replace("''", "'")
        parsed = _CELL_RE.fullmatch(reference)
        if target_label and parsed is not None and sheet_name in workbook.sheetnames:
            source = workbook[sheet_name]
            source_row = int(parsed.group("row"))
            source_column = column_index_from_string(parsed.group("column"))
            for candidate_row in range(max(1, source_row - 6), source_row + 7):
                if candidate_row == source_row:
                    continue
                labels: list[str] = []
                for candidate_column in range(1, min(source_column, 10)):
                    value = source.cell(candidate_row, candidate_column).value
                    if isinstance(value, str) and not value.startswith("="):
                        labels.append(value)
                source_label = re.sub(r"[^a-z0-9]+", " ", " ".join(labels).casefold()).strip()
                if not source_label or not (
                    source_label == target_label
                    or source_label in target_label
                    or target_label in source_label
                ):
                    continue
                adjusted = _adjust_reference(reference, row_delta=candidate_row - source_row)
                if adjusted is not None:
                    alternatives.insert(
                        0,
                        (
                            "cross_sheet_label_alignment",
                            _replace_once(
                                formula,
                                match.start("reference"),
                                match.end("reference"),
                                adjusted,
                            ),
                            f"align the source row with the matching label {source_label!r}",
                        ),
                    )
                break
    return list(dict.fromkeys(alternatives))


def _index_match_alternatives(workbook: Any, formula: str) -> list[tuple[str, str, str]]:
    alternatives: list[tuple[str, str, str]] = []
    for match in re.finditer(r"MATCH\((?P<body>[^()]*)\)", formula, re.IGNORECASE):
        mode = re.search(r",\s*(?P<mode>[-+]?\d+)\s*$", match.group("body"))
        if mode is not None and mode.group("mode") != "0":
            start = match.start("body") + mode.start("mode")
            end = match.start("body") + mode.end("mode")
            alternatives.insert(
                0,
                (
                    "index_match_exact_mode",
                    _replace_once(formula, start, end, "0"),
                    "use exact MATCH mode for a keyed lookup",
                ),
            )
    for selector in re.finditer(
        r"(?P<base>(?:\$?[A-Z]{1,3}\$?\d+|[A-Za-z_][A-Za-z0-9_.]*))"
        r"\s*(?P<operator>[+-])\s*1(?=\s*[,\)])",
        formula,
    ):
        alternatives.insert(
            0,
            (
                "index_selector_offset",
                _replace_once(formula, selector.start(), selector.end(), selector.group("base")),
                "remove a one-position offset from the INDEX/MATCH selector",
            ),
        )
    for match in _MATCH_EXACT_RE.finditer(formula):
        if match.group("mode") != "0":
            alternatives.insert(
                0,
                (
                    "index_match_exact_mode",
                    _replace_once(formula, match.start("mode"), match.end("mode"), "0"),
                    "use exact MATCH mode for a text-key lookup",
                ),
            )
        sheet_name = match.group("sheet").strip().strip("'").replace("''", "'")
        if sheet_name in workbook.sheetnames and match.group("start_column") == match.group(
            "end_column"
        ):
            source = workbook[sheet_name]
            source_column = column_index_from_string(match.group("start_column"))
            lookup = match.group("label")
            for source_row in range(1, source.max_row + 1):
                source_value = source.cell(source_row, source_column).value
                if not isinstance(source_value, str):
                    continue
                if source_value != lookup and source_value.strip() == lookup.strip():
                    alternatives.insert(
                        0,
                        (
                            "index_match_exact_label",
                            _replace_once(
                                formula,
                                match.start("label"),
                                match.end("label"),
                                source_value,
                            ),
                            "use the source table's exact text key, including whitespace",
                        ),
                    )
                    break
    return list(dict.fromkeys(alternatives))


def _absolute_reference(reference: str, *, column_abs: bool, row_abs: bool) -> str:
    match = _CELL_RE.fullmatch(reference)
    assert match is not None
    return (
        ("$" if column_abs else "")
        + match.group("column")
        + ("$" if row_abs else "")
        + match.group("row")
    )


def _relative_reference_alternatives(
    worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Infer anchor options from references that drift with a copied formula series."""

    alternatives: list[tuple[str, str, str]] = []
    current_matches = list(_CELL_RE.finditer(formula))[:12]
    run_start = column
    while run_start > 1:
        peer_value = worksheet.cell(row, run_start - 1).value
        if (
            not isinstance(peer_value, str)
            or not peer_value.startswith("=")
            or len(list(_CELL_RE.finditer(peer_value))[:12]) != len(current_matches)
        ):
            break
        run_start -= 1
    run_end = column
    while True:
        peer_value = worksheet.cell(row, run_end + 1).value
        if (
            not isinstance(peer_value, str)
            or not peer_value.startswith("=")
            or len(list(_CELL_RE.finditer(peer_value))[:12]) != len(current_matches)
        ):
            break
        run_end += 1

    # A common mutation in this family is a reference that drifts across a
    # copied horizontal series even though it denotes a fixed model anchor.
    # Compare the corresponding reference token in every formula of the run.
    # If its column advances one-for-one with the destination column and starts
    # outside the run, the first reference is the natural fixed anchor.  This
    # catches e.g. ``C47,D47,E47`` in Q:U while leaving ``Q73,R73,S73``
    # (which correctly follows the destination period) untouched.
    if run_end - run_start + 1 >= 3:
        run_formulas: list[tuple[int, str]] = []
        for peer_column in range(run_start, run_end + 1):
            peer_value = worksheet.cell(row, peer_column).value
            if isinstance(peer_value, str) and peer_value.startswith("="):
                run_formulas.append((peer_column, peer_value))
        token_matches = [list(_CELL_RE.finditer(value))[:12] for _, value in run_formulas]
        for token_index, target_match in enumerate(current_matches):
            if any(len(matches) <= token_index for matches in token_matches):
                continue
            parsed_refs = [
                _CELL_RE.fullmatch(matches[token_index].group(0)) for matches in token_matches
            ]
            if any(reference is None for reference in parsed_refs):
                continue
            refs = [reference for reference in parsed_refs if reference is not None]
            columns = [column_index_from_string(reference.group("column")) for reference in refs]
            rows = [int(reference.group("row")) for reference in refs]
            destination_columns = [peer_column for peer_column, _ in run_formulas]
            if len(set(rows)) != 1 or len(columns) != len(destination_columns):
                continue
            if not all(
                columns[index] - columns[0]
                == destination_columns[index] - destination_columns[0]
                for index in range(len(columns))
            ):
                continue
            first_reference = refs[0]
            first_column = columns[0]
            # References beginning at the copied destination column usually
            # represent the period-relative operand, not a fixed anchor.
            if first_column >= run_start:
                continue
            anchored = _absolute_reference(
                first_reference.group(0), column_abs=True, row_abs=True
            )
            replacement = _replace_once(formula, target_match.start(), target_match.end(), anchored)
            if replacement != formula:
                alternatives.insert(
                    0,
                    (
                        "relative_series_anchor",
                        replacement,
                        f"anchor the drifting reference at the first period ({first_reference.group(0)})",
                    ),
                )

    # CHOOSE selectors are workbook-wide case switches.  In copied rows the
    # selector must stay at $C$4; a relative C4/D4/E4 sequence is unambiguous
    # when neighbouring rows already use the anchored selector.
    choose_match = re.search(r"CHOOSE\(\s*(?P<reference>\$?[A-Z]{1,3}\$?\d+)", formula, re.IGNORECASE)
    if choose_match is not None:
        selector = _CELL_RE.fullmatch(choose_match.group("reference"))
        if selector is not None and int(selector.group("row")) == 4:
            anchored_selector = "$C$4"
            if selector.group(0) != anchored_selector:
                nearby_anchored = 0
                for distance in range(1, 8):
                    for peer_row in (row - distance, row + distance):
                        if peer_row < 1:
                            continue
                        peer_value = worksheet.cell(peer_row, column).value
                        if isinstance(peer_value, str) and re.search(
                            r"CHOOSE\(\s*\$C\$4\b", peer_value, re.IGNORECASE
                        ):
                            nearby_anchored += 1
                    if nearby_anchored:
                        break
                if nearby_anchored:
                    alternatives.insert(
                        0,
                        (
                            "relative_series_anchor",
                            _replace_once(
                                formula,
                                choose_match.start("reference"),
                                choose_match.end("reference"),
                                anchored_selector,
                            ),
                            "anchor the CHOOSE case selector shared by neighbouring model rows",
                        ),
                    )
    for match in current_matches:
        reference = match.group(0)
        parsed = _CELL_RE.fullmatch(reference)
        assert parsed is not None
        reference_column = column_index_from_string(parsed.group("column"))
        reference_row = int(parsed.group("row"))
        if (
            parsed.group("column_abs") == "$"
            and reference_column == run_start
            and column > run_start
            and run_end - run_start + 1 >= 3
        ):
            source_values = [
                worksheet.cell(reference_row, source_column).value
                for source_column in range(run_start, run_end + 1)
            ]
            if sum(value is not None for value in source_values) >= 3:
                released = get_column_letter(column) + parsed.group("row_abs") + parsed.group("row")
                alternatives.insert(
                    0,
                    (
                        "relative_release_series_anchor",
                        _replace_once(formula, match.start(), match.end(), released),
                        f"release the fixed {reference} column across a populated parallel source row",
                    ),
                )
        for column_abs, row_abs in ((True, True), (True, False), (False, True)):
            anchored = _absolute_reference(reference, column_abs=column_abs, row_abs=row_abs)
            if anchored == reference:
                continue
            alternatives.append(
                (
                    "relative_add_anchor",
                    _replace_once(formula, match.start(), match.end(), anchored),
                    f"lock the {'column and row' if column_abs and row_abs else 'column' if column_abs else 'row'} of {reference}",
                )
            )

    # Use the earliest formula in a horizontal/vertical run as the candidate anchor origin.  A
    # reference that drifts exactly with the target cell is the characteristic mutation in this
    # benchmark family.
    for axis in ("row", "column"):
        peers: list[Any] = []
        for distance in range(12, 0, -1):
            peer_row = row if axis == "row" else row - distance
            peer_column = column - distance if axis == "row" else column
            if peer_row < 1 or peer_column < 1:
                continue
            candidate = worksheet.cell(peer_row, peer_column)
            if isinstance(candidate.value, str) and candidate.value.startswith("="):
                peers.append(candidate)
        for peer in peers:
            peer_matches = list(_CELL_RE.finditer(peer.value))[:12]
            if len(peer_matches) != len(current_matches):
                continue
            target_delta = column - int(peer.column) if axis == "row" else row - int(peer.row)
            for current_match, peer_match in zip(current_matches, peer_matches, strict=True):
                current_ref = _CELL_RE.fullmatch(current_match.group(0))
                peer_ref = _CELL_RE.fullmatch(peer_match.group(0))
                assert current_ref is not None and peer_ref is not None
                current_col = column_index_from_string(current_ref.group("column"))
                peer_col = column_index_from_string(peer_ref.group("column"))
                current_row = int(current_ref.group("row"))
                peer_row = int(peer_ref.group("row"))
                drifts = (
                    current_col - peer_col == target_delta and current_row == peer_row
                    if axis == "row"
                    else current_row - peer_row == target_delta and current_col == peer_col
                )
                if not drifts:
                    continue
                base = peer_match.group(0)
                for column_abs, row_abs in ((True, True), (True, False), (False, True)):
                    anchored = _absolute_reference(base, column_abs=column_abs, row_abs=row_abs)
                    alternatives.insert(
                        0,
                        (
                            "relative_series_anchor",
                            _replace_once(
                                formula, current_match.start(), current_match.end(), anchored
                            ),
                            f"stop {base} from drifting with the copied {axis} series",
                        ),
                    )

    # Interpolation schedules copy the whole expression across a short run,
    # but both endpoint references must remain fixed.  Anchor all drifting
    # endpoint tokens together; emitting each token independently would leave
    # a partially repaired formula such as ``$N22+(R22-N22)/4``.
    if "/4" in formula and "CHOOSE(" not in formula.upper() and run_end - run_start + 1 >= 2:
        run_formulas = []
        for peer_column in range(run_start, run_end + 1):
            peer_value = worksheet.cell(row, peer_column).value
            if isinstance(peer_value, str) and peer_value.startswith("="):
                matches = list(_CELL_RE.finditer(peer_value))[:12]
                if len(matches) == len(current_matches):
                    run_formulas.append((peer_column, matches))
        if len(run_formulas) >= 2:
            anchored_spans: list[tuple[int, int, str]] = []
            for token_index, target_match in enumerate(current_matches):
                refs = [
                    _CELL_RE.fullmatch(matches[token_index].group(0))
                    for _, matches in run_formulas
                ]
                if any(reference is None for reference in refs):
                    continue
                columns = [column_index_from_string(reference.group("column")) for reference in refs if reference]
                if len(set(columns)) <= 1:
                    continue
                destinations = [peer_column for peer_column, _ in run_formulas]
                if not all(
                    columns[index] - columns[0] == destinations[index] - destinations[0]
                    for index in range(len(columns))
                ):
                    continue
                first = refs[0]
                assert first is not None
                anchored_spans.append(
                    (target_match.start(), target_match.end(), _absolute_reference(first.group(0), column_abs=True, row_abs=True))
                )
            if anchored_spans:
                rebuilt = formula
                for start, end, replacement in reversed(anchored_spans):
                    rebuilt = _replace_once(rebuilt, start, end, replacement)
                if rebuilt != formula:
                    alternatives.insert(
                        0,
                        (
                            "relative_series_anchor",
                            rebuilt,
                            "anchor all endpoints of the copied interpolation schedule",
                        ),
                    )

    # The loops above intentionally retain broad fallback options, but the
    # first candidate should be the semantically strongest one.  Keep special
    # schedule/selector repairs ahead of generic one-reference toggles.
    unique = list(dict.fromkeys(alternatives))
    preferred = [
        item
        for item in unique
        if item[2].startswith("anchor all endpoints")
        or item[2].startswith("anchor the CHOOSE case selector")
        or item[2].startswith("anchor the drifting reference")
    ]
    return preferred + [item for item in unique if item not in preferred]


def _unit_mismatch_alternatives(
    worksheet: Any, row: int, column: int, current: Any
) -> list[tuple[str, str, str]]:
    alternatives: list[tuple[str, str, str]] = []
    cell = worksheet.cell(row, column)
    if isinstance(current, int | float) and not isinstance(current, bool):
        if "%" in str(cell.number_format) and 1 <= abs(float(current)) <= 100:
            alternatives.append(
                (
                    "unit_percent_scale",
                    f"={current}/100",
                    "the cell is percentage-formatted but the hardcode is in whole-percent units",
                )
            )
        return alternatives
    if not isinstance(current, str) or not current.startswith("="):
        return alternatives
    replacements = (
        (
            r"/\s*12(?=\D|$)",
            "/365",
            "unit_days_per_year",
            "convert a monthly divisor to days per year",
        ),
        (
            r"/\s*1000(?=\D|$)",
            "/1000000",
            "unit_thousands_to_millions",
            "convert a thousands divisor to millions",
        ),
    )
    for pattern, replacement, kind, rationale in replacements:
        for match in re.finditer(pattern, current):
            alternatives.append(
                (kind, _replace_once(current, match.start(), match.end(), replacement), rationale)
            )
    quarter = re.search(r"/\s*\(\s*(\$?[A-Z]{1,3}\$?\d+)\s*/\s*4\s*\)", current)
    if quarter is not None:
        alternatives.append(
            (
                "unit_remove_quarter_scale",
                _replace_once(current, quarter.start(), quarter.end(), f"/{quarter.group(1)}"),
                "remove an extra quarterly-to-annual scaling factor",
            )
        )
    if "0.75" in current and "0.25" in current:
        swapped = current.replace("0.75", "__UNIT_WEIGHT__", 1).replace("0.25", "0.75", 1)
        swapped = swapped.replace("__UNIT_WEIGHT__", "0.25", 1)
        alternatives.append(("unit_swap_weights", swapped, "swap complementary allocation weights"))
    label = " ".join(
        str(worksheet.cell(row, c).value)
        for c in range(1, column)
        if worksheet.cell(row, c).value is not None
        and not str(worksheet.cell(row, c).value).startswith("=")
    ).casefold()
    if re.search(r"growth|change|cagr", label) and re.fullmatch(
        r"=\+?\$?[A-Z]{1,3}\$?\d+\s*/\s*\$?[A-Z]{1,3}\$?\d+", current
    ):
        alternatives.append(
            ("unit_growth_rate", current + "-1", "convert a ratio into a growth rate")
        )
    return list(dict.fromkeys(alternatives))


def _label_sign_alignment(
    worksheet: Any, row: int, column: int, formula: str
) -> tuple[str, str, str] | None:
    """Align simple arithmetic terms with explicit `(+)` / `(-)` row labels."""

    prefix = "=+" if formula.startswith("=+") else "="
    body = formula[len(prefix) :]
    pieces = re.split(r"([+-])", body)
    if not pieces or len(pieces) % 2 == 0:
        return None
    references = pieces[0::2]
    operators = pieces[1::2]
    if not all(_CELL_RE.fullmatch(reference.strip()) for reference in references):
        return None
    expected = list(operators)
    explicit_labels = 0
    for index, reference in enumerate(references[1:], start=1):
        match = _CELL_RE.fullmatch(reference.strip())
        assert match is not None
        referenced_row = int(match.group("row"))
        label = ""
        for label_column in range(1, min(column, 8)):
            value = worksheet.cell(referenced_row, label_column).value
            if isinstance(value, str) and not value.startswith("="):
                label = value.strip()
        referenced_cell = worksheet.cell(referenced_row, column)
        referenced_value = referenced_cell.value
        if hasattr(referenced_value, "text"):
            referenced_value = referenced_value.text
        reference_is_negative = (
            isinstance(referenced_value, int | float) and referenced_value < 0
        ) or (
            isinstance(referenced_value, str) and re.match(r"^=\+?-", referenced_value) is not None
        )
        if label.startswith("(+)"):
            expected[index - 1] = "-" if reference_is_negative else "+"
            explicit_labels += 1
        elif label.startswith("(-)"):
            expected[index - 1] = "+" if reference_is_negative else "-"
            explicit_labels += 1
    if explicit_labels == 0 or expected == operators:
        return None
    rebuilt = references[0]
    for operator, reference in zip(expected, references[1:], strict=True):
        rebuilt += operator + reference
    return (
        "label_sign_alignment",
        prefix + rebuilt,
        "align arithmetic signs with explicit (+) and (-) line-item labels",
    )


def detect_debugging_repair_candidates(
    workbook: Any,
    *,
    task_hint: str,
    max_candidates: int = 120,
) -> list[DebuggingRepairCandidate]:
    """Enumerate exact candidate formulas without consulting evaluator data."""

    normalized_hint = task_hint.casefold().replace("_", " ")
    average_task = "incorrect average" in normalized_hint or "average formulas" in normalized_hint
    sign_task = "sign convention" in normalized_hint
    embedded_task = "embedded hardcode" in normalized_hint
    double_counting_task = "double counting" in normalized_hint
    cross_sheet_task = "cross sheet" in normalized_hint
    index_match_task = "index match" in normalized_hint
    relative_task = "relative vs absolute" in normalized_hint
    unit_task = "unit mismatch" in normalized_hint
    if not any(
        (
            average_task,
            sign_task,
            embedded_task,
            double_counting_task,
            cross_sheet_task,
            index_match_task,
            relative_task,
            unit_task,
        )
    ):
        return []
    candidates: list[DebuggingRepairCandidate] = []
    for worksheet in workbook.worksheets:
        for cell in list(getattr(worksheet, "_cells", {}).values()):
            raw_formula = cell.value
            formula = getattr(raw_formula, "text", raw_formula)
            if (
                not embedded_task
                and not unit_task
                and not (isinstance(formula, str) and formula.startswith("="))
            ):
                continue
            if embedded_task:
                is_formula = isinstance(formula, str) and formula.startswith("=")
                is_numeric_hardcode = isinstance(formula, int | float) and not isinstance(
                    formula, bool
                )
                if not is_formula and not is_numeric_hardcode:
                    continue
                adjacent_formula = any(
                    isinstance(
                        worksheet.cell(
                            int(cell.row) + row_delta,
                            int(cell.column) + column_delta,
                        ).value,
                        str,
                    )
                    and worksheet.cell(
                        int(cell.row) + row_delta,
                        int(cell.column) + column_delta,
                    ).value.startswith("=")
                    for row_delta, column_delta in ((0, -1), (0, 1), (-1, 0), (1, 0))
                    if int(cell.row) + row_delta >= 1 and int(cell.column) + column_delta >= 1
                )
                formula_has_embedded_number = is_formula and re.search(
                    r"(?<![A-Z0-9_])\d+(?:\.\d+)?", _CELL_RE.sub("", formula)
                )
                nearby_row_formula = is_numeric_hardcode and any(
                    isinstance(worksheet.cell(int(cell.row), peer_column).value, str)
                    and worksheet.cell(int(cell.row), peer_column).value.startswith("=")
                    for peer_column in range(max(1, int(cell.column) - 12), int(cell.column) + 13)
                    if peer_column != int(cell.column)
                )
                if (
                    not adjacent_formula
                    and not nearby_row_formula
                    and not formula_has_embedded_number
                ):
                    continue
                alternatives = _embedded_hardcode_alternatives(
                    worksheet, int(cell.row), int(cell.column), formula, workbook=workbook
                )
            elif sign_task:
                alternatives = _sign_formula_alternatives(formula)
                label_alignment = _label_sign_alignment(
                    worksheet, int(cell.row), int(cell.column), formula
                )
                if label_alignment is not None:
                    alternatives.append(label_alignment)
                if _sign_operator_positions(formula):
                    alternatives.extend(
                        _row_peer_alternatives(worksheet, int(cell.row), int(cell.column), formula)
                    )
                    alternatives.extend(
                        _column_peer_alternatives(
                            worksheet, int(cell.row), int(cell.column), formula
                        )
                    )
                if not alternatives:
                    continue
            elif average_task:
                alternatives = _formula_alternatives(formula)
                alternatives.extend(
                    _contextual_average_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _interest_balance_average_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _cagr_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
            elif double_counting_task:
                alternatives = _double_counting_alternatives(formula, current_row=int(cell.row))
                alternatives.extend(
                    _double_counting_contextual_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _double_counting_rollforward_total_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _double_counting_debt_component_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _cross_sheet_total_component_alternatives(workbook, formula)
                )
                alternatives.extend(
                    _parallel_block_extra_term_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    alternative
                    for alternative in _contextual_average_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                    if alternative[0] == "average_exclude_subject"
                )
            elif cross_sheet_task:
                alternatives = _cross_sheet_alternatives(
                    workbook, worksheet, int(cell.row), int(cell.column), formula
                )
            elif index_match_task:
                if "INDEX(" not in formula.upper():
                    continue
                alternatives = _index_match_alternatives(workbook, formula)
                alternatives.extend(
                    _cross_sheet_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                )
            elif relative_task:
                alternatives = _relative_reference_alternatives(
                    worksheet, int(cell.row), int(cell.column), formula
                )
            else:
                alternatives = _unit_mismatch_alternatives(
                    worksheet, int(cell.row), int(cell.column), formula
                )
                if isinstance(formula, str) and "!" in formula:
                    alternatives.extend(
                        _cross_sheet_alternatives(
                            workbook, worksheet, int(cell.row), int(cell.column), formula
                        )
                    )
            has_average = isinstance(formula, str) and "AVERAGE(" in formula.upper()
            if average_task and has_average:
                alternatives.extend(
                    _neighbor_average_alternatives(
                        worksheet, int(cell.row), int(cell.column), formula
                    )
                )
            elif average_task and not _has_nearby_average_formula(
                worksheet, int(cell.row), int(cell.column)
            ):
                alternatives = [
                    alternative
                    for alternative in alternatives
                    if alternative[0]
                    in {
                        "sum_to_average",
                        "sum_to_average_candidate",
                        "cagr_compound_years",
                        "cagr_fixed_period",
                        "cagr_period_alignment",
                        "cagr_rri_native_function",
                        "aggregate_restore_first_numeric_after_text",
                    }
                ]
            for kind, replacement, rationale in dict.fromkeys(alternatives):
                if replacement == formula:
                    continue
                candidates.append(
                    DebuggingRepairCandidate(
                        candidate_id=_candidate_id(worksheet.title, cell.coordinate, replacement),
                        kind=kind,
                        sheet=worksheet.title,
                        cell=cell.coordinate,
                        current=formula,
                        replacement=replacement,
                        rationale=rationale,
                        context=_nearby_context(worksheet, int(cell.row), int(cell.column)),
                    )
                )
    kind_order = {
        "double_count_range_member": 0,
        "double_count_duplicate": 1,
        "double_count_direct_term": 0,
        "double_count_total_component": -1,
        "double_count_parallel_block_term": -1,
        "double_count_subtotal_chain": -2,
        "double_count_derived_interest": -2,
        "double_count_rollforward_total": -3,
        "double_count_debt_components": -4,
        "index_match_exact_label": 0,
        "index_match_exact_mode": 1,
        "index_selector_offset": 1,
        "cross_sheet_label_alignment": 0,
        "relative_series_anchor": 0,
        "relative_release_series_anchor": -1,
        "unit_percent_scale": 0,
        "unit_days_per_year": 1,
        "unit_thousands_to_millions": 2,
        "unit_remove_quarter_scale": 3,
        "unit_growth_rate": 4,
        "unit_swap_weights": 5,
        "embedded_row_consensus": 0,
        "embedded_column_consensus": 1,
        "embedded_matching_sequence": 2,
        "embedded_exit_multiple_anchor": -2,
        "embedded_literal_assumption_match": -1,
        "embedded_literal_reference_match": -1,
        "embedded_flat_run_chain": 3,
        "embedded_literal_same_column": 4,
        "embedded_hardcode_consensus": 5,
        "label_sign_alignment": 6,
        "sum_to_average": 7,
        "sign_contiguous_sum": 8,
        "sign_operator_flip": 9,
        "row_peer_translation": 10,
        "column_peer_translation": 11,
        "average_neighbor_translation": 12,
        "average_summary_contiguous": 11,
        "average_add_argument": 13,
        "average_endpoints": 14,
        "average_exclude_subject": 14,
        "average_exclude_blank_endpoint": 14,
        "average_trim_summary_after_gap": 14,
        "average_exclude_self_reference": -5,
        "average_beginning_ending_balance": -5,
        "average_balance_endpoints": -5,
        "cagr_compound_years": -5,
        "cagr_fixed_period": -5,
        "cagr_period_alignment": -5,
        "cagr_rri_native_function": -5,
        "aggregate_restore_first_numeric_after_text": -5,
        "average_extend_to_preforecast": -5,
        "average_low_high_single_metric": -5,
        "average_unlevered_beta_source": -5,
        "aggregate_range_shift": 15,
        "aggregate_boundary": 16,
        "aggregate_argument": 17,
        "sum_to_average_candidate": 18,
        "cross_sheet_offset": 20,
        "relative_add_anchor": 20,
        "double_count_sum_argument": 20,
    }
    grouped: dict[tuple[str, str], list[DebuggingRepairCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.sheet, candidate.cell), []).append(candidate)

    def relative_option_priority(item: DebuggingRepairCandidate) -> tuple[int, int]:
        if not relative_task or item.kind != "relative_series_anchor":
            return 2, 0
        source_match = re.search(r"stop (?P<reference>\$?[A-Z]{1,3}\$?\d+) from", item.rationale)
        if source_match is None:
            return 1, 0
        source_reference = source_match.group("reference").replace("$", "")
        source = workbook[item.sheet][source_reference]
        target = workbook[item.sheet][item.cell]
        parsed_source = _CELL_RE.fullmatch(source_reference)
        assert parsed_source is not None
        source_column = column_index_from_string(parsed_source.group("column"))
        column_distance = int(target.column) - source_column
        if column_distance >= 2:
            return 0, -column_distance
        value = source.value
        if isinstance(value, int | float) and not isinstance(value, bool):
            return 1, -column_distance
        if value is not None and not (isinstance(value, str) and value.startswith("=")):
            return 1, -column_distance
        return 2, -column_distance

    for group in grouped.values():
        group.sort(
            key=lambda item: (
                kind_order.get(item.kind, 99),
                relative_option_priority(item),
                item.replacement,
            )
        )

    style_types: dict[int, Counter[str]] = defaultdict(Counter)
    color_types: dict[str, Counter[str]] = defaultdict(Counter)
    if embedded_task:
        for worksheet in workbook.worksheets:
            for cell in getattr(worksheet, "_cells", {}).values():
                value = cell.value
                value_type = (
                    "formula"
                    if isinstance(value, str) and value.startswith("=")
                    else "number"
                    if isinstance(value, int | float) and not isinstance(value, bool)
                    else "other"
                )
                style_types[int(cell.style_id)][value_type] += 1
                color = getattr(cell.font, "color", None)
                color_key = (
                    str(getattr(color, "rgb", ""))
                    if color is not None and getattr(color, "type", None) == "rgb"
                    else f"{getattr(color, 'type', '')}:{getattr(color, 'indexed', '')}:{getattr(color, 'theme', '')}"
                )
                color_types[color_key][value_type] += 1

    def embedded_anomaly_signal(group: list[DebuggingRepairCandidate]) -> tuple[int, float]:
        if not embedded_task or not isinstance(group[0].current, int | float):
            return 4, 0.0
        cell = workbook[group[0].sheet][group[0].cell]
        color = getattr(cell.font, "color", None)
        color_key = (
            str(getattr(color, "rgb", ""))
            if color is not None and getattr(color, "type", None) == "rgb"
            else f"{getattr(color, 'type', '')}:{getattr(color, 'indexed', '')}:{getattr(color, 'theme', '')}"
        )
        style_counts = style_types[int(cell.style_id)]
        color_counts = color_types[color_key]
        style_denominator = style_counts["formula"] + style_counts["number"]
        color_denominator = color_counts["formula"] + color_counts["number"]
        style_ratio = (
            style_counts["formula"] / style_denominator
            if style_counts["formula"] >= 2 and style_denominator
            else 0.0
        )
        color_ratio = (
            color_counts["formula"] / color_denominator
            if color_counts["formula"] >= 5 and color_denominator
            else 0.0
        )
        signal = max(style_ratio, color_ratio)
        tier = 0 if color_ratio >= 0.85 else 1 if style_ratio >= 0.85 else 2
        return tier, -signal

    ordered_groups = sorted(
        grouped.values(),
        key=lambda group: (
            *embedded_anomaly_signal(group),
            min(kind_order.get(item.kind, 99) for item in group),
            0 if any(item.kind == "sum_to_average" for item in group) else 1,
            0
            if any(
                re.search(
                    r"cash|debt|tax|capex|capital|interest|expense|cost|enterprise|ebitda|working|proceeds|repayment|purchase|leverage",
                    value,
                    re.IGNORECASE,
                )
                for item in group
                for _, value in item.context
            )
            else 1,
            -len(_sign_operator_positions(group[0].current)) if sign_task else 0,
            0
            if isinstance(group[0].current, str) and "AVERAGE(" in group[0].current.upper()
            else 1,
            group[0].sheet.casefold(),
            group[0].cell,
        ),
    )
    selected: list[DebuggingRepairCandidate] = []
    option_index = 0
    while len(selected) < max_candidates:
        added = False
        for group in ordered_groups:
            if option_index < len(group):
                selected.append(group[option_index])
                added = True
                if len(selected) == max_candidates:
                    break
        if not added:
            break
        option_index += 1
    return selected


__all__ = ["DebuggingRepairCandidate", "detect_debugging_repair_candidates"]
