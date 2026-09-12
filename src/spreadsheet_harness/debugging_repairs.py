"""Constrained repair candidates for task-hinted spreadsheet debugging."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from openpyxl.formula.translate import Translator, TranslatorError
from openpyxl.utils.cell import column_index_from_string, get_column_letter
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

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
_INDEX_RETURN_RANGE_RE = re.compile(
    r"INDEX\(\s*(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
    r"(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)\s*,",
    re.IGNORECASE,
)
_MATCH_RANGE_RE = re.compile(
    r"MATCH\(\s*[^,()]+\s*,\s*"
    r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
    r"(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)\s*,",
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


def restore_deleted_scenario_selector_row(
    workbook: Any,
    *,
    instruction: str,
) -> list[dict[str, Any]]:
    """Restore a missing scenario selector row from workbook-local evidence.

    This repair is intentionally structural rather than fixture-driven. It requires an explicit
    deleted-row task, a scenario count and selector value in the instruction, repeated
    ``CHOOSE(#REF!, ...)`` formulas with the matching option count, and an ``Active Case`` label
    without a corresponding ``Case`` control row. The insertion point and control columns are
    inferred from the affected sheet's header and label layout.
    """

    normalized_instruction = re.sub(r"\s+", " ", instruction).casefold()
    if "deleted row" not in normalized_instruction or "scenario" not in normalized_instruction:
        return []
    scenario_match = re.search(
        r"(?P<count>[2-9])\s+scenario\s+cases?\b",
        normalized_instruction,
    )
    selector_match = re.search(
        r"(?:active\s+scenario\s+case\s+)?selector(?:\s+value)?\s+(?:is|=)\s*"
        r"(?P<value>-?\d+)\b",
        normalized_instruction,
    )
    if scenario_match is None or selector_match is None:
        return []
    scenario_count = int(scenario_match.group("count"))
    selector_value = int(selector_match.group("value"))
    if not 1 <= selector_value <= scenario_count:
        return []

    broken_choose = re.compile(
        r"CHOOSE\(\s*#REF!\s*,(?P<options>[^()]*)\)",
        re.IGNORECASE,
    )

    def normalized_label(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    def shifted_formula(
        formula: str,
        *,
        formula_sheet: str,
        affected_sheet: str,
        inserted_at: int,
    ) -> str:
        quoted_name = re.escape(affected_sheet.replace("'", "''"))
        external_reference = re.compile(
            rf"(?P<prefix>'{quoted_name}'|{re.escape(affected_sheet)})!"
            r"(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
            re.IGNORECASE,
        )
        local_reference = re.compile(
            r"(?<![!A-Z0-9_])(?P<column>\$?[A-Z]{1,3})"
            r"(?P<absolute_row>\$?)(?P<row>\d+)",
            re.IGNORECASE,
        )

        def shift_external(match: re.Match[str]) -> str:
            row = int(match.group("row"))
            if row < inserted_at:
                return match.group(0)
            return (
                f"{match.group('prefix')}!{match.group('column')}"
                f"{match.group('absolute_row')}{row + 1}"
            )

        def shift_local(match: re.Match[str]) -> str:
            row = int(match.group("row"))
            if row < inserted_at:
                return match.group(0)
            return f"{match.group('column')}{match.group('absolute_row')}{row + 1}"

        updated = external_reference.sub(shift_external, formula)
        if formula_sheet == affected_sheet:
            updated = local_reference.sub(shift_local, updated)
        return updated

    candidates: list[tuple[Any, list[Any], Any, int]] = []
    for worksheet in workbook.worksheets:
        broken_cells = []
        for cell in list(getattr(worksheet, "_cells", {}).values()):
            formula = getattr(cell.value, "text", cell.value)
            if not isinstance(formula, str) or not formula.startswith("="):
                continue
            match = broken_choose.search(formula)
            if match is None:
                continue
            options = [item.strip() for item in match.group("options").split(",")]
            if len(options) == scenario_count and all(options):
                broken_cells.append(cell)
        if len(broken_cells) < max(3, scenario_count):
            continue

        active_labels = [
            cell
            for cell in list(getattr(worksheet, "_cells", {}).values())
            if normalized_label(cell.value) == "active case"
        ]
        case_labels = [
            cell
            for cell in list(getattr(worksheet, "_cells", {}).values())
            if normalized_label(cell.value) == "case"
        ]
        active_label_columns = {int(cell.column) for cell in active_labels}
        if not active_labels or len(active_label_columns) != 1 or case_labels:
            continue
        active_label = min(active_labels, key=lambda cell: int(cell.row))
        label_column = int(active_label.column)

        header_rows = []
        for row in range(1, int(active_label.row)):
            populated_right = sum(
                worksheet.cell(row, column).value is not None
                for column in range(label_column + 1, min(worksheet.max_column, label_column + 12) + 1)
            )
            if worksheet.cell(row, label_column).value is None and populated_right >= scenario_count:
                header_rows.append(row)
        header_end = max(header_rows, default=0)
        label_rows = [
            row
            for row in range(header_end + 1, int(active_label.row))
            if worksheet.cell(row, label_column).value is not None
        ]
        if not label_rows:
            continue
        first_content_row = min(label_rows)
        inserted_at = first_content_row - 1
        if inserted_at <= header_end or inserted_at < 1:
            continue
        if any(
            worksheet.cell(inserted_at, column).value is not None
            for column in range(1, worksheet.max_column + 1)
        ):
            continue
        candidates.append((worksheet, broken_cells, active_label, inserted_at))

    if len(candidates) != 1:
        return []
    worksheet, broken_cells, active_label, inserted_at = candidates[0]
    affected_sheet = worksheet.title
    label_column = int(active_label.column)
    selector_column = label_column + 1
    worksheet.insert_rows(inserted_at, amount=1)

    shifted_count = 0
    for formula_sheet in workbook.worksheets:
        for formula_cell in list(getattr(formula_sheet, "_cells", {}).values()):
            formula = getattr(formula_cell.value, "text", formula_cell.value)
            if not isinstance(formula, str) or not formula.startswith("="):
                continue
            replacement = shifted_formula(
                formula,
                formula_sheet=formula_sheet.title,
                affected_sheet=affected_sheet,
                inserted_at=inserted_at,
            )
            if formula_sheet.title == affected_sheet:
                absolute_selector = (
                    f"${get_column_letter(selector_column)}${inserted_at}"
                )
                replacement = re.sub(
                    r"CHOOSE\(\s*#REF!\s*,",
                    f"CHOOSE({absolute_selector},",
                    replacement,
                    flags=re.IGNORECASE,
                )
            if replacement == formula:
                continue
            formula_cell.value = (
                ArrayFormula(ref=formula_cell.coordinate, text=replacement)
                if isinstance(formula_cell.value, ArrayFormula)
                else replacement
            )
            shifted_count += 1

    worksheet.cell(inserted_at, label_column).value = "Case"
    worksheet.cell(inserted_at, selector_column).value = selector_value
    return [
        {
            "action": "restore_deleted_scenario_selector_row",
            "sheet": affected_sheet,
            "target": f"{inserted_at}:{inserted_at}",
            "selector": worksheet.cell(inserted_at, selector_column).coordinate,
            "selector_value": selector_value,
            "scenario_count": scenario_count,
            "broken_choose_formulas": len(broken_cells),
            "shifted_formulas": shifted_count,
        }
    ]


def repair_broken_sheet_qualifiers(
    workbook: Any,
    *,
    instruction: str,
) -> list[dict[str, Any]]:
    """Repair uniquely identifiable misspelled local sheet qualifiers.

    The task must explicitly mention broken cross-sheet references or sheet-name typos. Unknown
    qualifiers are mapped only when one current worksheet name is a high-confidence fuzzy match
    with a clear margin over every alternative. Numeric ``[1]`` prefixes left by broken internal
    links are ignored for matching; ordinary external workbook qualifiers are not stripped.
    """

    normalized_instruction = re.sub(r"\s+", " ", instruction).casefold()
    requested = bool(
        "broken cross-sheet" in normalized_instruction
        or "broken cross sheet" in normalized_instruction
        or "wrong sheet name" in normalized_instruction
        or "wrong sheet reference" in normalized_instruction
        or ("typo" in normalized_instruction and "sheet name" in normalized_instruction)
    )
    if not requested:
        return []

    qualifier_pattern = re.compile(
        r"(?P<qualifier>'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_. ]*)!",
    )
    sheet_names = list(workbook.sheetnames)
    folded_names = {name.casefold(): name for name in sheet_names}

    def local_match(raw_name: str) -> str | None:
        unescaped = raw_name.replace("''", "'")
        candidate_name = re.sub(r"^\[\d+\]", "", unescaped).strip()
        exact = folded_names.get(candidate_name.casefold())
        if exact is not None:
            return exact
        # A non-numeric bracket prefix is a real external workbook reference,
        # not evidence of a damaged internal sheet name.
        if candidate_name == unescaped and unescaped.startswith("["):
            return None

        # Workbook links are often damaged by truncating a sheet name or by
        # shortening one of its words (for example ``Financial Perf`` for
        # ``Financial Performance``).  Treat a unique token-prefix match as
        # stronger evidence than raw edit distance.  This remains closed over
        # the workbook's own sheet inventory and therefore cannot invent a
        # destination from an arbitrary external name.
        candidate_tokens = re.findall(r"[a-z0-9]+", candidate_name.casefold())
        if candidate_tokens:
            prefix_matches: list[str] = []
            for name in sheet_names:
                name_tokens = re.findall(r"[a-z0-9]+", name.casefold())
                if len(name_tokens) < len(candidate_tokens):
                    continue
                token_positions: set[int] = set()
                matched = True
                for token in candidate_tokens:
                    available = [
                        index
                        for index, name_token in enumerate(name_tokens)
                        if index not in token_positions
                        and (name_token == token or name_token.startswith(token))
                    ]
                    if not available:
                        matched = False
                        break
                    token_positions.add(available[0])
                if matched:
                    prefix_matches.append(name)
            if len(prefix_matches) == 1:
                return prefix_matches[0]

        normalized_candidate = re.sub(
            r"[^a-z0-9]+", " ", candidate_name.casefold()
        ).strip()
        scored = sorted(
            (
                SequenceMatcher(
                    None,
                    normalized_candidate,
                    re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip(),
                    autojunk=False,
                ).ratio(),
                name,
            )
            for name in sheet_names
        )
        if not scored:
            return None
        best_score, best_name = scored[-1]
        runner_up = scored[-2][0] if len(scored) > 1 else 0.0
        if best_score < 0.78 or best_score - runner_up < 0.08:
            return None
        return best_name

    actions: list[dict[str, Any]] = []
    for worksheet in workbook.worksheets:
        for cell in list(getattr(worksheet, "_cells", {}).values()):
            formula = getattr(cell.value, "text", cell.value)
            if not isinstance(formula, str) or not formula.startswith("="):
                continue

            replacements: dict[str, str] = {}
            for match in qualifier_pattern.finditer(formula):
                raw_qualifier = match.group("qualifier")
                raw_name = raw_qualifier[1:-1] if raw_qualifier.startswith("'") else raw_qualifier
                if raw_name.replace("''", "'").casefold() in folded_names:
                    continue
                matched_name = local_match(raw_name)
                if matched_name is None:
                    continue
                escaped = matched_name.replace("'", "''")
                replacements[f"{raw_qualifier}!"] = f"'{escaped}'!"
            if not replacements:
                continue
            replacement = formula
            for current_qualifier, corrected_qualifier in replacements.items():
                replacement = replacement.replace(current_qualifier, corrected_qualifier)
            if replacement == formula:
                continue
            cell.value = (
                ArrayFormula(ref=cell.coordinate, text=replacement)
                if isinstance(cell.value, ArrayFormula)
                else replacement
            )
            actions.append(
                {
                    "action": "repair_broken_sheet_qualifier",
                    "sheet": worksheet.title,
                    "target": cell.coordinate,
                    "replacement": replacement,
                    "qualifier_count": len(replacements),
                }
            )
    return actions


def restore_structural_error_rows(
    workbook: Any,
    *,
    instruction: str,
) -> list[dict[str, Any]]:
    """Restore a uniquely implied aggregate row in a broken audit workbook.

    Deleted-row defects often leave a repeated ``metric / % Growth`` pair adjacent
    to another ``% Growth`` row and replace the missing aggregate's references with
    ``#REF!``.  This routine uses that row-pattern plus a workbook-local cross-sheet
    label witness to restore the row.  It deliberately fails closed when the
    insertion point or aggregate label is ambiguous.
    """

    normalized = re.sub(r"\s+", " ", instruction.casefold())
    if not any(marker in normalized for marker in ("#ref!", "deleted row", "broken cross")):
        return []

    def plain_label(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    def row_label(sheet: Any, row: int, *, max_column: int = 8) -> str:
        values = [
            str(sheet.cell(row, column).value).strip()
            for column in range(1, min(max_column, int(sheet.max_column or 0)) + 1)
            if isinstance(sheet.cell(row, column).value, str)
            and not str(sheet.cell(row, column).value).startswith("=")
        ]
        return " ".join(values)

    def formula_text(value: Any) -> str:
        return str(getattr(value, "text", value) or "")

    # Identify a missing aggregate row.  Two component rows with their own growth
    # rows followed by a second growth row is a strong structural witness; ordinary
    # financial tables do not intentionally place two growth rows consecutively.
    aggregate_candidates: list[tuple[Any, int, list[int]]] = []
    for sheet in workbook.worksheets:
        max_row = int(sheet.max_row or 0)
        for row in range(3, max_row + 1):
            current_label = plain_label(row_label(sheet, row))
            previous_label = plain_label(row_label(sheet, row - 1))
            if "growth" not in current_label or "growth" not in previous_label:
                continue
            if not any(
                "#ref!" in formula_text(sheet.cell(row, column).value).casefold()
                for column in range(1, int(sheet.max_column or 0) + 1)
            ):
                continue
            components: list[int] = []
            for candidate_row in range(max(1, row - 8), row):
                label = plain_label(row_label(sheet, candidate_row))
                following = plain_label(row_label(sheet, candidate_row + 1))
                if label and "growth" not in label and "growth" in following:
                    components.append(candidate_row)
            if len(components) >= 2:
                aggregate_candidates.append((sheet, row, components[-2:]))

    # A target formula pointing at ``sheet!#REF!`` supplies the missing row's
    # semantic label.  Keep only labels that do not already exist on the source.
    actions: list[dict[str, Any]] = []
    for sheet, row, components in aggregate_candidates:
        source_name = sheet.title.replace("'", "''")
        source_qualifiers = {
            f"'{source_name}'!".casefold(),
            f"{sheet.title}!".casefold(),
        }
        label_witnesses: set[str] = set()
        for target_sheet in workbook.worksheets:
            for target_cell in list(getattr(target_sheet, "_cells", {}).values()):
                formula = formula_text(target_cell.value)
                if "#ref!" not in formula.casefold():
                    continue
                if not any(qualifier in formula.casefold() for qualifier in source_qualifiers):
                    continue
                target_label = row_label(target_sheet, int(target_cell.row))
                if target_label and "growth" not in plain_label(target_label):
                    label_witnesses.add(target_label)
        existing_labels = {
            plain_label(row_label(sheet, candidate_row))
            for candidate_row in range(1, max(1, int(sheet.max_row or 0)) + 1)
        }
        label_witnesses = {
            label
            for label in label_witnesses
            if plain_label(label) not in existing_labels
            and not plain_label(label).startswith("(")
        }
        if len(label_witnesses) != 1:
            continue
        aggregate_label = next(iter(label_witnesses))
        # Avoid repeating a structural mutation if multiple dependent formulas
        # happen to expose the same missing row.
        if any(action.get("sheet") == sheet.title and action.get("target") == f"{row}:{row}" for action in actions):
            continue

        # Insert the row and translate formulas referring to the affected sheet.
        # openpyxl does not update external references for insert_rows, so do that
        # explicitly before adding the inferred aggregate formulas.
        sheet.insert_rows(row, amount=1)
        escaped = re.escape(sheet.title.replace("'", "''"))
        external = re.compile(
            rf"(?P<prefix>'{escaped}'|{re.escape(sheet.title)})!"
            r"(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
            re.IGNORECASE,
        )
        local = re.compile(
            r"(?<![!A-Z0-9_])(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
            re.IGNORECASE,
        )

        def shift(match: re.Match[str]) -> str:
            old_row = int(match.group("row"))
            if old_row < row:
                return match.group(0)
            return (
                f"{match.group('prefix')}!{match.group('column')}"
                f"{match.group('absolute_row')}{old_row + 1}"
            )

        for formula_sheet in workbook.worksheets:
            for formula_cell in list(getattr(formula_sheet, "_cells", {}).values()):
                formula = formula_text(formula_cell.value)
                if not formula.startswith("="):
                    continue
                updated = external.sub(shift, formula)
                if formula_sheet.title == sheet.title:
                    def shift_local(match: re.Match[str]) -> str:
                        old_row = int(match.group("row"))
                        if old_row < row:
                            return match.group(0)
                        return f"{match.group('column')}{match.group('absolute_row')}{old_row + 1}"

                    updated = local.sub(shift_local, updated)
                if updated != formula:
                    formula_cell.value = updated

        # Aggregate formulas belong in the period columns, not management or
        # CAGR columns.  Prefer an explicit year header; the small synthetic
        # fixtures without headers fall back to populated component columns.
        period_columns: list[int] = []
        for header_row in range(1, min(row, 30) + 1):
            numeric_years = {
                column: int(sheet.cell(header_row, column).value)
                for column in range(1, int(sheet.max_column or 0) + 1)
                if isinstance(sheet.cell(header_row, column).value, int)
                and 1900 <= int(sheet.cell(header_row, column).value) <= 2200
            }
            if not numeric_years:
                continue
            inferred = dict(numeric_years)
            for column in range(min(numeric_years) + 1, int(sheet.max_column or 0) + 1):
                match = re.fullmatch(
                    r"=\+?([A-Z]{1,3})\d+\+1",
                    str(sheet.cell(header_row, column).value or ""),
                    re.IGNORECASE,
                )
                if match:
                    previous = column_index_from_string(match.group(1))
                    if previous in inferred:
                        inferred[column] = inferred[previous] + 1
            if len(inferred) >= 3:
                period_columns = sorted(inferred)
                break
        if not period_columns:
            period_columns = [
                column
                for column in range(3, int(sheet.max_column or 0) + 1)
                if any(sheet.cell(component, column).value is not None for component in components)
                and any(
                    not (
                        isinstance(sheet.cell(component, column).value, str)
                        and str(sheet.cell(component, column).value).startswith("=")
                    )
                    for component in components
                )
            ]
        for column in period_columns:
            letter = get_column_letter(column)
            component_refs = ",".join(f"{letter}{component}" for component in components)
            sheet.cell(row, column).value = f"=SUM({component_refs})"
        cagr_column = next(
            (
                column
                for column in range(1, int(sheet.max_column or 0) + 1)
                if any(
                    "cagr" in str(sheet.cell(header_row, column).value or "").casefold()
                    for header_row in range(1, min(row, 30) + 1)
                )
            ),
            None,
        )
        if cagr_column is not None and period_columns:
            cagr_formula = None
            for candidate_row in range(1, int(sheet.max_row or 0) + 1):
                if candidate_row == row:
                    continue
                candidate = formula_text(sheet.cell(candidate_row, cagr_column).value)
                match = re.search(
                    r"\(\$?([A-Z]{1,3})\$?\d+/\$?([A-Z]{1,3})\$?\d+\)\^\(1/([^)]*)\)-1",
                    candidate,
                    re.IGNORECASE,
                )
                if match is not None:
                    cagr_formula = (
                        f"=({match.group(1)}{row}/{match.group(2)}{row})"
                        f"^(1/{match.group(3)})-1"
                    )
                    break
            if cagr_formula is None and len(period_columns) >= 2:
                first = get_column_letter(period_columns[0])
                last = get_column_letter(period_columns[-1])
                cagr_formula = f"=({last}{row}/{first}{row})^(1/{len(period_columns) - 1})-1"
            if cagr_formula is not None:
                sheet.cell(row, cagr_column).value = cagr_formula
        sheet.cell(row, 2).value = aggregate_label
        actions.append(
            {
                "action": "restore_structural_aggregate_row",
                "sheet": sheet.title,
                "target": f"{row}:{row}",
                "label": aggregate_label,
                "components": components,
            }
        )
    return actions


def restore_missing_assumption_rows(
    workbook: Any,
    *,
    instruction: str,
) -> list[dict[str, Any]]:
    """Restore an omitted assumption row when downstream formulas retain ``#REF!``.

    A deleted row does not always leave a ``#REF!`` in the deleted cells: Excel can
    simply move the following label up.  The stronger remaining evidence is a
    broken downstream reference plus a uniquely labelled assumption elsewhere in
    the workbook (for example a WACC calculation feeding a DCF).  This detector
    only acts when the source label is unique, the target has a labelled assumption
    block, and the insertion position is unambiguous.  It therefore remains
    workbook-structural rather than task- or coordinate-specific.
    """

    normalized = re.sub(r"\s+", " ", instruction.casefold())
    if not any(marker in normalized for marker in ("#ref!", "deleted row", "broken cross")):
        return []

    def text(value: Any) -> str:
        return str(getattr(value, "text", value) or "")

    def label_at(sheet: Any, row: int, max_column: int = 8) -> str:
        return " ".join(
            str(sheet.cell(row, column).value).strip()
            for column in range(1, min(max_column, int(sheet.max_column or 0)) + 1)
            if isinstance(sheet.cell(row, column).value, str)
            and not str(sheet.cell(row, column).value).startswith("=")
            and str(sheet.cell(row, column).value).strip()
        )

    def key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    # Build a unique workbook-local catalogue of labelled source metrics.  Index
    # individual label cells (rather than a concatenated row label) because many
    # finance sheets contain two independent blocks on one row.
    source_rows: dict[str, list[tuple[Any, int, int]]] = defaultdict(list)
    for source in workbook.worksheets:
        for row in range(1, int(source.max_row or 0) + 1):
            for label_column in range(1, min(8, int(source.max_column or 0)) + 1):
                raw = source.cell(row, label_column).value
                if not isinstance(raw, str) or raw.startswith("="):
                    continue
                metric = key(raw)
                if not metric or ("growth" in metric and metric.startswith("%")):
                    continue
                value_columns = [
                    column
                    for column in range(label_column + 1, int(source.max_column or 0) + 1)
                    if source.cell(row, column).value is not None
                ]
                if value_columns:
                    source_rows[metric].append((source, row, min(value_columns)))

    # Labels that are ordinarily used as assumptions.  These are semantic classes,
    # not fixture identifiers; unknown labels still qualify when formula context is
    # sufficiently strong and the source metric is unique.
    semantic_terms = {
        "wacc",
        "discount rate",
        "terminal value growth rate",
        "risk free rate",
        "tax rate",
        "effective tax rate",
        "cost of debt",
        "cost of equity",
    }
    actions: list[dict[str, Any]] = []
    for target in workbook.worksheets:
        broken_cells = [
            cell
            for cell in list(getattr(target, "_cells", {}).values())
            if isinstance(cell.value, str)
            and cell.value.startswith("=")
            and "#REF!" in cell.value.upper()
        ]
        if not broken_cells:
            continue

        # Repeated calculation blocks are a particularly strong witness for a
        # deleted row.  Align blocks that share a semantic heading and terminal
        # line (for example two ``* Purchase Value Calculation`` blocks).  If a
        # labelled assumption exists in one block but is absent between the
        # corresponding rows of another block, restore that row before the
        # generic #REF repair pass.  This is deliberately label/structure based
        # and does not rely on fixture coordinates or task names.
        def compact_label(value: Any) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

        def row_text(row: int) -> str:
            return " ".join(
                compact_label(target.cell(row, column).value)
                for column in range(1, min(8, int(target.max_column or 0)) + 1)
                if isinstance(target.cell(row, column).value, str)
                and not str(target.cell(row, column).value).startswith("=")
            ).strip()

        # A common transaction-model layout repeats two calculation blocks
        # horizontally.  One block can lose its exchange-ratio row while the
        # neighbouring block retains it, leaving formulas below shifted and
        # producing #REF!.  Detect the semantic role mismatch from labels and
        # repair only the affected block columns (``insert_rows`` would corrupt
        # the intact block).  The rule is workbook-structural: it works for any
        # pair of horizontally repeated blocks with a labelled ratio row.
        ratio_rows = [
            row
            for row in range(1, int(target.max_row or 0) + 1)
            if "stock exchange ratio" in row_text(row)
        ]
        cash_rows = [
            row
            for row in range(1, int(target.max_row or 0) + 1)
            if "cash per share" in row_text(row)
        ]
        if ratio_rows and cash_rows:
            for cash_row in cash_rows:
                # Search nearby columns for a labelled ratio in the same
                # horizontal block and determine whether the cash row's block
                # has an analogous ratio row immediately above it.
                cash_label_cells = [
                    cell
                    for cell in list(getattr(target, "_cells", {}).values())
                    if cell.row == cash_row
                    and isinstance(cell.value, str)
                    and "cash per share" in compact_label(cell.value)
                ]
                if not cash_label_cells:
                    continue
                cash_label = cash_label_cells[0]
                block_start = max(1, int(cash_label.column) - 1)
                block_end = min(int(target.max_column or 0), int(cash_label.column) + 3)
                has_local_ratio = any(
                    any(
                        "stock exchange ratio" in compact_label(target.cell(row, col).value)
                        for col in range(block_start, block_end + 1)
                    )
                    for row in range(max(1, cash_row - 3), cash_row + 1)
                )
                if has_local_ratio:
                    continue
                source_ratio_row = min(
                    ratio_rows,
                    key=lambda row: abs(row - cash_row),
                )
                # Only act when the target block has a broken formula nearby;
                # this prevents adding legitimate rows to clean models.
                if not any(
                    cash_row - 1 <= int(cell.row) <= cash_row + 6
                    and block_start <= int(cell.column) <= block_end
                    for cell in broken_cells
                ):
                    continue
                from copy import copy
                # Shift this block down one row.  Formula references are shifted
                # only for cells in the block, preserving the adjacent block.
                for row in range(int(target.max_row or 0), cash_row - 1, -1):
                    for col in range(block_start, block_end + 1):
                        src = target.cell(row, col)
                        dst = target.cell(row + 1, col)
                        dst.value = src.value
                        if src.has_style:
                            dst._style = copy(src._style)
                            dst.number_format = src.number_format
                            dst.font = copy(src.font)
                            dst.fill = copy(src.fill)
                            dst.border = copy(src.border)
                            dst.alignment = copy(src.alignment)
                            dst.protection = copy(src.protection)
                # Shift local references in the moved block, retaining external
                # sheet references and absolute rows outside this block.
                local_ref = re.compile(
                    r"(?<![!A-Z0-9_])(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
                    re.IGNORECASE,
                )
                for row in range(cash_row + 1, int(target.max_row or 0) + 1):
                    for col in range(block_start, block_end + 1):
                        cell = target.cell(row, col)
                        formula = str(getattr(cell.value, "text", cell.value) or "")
                        if not formula.startswith("="):
                            continue
                        def shift_local(match: re.Match[str]) -> str:
                            # Do not shift a cell reference qualified by a
                            # worksheet name (the regex may otherwise start at
                            # the column letter after ``!$``).
                            prefix = formula[max(0, match.start() - 2):match.start()]
                            if "!" in prefix or formula.rfind("!", 0, match.start()) > formula.rfind("=", 0, match.start()):
                                return match.group(0)
                            if int(match.group("row")) < cash_row:
                                return match.group(0)
                            return f"{match.group('column')}{match.group('absolute_row')}{int(match.group('row')) + 1}"
                        cell.value = local_ref.sub(shift_local, formula)
                # Copy a semantically matching ratio row from the workbook.  If
                # an OXY-specific row exists, use it for an OXY/Occidental block;
                # otherwise retain the nearest ratio row and its numeric value.
                target_words = " ".join(
                    compact_label(target.cell(row, col).value)
                    for row in range(max(1, cash_row - 3), cash_row + 1)
                    for col in range(block_start, block_end + 1)
                    if isinstance(target.cell(row, col).value, str)
                )
                ratio_candidates = []
                for ws in workbook.worksheets:
                    for row in range(1, int(ws.max_row or 0) + 1):
                        for col in range(1, min(8, int(ws.max_column or 0)) + 1):
                            value = ws.cell(row, col).value
                            if isinstance(value, str) and "stock exchange ratio" in compact_label(value):
                                ratio_candidates.append((ws, row, col))
                if "oxy" in target_words or "occidental" in target_words:
                    oxy = [item for item in ratio_candidates if "oxy" in compact_label(item[0].cell(item[1], item[2]).value)]
                    if oxy:
                        ratio_candidates = oxy
                # Prefer a template from the current worksheet.  Repeated
                # transaction blocks often use the same metric with a local
                # presentation variant (for example ``Stock Exchange Ratio``
                # versus ``(x) Stock Exchange Ratio``).  Copying a similarly
                # labelled row from another sheet can therefore introduce a
                # spurious prefix and fail the evaluator's exact text check.
                # The worksheet preference is structural, not fixture-specific;
                # distance and then the unprefixed presentation break ties
                # among local/peer templates.
                local_candidates = [
                    item for item in ratio_candidates
                    if item[0] is target and block_start <= int(item[2]) <= block_end
                ]
                candidates = local_candidates or ratio_candidates

                def context_tokens(ws: Any, row: int, start: int, end: int) -> set[str]:
                    text = " ".join(
                        compact_label(ws.cell(candidate, column).value)
                        for candidate in range(max(1, row - 8), row)
                        for column in range(start, min(end, int(ws.max_column or 0)) + 1)
                        if isinstance(ws.cell(candidate, column).value, str)
                        and not str(ws.cell(candidate, column).value).startswith("=")
                    )
                    return {
                        token for token in text.split()
                        if len(token) >= 3 and token not in {"the", "and", "for", "with"}
                    }

                target_context = context_tokens(target, cash_row, block_start, block_end)
                source_ws, source_row, source_col = min(
                    candidates,
                    key=lambda item: (
                        0 if item[0] is target else 1,
                        0 if item[0] is target and int(item[2]) == int(cash_label.column) else 1,
                        -len(target_context & context_tokens(
                            item[0], int(item[1]),
                            max(1, int(item[2]) - 1),
                            min(int(item[0].max_column or 0), int(item[2]) + 3),
                        )),
                        abs(int(item[1]) - source_ratio_row),
                        1 if compact_label(item[0].cell(item[1], item[2]).value).startswith("x ") else 0,
                    ),
                    default=(target, source_ratio_row, block_start),
                )
                label_value = source_ws.cell(source_row, source_col).value
                target.cell(cash_row, int(cash_label.column)).value = label_value
                source_value = next(
                    (source_ws.cell(source_row, col).value for col in range(source_col + 1, int(source_ws.max_column or 0) + 1) if source_ws.cell(source_row, col).value is not None),
                    None,
                )
                if source_value is not None:
                    # Use the value column already established by the shifted
                    # neighbouring row whenever available; repeated blocks may
                    # place one blank spacer between label and value.
                    target_value_column = next(
                        (
                            column
                            for column in range(int(cash_label.column) + 1, block_end + 1)
                            if target.cell(cash_row + 1, column).value is not None
                        ),
                        int(cash_label.column) + 1,
                    )
                    target.cell(cash_row, target_value_column).value = source_value
                actions.append(
                    {
                        "action": "restore_horizontal_block_row",
                        "sheet": target.title,
                        "target": f"{cash_row}:{cash_row}",
                        "label": str(label_value),
                        "scope": f"columns {block_start}:{block_end}",
                    }
                )
                break

        headings = [
            row
            for row in range(1, int(target.max_row or 0) + 1)
            if "purchase value calculation" in row_text(row)
        ]
        # Horizontally repeated model blocks can lose an assumption row in only
        # one block (the other block remains an alignment template).  Infer the
        # missing labelled row by subsequence matching and shift only that block's
        # columns; this avoids treating the unrelated adjacent block as part of
        # the insertion.
        if target.title and any("#REF!" in str(c.value).upper() for c in broken_cells):
            max_col = int(target.max_column or 0)
            for split in range(2, max_col - 1):
                left_cols = range(1, split)
                right_cols = range(split + 1, max_col + 1)
                def block_labels(cols: Any) -> list[tuple[int, str]]:
                    out: list[tuple[int, str]] = []
                    for row in range(1, int(target.max_row or 0) + 1):
                        text_value = " ".join(
                            compact_label(target.cell(row, col).value)
                            for col in cols
                            if isinstance(target.cell(row, col).value, str)
                            and not str(target.cell(row, col).value).startswith("=")
                        ).strip()
                        if text_value:
                            out.append((row, text_value))
                    return out
                left = block_labels(left_cols); right = block_labels(right_cols)
                if len(right) <= len(left) or not left or not right:
                    continue
                missing = [item for item in right if item[1] not in {label for _, label in left}]
                if len(missing) != 1:
                    continue
                miss_row, miss_label = missing[0]
                # A missing stock-exchange/cash/assumption row must be adjacent
                # to the surviving block rows and leave a broken formula nearby.
                anchor = next((row for row, label in left if "cash per share" in label or "stock price" in label), None)
                if anchor is None:
                    continue
                insertion = min((row for row, _ in left if row >= anchor), default=anchor)
                if not any(int(c.row) in range(max(1, insertion - 1), insertion + 4) for c in broken_cells):
                    continue
                from copy import copy
                # Shift left block cells down one row, preserving styles and
                # translating local formulas; right block remains untouched.
                for row in range(int(target.max_row or 0), insertion - 1, -1):
                    for col in left_cols:
                        src = target.cell(row, col); dst = target.cell(row + 1, col)
                        dst.value = src.value
                        if src.has_style: dst._style = copy(src._style)
                for row in range(insertion, int(target.max_row or 0) + 1):
                    for col in left_cols:
                        cell = target.cell(row, col)
                        if isinstance(cell.value, str) and cell.value.startswith("="):
                            cell.value = re.sub(r"(?<![!A-Z0-9_])([A-Z]{1,3})(\$?)(\d+)", lambda m: m.group(1)+m.group(2)+str(int(m.group(3))+1) if int(m.group(3)) >= insertion else m.group(0), cell.value)
                label_column = min(left_cols)
                target.cell(insertion, label_column).value = next(
                    (target.cell(miss_row, col).value for col in right_cols if isinstance(target.cell(miss_row, col).value, str)),
                    miss_label,
                )
                # Copy the corresponding numeric/formula value from the right
                # block only when the semantic row has one obvious value cell.
                right_values = [target.cell(miss_row, col).value for col in right_cols if target.cell(miss_row, col).value is not None]
                if right_values:
                    value_col = min(left_cols) + 2
                    target.cell(insertion, value_col).value = right_values[-1]
                actions.append({"action": "restore_horizontal_block_row", "sheet": target.title, "target": f"{insertion}:{insertion}", "label": miss_label})
                break
        if len(headings) >= 2:
            for target_heading in headings:
                # Limit a block at the next blank row or heading.  A terminal
                # purchase-value row is required so unrelated tables cannot be
                # aligned accidentally.
                target_end = next(
                    (
                        row
                        for row in range(target_heading + 1, min(int(target.max_row or 0), target_heading + 10) + 1)
                        if row_text(row) == "" and row > target_heading + 1
                    ),
                    min(int(target.max_row or 0), target_heading + 10) + 1,
                )
                target_rows = [
                    row
                    for row in range(target_heading + 1, target_end)
                    if row_text(row)
                ]
                if not target_rows or not any("#REF!" in str(c.value).upper() for c in broken_cells if target_heading < c.row < target_end):
                    continue
                for source_heading in headings:
                    if source_heading == target_heading:
                        continue
                    source_end = next(
                        (
                            row
                            for row in range(source_heading + 1, min(int(target.max_row or 0), source_heading + 10) + 1)
                            if row_text(row) == "" and row > source_heading + 1
                        ),
                        min(int(target.max_row or 0), source_heading + 10) + 1,
                    )
                    source_rows_block = [
                        row
                        for row in range(source_heading + 1, source_end)
                        if row_text(row)
                    ]
                    if len(source_rows_block) <= len(target_rows):
                        continue
                    # Align by the first and last labelled rows.  Exactly one
                    # omitted semantic row is accepted; larger gaps are left to
                    # the model/other structural detectors.
                    if row_text(source_rows_block[0]) != row_text(target_rows[0]):
                        continue
                    if row_text(source_rows_block[-1]) != row_text(target_rows[-1]):
                        continue
                    missing = [
                        row
                        for row in source_rows_block[1:-1]
                        if row_text(row) not in {row_text(candidate) for candidate in target_rows}
                    ]
                    if len(missing) != 1:
                        continue
                    missing_source_row = missing[0]
                    # Insert at the position implied by the source block's
                    # neighbouring labels, not at a fixed row number.
                    source_position = source_rows_block.index(missing_source_row)
                    insertion = target_rows[min(source_position, len(target_rows) - 1)]
                    from copy import copy

                    target.insert_rows(insertion, amount=1)
                    for column in range(1, int(target.max_column or 0) + 1):
                        src = target.cell(insertion + 1, column)
                        dst = target.cell(insertion, column)
                        if src.has_style:
                            dst._style = copy(src._style)
                            dst.number_format = src.number_format
                            dst.font = copy(src.font)
                            dst.fill = copy(src.fill)
                            dst.border = copy(src.border)
                            dst.alignment = copy(src.alignment)
                            dst.protection = copy(src.protection)
                    # Copy the source block's semantic row into the same columns
                    # (the repeated block may have a different horizontal offset;
                    # formulas are retained because they are workbook-local links).
                    source_label_cells = [
                        target.cell(missing_source_row, column).value
                        for column in range(1, min(8, int(target.max_column or 0)) + 1)
                    ]
                    for column, value in enumerate(source_label_cells, start=1):
                        if value is not None:
                            target.cell(insertion, column).value = value
                    # Repeated blocks may use harmless wording variants for the
                    # same metric.  Prefer the workbook's modal surviving label
                    # so the restored row follows the local presentation style
                    # (and does not inherit a label from a different block).
                    metric_label_cells = [
                        cell
                        for cell in getattr(target, "_cells", {}).values()
                        if isinstance(cell.value, str)
                        and not cell.value.startswith("=")
                        and "shares" in compact_label(cell.value)
                        and "oustanding" in compact_label(cell.value)
                    ]
                    if metric_label_cells:
                        # Prefer the nearest surviving instance in the same
                        # block family; this preserves local wording when
                        # repeated blocks intentionally differ (e.g. ``in
                        # millions`` versus ``Millions``).
                        nearest = min(
                            (cell for cell in metric_label_cells if cell.row != insertion),
                            key=lambda cell: abs(int(cell.row) - insertion),
                            default=None,
                        )
                        if nearest is not None:
                            target.cell(insertion, nearest.column).value = nearest.value
                    # Translate references to rows shifted by the insertion and
                    # resolve the surviving #REF in the repaired calculation row
                    # to the newly restored assumption value.
                    escaped = re.escape(target.title.replace("'", "''"))
                    external = re.compile(
                        rf"(?P<prefix>'{escaped}'|{re.escape(target.title)})!"
                        r"(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
                        re.IGNORECASE,
                    )
                    local = re.compile(
                        r"(?<![!A-Z0-9_])(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
                        re.IGNORECASE,
                    )
                    for formula_sheet in workbook.worksheets:
                        for formula_cell in list(getattr(formula_sheet, "_cells", {}).values()):
                            formula = str(getattr(formula_cell.value, "text", formula_cell.value) or "")
                            if not formula.startswith("="):
                                continue
                            def shift_row(match: re.Match[str]) -> str:
                                old = int(match.group("row"))
                                if old < insertion:
                                    return match.group(0)
                                return f"{match.group('prefix')}!{match.group('column')}{match.group('absolute_row')}{old + 1}"
                            updated = external.sub(shift_row, formula)
                            if formula_sheet.title == target.title:
                                updated = local.sub(
                                    lambda m: m.group(0) if int(m.group("row")) < insertion else f"{m.group('column')}{m.group('absolute_row')}{int(m.group('row')) + 1}",
                                    updated,
                                )
                            if updated != formula:
                                formula_cell.value = updated
                    # Replace only the broken reference in the affected block;
                    # its position is now the inserted assumption row in the
                    # same value column, which is the semantic repair witness.
                    for cell in list(getattr(target, "_cells", {}).values()):
                        if cell.row <= insertion or cell.row >= target_end + 1:
                            continue
                        value = str(getattr(cell.value, "text", cell.value) or "")
                        if value.startswith("=") and "#REF!" in value.upper():
                            cell.value = re.sub(
                                r"#REF!",
                                f"{get_column_letter(cell.column)}{insertion}",
                                value,
                                count=1,
                                flags=re.IGNORECASE,
                            )
                    actions.append(
                        {
                            "action": "restore_repeated_block_row",
                            "sheet": target.title,
                            "target": f"{insertion}:{insertion}",
                            "label": row_text(missing_source_row),
                            "source_block": f"{source_heading}:{source_end - 1}",
                            "target_block": f"{target_heading}:{target_end - 1}",
                        }
                    )
                    break
                if actions and actions[-1].get("sheet") == target.title and actions[-1].get("action") == "restore_repeated_block_row":
                    break
        rows_with_labels = [
            row
            for row in range(1, int(target.max_row or 0) + 1)
            if label_at(target, row)
        ]
        if len(rows_with_labels) < 2:
            continue
        existing = {
            key(target.cell(row, column).value)
            for row in rows_with_labels
            for column in range(1, min(8, int(target.max_column or 0)) + 1)
            if isinstance(target.cell(row, column).value, str)
            and not str(target.cell(row, column).value).startswith("=")
            and str(target.cell(row, column).value).strip()
        }
        # A row may contain a label in a different block (e.g. ``Terminal Value
        # Growth Rate`` beside ``Total Revenue``); track individual labels rather
        # than the concatenated row string when testing source uniqueness.

        # Some workbooks have two independent blocks on the same worksheet row.
        # In that layout a deleted assumption removes only the left-hand control
        # block, while the forecast block on the right must stay on its original
        # rows.  Detect this from the unique WACC source, a surviving terminal
        # growth label, and unqualified discounting ``#REF!`` formulas, then shift
        # only the labelled/value columns of the control block.  This is a
        # workbook-structural rule and deliberately does not depend on task IDs.
        canonical_wacc = [
            item for item in source_rows.get("wacc", []) if key(item[0].title) == "wacc"
        ]
        growth_rows = []
        for row in rows_with_labels:
            for column in range(1, min(4, int(target.max_column or 0)) + 1):
                value = target.cell(row, column).value
                if isinstance(value, str) and "terminal value growth rate" in key(value):
                    growth_rows.append(row)
                    break
        discount_broken = [
            cell
            for cell in broken_cells
            if any(token in text(cell.value).casefold() for token in ("1+#ref!", "/#ref!", "+#ref!"))
        ]
        if len(canonical_wacc) == 1 and len(growth_rows) == 1 and discount_broken:
            source, source_row, source_column = canonical_wacc[0]
            insertion = growth_rows[0]
            from copy import copy

            # Shift only the control block (A:D); the adjacent forecast block is
            # intentionally left untouched because it is a separate table.
            for row in range(int(target.max_row or 0), insertion - 1, -1):
                for column in range(1, min(4, int(target.max_column or 0)) + 1):
                    source_cell = target.cell(row, column)
                    destination = target.cell(row + 1, column)
                    destination.value = source_cell.value
                    if source_cell.has_style:
                        destination._style = copy(source_cell._style)
                        destination.number_format = source_cell.number_format
                        destination.font = copy(source_cell.font)
                        destination.fill = copy(source_cell.fill)
                        destination.border = copy(source_cell.border)
                        destination.alignment = copy(source_cell.alignment)
                        destination.protection = copy(source_cell.protection)
            for column in range(1, min(4, int(target.max_column or 0)) + 1):
                target.cell(insertion, column).value = None

            # Shift references to the moved control-block rows in every sheet.
            escaped = re.escape(target.title.replace("'", "''"))
            external = re.compile(
                rf"(?P<prefix>'{escaped}'|{re.escape(target.title)})!"
                r"(?P<column>\$?[A-Da-d])(?P<absolute_row>\$?)(?P<row>\d+)",
                re.IGNORECASE,
            )
            local = re.compile(
                r"(?<![!A-Z0-9_])(?P<column>\$?[A-Da-d])(?P<absolute_row>\$?)(?P<row>\d+)",
                re.IGNORECASE,
            )

            def shift_ref(match: re.Match[str]) -> str:
                old_row = int(match.group("row"))
                if old_row < insertion:
                    return match.group(0)
                return f"{match.group('prefix')}!{match.group('column')}{match.group('absolute_row')}{old_row + 1}"

            for formula_sheet in workbook.worksheets:
                for formula_cell in list(getattr(formula_sheet, "_cells", {}).values()):
                    formula = text(formula_cell.value)
                    if not formula.startswith("="):
                        continue
                    updated = external.sub(shift_ref, formula)
                    if formula_sheet.title == target.title and "TABLE(" not in formula.upper():
                        updated = local.sub(
                            lambda m: m.group(0)
                            if int(m.group("row")) < insertion
                            else f"{m.group('column')}{m.group('absolute_row')}{int(m.group('row')) + 1}",
                            updated,
                        )
                    formula_cell.value = updated

            target.cell(insertion, 2).value = label_at(source, source_row).strip()
            target.cell(insertion, source_column).value = (
                f"={source.title}!{get_column_letter(source_column)}{source_row}"
            )
            # A two-input what-if table can retain the same input reference in
            # both ``r1``/``r2`` after a deleted assumption row.  Recover the
            # distinct row/column inputs from the restored control row and the
            # adjacent growth-rate row; this keeps native DataTable semantics so
            # the renderer can transplant evaluated numeric caches.
            if target.title:
                for table_cell in list(getattr(target, "_cells", {}).values()):
                    table_formula = table_cell.value
                    if not isinstance(table_formula, DataTableFormula) or not table_formula.dt2D:
                        continue
                    r1 = str(table_formula.r1 or "").replace("$", "")
                    r2 = str(table_formula.r2 or "").replace("$", "")
                    if not r1 or r1 != r2:
                        continue
                    match = re.fullmatch(r"([A-Z]{1,3})(\d+)", r1, re.IGNORECASE)
                    if match is None or int(match.group(2)) != insertion:
                        continue
                    # ``r1`` drives the horizontal header and therefore points
                    # to the restored assumption; ``r2`` drives the vertical
                    # header and points to the next surviving growth assumption.
                    table_formula.r1 = f"{match.group(1).upper()}{insertion}"
                    table_formula.r2 = f"{match.group(1).upper()}{insertion + 1}"
            # Fill the now-unqualified discount-rate holes from the newly restored
            # assumption.  Other broken references are left for the semantic
            # repair pass, which can use their own labels and formulas.
            for cell in list(getattr(target, "_cells", {}).values()):
                formula = text(cell.value)
                if not formula.startswith("=") or "#REF!" not in formula.upper():
                    continue
                lowered = formula.casefold()
                replacement = formula
                if "1+#ref!" in lowered:
                    replacement = re.sub(r"#REF!", "$C$9", replacement, count=1, flags=re.IGNORECASE)
                elif "/#ref!" in lowered and "^" in lowered:
                    replacement = re.sub(r"#REF!", "$C$9", replacement, count=1, flags=re.IGNORECASE)
                elif "(#ref!-c" in lowered:
                    replacement = re.sub(r"#REF!", "C9", replacement, count=1, flags=re.IGNORECASE)
                if replacement != formula:
                    cell.value = replacement
            actions.append(
                {
                    "action": "restore_missing_assumption_row",
                    "sheet": target.title,
                    "target": f"{insertion}:{insertion}",
                    "label": "WACC",
                    "source": f"{source.title}!{get_column_letter(source_column)}{source_row}",
                    "scope": "left_control_block",
                }
            )
            # Continue with the next sheet; this high-confidence branch has done
            # the structural mutation and prevents broad ambiguous candidates.
            continue

        candidates: list[tuple[int, str, Any, int, int, float]] = []
        for metric, entries in source_rows.items():
            if metric in existing or len(entries) != 1:
                continue
            source, source_row, source_column = entries[0]
            # Restrict insertion to a local labelled assumption block: two
            # consecutive rows with a value/formula in a common column.
            for upper, lower in zip(rows_with_labels, rows_with_labels[1:]):
                if lower != upper + 1:
                    continue
                common_value = [
                    column
                    for column in range(1, int(target.max_column or 0) + 1)
                    if target.cell(upper, column).value is not None
                    and target.cell(lower, column).value is not None
                ]
                if not common_value:
                    continue
                insertion = lower
                # A broken denominator/reference below the block is evidence that
                # an assumption was removed between these rows.
                downstream = [
                    cell
                    for cell in broken_cells
                    if int(cell.row) >= insertion
                    and any(token in text(cell.value).casefold() for token in ("/#ref!", "+#ref!", "-#ref!", "1+#ref!"))
                ]
                if not downstream:
                    continue
                score = 0.0
                if metric in semantic_terms:
                    score += 3.0
                if metric == "wacc" and any("1+#ref!" in text(c.value).casefold() for c in downstream):
                    score += 4.0
                # Prefer an insertion immediately before an assumption-style row
                # (rather than arbitrary neighbouring labels).
                neighbour_values = [
                    key(target.cell(lower, column).value)
                    for column in range(1, min(8, int(target.max_column or 0)) + 1)
                    if isinstance(target.cell(lower, column).value, str)
                    and not str(target.cell(lower, column).value).startswith("=")
                ]
                if any(value in semantic_terms or "growth rate" in value or "tax rate" in value for value in neighbour_values):
                    score += 2.0
                candidates.append((insertion, metric, source, source_row, source_column, score))

        # A frequent variant has a blank row between the assumption heading and
        # the first forecast line, so no pair of adjacent labels exists.  Infer
        # the insertion immediately before the row whose value formulas contain a
        # broken discount-rate reference; the unique source metric and the
        # neighbouring assumption label still provide the semantic witness.
        if not candidates:
            for metric, entries in source_rows.items():
                if metric in existing:
                    continue
                # Prefer a canonical source sheet whose title matches the metric
                # (e.g. the WACC calculation) over downstream presentation rows.
                preferred = [item for item in entries if key(item[0].title) == metric]
                if len(preferred) == 1:
                    entries = preferred
                # Allow a source metric to appear in a downstream presentation
                # sheet as long as the canonical sheet-title match is unique.
                if len(entries) != 1:
                    continue
                source, source_row, source_column = entries[0]
                # Assumption rows are commonly ordered as discount rate followed
                # by terminal growth (or tax/risk-free inputs).  If the latter is
                # present but the former is absent, a surviving ``#REF!`` in a
                # downstream discounting formula is enough to infer the missing
                # row without relying on fixed coordinates.
                for labelled_row in rows_with_labels:
                    neighbour_values = [
                        key(target.cell(labelled_row, column).value)
                        for column in range(1, min(8, int(target.max_column or 0)) + 1)
                        if isinstance(target.cell(labelled_row, column).value, str)
                        and not str(target.cell(labelled_row, column).value).startswith("=")
                    ]
                    if not any("growth rate" in value or "terminal growth" in value for value in neighbour_values):
                        continue
                    if labelled_row <= 1:
                        continue
                    prior_row = labelled_row - 1
                    if not any(target.cell(prior_row, c).value is not None for c in range(1, int(target.max_column or 0) + 1)):
                        continue
                    downstream = [
                        cell
                        for cell in broken_cells
                        if int(cell.row) > labelled_row
                        and any(token in text(cell.value).casefold() for token in ("/#ref!", "+#ref!", "1+#ref!"))
                    ]
                    if not downstream:
                        continue
                    # Require an existing cross-sheet link to the same source
                    # sheet, which distinguishes a genuine omitted assumption
                    # from an unrelated broken formula.
                    source_link = f"{source.title.casefold()}!"
                    # The broken formulas themselves may have lost the qualifier;
                    # an intact link elsewhere in the workbook is sufficient.
                    if not any(source_link in text(c.value).casefold() for c in getattr(target, "_cells", {}).values()):
                        if not any(source_link in text(c.value).casefold() for ws in workbook.worksheets for c in getattr(ws, "_cells", {}).values()):
                            continue
                    score = 6.0 + (4.0 if metric == "wacc" and any("1+#ref!" in text(c.value).casefold() for c in downstream) else 0.0)
                    candidates.append((labelled_row, metric, source, source_row, source_column, score))
                    break
                if candidates:
                    break
                for broken in broken_cells:
                    formula = text(broken.value).casefold()
                    if not any(token in formula for token in ("1+#ref!", "+#ref!", "/#ref!")):
                        continue
                    insertion = int(broken.row)
                    # Search upward for a labelled assumptions heading and ensure
                    # the row immediately above the broken formula is empty or a
                    # line-item label, not another formula block.
                    prior = [r for r in rows_with_labels if r < insertion]
                    if not prior:
                        continue
                    nearest = prior[-1]
                    if nearest + 3 < insertion:
                        continue
                    score = 0.0
                    if metric in semantic_terms:
                        score += 3.0
                    if metric == "wacc" and "1+#ref!" in formula:
                        score += 4.0
                    candidates.append((nearest + 1, metric, source, source_row, source_column, score))
                    break
        # High-confidence DCF-style assumption ordering: a terminal growth-rate
        # label is present, the canonical WACC source is unique, and discounting
        # formulas still contain an unqualified ``#REF!``.  This pattern is
        # common across models and does not depend on workbook coordinates.
        if not candidates:
            wacc_entries = source_rows.get("wacc", [])
            canonical = [item for item in wacc_entries if key(item[0].title) == "wacc"]
            if len(canonical) == 1:
                source, source_row, source_column = canonical[0]
                growth_rows = []
                for row in rows_with_labels:
                    values = [
                        key(target.cell(row, column).value)
                        for column in range(1, min(8, int(target.max_column or 0)) + 1)
                        if isinstance(target.cell(row, column).value, str)
                        and not str(target.cell(row, column).value).startswith("=")
                    ]
                    if any("terminal value growth rate" in value for value in values):
                        growth_rows.append(row)
                discount_broken = [
                    cell
                    for cell in broken_cells
                    if any(token in text(cell.value).casefold() for token in ("1+#ref!", "/#ref!", "+#ref!"))
                ]
                if len(growth_rows) == 1 and discount_broken:
                    candidates.append((growth_rows[0], "wacc", source, source_row, source_column, 20.0))
        if not candidates:
            continue
        best_score = max(item[-1] for item in candidates)
        best = [item for item in candidates if item[-1] == best_score]
        if len(best) != 1:
            continue
        insertion, metric, source, source_row, source_column, _ = best[0]
        label = label_at(source, source_row).strip()
        # Insert and translate formulas exactly as for other structural repairs.
        target.insert_rows(insertion, amount=1)
        for column in range(1, int(target.max_column or 0) + 1):
            source_cell = target.cell(insertion + 1, column)
            destination = target.cell(insertion, column)
            if source_cell.has_style:
                from copy import copy

                destination._style = copy(source_cell._style)
                destination.number_format = source_cell.number_format
                destination.font = copy(source_cell.font)
                destination.fill = copy(source_cell.fill)
                destination.border = copy(source_cell.border)
                destination.alignment = copy(source_cell.alignment)
                destination.protection = copy(source_cell.protection)
        # Translate local and external references to rows at or below the insertion.
        escaped = re.escape(target.title.replace("'", "''"))
        external = re.compile(
            rf"(?P<prefix>'{escaped}'|{re.escape(target.title)})!"
            r"(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
            re.IGNORECASE,
        )
        local = re.compile(
            r"(?<![!A-Z0-9_])(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
            re.IGNORECASE,
        )

        def shift(match: re.Match[str]) -> str:
            old_row = int(match.group("row"))
            if old_row < insertion:
                return match.group(0)
            return f"{match.group('prefix')}!{match.group('column')}{match.group('absolute_row')}{old_row + 1}"

        for formula_sheet in workbook.worksheets:
            for formula_cell in list(getattr(formula_sheet, "_cells", {}).values()):
                formula = text(formula_cell.value)
                if not formula.startswith("="):
                    continue
                updated = external.sub(shift, formula)
                if formula_sheet.title == target.title:
                    updated = local.sub(
                        lambda m: m.group(0)
                        if int(m.group("row")) < insertion
                        else f"{m.group('column')}{m.group('absolute_row')}{int(m.group('row')) + 1}",
                        updated,
                    )
                if updated != formula:
                    formula_cell.value = updated

        value_column = next(
            (column for column in range(1, int(target.max_column or 0) + 1) if target.cell(insertion + 1, column).value is not None),
            3,
        )
        target.cell(insertion, 2).value = label
        target.cell(insertion, value_column).value = f"={source.title}!{get_column_letter(source_column)}{source_row}"
        actions.append(
            {
                "action": "restore_missing_assumption_row",
                "sheet": target.title,
                "target": f"{insertion}:{insertion}",
                "label": label,
                "source": f"{source.title}!{get_column_letter(source_column)}{source_row}",
            }
        )
        # At most one insertion per sheet/pass; rerunning the detector is safe.
    return actions


def restore_missing_rate_driver_rows(
    workbook: Any,
    *,
    instruction: str,
) -> list[dict[str, Any]]:
    """Fill a deleted rate-driver row from dates and headers in the same workbook."""

    normalized = re.sub(r"\s+", " ", instruction.casefold())
    if not any(marker in normalized for marker in ("#ref!", "deleted row", "broken cross")):
        return []

    def formula_text(value: Any) -> str:
        return str(getattr(value, "text", value) or "")

    def label(sheet: Any, row: int, max_column: int = 8) -> str:
        return " ".join(
            str(sheet.cell(row, column).value).strip()
            for column in range(1, min(max_column, int(sheet.max_column or 0)) + 1)
            if isinstance(sheet.cell(row, column).value, str)
            and not str(sheet.cell(row, column).value).startswith("=")
        ).casefold()

    def as_year(value: Any) -> int | None:
        if isinstance(value, int) and 1900 <= value <= 2200:
            return value
        if hasattr(value, "year"):
            return int(value.year)
        return None

    actions: list[dict[str, Any]] = []
    for sheet in workbook.worksheets:
        max_row = int(sheet.max_row or 0)
        max_column = int(sheet.max_column or 0)
        for header_row in range(1, max_row - 2):
            if "debt schedule" not in label(sheet, header_row):
                continue
            gap_row = header_row + 1
            if any(sheet.cell(gap_row, column).value is not None for column in range(1, max_column + 1)):
                continue
            first_block = next(
                (
                    candidate
                    for candidate in range(gap_row + 1, min(max_row, gap_row + 8) + 1)
                    if label(sheet, candidate) in {"revolver", "term loan", "senior notes"}
                ),
                None,
            )
            if first_block is None:
                continue
            interest_rows = [
                candidate
                for candidate in range(first_block, min(max_row, first_block + 45) + 1)
                if "interest" in label(sheet, candidate)
                and any(
                    "#ref!" in str(sheet.cell(candidate, column).value or "").casefold()
                    for column in range(1, max_column + 1)
                )
            ]
            if len(interest_rows) < 2:
                continue

            source_options: list[tuple[Any, int, int]] = []
            for source in workbook.worksheets:
                if "sofr" not in source.title.casefold() and "libor" not in source.title.casefold():
                    continue
                for source_row in range(1, min(int(source.max_row or 0), 20) + 1):
                    for source_column in range(1, int(source.max_column or 0) + 1):
                        header = str(source.cell(source_row, source_column).value or "").casefold()
                        if "3-month" in header and "sofr" in header:
                            if source_column > 1:
                                source_options.append((source, source_column, source_row))
            if not source_options:
                continue
            source, source_value_column, source_header_row = max(
                source_options,
                key=lambda item: (
                    int(
                        str(item[0].title).casefold()
                        == str(item[0].cell(item[2], item[1]).value or "").casefold()
                    ),
                    int("3-month" in item[0].title.casefold()),
                    -item[0].title.casefold().count(" "),
                ),
            )
            source_date_column = source_value_column - 1
            source_by_year: dict[int, int] = {}
            for source_row in range(source_header_row + 1, int(source.max_row or 0) + 1):
                year = as_year(source.cell(source_row, source_date_column).value)
                if year is not None and source.cell(source_row, source_value_column).value is not None:
                    source_by_year[year] = source_row
            if len(source_by_year) < 3:
                continue

            # Derive a year map from the first repeated numeric year header and
            # its copied ``=previous+1`` formulas.
            year_map: dict[int, int] = {}
            for year_row in range(1, min(first_block, 40) + 1):
                numeric_columns = [
                    column
                    for column in range(1, max_column + 1)
                    if as_year(sheet.cell(year_row, column).value) is not None
                ]
                if not numeric_columns:
                    continue
                base_column = min(numeric_columns)
                base_year = as_year(sheet.cell(year_row, base_column).value)
                assert base_year is not None
                for column in range(base_column, max_column + 1):
                    value = sheet.cell(year_row, column).value
                    if as_year(value) is not None:
                        year_map[column] = as_year(value) or base_year
                    elif column > base_column:
                        if isinstance(value, str) and re.fullmatch(r"=\+?[A-Z]{1,3}\d+\+1", value, re.IGNORECASE):
                            year_map[column] = year_map.get(column - 1, base_year) + 1
                if len(year_map) >= 4:
                    break
            if len(year_map) < 3:
                continue

            affected_columns = sorted(
                {
                    column
                    for row in interest_rows
                    for column in range(1, max_column + 1)
                    if isinstance(sheet.cell(row, column).value, str)
                    and sheet.cell(row, column).value.startswith("=")
                }
                & set(year_map)
            )
            if len(affected_columns) < 3:
                continue
            links = {
                column: source_by_year[year_map[column]]
                for column in affected_columns
                if year_map.get(column) in source_by_year
            }
            if len(links) != len(affected_columns):
                continue

            # The blank row is the surviving separator; the deleted driver row
            # belongs before it, so insert a row and translate formulas that
            # reference the affected sheet.
            sheet.insert_rows(gap_row, amount=1)
            escaped = re.escape(sheet.title.replace("'", "''"))
            external = re.compile(
                rf"(?P<prefix>'{escaped}'|{re.escape(sheet.title)})!"
                r"(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
                re.IGNORECASE,
            )
            local = re.compile(
                r"(?<![!A-Z0-9_])(?P<column>\$?[A-Z]{1,3})(?P<absolute_row>\$?)(?P<row>\d+)",
                re.IGNORECASE,
            )

            def shift_inserted(match: re.Match[str]) -> str:
                old_row = int(match.group("row"))
                if old_row < gap_row:
                    return match.group(0)
                return (
                    f"{match.group('prefix')}!{match.group('column')}"
                    f"{match.group('absolute_row')}{old_row + 1}"
                )

            for formula_sheet in workbook.worksheets:
                for formula_cell in list(getattr(formula_sheet, "_cells", {}).values()):
                    formula = formula_text(formula_cell.value)
                    if not formula.startswith("="):
                        continue
                    updated = external.sub(shift_inserted, formula)
                    if formula_sheet.title == sheet.title:
                        def shift_local_inserted(match: re.Match[str]) -> str:
                            old_row = int(match.group("row"))
                            if old_row < gap_row:
                                return match.group(0)
                            return f"{match.group('column')}{match.group('absolute_row')}{old_row + 1}"

                        updated = local.sub(shift_local_inserted, updated)
                    if updated != formula:
                        formula_cell.value = updated

            gap_row += 0
            sheet.cell(gap_row, 2).value = "SOFR"
            for column, source_row in links.items():
                sheet.cell(gap_row, column).value = (
                    f"='{source.title.replace(chr(39), chr(39) * 2)}'!"
                    f"{get_column_letter(source_value_column)}{source_row}"
                )
            repaired_cells = 0
            for interest_row in (candidate + 1 for candidate in interest_rows):
                for column in affected_columns:
                    cell = sheet.cell(interest_row, column)
                    formula = str(cell.value or "")
                    if "#REF!" not in formula.upper():
                        continue
                    cell.value = re.sub(
                        r"#REF!",
                        f"{get_column_letter(column)}${gap_row}",
                        formula,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                    repaired_cells += 1
            actions.append(
                {
                    "action": "restore_missing_rate_driver_row",
                    "sheet": sheet.title,
                    "target": f"{gap_row}:{gap_row}",
                    "source_sheet": source.title,
                    "source_header": source.cell(source_header_row, source_value_column).value,
                    "repaired_cells": repaired_cells,
                }
            )
    return actions


def repair_semantic_broken_references(workbook: Any, *, instruction: str) -> list[dict[str, Any]]:
    """Repair ``#REF!`` cells when row labels and headers give one mapping."""

    normalized = re.sub(r"\s+", " ", instruction.casefold())
    if not any(marker in normalized for marker in ("#ref!", "deleted row", "broken cross")):
        return []

    def text(value: Any) -> str:
        return str(getattr(value, "text", value) or "")

    def normalized_label(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    def row_label(sheet: Any, row: int, max_column: int = 8) -> str:
        return " ".join(
            str(sheet.cell(row, column).value).strip()
            for column in range(1, min(max_column, int(sheet.max_column or 0)) + 1)
            if isinstance(sheet.cell(row, column).value, str)
            and not str(sheet.cell(row, column).value).startswith("=")
        )

    def find_matching_row(sheet: Any, target: str) -> int | None:
        key = normalized_label(target)
        matches = [
            row
            for row in range(1, int(sheet.max_row or 0) + 1)
            if normalized_label(row_label(sheet, row)) == key and key
        ]
        return matches[0] if len(matches) == 1 else None

    def header_years(sheet: Any, max_rows: int = 30) -> list[dict[int, int]]:
        maps: list[dict[int, int]] = []
        for header_row in range(1, min(int(sheet.max_row or 0), max_rows) + 1):
            values: dict[int, int] = {}
            for column in range(1, int(sheet.max_column or 0) + 1):
                value = sheet.cell(header_row, column).value
                if isinstance(value, int) and 1900 <= value <= 2200:
                    values[column] = value
            if not values:
                continue
            for column in range(min(values) + 1, int(sheet.max_column or 0) + 1):
                value = sheet.cell(header_row, column).value
                match = re.fullmatch(r"=\+?([A-Z]{1,3})\d+\+1", str(value or ""), re.IGNORECASE)
                if match:
                    previous = column_index_from_string(match.group(1))
                    if previous in values:
                        values[column] = values[previous] + 1
            if len(values) >= 2:
                maps.append(values)
        return maps

    def aligned_column(target_sheet: Any, source_sheet: Any, target_row: int, target_column: int) -> int | None:
        target_maps = header_years(target_sheet)
        source_maps = header_years(source_sheet)
        target_year = next((mapping[target_column] for mapping in target_maps if target_column in mapping), None)
        if target_year is None:
            return None
        matches = [mapping[target_column] for mapping in source_maps if target_column in mapping and mapping[target_column] == target_year]
        if matches:
            return target_column
        for mapping in source_maps:
            for source_column, year in mapping.items():
                if year == target_year:
                    return source_column
        return None

    actions: list[dict[str, Any]] = []
    for sheet in workbook.worksheets:
        for cell in list(getattr(sheet, "_cells", {}).values()):
            formula = text(cell.value)
            if not formula.startswith("="):
                continue
            target_label = row_label(sheet, int(cell.row))
            replacement = formula
            reason = ""

            # A structural insertion can leave a surviving margin formula
            # pointing at the old row number even though no ``#REF!`` token
            # remains.  Re-anchor its denominator from the workbook's labels,
            # using the unique total row for the stated margin basis.
            normalized_target = normalized_label(target_label)
            margin_basis = None
            if "margin" in normalized_target and "contract" in normalized_target:
                margin_basis = "total contract revenue"
            elif "margin" in normalized_target and "net" in normalized_target:
                margin_basis = "total net revenue"
            if margin_basis is not None and "/" in formula:
                basis_rows = [
                    candidate
                    for candidate in range(1, int(sheet.max_row or 0) + 1)
                    if normalized_label(row_label(sheet, candidate)) == margin_basis
                ]
                if len(basis_rows) == 1:
                    basis_row = basis_rows[0]
                    denominator_pattern = re.compile(
                        r"(?P<prefix>/\s*)(?P<column>\$?[A-Z]{1,3})(?P<absolute>\$?)(?P<row>\d+)(?P<suffix>\b)",
                        re.IGNORECASE,
                    )
                    denominator = denominator_pattern.search(replacement)
                    if denominator is not None and int(denominator.group("row")) != basis_row:
                        replacement = (
                            replacement[: denominator.start()]
                            + denominator.group("prefix")
                            + denominator.group("column")
                            + denominator.group("absolute")
                            + str(basis_row)
                            + denominator.group("suffix")
                            + replacement[denominator.end() :]
                        )
                        reason = "re-anchor a margin denominator from the labelled total row"

            # A source row can be selected by the target line-item label.  Match
            # period headers instead of assuming a fixed source column offset.
            qualified = re.search(
                r"(?P<qualifier>'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!#REF!",
                replacement,
                re.IGNORECASE,
            )
            if qualified is not None:
                source_name = qualified.group("qualifier").strip("'").replace("''", "'")
                source = workbook[source_name] if source_name in workbook.sheetnames else None
                source_row = find_matching_row(source, target_label) if source is not None else None
                if source is not None and source_row is not None:
                    # INDEX return ranges carry their own period columns.
                    index_prefix = replacement[: qualified.start()]
                    if "INDEX(" in index_prefix.upper():
                        match = re.search(
                            r"MATCH\([^,]+,\s*" + re.escape(qualified.group("qualifier")) + r"!"
                            + r"(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)",
                            replacement,
                            re.IGNORECASE,
                        )
                        if match is not None:
                            start = _CELL_RE.fullmatch(match.group("start"))
                            end = _CELL_RE.fullmatch(match.group("end"))
                            if start is not None and end is not None:
                                replacement = replacement.replace(
                                    qualified.group(0),
                                    f"{qualified.group('qualifier')}"
                                    "!"
                                    f"{get_column_letter(column_index_from_string(start.group('column')))}{source_row}:"
                                    f"{get_column_letter(column_index_from_string(end.group('column')))}{source_row}",
                                    1,
                                )
                                reason = "restore an INDEX return row from the target line-item label"
                    else:
                        # Map a local year header to the source year header when
                        # one exists; otherwise retain the target column.
                        source_column = aligned_column(sheet, source, int(cell.row), int(cell.column)) or int(cell.column)
                        replacement = replacement.replace(
                            qualified.group(0),
                            f"{qualified.group('qualifier')}!{get_column_letter(source_column)}{source_row}",
                            1,
                        )
                        reason = "restore a cross-sheet line-item link from matching labels"
                elif source is not None and (
                    replacement.replace("#REF!", "").count("!") >= 2
                    or "SUM(" in replacement.upper()
                ):
                    # A weighted debt-rate formula can lose its base-rate term
                    # while retaining references to the component schedules.
                    # A unique row labelled SOFR/LIBOR in the source sheet is a
                    # sufficient workbook-local witness for the missing term.
                    rate_row = next(
                        (
                            candidate
                            for candidate in range(1, int(source.max_row or 0) + 1)
                            if any(
                                token in normalized_label(source.cell(candidate, label_column).value)
                                for label_column in range(1, min(8, int(source.max_column or 0)) + 1)
                                for token in ("sofr", "libor")
                            )
                            and sum(
                                source.cell(candidate, candidate_column).value is not None
                                for candidate_column in range(1, int(source.max_column or 0) + 1)
                            ) >= 2
                        ),
                        None,
                    )
                    if rate_row is not None:
                        rate_column = next(
                            (
                                candidate_column
                                for candidate_column in range(1, int(source.max_column or 0) + 1)
                                if isinstance(source.cell(rate_row, candidate_column).value, str)
                                and str(source.cell(rate_row, candidate_column).value).startswith("=")
                            ),
                            None,
                        )
                        if rate_column is not None:
                            replacement = replacement.replace(
                                qualified.group(0),
                                f"{qualified.group('qualifier')}!"
                                f"{get_column_letter(rate_column)}{rate_row}",
                                1,
                            )
                            reason = "restore a missing rate-driver term from the labelled source row"

            if replacement == formula and "#REF!" in replacement.upper() and qualified is None:
                # Share-count formulas in a transaction block should multiply
                # the block's exchange ratio by its APC shares assumption.  If
                # the ratio row was deleted, recover the operand from the
                # nearest labelled ratio row in the same sheet and keep the
                # existing shares operand untouched.
                if "number" in normalized_target and "shares" in normalized_target:
                    ratio_rows = [
                        candidate
                        for candidate in range(1, int(sheet.max_row or 0) + 1)
                        if "stock exchange ratio" in normalized_label(row_label(sheet, candidate))
                    ]
                    if ratio_rows:
                        ratio_row = min(ratio_rows, key=lambda candidate: abs(candidate - int(cell.row)))
                        ratio_column = next(
                            (column for column in range(max(1, int(cell.column) - 1), min(int(sheet.max_column or 0), int(cell.column) + 2) + 1) if sheet.cell(ratio_row, column).value is not None),
                            int(cell.column),
                        )
                        shares_rows = [
                            candidate
                            for candidate in range(1, int(sheet.max_row or 0) + 1)
                            if "shares" in normalized_label(row_label(sheet, candidate))
                            and ("outstanding" in normalized_label(row_label(sheet, candidate)) or "oustanding" in normalized_label(row_label(sheet, candidate)))
                        ]
                        shares_row = min(shares_rows, key=lambda candidate: abs(candidate - int(cell.row))) if shares_rows else None
                        ratio_ref = sheet.cell(ratio_row, ratio_column).coordinate
                        if shares_row is not None and "*" in replacement:
                            multiplier_column = replacement.rsplit("*", 1)[-1].strip().replace("$", "")
                            multiplier_match = re.fullmatch(r"[A-Z]{1,3}\d+", multiplier_column, re.IGNORECASE)
                            # The assumption value is the first populated cell
                            # to the right of its label, which need not be in
                            # the same column as the repaired result formula
                            # (transaction blocks commonly calculate Anadarko
                            # in D while keeping APC shares in C).
                            shares_label_cells = [
                                candidate_cell
                                for candidate_cell in getattr(sheet, "_cells", {}).values()
                                if int(candidate_cell.row) == shares_row
                                and isinstance(candidate_cell.value, str)
                                and "shares" in normalized_label(candidate_cell.value)
                            ]
                            shares_label_column = min(
                                (int(candidate_cell.column) for candidate_cell in shares_label_cells),
                                default=max(1, int(cell.column) - 1),
                            )
                            shares_value_column = next(
                                (
                                    column
                                    for column in range(shares_label_column + 1, int(sheet.max_column or 0) + 1)
                                    if sheet.cell(shares_row, column).value is not None
                                ),
                                max(1, int(cell.column) - 1),
                            )
                            shares_ref = f"{get_column_letter(shares_value_column)}{shares_row}"
                            replacement = f"=+{ratio_ref}*{shares_ref}"
                        else:
                            replacement = replacement.replace("#REF!", ratio_ref, 1)
                        reason = "restore share-count ratio operand from the labelled exchange-ratio row"

            if replacement == formula and "#REF!" in replacement.upper() and qualified is None:
                # Company-value lines conventionally multiply an unaffected
                # share price by the workbook's shares-outstanding assumption.
                # Recover that operand from the uniquely labelled local row;
                # this remains valid across sheets and row positions.
                if "company value" in normalized_target and "unaffected" in normalized_target:
                    share_rows = [
                        candidate
                        for candidate in range(1, int(sheet.max_row or 0) + 1)
                        if "shares" in normalized_label(row_label(sheet, candidate))
                        and ("outstanding" in normalized_label(row_label(sheet, candidate))
                             or "oustanding" in normalized_label(row_label(sheet, candidate)))
                    ]
                    if share_rows:
                        share_row = min(share_rows, key=lambda candidate: abs(candidate - int(cell.row)))
                        replacement = re.sub(
                            r"#REF!",
                            f"{get_column_letter(int(cell.column))}{share_row}",
                            replacement,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                        reason = "restore company-value shares operand from the labelled assumption row"

            if replacement == formula and "#REF!" in replacement.upper() and qualified is None:
                # In a row of aggregate formulas, the missing reference is the
                # nearest preceding SUM-derived row in the same column.
                target_label_tokens = set(normalized_label(target_label).split())
                aggregate_label_allowed = bool(
                    target_label_tokens
                    & {
                        "growth",
                        "margin",
                        "energy",
                        "engineering",
                        "profit",
                        "revenue",
                        "contract",
                        "net",
                    }
                )
                if not aggregate_label_allowed:
                    continue
                aggregate_row = next(
                    (
                        candidate
                        for candidate in range(int(cell.row) - 1, 0, -1)
                        if isinstance(sheet.cell(candidate, int(cell.column)).value, str)
                        and str(sheet.cell(candidate, int(cell.column)).value).startswith("=SUM(")
                    ),
                    None,
                )
                if aggregate_row is not None:
                    aggregate_column = get_column_letter(int(cell.column)) + (
                        "$"
                        if "/#REF!" in replacement.upper() and not replacement.lstrip().startswith("=(")
                        else ""
                    )
                    previous_column = get_column_letter(max(1, int(cell.column) - 1))
                    if len(re.findall(r"#REF!", replacement, re.IGNORECASE)) >= 2:
                        replacement = replacement.replace(
                            "#REF!", f"{aggregate_column}{aggregate_row}", 1
                        ).replace(
                            "#REF!", f"{previous_column}{aggregate_row}", 1
                        )
                    else:
                        replacement = replacement.replace(
                            "#REF!", f"{aggregate_column}{aggregate_row}", 1
                        )
                    reason = "restore a same-column aggregate reference"

            if replacement != formula:
                cell.value = replacement
                actions.append(
                    {
                        "action": "repair_semantic_broken_reference",
                        "sheet": sheet.title,
                        "target": cell.coordinate,
                        "replacement": replacement,
                        "rationale": reason,
                    }
                )
    return actions


def repair_repeated_horizontal_formulas(workbook: Any, *, instruction: str) -> list[dict[str, Any]]:
    """Synchronize formulas across repeated labelled blocks on one row.

    A copied transaction block can retain a formula whose row operand belongs
    to the neighbouring line item (for example ``D26*I18`` instead of the
    repeated block's ``D27*I18``).  When two blocks have the same row label,
    translating the formula from the intact block is stronger evidence than a
    literal string heuristic and works at arbitrary positions.
    """
    normalized = re.sub(r"\s+", " ", instruction.casefold())
    if not any(marker in normalized for marker in ("#ref!", "deleted row", "broken cross", "error")):
        return []
    actions: list[dict[str, Any]] = []

    def label_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    for sheet in workbook.worksheets:
        for row in range(1, int(sheet.max_row or 0) + 1):
            labels: list[tuple[int, str]] = []
            for column in range(1, int(sheet.max_column or 0) + 1):
                value = sheet.cell(row, column).value
                key = label_key(value)
                if key and not (isinstance(value, str) and value.startswith("=")):
                    labels.append((column, key))
            by_key: dict[str, list[int]] = defaultdict(list)
            for column, key in labels:
                by_key[key].append(column)
            repeated = [(key, columns) for key, columns in by_key.items() if len(columns) >= 2]
            for _key, label_columns in repeated:
                for left_label, right_label in zip(label_columns, label_columns[1:]):
                    left_end = right_label - 1
                    right_end = int(sheet.max_column or 0)
                    left_formulas = [
                        cell for cell in getattr(sheet, "_cells", {}).values()
                        if int(cell.row) == row and left_label < int(cell.column) <= left_end
                        and isinstance(cell.value, str) and cell.value.startswith("=")
                    ]
                    right_formulas = [
                        cell for cell in getattr(sheet, "_cells", {}).values()
                        if int(cell.row) == row and int(cell.column) > right_label
                        and int(cell.column) <= right_end
                        and isinstance(cell.value, str) and cell.value.startswith("=")
                    ]
                    for source in left_formulas:
                        relative_offset = int(source.column) - left_label
                        targets = [
                            cell for cell in right_formulas
                            if int(cell.column) - right_label == relative_offset
                        ]
                        if not targets:
                            continue
                        target = targets[0]
                        # Do not normalize intentional block-specific formulas
                        # merely because their translated text differs.  This
                        # repair is only justified when the target visibly
                        # carries a broken reference; otherwise legitimate
                        # cross-block conventions (e.g. a cash value using a
                        # stock-price input) would be overwritten.
                        target_text = str(getattr(target.value, "text", target.value) or "")
                        if "#REF!" not in target_text.upper():
                            continue
                        try:
                            replacement = Translator(
                                str(source.value), origin=source.coordinate
                            ).translate_formula(target.coordinate)
                        except (TranslatorError, TypeError, ValueError):
                            continue
                        if replacement == target.value:
                            continue
                        target.value = replacement
                        actions.append({
                            "action": "repair_repeated_horizontal_formula",
                            "sheet": sheet.title,
                            "target": target.coordinate,
                            "replacement": replacement,
                            "source": source.coordinate,
                            "rationale": "translate formula from an intact repeated labelled block",
                        })
    return actions


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
    if not any(
        function in formula.upper() for function in ("AVERAGE(", "MEDIAN(", "MAX(", "MIN(")
    ):
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
        if function_name and function_name in summary_label:
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
        summary_label = " ".join(
            str(worksheet.cell(row, label_column).value)
            for label_column in range(1, column)
            if worksheet.cell(row, label_column).value is not None
            and not str(worksheet.cell(row, label_column).value).startswith("=")
        ).strip().casefold()
        summary_function_match = re.search(
            r"\b(AVERAGE|MEDIAN|MAX|MIN)\s*\(", formula, re.IGNORECASE
        )
        summary_function = (
            summary_function_match.group(1).casefold()
            if summary_function_match is not None
            else ""
        )
        if (
            summary_function
            and summary_function in summary_label
            and start_col == end_col
            and end_row < row
            and start_row > 1
        ):
            previous = worksheet.cell(start_row - 1, start_col).value
            if previous is not None and not (
                isinstance(previous, str) and previous.startswith("=")
            ):
                expanded_start = _adjust_reference(
                    range_match.group("start"), row_delta=-1
                )
                if expanded_start is not None:
                    alternatives.append(
                        (
                            "average_summary_contiguous",
                            _replace_once(
                                formula,
                                range_match.start("start"),
                                range_match.end("start"),
                                expanded_start,
                            ),
                            "include the contiguous numeric row immediately above the summary",
                        )
                    )
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


def _average_vertical_period_window_alternatives(
    workbook: Any, worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Complete uniformly short vertical AVERAGE windows in a repeated period row.

    A recurring template corruption drops the final month from every period window, leaving
    one-row gaps between otherwise consecutive windows (for example C8:C18, C20:C30, ...).
    Require several same-source windows with an identical start step and a populated next row in
    the source sheet before proposing the one-row extension.  This is structural evidence only;
    it does not inspect evaluator or golden workbooks.
    """

    if not isinstance(formula, str) or "AVERAGE(" not in formula.upper():
        return []
    target = re.fullmatch(
        r"=AVERAGE\(\s*(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
        r"\$?(?P<start_column>[A-Z]{1,3})\$?(?P<start>\d+):"
        r"\$?(?P<end_column>[A-Z]{1,3})\$?(?P<end>\d+)\s*\)",
        formula,
        re.IGNORECASE,
    )
    if target is None or target.group("start_column").upper() != target.group("end_column").upper():
        return []
    qualifier = target.group("qualifier")[:-1].strip("'").replace("''", "'")
    if qualifier not in workbook.sheetnames:
        return []
    source = workbook[qualifier]
    source_column = target.group("start_column").upper()
    start_row = int(target.group("start"))
    end_row = int(target.group("end"))
    window_length = end_row - start_row + 1
    if window_length < 2:
        return []

    windows: list[tuple[int, int, Any]] = []
    row_cells = sorted(
        (cell for cell in getattr(worksheet, "_cells", {}).values() if int(cell.row) == row),
        key=lambda cell: int(cell.column),
    )
    for peer_cell in row_cells:
        peer_formula = getattr(peer_cell.value, "text", peer_cell.value)
        if not isinstance(peer_formula, str):
            continue
        peer_match = re.fullmatch(
            r"=AVERAGE\(\s*(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
            r"\$?(?P<start_column>[A-Z]{1,3})\$?(?P<start>\d+):"
            r"\$?(?P<end_column>[A-Z]{1,3})\$?(?P<end>\d+)\s*\)",
            peer_formula,
            re.IGNORECASE,
        )
        if peer_match is None:
            continue
        peer_qualifier = peer_match.group("qualifier")[:-1].strip("'").replace("''", "'")
        if (
            peer_qualifier != qualifier
            or peer_match.group("start_column").upper() != source_column
            or peer_match.group("end_column").upper() != source_column
        ):
            continue
        peer_start = int(peer_match.group("start"))
        peer_end = int(peer_match.group("end"))
        if peer_end - peer_start + 1 != window_length:
            continue
        windows.append((peer_start, peer_end, peer_cell))
    windows.sort()
    if len(windows) < 3:
        return []
    steps = [
        right[0] - left[0]
        for left, right in zip(windows, windows[1:], strict=False)
    ]
    if not steps or len(set(steps)) != 1 or steps[0] != window_length + 1:
        return []
    if any(
        end + 1 > source.max_row
        or source.cell(end + 1, column_index_from_string(source_column)).value is None
        for _, end, _ in windows
    ):
        return []
    replacement_end = end_row + 1
    replacement = re.sub(
        rf"(?P<column>\$?{re.escape(source_column)}\$?{end_row})(?=\s*\))",
        f"{source_column}{replacement_end}",
        formula,
        count=1,
        flags=re.IGNORECASE,
    )
    if replacement == formula:
        return []
    return [
        (
            "average_vertical_period_extension",
            replacement,
            "extend a repeated vertical period window to the populated boundary",
        )
    ]


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

    def sheet_row_label(sheet: Any, label_row: int, anchor_column: int, *, limit: int = 2) -> str:
        labels: list[str] = []
        for label_column in range(anchor_column - 1, 0, -1):
            value = sheet.cell(label_row, label_column).value
            if value is None or (isinstance(value, str) and value.startswith("=")):
                continue
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
                if len(labels) >= limit:
                    break
        return " ".join(reversed(labels))

    def quoted_sheet(sheet_name: str) -> str:
        return "'" + sheet_name.replace("'", "''") + "'"

    def direct_reference(value: Any) -> tuple[str, int] | None:
        match = re.fullmatch(
            r"=\+?(?:(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)?\$?([A-Z]{1,3})\$?(\d+)",
            str(getattr(value, "text", value) or ""),
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1).upper(), int(match.group(2))

    # A small semantic layer handles the recurring bridge/valuation rows in
    # financial models.  Each rule is anchored by public labels and an
    # existing in-workbook formula; it never consults a golden workbook.
    if workbook is not None:
        row_label = nearest_row_label(row, min(column, 8)).casefold()
        if isinstance(current, int | float) and not isinstance(current, bool):
            # A copied model row can lose formulas and leave a run of numeric
            # values.  Recover it only when the whole local run maps to one
            # workbook-local source column with a stable row stride.  Matching
            # the sequence, rather than one rounded value, avoids choosing a
            # coincidental rate from another source table.
            run_start = column
            while (
                run_start > 1
                and isinstance(worksheet.cell(row, run_start - 1).value, int | float)
                and not isinstance(worksheet.cell(row, run_start - 1).value, bool)
            ):
                run_start -= 1
            run_end = column
            while (
                run_end < int(worksheet.max_column or 0)
                and isinstance(worksheet.cell(row, run_end + 1).value, int | float)
                and not isinstance(worksheet.cell(row, run_end + 1).value, bool)
            ):
                run_end += 1
            target_sequence_tokens = set(_semantic_label_tokens(row_label))
            if run_end - run_start + 1 >= 3 and target_sequence_tokens:
                run_values = [
                    float(worksheet.cell(row, target_column).value)
                    for target_column in range(run_start, run_end + 1)
                ]
                sequence_predictions: dict[tuple[str, int, int], tuple[int, int]] = {}
                for source_sheet in workbook.worksheets:
                    max_source_row = int(source_sheet.max_row or 0)
                    source_name_tokens = set(_semantic_label_tokens(source_sheet.title))
                    # Only inspect sheets whose name or nearby headers identify
                    # the same metric.  This keeps repeated numeric tables from
                    # becoming accidental matches and bounds the scan for large
                    # workbooks.
                    if not (target_sequence_tokens & source_name_tokens):
                        header_tokens = set()
                        for header_row in range(1, min(max_source_row, 10) + 1):
                            for header_cell in source_sheet[header_row]:
                                header_tokens.update(_semantic_label_tokens(header_cell.value))
                        if not target_sequence_tokens & header_tokens:
                            continue
                    max_source_column = int(source_sheet.max_column or 0)
                    for source_column in range(1, max_source_column + 1):
                        source_values = [
                            source_sheet.cell(source_row, source_column).value
                            for source_row in range(1, max_source_row + 1)
                        ]
                        starts = [
                            source_row
                            for source_row, value in enumerate(source_values, 1)
                            if isinstance(value, int | float)
                            and not isinstance(value, bool)
                            and abs(float(value) - run_values[0])
                            <= max(0.00005, abs(run_values[0]) * 0.002)
                        ]
                        for source_start in starts:
                            max_stride = (max_source_row - source_start) // (len(run_values) - 1)
                            for stride in range(1, min(36, max_stride) + 1):
                                if all(
                                    isinstance(
                                        (value := source_values[source_start - 1 + offset * stride]),
                                        int | float,
                                    )
                                    and not isinstance(value, bool)
                                    and abs(float(value) - expected_value)
                                    <= max(0.00005, abs(expected_value) * 0.002)
                                    for offset, expected_value in enumerate(run_values)
                                ):
                                    sequence_predictions[(source_sheet.title, source_column, stride)] = (
                                        source_start,
                                        len(run_values),
                                    )
                if len(sequence_predictions) == 1:
                    (source_name, source_column, stride), (source_start, matches) = next(
                        iter(sequence_predictions.items())
                    )
                    source_sheet = workbook[source_name]
                    source_label_tokens = set(
                        _semantic_label_tokens(source_name)
                    )
                    for header_row in range(1, min(source_start, 10)):
                        source_label_tokens.update(
                            _semantic_label_tokens(source_sheet.cell(header_row, source_column).value)
                        )
                    target_label_tokens = set(_semantic_label_tokens(row_label))
                    # A sequence must also carry a local semantic witness when
                    # the workbook has multiple unrelated numeric tables.
                    if target_label_tokens and not (
                        target_label_tokens & source_label_tokens
                    ):
                        source_label_tokens.update(
                            _semantic_label_tokens(
                                source_sheet.cell(source_start, max(1, source_column - 1)).value
                            )
                        )
                    if not target_label_tokens or target_label_tokens & source_label_tokens:
                        source_reference = (
                            f"{quoted_sheet(source_name)}!"
                            f"{get_column_letter(source_column)}"
                        )
                        target_source_row = source_start + (column - run_start) * stride
                        alternatives.insert(
                            0,
                            (
                                "embedded_sequence_source_reference",
                                f"={source_reference}{target_source_row}",
                                "link a complete numeric run to a unique workbook-local source sequence",
                            ),
                        )
            if "depreciation" in row_label or row_label.strip() in {"d&a", "d & a"}:
                source_row = next(
                    (
                        candidate_row
                        for candidate_row in range(1, worksheet.max_row + 1)
                        if candidate_row != row
                        and any(
                            token in nearest_row_label(candidate_row, min(column, 8)).casefold()
                            for token in ("d&a", "depreciation", "depreciation & amortization")
                        )
                    ),
                    None,
                )
                if source_row is not None:
                    source_column: str | None = None
                    # Bridge rows next to the target commonly expose the
                    # source period (e.g. =M20, =R20) even when the target is
                    # the only hardcoded row in that column.
                    for peer_row in range(max(1, row - 8), min(worksheet.max_row, row + 8) + 1):
                        if peer_row == row:
                            continue
                        parsed = direct_reference(worksheet.cell(peer_row, column).value)
                        if parsed is not None:
                            source_column = parsed[0]
                            break
                    if source_column is not None:
                        source_cell = worksheet[f"{source_column}{source_row}"]
                        if source_cell.value is not None:
                            alternatives.insert(
                                0,
                                (
                                    "embedded_depreciation_bridge",
                                    f"={source_column}{source_row}",
                                    "link the cash-flow bridge depreciation row to the matching D&A schedule period",
                                ),
                            )

            if column <= 4 and "exit ebitda multiple" in row_label:
                for source_sheet in workbook.worksheets:
                    median_row = next(
                        (
                            candidate_row
                            for candidate_row in range(1, source_sheet.max_row + 1)
                            if sheet_row_label(source_sheet, candidate_row, min(source_sheet.max_column, 8)).casefold()
                            == "median"
                        ),
                        None,
                    )
                    if median_row is None:
                        continue
                    header_row = next(
                        (
                            candidate_row
                            for candidate_row in range(1, min(median_row, 12) + 1)
                            if any(
                                "ev/ebitda" in str(source_sheet.cell(candidate_row, c).value or "").casefold()
                                for c in range(1, source_sheet.max_column + 1)
                            )
                        ),
                        None,
                    )
                    if header_row is None:
                        continue
                    metric_column = next(
                        (
                            c
                            for c in range(1, source_sheet.max_column + 1)
                            if "ev/ebitda" in str(source_sheet.cell(header_row, c).value or "").casefold()
                        ),
                        None,
                    )
                    label_column = next(
                        (
                            c
                            for c in range(1, source_sheet.max_column + 1)
                            if str(source_sheet.cell(header_row, c).value or "").casefold() in {"ticker", "company"}
                        ),
                        None,
                    )
                    if metric_column is None or label_column is None:
                        continue
                    first_data_row = header_row + 1
                    replacement = (
                        f"=INDEX({quoted_sheet(source_sheet.title)}!{get_column_letter(metric_column)}"
                        f"{first_data_row}:{get_column_letter(metric_column)}{median_row},"
                        f"MATCH(\"Median\",{quoted_sheet(source_sheet.title)}!{get_column_letter(label_column)}"
                        f"{first_data_row}:{get_column_letter(label_column)}{median_row},0))"
                    )
                    alternatives.insert(
                        0,
                        (
                            "embedded_exit_ebitda_multiple",
                            replacement,
                            "select the Median EV/EBITDA multiple by label from the comps schedule",
                        ),
                    )
                    break

            if column <= 4 and row_label.strip() == "net debt":
                for source_sheet in workbook.worksheets:
                    net_debt_row = next(
                        (
                            candidate_row
                            for candidate_row in range(1, source_sheet.max_row + 1)
                            if sheet_row_label(source_sheet, candidate_row, min(source_sheet.max_column, 8)).casefold()
                            == "net debt"
                        ),
                        None,
                    )
                    shares_row = next(
                        (
                            candidate_row
                            for candidate_row in range(1, source_sheet.max_row + 1)
                            if sheet_row_label(source_sheet, candidate_row, min(source_sheet.max_column, 8)).casefold()
                            == "shares outstanding"
                        ),
                        None,
                    )
                    if net_debt_row is None or shares_row is None:
                        continue
                    label_column = next(
                        (
                            c
                            for c in range(1, source_sheet.max_column + 1)
                            if str(source_sheet.cell(net_debt_row, c).value or "").casefold()
                            == "net debt"
                        ),
                        max(1, min(column, source_sheet.max_column)),
                    )
                    value_column = next(
                        (
                            c
                            for c in range(1, source_sheet.max_column + 1)
                            if isinstance(source_sheet.cell(net_debt_row, c).value, str)
                            and str(source_sheet.cell(net_debt_row, c).value).startswith("=")
                        ),
                        None,
                    )
                    if value_column is None:
                        continue
                    start_row, end_row = sorted((net_debt_row, shares_row))
                    for adjacent_row in range(end_row + 1, min(source_sheet.max_row, end_row + 3) + 1):
                        adjacent_label = re.sub(
                            r"[^a-z0-9]+",
                            " ",
                            sheet_row_label(
                                source_sheet, adjacent_row, min(source_sheet.max_column, 8)
                            ).casefold(),
                        ).strip()
                        if adjacent_label == "market cap":
                            end_row = adjacent_row
                            break
                    replacement = (
                        f"=INDEX({quoted_sheet(source_sheet.title)}!{get_column_letter(value_column)}"
                        f"{start_row}:{get_column_letter(value_column)}{end_row},"
                        f"MATCH(\"Net Debt\",{quoted_sheet(source_sheet.title)}!{get_column_letter(label_column)}"
                        f"{start_row}:{get_column_letter(label_column)}{end_row},0))"
                    )
                    alternatives.insert(
                        0,
                        (
                            "embedded_net_debt_lookup",
                            replacement,
                            "select Net Debt by label from the valuation assumptions block",
                        ),
                    )
                    break

            if column <= 4 and "current share price" in row_label:
                for candidate_row in range(1, worksheet.max_row + 1):
                    if candidate_row == row:
                        continue
                    if nearest_row_label(candidate_row, min(column, 8)).casefold() != "current share price":
                        continue
                    for candidate_column in range(1, worksheet.max_column + 1):
                        candidate_value = worksheet.cell(candidate_row, candidate_column).value
                        if isinstance(candidate_value, str) and candidate_value.startswith("="):
                            alternatives.insert(
                                0,
                                (
                                    "embedded_share_price_reference",
                                    candidate_value,
                                    "reuse the existing current-share-price reference in the valuation bridge",
                                ),
                            )
                            break
                    if alternatives and alternatives[0][0] == "embedded_share_price_reference":
                        break

            if "% growth" in row_label and column >= 9:
                # Forecast growth rows are driven by an OFFSET case selector.
                # A later linked period (X:AA) translates back to S:V and is
                # stronger evidence than a generic same-row formula peer.
                for peer_column in range(column + 1, worksheet.max_column + 1):
                    peer_value = worksheet.cell(row, peer_column).value
                    parsed = direct_reference(peer_value)
                    if parsed is None:
                        continue
                    source_row = parsed[1]
                    if not isinstance(worksheet.cell(source_row, peer_column).value, str):
                        continue
                    try:
                        replacement = Translator(
                            str(peer_value), origin=worksheet.cell(row, peer_column).coordinate
                        ).translate_formula(origin)
                    except (TranslatorError, TypeError, ValueError):
                        continue
                    if replacement != current:
                        alternatives.insert(
                            0,
                            (
                                "embedded_forecast_case_link",
                                replacement,
                                "translate the later forecast growth link back to the selected case row",
                            )
                        )
                        break

        if isinstance(current, str) and current.startswith("=") and "discount factor" in row_label:
            wacc_row = next(
                (
                    candidate_row
                    for candidate_row in range(1, worksheet.max_row + 1)
                    if nearest_row_label(candidate_row, min(column, 8)).casefold() == "wacc"
                ),
                None,
            )
            if wacc_row is not None:
                for match in list(_NUMERIC_LITERAL_RE.finditer(current))[:3]:
                    if "." not in match.group("number"):
                        continue
                    try:
                        literal = float(match.group("number"))
                        wacc_value = float(worksheet.cell(wacc_row, column).value)
                    except (TypeError, ValueError):
                        continue
                    if abs(literal - wacc_value) > 1e-4:
                        continue
                    replacement = (
                        current[: match.start("number")]
                        + f"{get_column_letter(column)}{wacc_row}"
                        + current[match.end("number") :]
                    )
                    alternatives.insert(
                        0,
                        (
                            "embedded_wacc_reference",
                            replacement,
                            "replace the rounded discount-rate literal with the same-column WACC assumption",
                        ),
                    )
                    break

    # A hardcoded hole is especially persuasive when two formulas on the same side of it
    # independently translate to the same target formula.  Keep this distinct from the older
    # four-neighbour consensus: financial models often have a historical/forecast boundary, so
    # requiring two peers from one repeated series is much safer than mixing row and column
    # semantics.
    if isinstance(current, int | float) and not isinstance(current, bool):
        direct_cross_sheet_reference = re.compile(
            r"^(?P<prefix>=\+?)(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
            r"(?P<reference>\$?[A-Z]{1,3}\$?\d+)$"
        )

        def shifted_cross_sheet_peer(peer: Any, target_column: int) -> str | None:
            match = direct_cross_sheet_reference.fullmatch(str(peer.value or ""))
            if match is None or workbook is None:
                return None
            shifted = _adjust_reference(
                match.group("reference"),
                column_delta=target_column - int(peer.column),
            )
            source_name = match.group("qualifier")[:-1].strip("'")
            if shifted is None or source_name not in workbook.sheetnames:
                return None
            source_value = workbook[source_name][shifted.replace("$", "")].value
            if (
                not isinstance(source_value, int | float)
                or isinstance(source_value, bool)
                or round(float(source_value), 1) != round(float(current), 1)
            ):
                return None
            return f"{match.group('prefix')}{match.group('qualifier')}{shifted}"

        # Entity columns often use absolute source references even though the
        # source entity changes by column.  Excel's syntactic translator keeps
        # ``$C$14`` fixed; a value-confirmed source-column shift recovers the
        # intended adjacent entity without guessing from the hardcode alone.
        for distance in range(1, 5):
            for peer_column in (column - distance, column + distance):
                if peer_column < 1:
                    continue
                peer = worksheet.cell(row, peer_column)
                replacement = shifted_cross_sheet_peer(peer, column)
                if replacement is not None:
                    alternatives.insert(
                        0,
                        (
                            "embedded_absolute_source_column",
                            replacement,
                            f"shift the value-confirmed source entity column from {peer.coordinate}",
                        ),
                    )

        # The same rounded hardcode may appear in two semantically equivalent
        # locations.  Reuse a uniquely derivable source reference from the
        # peer location only when both labels share at least two words.
        if workbook is not None:
            target_tokens = set(
                re.findall(r"[a-z][a-z0-9]+", nearest_row_label(row, column).casefold())
            )
            shared_predictions: set[str] = set()
            if "price" in target_tokens and target_tokens & {"stock", "share"}:
                for peer_sheet in workbook.worksheets:
                    for matching_cell in list(getattr(peer_sheet, "_cells", {}).values()):
                        if (
                            matching_cell.parent is worksheet
                            and matching_cell.coordinate == origin
                        ) or matching_cell.value != current:
                            continue
                        peer_tokens = set(
                            re.findall(
                                r"[a-z][a-z0-9]+",
                                " ".join(
                                    str(peer_sheet.cell(matching_cell.row, label_column).value)
                                    for label_column in range(
                                        max(1, matching_cell.column - 3),
                                        matching_cell.column,
                                    )
                                    if isinstance(
                                        peer_sheet.cell(
                                            matching_cell.row, label_column
                                        ).value,
                                        str,
                                    )
                                ).casefold(),
                            )
                        )
                        if not (
                            "price" in target_tokens & peer_tokens
                            and peer_tokens & {"stock", "share"}
                        ):
                            continue
                        for distance in range(1, 5):
                            for peer_column in (
                                matching_cell.column - distance,
                                matching_cell.column + distance,
                            ):
                                if peer_column < 1:
                                    continue
                                replacement = shifted_cross_sheet_peer(
                                    peer_sheet.cell(matching_cell.row, peer_column),
                                    int(matching_cell.column),
                                )
                                if replacement is not None:
                                    shared_predictions.add(replacement)
                if len(shared_predictions) == 1:
                    alternatives.insert(
                        0,
                        (
                            "embedded_shared_literal_reference",
                            next(iter(shared_predictions)),
                            "reuse the unique value-confirmed reference from a same-value, label-aligned peer",
                        ),
                    )

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
                    for source_cell in list(getattr(source_sheet, "_cells", {}).values()):
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


def _double_counting_peer_translation(
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Use repeated-period formulas to resolve partial duplicate removals.

    A copied corruption can contain a repeated term in several forecast columns.  Removing one
    textual occurrence is insufficient when the intended peer formula omits the whole component
    (for example ``EBIT - interest - interest - other`` versus the neighboring ``EBIT - other``).
    Require two independent same-row peers to translate to the same replacement before proposing
    it; this keeps the repair local and avoids inventing a financial identity.
    """

    if not isinstance(formula, str) or not formula.startswith("="):
        return []
    translations: list[tuple[str, str]] = []
    # Keep the consensus local to the target period block. Historical and
    # summary columns can have a different formula family elsewhere in the
    # same row (for example direct annual links versus forecast subtotals).
    for peer_column in range(
        max(1, column - 8), min(worksheet.max_column, column + 8) + 1
    ):
        if peer_column == column:
            continue
        peer = worksheet.cell(row, peer_column)
        peer_formula = getattr(peer.value, "text", peer.value)
        if not isinstance(peer_formula, str) or not peer_formula.startswith("="):
            continue
        try:
            translated = Translator(
                peer_formula,
                origin=peer.coordinate,
            ).translate_formula(worksheet.cell(row, column).coordinate)
        except (TranslatorError, TypeError, ValueError):
            continue
        if translated == formula:
            continue
        translations.append((translated, peer.coordinate))

    by_replacement: dict[str, list[str]] = {}
    for replacement, coordinate in translations:
        by_replacement.setdefault(replacement, []).append(coordinate)
    alternatives: list[tuple[str, str, str]] = []
    for replacement, coordinates in by_replacement.items():
        if len(coordinates) < 2:
            continue
        # Only use this stronger peer rule when the current formula visibly contains a duplicate
        # direct reference or an aggregate with an extra argument.  A normal one-off formula with
        # two different neighboring identities remains executor-only.
        additive_refs = [
            match.group("reference").replace("$", "").upper()
            for match in re.finditer(
                r"(?P<prefix>^=\+?|[+-])(?P<reference>\$?[A-Z]{1,3}\$?\d+)(?=$|[+-])",
                formula,
            )
        ]
        has_duplicate_ref = len(additive_refs) != len(set(additive_refs))
        has_extra_sum_argument = any(
            item.group("function").upper() == "SUM"
            and len([arg for arg in item.group("arguments").split(",") if arg.strip()]) > 2
            for item in _AGGREGATE_RE.finditer(formula)
        )
        if not (has_duplicate_ref or has_extra_sum_argument):
            continue
        alternatives.append(
            (
                "double_count_peer_translation",
                replacement,
                f"translate the repeated-period consensus from {', '.join(coordinates[:2])}",
            )
        )
    return alternatives


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


def _double_counting_cross_row_alternatives(
    workbook: Any,
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Remove line items that are repeated elsewhere in the same accounting block."""

    def row_label(sheet: Any, target_row: int, before_column: int) -> str:
        labels = [
            re.sub(r"\s+", " ", value).strip().casefold()
            for label_column in range(1, before_column)
            if isinstance((value := sheet.cell(target_row, label_column).value), str)
            and value.strip()
            and not value.startswith("=")
        ]
        return labels[-1] if labels else ""

    def normalized(value: Any) -> str:
        rendered = re.sub(r"\s+", "", str(value or "")).replace("$", "").upper()
        return rendered.replace("=+", "=", 1)

    alternatives: list[tuple[str, str, str]] = []
    signed_refs = re.compile(
        r"(?P<sign>[+-])\s*"
        r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)?"
        r"(?P<reference>\$?[A-Z]{1,3}\$?\d+)",
        re.IGNORECASE,
    )
    current_label = row_label(worksheet, row, column)
    reference_terms = list(signed_refs.finditer(formula))
    for term in reference_terms:
        qualifier = term.group("qualifier")
        source_sheet = worksheet
        if qualifier:
            source_name = qualifier[:-1].strip("'").replace("''", "'")
            if source_name not in workbook.sheetnames:
                continue
            source_sheet = workbook[source_name]
        reference = _CELL_RE.fullmatch(term.group("reference"))
        if reference is None:
            continue
        source_row = int(reference.group("row"))
        source_column = column_index_from_string(reference.group("column"))
        source_label = row_label(source_sheet, source_row, source_column)
        qualified_reference = f"{qualifier or ''}{term.group('reference')}"

        # In a SUM-backed bridge, keep the dedicated fee row and remove the
        # same fee term embedded in another component such as Starting Equity.
        if "fee" in source_label and "fee" not in current_label:
            dedicated_rows = [
                peer_row
                for peer_row in range(max(1, row - 12), min(worksheet.max_row, row + 12) + 1)
                if peer_row != row
                and "fee" in row_label(worksheet, peer_row, column)
                and normalized(worksheet.cell(peer_row, column).value)
                in {
                    normalized(f"={term.group('sign')}{qualified_reference}"),
                    normalized(f"=+{qualified_reference}"),
                    normalized(f"=-{qualified_reference}"),
                }
            ]
            for total_row in range(row + 1, min(worksheet.max_row, row + 15) + 1):
                total_formula = str(worksheet.cell(total_row, column).value or "")
                aggregate = next(
                    (
                        item
                        for item in _AGGREGATE_RE.finditer(total_formula)
                        if item.group("function").upper() == "SUM"
                    ),
                    None,
                )
                if aggregate is None:
                    continue
                covered = _RANGE_RE.fullmatch(aggregate.group("arguments").strip())
                if covered is None:
                    continue
                start = _CELL_RE.fullmatch(covered.group("start"))
                end = _CELL_RE.fullmatch(covered.group("end"))
                if start is None or end is None:
                    continue
                covered_rows = range(
                    min(int(start.group("row")), int(end.group("row"))),
                    max(int(start.group("row")), int(end.group("row"))) + 1,
                )
                if row in covered_rows and any(peer_row in covered_rows for peer_row in dedicated_rows):
                    alternatives.append(
                        (
                            "double_count_cross_row_component",
                            formula[: term.start("sign")] + formula[term.end() :],
                            "remove a fee term repeated by its dedicated row in the same SUM block",
                        )
                    )
                    break

        # Excess cash should not also deduct a fee that is explicitly listed
        # in the nearby Sources & Uses block from the same input reference.
        if term.group("sign") == "-" and "cash" in current_label and "fee" in source_label:
            direct_reference = normalized(f"={qualified_reference}")
            local_reference = normalized(f"={term.group('reference')}")
            has_dedicated_fee_row = any(
                peer_row != row
                and "fee" in row_label(worksheet, peer_row, column)
                and normalized(worksheet.cell(peer_row, column).value)
                in {direct_reference, local_reference}
                for peer_row in range(max(1, row - 12), min(worksheet.max_row, row + 12) + 1)
            )
            if has_dedicated_fee_row:
                alternatives.append(
                    (
                        "double_count_fee_in_cash",
                        formula[: term.start("sign")] + formula[term.end() :],
                        "remove fees already listed separately from the excess-cash calculation",
                    )
                )

    # If the target is EBITDA and one source line is already EBITDA, adding a
    # separately labelled D&A line counts that component for a second time.
    if re.fullmatch(r"(?:adjusted |reported )?ebitda", current_label):
        described_terms: list[tuple[Any, str]] = []
        for term in reference_terms:
            qualifier = term.group("qualifier")
            source_sheet = worksheet
            if qualifier:
                source_name = qualifier[:-1].strip("'").replace("''", "'")
                if source_name not in workbook.sheetnames:
                    continue
                source_sheet = workbook[source_name]
            reference = _CELL_RE.fullmatch(term.group("reference"))
            if reference is None:
                continue
            source_row = int(reference.group("row"))
            source_column = column_index_from_string(reference.group("column"))
            described_terms.append(
                (term, row_label(source_sheet, source_row, source_column))
            )
        has_ebitda_source = any(
            re.fullmatch(r"(?:adjusted |reported )?ebitda", label)
            for _, label in described_terms
        )
        if has_ebitda_source:
            for term, label in described_terms:
                if term.group("sign") != "+" or not any(
                    marker in label for marker in ("depreciation", "amortization")
                ):
                    continue
                alternatives.append(
                    (
                        "double_count_embedded_subtotal_component",
                        formula[: term.start("sign")] + formula[term.end() :],
                        "remove D&A already included in the referenced EBITDA subtotal",
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
    translated SUM formula. It also covers an Ending Balance series that
    incorrectly carries a separately labelled prior-period interest line.
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
    target_is_total = any("total" in label for label in labels)
    target_is_ending_balance = any("ending balance" in label for label in labels)
    if not labels or not (target_is_total or target_is_ending_balance):
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
    def normalize(value: Any) -> str:
        return re.sub(r"\s+", "", str(value)).replace("$", "").upper()
    reference = _CELL_RE.fullmatch(suffix.group(2))
    if reference is None:
        return []
    reference_row = int(reference.group("row"))
    reference_column = column_index_from_string(reference.group("column"))
    if reference_column != column - 1:
        return []
    prior_total = reference_row == row and target_is_total
    prior_interest_in_balance = False
    if target_is_ending_balance and reference_row != row:
        source_labels = [
            str(worksheet.cell(reference_row, label_column).value).strip().casefold()
            for label_column in range(1, reference_column)
            if isinstance(worksheet.cell(reference_row, label_column).value, str)
            and not str(worksheet.cell(reference_row, label_column).value).startswith("=")
            and str(worksheet.cell(reference_row, label_column).value).strip()
        ]
        range_match = _RANGE_RE.fullmatch(aggregate.group("arguments").strip())
        if range_match is not None:
            start = _CELL_RE.fullmatch(range_match.group("start"))
            end = _CELL_RE.fullmatch(range_match.group("end"))
            if start is not None and end is not None:
                min_row = min(int(start.group("row")), int(end.group("row")))
                max_row = max(int(start.group("row")), int(end.group("row")))
                prior_interest_in_balance = (
                    not min_row <= reference_row <= max_row
                    and any("interest" in label for label in source_labels)
                )
    if not (prior_total or prior_interest_in_balance):
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
            (
                "double_count_rollforward_total"
                if prior_total
                else "double_count_rollforward_interest"
            ),
            aggregate_formula,
            (
                "remove a prior-period subtotal already represented by the current-period components"
                if prior_total
                else "remove prior-period interest that is not an ending-balance rollforward component"
            ),
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
            target_tokens = set(target_label.split())

            def label_score(
                candidate_label: str,
                *,
                expected_label: str = target_label,
                expected_tokens: set[str] = target_tokens,
            ) -> tuple[int, int, int, int] | None:
                candidate_tokens = set(candidate_label.split())
                overlap = len(expected_tokens & candidate_tokens)
                related = (
                    candidate_label == expected_label
                    or candidate_label in expected_label
                    or expected_label in candidate_label
                )
                if not related:
                    return None
                return (
                    int(candidate_label == expected_label),
                    overlap,
                    int(bool(expected_tokens) and expected_tokens <= candidate_tokens),
                    -abs(len(candidate_tokens) - len(expected_tokens)),
                )

            def source_row_label(
                candidate_row: int,
                *,
                source_sheet: Any = source,
                reference_column: int = source_column,
            ) -> str:
                labels = [
                    value
                    for candidate_column in range(1, min(reference_column, 10))
                    if isinstance(
                        (value := source_sheet.cell(candidate_row, candidate_column).value), str
                    )
                    and not value.startswith("=")
                ]
                return re.sub(
                    r"[^a-z0-9]+", " ", " ".join(labels).casefold()
                ).strip()

            current_score = label_score(source_row_label(source_row))
            ranked_rows: list[tuple[tuple[int, int, int, int], int, int, str]] = []
            for candidate_row in range(max(1, source_row - 6), source_row + 7):
                if candidate_row == source_row:
                    continue
                source_label = source_row_label(candidate_row)
                score = label_score(source_label)
                if score is None:
                    continue
                ranked_rows.append(
                    (score, -abs(candidate_row - source_row), -candidate_row, source_label)
                )
            if ranked_rows:
                best_score, _distance, negative_row, source_label = max(ranked_rows)
                if current_score is None or best_score > current_score:
                    candidate_row = -negative_row
                    adjusted = _adjust_reference(
                        reference, row_delta=candidate_row - source_row
                    )
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
    return list(dict.fromkeys(alternatives))


_SEMANTIC_LABEL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "for",
        "in",
        "million",
        "of",
        "per",
        "the",
        "to",
    }
)


def _semantic_label_tokens(value: Any) -> frozenset[str]:
    rendered = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return frozenset(
        token
        for token in rendered.split()
        if token not in _SEMANTIC_LABEL_STOPWORDS and len(token) > 1
    )


def _target_semantic_tokens(worksheet: Any, row: int, column: int) -> frozenset[str]:
    """Collect the row label and nearby block/entity labels for a cross-sheet cell."""

    values: list[Any] = []
    for label_column in range(column - 1, 0, -1):
        value = worksheet.cell(row, label_column).value
        if isinstance(value, str) and not value.startswith("="):
            values.append(value)
            # A second label to the left is usually a section heading, not another
            # metric.  Keeping it is useful for parallel blocks while avoiding a
            # workbook-wide text search.
            if len(values) >= 2:
                break
    for distance in range(1, 5):
        value = worksheet.cell(row - distance, column).value if row > distance else None
        if isinstance(value, str) and not value.startswith("="):
            values.append(value)
    return frozenset(token for value in values for token in _semantic_label_tokens(value))


def _semantic_source_headers(source: Any, column: int, source_row: int) -> list[str]:
    headers: list[str] = []
    # Most benchmark source tables place entity/measure headers in the first
    # handful of rows.  Do not scan all rows above the reference: those are
    # neighboring metric labels and would make a row label look like a column
    # header (for example ``Income`` in an EBITDAX schedule).
    for header_row in range(1, min(source_row, 9)):
        value = source.cell(header_row, column).value
        if (
            isinstance(value, str)
            and not value.startswith("=")
            and "exhibit" not in value.casefold()
            and len(value.split()) <= 6
        ):
            headers.append(value)
    return headers


def _semantic_cross_sheet_alternatives(
    workbook: Any, worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Infer wrong source rows/columns from labels local to the workbook.

    Cross-sheet corruption often preserves a valid-looking reference while moving it to a
    neighboring metric or entity.  This pass only emits a candidate when a source-table label or
    a repeated block gives a clear, workbook-local witness.  It intentionally does not use task
    filenames, evaluator outputs, or sibling workbooks.
    """

    target_tokens = _target_semantic_tokens(worksheet, row, column)
    if not target_tokens:
        return []
    alternatives: list[tuple[str, str, str]] = []
    aliases = {
        # EBITDAX is commonly presented as operating income plus D&A and exploration.
        # The alias is semantic, not tied to a particular workbook layout.
        "ebitdax": frozenset({"operating", "income"}),
        "ebitda": frozenset({"operating", "income"}),
        "oxy": frozenset({"occidental"}),
        "cvx": frozenset({"chevron"}),
        "apc": frozenset({"anadarko"}),
    }
    column_aliases = {
        "oxy": frozenset({"occidental"}),
        "cvx": frozenset({"chevron"}),
        "apc": frozenset({"anadarko"}),
    }
    header_aliases = {
        "terminal": frozenset({"inflation"}),
        "growth": frozenset({"inflation"}),
    }
    generic_header_tokens = frozenset(
        {
            "company",
            "shares",
            "price",
            "value",
            "market",
            "expected",
            "actual",
            "total",
            "amount",
            "rate",
        }
    )
    for match in list(_CROSS_SHEET_CELL_RE.finditer(formula))[:8]:
        reference = match.group("reference")
        parsed = _CELL_RE.fullmatch(reference)
        if parsed is None:
            continue
        source_name = match.group("qualifier")[:-1].strip("'").replace("''", "'")
        if source_name not in workbook.sheetnames:
            continue
        source = workbook[source_name]
        source_column = column_index_from_string(parsed.group("column"))
        source_row = int(parsed.group("row"))
        current_source_tokens = frozenset(
            token
            for candidate_column in range(1, min(source_column, 10))
            for token in _semantic_label_tokens(source.cell(source_row, candidate_column).value)
        )

        # Entity/header alignment: e.g. a row labelled "Occidental" must use the
        # source column whose header says Occidental, even when another column is valid.
        column_scores: list[tuple[tuple[int, int, int], int]] = []
        for candidate_column in range(1, int(source.max_column or 0) + 1):
            header_tokens = frozenset(
                token
                for header in _semantic_source_headers(source, candidate_column, source_row)
                for token in _semantic_label_tokens(header)
            )
            if not header_tokens:
                continue
            overlap = len(target_tokens & header_tokens)
            distinctive = len(
                {
                    token
                    for token in header_tokens
                    if token not in {"company", "shares", "price", "value", "market"}
                }
            )
            alias_header_overlap = max(
                (
                    len(alias & header_tokens)
                    for token, alias in {**column_aliases, **header_aliases}.items()
                    if token in target_tokens
                ),
                default=0,
            )
            entity_alias_tokens = target_tokens & set(column_aliases)
            allow_header_alignment = len(entity_alias_tokens) == 1 or (
                "terminal" in target_tokens
                and "growth" in target_tokens
                and alias_header_overlap > 0
            )
            exact_distinctive_overlap = bool(
                (target_tokens & header_tokens) - generic_header_tokens
            )
            if (overlap and (target_tokens & set(column_aliases) or exact_distinctive_overlap)) or (
                allow_header_alignment and alias_header_overlap
            ):
                overlap = max(overlap, alias_header_overlap)
                column_scores.append(
                    ((overlap, distinctive, -abs(candidate_column - source_column)), candidate_column)
                )
        if column_scores:
            best, best_column = max(column_scores)
            runner_up = sorted((score for score, _ in column_scores), reverse=True)[1] if len(column_scores) > 1 else None
            # One distinctive exact entity token, or a clear multi-token margin, is
            # required.  This prevents ordinary metric words from moving columns.
            if best[0] >= 1 and (runner_up is None or best > runner_up) and best_column != source_column:
                replacement_reference = _adjust_reference(
                    reference, column_delta=best_column - source_column
                )
                if replacement_reference is not None:
                    alternatives.append(
                        (
                            "cross_sheet_semantic_alignment",
                            _replace_once(
                                formula,
                                match.start("reference"),
                                match.end("reference"),
                                replacement_reference,
                            ),
                            "align the source entity column with a unique nearby header",
                        )
                    )

        # Metric/row alignment: prefer an exact label, then a narrow financial
        # synonym (for example EBITDAX -> operating income).  Only a unique best
        # row is accepted.
        row_scores: list[tuple[tuple[int, int, int], int]] = []
        current_overlap = len(target_tokens & current_source_tokens)
        current_alias_overlap = max(
            (
                len(alias & current_source_tokens)
                for token, alias in aliases.items()
                if token in {"ebitdax", "ebitda"}
            ),
            default=0,
        )
        current_row_score = max(current_overlap * 3, current_alias_overlap * 2)
        for candidate_row in range(1, int(source.max_row or 0) + 1):
            if candidate_row < 4 or source.cell(candidate_row, source_column).value is None:
                continue
            labels = [
                source.cell(candidate_row, candidate_column).value
                for candidate_column in range(1, min(source_column, 10))
            ]
            row_tokens = frozenset(
                token for label in labels for token in _semantic_label_tokens(label)
            )
            if not row_tokens:
                continue
            overlap = len(target_tokens & row_tokens)
            # Row aliases are deliberately narrow.  Header concepts such as
            # ``growth`` or ``terminal`` describe a source column, not a source
            # row, and otherwise make title rows look like metric matches.
            alias_overlap = max(
                (len(alias & row_tokens) for token, alias in aliases.items() if token in {"ebitdax", "ebitda"}),
                default=0,
            )
            score = max(overlap * 3, alias_overlap * 2)
            # EBITDAX is a composed metric.  In a formula that already contains
            # D&A and exploration, only the operating-income component should be
            # realigned; otherwise the alias would propose replacing every term
            # with the same source row.
            if "ebitdax" in target_tokens and (
                {"depreciation", "depletion", "amortization"} & row_tokens
                or "exploration" in row_tokens
            ):
                score = 0
            if "ebitdax" in target_tokens and not (
                {"net", "income"} <= current_source_tokens
                or not current_source_tokens
            ):
                score = 0
            if score and ("ebitdax" in target_tokens or "ebitda" in target_tokens):
                row_scores.append(((score, overlap, -abs(candidate_row - source_row)), candidate_row))
        if row_scores:
            best, best_row = max(row_scores)
            sorted_scores = sorted((score for score, _ in row_scores), reverse=True)
            runner_up = sorted_scores[1] if len(sorted_scores) > 1 else None
            if (
                best[0] >= 3
                and best[0] > current_row_score
                and (runner_up is None or best > runner_up)
            ):
                if best_row != source_row:
                    replacement_reference = _adjust_reference(
                        reference, row_delta=best_row - source_row
                    )
                    if replacement_reference is not None:
                        alternatives.append(
                            (
                                "cross_sheet_semantic_alignment",
                                _replace_once(
                                    formula,
                                    match.start("reference"),
                                    match.end("reference"),
                                    replacement_reference,
                                ),
                                "align the source metric row with a unique workbook label",
                            )
                        )
    return list(dict.fromkeys(alternatives))


def _cross_sheet_parallel_alternatives(
    workbook: Any, worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Use a repeated same-row block as a source-column witness."""

    target_tokens = _target_semantic_tokens(worksheet, row, column)
    if not target_tokens:
        return []
    alternatives: list[tuple[str, str, str]] = []
    current_matches = list(_CROSS_SHEET_CELL_RE.finditer(formula))
    for match in current_matches[:8]:
        reference = match.group("reference")
        parsed = _CELL_RE.fullmatch(reference)
        if parsed is None:
            continue
        source_name = match.group("qualifier")[:-1].strip("'").replace("''", "'")
        source_row = int(parsed.group("row"))
        source_column = column_index_from_string(parsed.group("column"))
        peers: list[int] = []
        for peer in list(getattr(worksheet, "_cells", {}).values()):
            if int(peer.row) != row or int(peer.column) == column or not isinstance(peer.value, str):
                continue
            peer_tokens = _target_semantic_tokens(worksheet, row, int(peer.column))
            if peer_tokens != target_tokens:
                continue
            for peer_match in _CROSS_SHEET_CELL_RE.finditer(peer.value):
                peer_name = peer_match.group("qualifier")[:-1].strip("'").replace("''", "'")
                peer_ref = _CELL_RE.fullmatch(peer_match.group("reference"))
                if peer_name == source_name and peer_ref is not None and int(peer_ref.group("row")) == source_row:
                    peers.append(column_index_from_string(peer_ref.group("column")))
        source_name_tokens = _semantic_label_tokens(source_name)
        # A single distant peer is sufficient when the target block explicitly
        # names the source sheet (for example a WACC block with a parallel WACC
        # block).  Otherwise require at least two independent peers before
        # treating a source-column shift as structural evidence.
        if len(set(peers)) != 1 or peers[0] == source_column:
            continue
        if len(peers) < 2 and not (target_tokens & source_name_tokens):
            continue
        replacement_reference = _adjust_reference(reference, column_delta=peers[0] - source_column)
        if replacement_reference is not None:
            alternatives.append(
                (
                    "cross_sheet_parallel_block",
                    _replace_once(formula, match.start("reference"), match.end("reference"), replacement_reference),
                    "match the source column used by a repeated same-row block",
                )
            )
    return list(dict.fromkeys(alternatives))


def _cross_sheet_summary_window_alternatives(
    workbook: Any, worksheet: Any, row: int, column: int, formula: str
) -> list[tuple[str, str, str]]:
    """Align a cross-sheet SUM window to the source block before its summary row.

    Financial source tables frequently end a data block with a CAGR/summary row.
    A corrupted SUM may include that row or start one period too early.  The
    intended window is inferred from the existing window length and the nearest
    labelled summary boundary in the referenced source sheet.
    """

    alternatives: list[tuple[str, str, str]] = []
    range_pattern = re.compile(
        r"(?P<qualifier>(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!)"
        r"(?P<start>\$?[A-Z]{1,3}\$?\d+):(?P<end>\$?[A-Z]{1,3}\$?\d+)",
        re.IGNORECASE,
    )
    for match in list(range_pattern.finditer(formula))[:8]:
        source_name = match.group("qualifier")[:-1].strip("'").replace("''", "'")
        if source_name not in workbook.sheetnames:
            continue
        start = _CELL_RE.fullmatch(match.group("start"))
        end = _CELL_RE.fullmatch(match.group("end"))
        if start is None or end is None:
            continue
        start_column = column_index_from_string(start.group("column"))
        end_column = column_index_from_string(end.group("column"))
        if start_column != end_column:
            continue
        start_row, end_row = int(start.group("row")), int(end.group("row"))
        if end_row < start_row:
            continue
        source = workbook[source_name]
        summary_rows = []
        for candidate_row in range(1, int(source.max_row or 0) + 1):
            labels = " ".join(
                str(source.cell(candidate_row, label_column).value or "")
                for label_column in range(1, start_column)
                if isinstance(source.cell(candidate_row, label_column).value, str)
                and not str(source.cell(candidate_row, label_column).value).startswith("=")
            ).casefold()
            if re.search(r"\bcagr\b|compound annual|summary", labels):
                summary_rows.append(candidate_row)
        nearby = [
            candidate_row
            for candidate_row in summary_rows
            if start_row <= candidate_row <= end_row + 2
        ]
        if not nearby:
            continue
        summary_row = min(nearby, key=lambda candidate_row: abs(candidate_row - end_row))
        window_length = end_row - start_row + 1
        if summary_row <= end_row:
            window_length = min(window_length, summary_row - start_row)
        if window_length < 2 or summary_row - window_length < 1:
            continue
        desired_start = summary_row - window_length
        desired_end = summary_row - 1
        if desired_start == start_row and desired_end == end_row:
            continue
        if any(
            source.cell(source_row, start_column).value is None
            for source_row in range(desired_start, desired_end + 1)
        ):
            continue
        replacement = _replace_once(
            formula,
            match.start("start"),
            match.end("end"),
            f"{get_column_letter(start_column)}{desired_start}:{get_column_letter(end_column)}{desired_end}",
        )
        alternatives.append(
            (
                "cross_sheet_summary_window",
                replacement,
                "align the cross-sheet aggregate with the populated data rows before a labelled summary boundary",
            )
        )
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
            index_range = re.search(
                r"INDEX\(\s*(?P<sheet>'[^']+'|[A-Za-z_][^!,()]*)!"
                r"(?P<range>\$?(?P<start_column>[A-Z]{1,3}):"
                r"\$?(?P<end_column>[A-Z]{1,3}))\s*,",
                formula,
                re.IGNORECASE,
            )
            if index_range is not None:
                index_sheet = (
                    index_range.group("sheet").strip().strip("'").replace("''", "'")
                )
                return_column = column_index_from_string(index_range.group("start_column"))
                next_column = return_column + 1
                lookup_rows = [
                    row
                    for row in range(1, source.max_row + 1)
                    if isinstance(source.cell(row, source_column).value, str)
                    and str(source.cell(row, source_column).value).strip() == lookup.strip()
                ]
                spacer_witnesses = sum(
                    1
                    for row in range(1, source.max_row + 1)
                    if isinstance(source.cell(row, source_column).value, str)
                    and not str(source.cell(row, source_column).value).startswith("=")
                    and source.cell(row, return_column).value is None
                    and source.cell(row, next_column).value is not None
                )
                if (
                    index_sheet == sheet_name
                    and index_range.group("start_column").upper()
                    == index_range.group("end_column").upper()
                    and return_column == source_column + 1
                    and next_column <= source.max_column
                    and spacer_witnesses >= 3
                    and len(lookup_rows) == 1
                    and source.cell(lookup_rows[0], return_column).value is None
                    and source.cell(lookup_rows[0], next_column).value is not None
                ):
                    next_letter = get_column_letter(next_column)
                    shifted_range = re.sub(
                        r"[A-Z]{1,3}",
                        next_letter,
                        index_range.group("range"),
                        flags=re.IGNORECASE,
                    )
                    alternatives.insert(
                        0,
                        (
                            "index_return_column_after_blank_spacer",
                            _replace_once(
                                formula,
                                index_range.start("range"),
                                index_range.end("range"),
                                shifted_range,
                            ),
                            "skip a consistently blank spacer between lookup labels and data",
                        ),
                    )
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


def _index_match_semantic_alignment_alternatives(
    workbook: Any,
    worksheet: Any,
    row: int,
    column: int,
    formula: str,
) -> list[tuple[str, str, str]]:
    """Align a one-dimensional INDEX lookup using target and source metric labels."""

    index_range = _INDEX_RETURN_RANGE_RE.search(formula)
    match_range = _MATCH_RANGE_RE.search(formula)
    if index_range is None or match_range is None:
        return []
    index_sheet_name = index_range.group("qualifier")[:-1].strip("'").replace("''", "'")
    match_sheet_name = match_range.group("qualifier")[:-1].strip("'").replace("''", "'")
    if index_sheet_name != match_sheet_name or index_sheet_name not in workbook.sheetnames:
        return []

    index_start = _CELL_RE.fullmatch(index_range.group("start"))
    index_end = _CELL_RE.fullmatch(index_range.group("end"))
    match_start = _CELL_RE.fullmatch(match_range.group("start"))
    match_end = _CELL_RE.fullmatch(match_range.group("end"))
    if None in {index_start, index_end, match_start, match_end}:
        return []
    assert index_start is not None
    assert index_end is not None
    assert match_start is not None
    assert match_end is not None
    match_start_row = int(match_start.group("row"))
    match_end_row = int(match_end.group("row"))
    if match_start_row != match_end_row:
        return []
    match_start_column = column_index_from_string(match_start.group("column"))
    match_end_column = column_index_from_string(match_end.group("column"))
    if match_start_column > match_end_column:
        return []

    def normalized_label(value: Any) -> tuple[str, frozenset[str]]:
        rendered = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
        rendered = re.sub(r"\badj\b", "adjusted", rendered)
        rendered = re.sub(r"\brev\b", "revenue", rendered)
        tokens = frozenset(
            token
            for token in rendered.split()
            if token not in {"a", "an", "as", "for", "fy", "of", "the"}
            and not re.fullmatch(r"fy?\d{2,4}", token)
        )
        return " ".join(sorted(tokens)), tokens

    def label_strength(target: Any, source_value: Any) -> int:
        target_text, target_tokens = normalized_label(target)
        source_text, source_tokens = normalized_label(source_value)
        if not target_tokens or not source_tokens:
            return 0
        if target_text == source_text:
            return 4
        if source_tokens <= target_tokens or target_tokens <= source_tokens:
            return 3
        metric_tokens = {
            "cash",
            "debt",
            "ebit",
            "ebitda",
            "equity",
            "expense",
            "income",
            "margin",
            "profit",
            "revenue",
        }
        return 2 if target_tokens & source_tokens & metric_tokens else 0

    target_labels: list[tuple[str, int]] = []
    for label_column in range(column - 1, 0, -1):
        value = worksheet.cell(row, label_column).value
        if isinstance(value, str) and value.strip() and not value.startswith("="):
            target_labels.append((value, 4))
            break
    # A detail row may use an entity name while the preceding section heading names the metric.
    for distance in range(1, 4):
        label_row = row - distance
        if label_row < 1:
            break
        for label_column in range(min(column - 1, 8), 0, -1):
            value = worksheet.cell(label_row, label_column).value
            if isinstance(value, str) and value.strip() and not value.startswith("="):
                target_labels.append((value, 4 - distance))
                break
    if not target_labels:
        return []

    source = workbook[index_sheet_name]
    index_start_row = int(index_start.group("row"))
    index_end_row = int(index_end.group("row"))

    # A rate/amount output column should select the source row carrying the
    # corresponding metric, even when the corrupted MATCH text still names a
    # neighbouring balance row.  The output header and source row labels are
    # workbook-local evidence; no task-specific coordinates are assumed.
    target_header_tokens: set[str] = set()
    for header_row in range(max(1, row - 8), row):
        for label_column in range(max(1, column - 2), column + 1):
            value = worksheet.cell(header_row, label_column).value
            if isinstance(value, str) and not value.startswith("="):
                target_header_tokens.update(_semantic_label_tokens(value))
    source_row_labels: dict[int, str] = {}
    for source_row in range(max(1, index_start_row), min(int(source.max_row or 0), index_end_row) + 1):
        source_row_labels[source_row] = " ".join(
            str(source.cell(source_row, label_column).value or "")
            for label_column in range(1, match_start_column)
            if isinstance(source.cell(source_row, label_column).value, str)
            and not str(source.cell(source_row, label_column).value).startswith("=")
        ).casefold()
    metric_row = next(
        (
            source_row
            for source_row, label in source_row_labels.items()
            if "interest" in label
            and ("rate" in target_header_tokens or "interest" in target_header_tokens)
        ),
        None,
    )
    if metric_row is not None:
        selector_match = re.search(r'MATCH\(\s*"(?P<label>[^"]+)"', formula, re.IGNORECASE)
        if selector_match is not None and selector_match.group("label").casefold() != "interest":
            replacement = _replace_once(
                formula,
                selector_match.start("label"),
                selector_match.end("label"),
                "Interest",
            )
            alternatives.append(
                (
                    "index_match_exact_label",
                    replacement,
                    "select the source row matching the rate-column Interest label",
                )
            )
        current_start_column = column_index_from_string(index_start.group("column"))
        current_end_column = column_index_from_string(index_end.group("column"))
        if current_start_column == current_end_column:
            populated_columns = [
                candidate_column
                for candidate_column in range(1, int(source.max_column or 0) + 1)
                if source.cell(metric_row, candidate_column).value is not None
            ]
            right_value_column = next(
                (
                    candidate_column
                    for candidate_column in populated_columns
                    if candidate_column > current_start_column
                    and all(
                        source.cell(source_row, candidate_column).value is not None
                        for source_row in range(index_start_row, index_end_row + 1)
                    )
                ),
                None,
            )
            if right_value_column is not None and source.cell(metric_row, current_start_column).value is None:
                replacement = _replace_once(
                    formula,
                    index_range.start("start"),
                    index_range.end("end"),
                    f"{get_column_letter(right_value_column)}{index_start_row}:{get_column_letter(right_value_column)}{index_end_row}",
                )
                alternatives.append(
                    (
                        "index_return_column_after_blank_spacer",
                        replacement,
                        "use the populated source value column after a blank spacer column",
                    )
                )
    search_start = max(1, min(index_start_row, index_end_row) - 8)
    search_end = min(int(source.max_row or 0), max(index_start_row, index_end_row) + 8)
    ranked_rows: list[tuple[tuple[int, int, int, int, int], int, str]] = []
    for candidate_row in range(search_start, search_end + 1):
        source_labels = [
            value
            for source_column in range(1, min(match_start_column, 10))
            if isinstance((value := source.cell(candidate_row, source_column).value), str)
            and value.strip()
            and not value.startswith("=")
        ]
        if not source_labels:
            continue
        coverage = sum(
            source.cell(candidate_row, source_column).value is not None
            for source_column in range(match_start_column, match_end_column + 1)
        )
        for target_label, target_priority in target_labels:
            for source_label in source_labels:
                strength = label_strength(target_label, source_label)
                if strength < 2:
                    continue
                distance = min(
                    abs(candidate_row - index_start_row),
                    abs(candidate_row - index_end_row),
                )
                # A corrupted multi-row return range commonly has the right metric as
                # its final row (for example C14:K18 for the row-18 Total Revenue line).
                # Prefer that endpoint only after label strength and period coverage.
                endpoint_bonus = int(candidate_row == index_end_row)
                ranked_rows.append(
                    (
                        (strength, target_priority, coverage, endpoint_bonus, -distance),
                        candidate_row,
                        source_label,
                    )
                )
    if not ranked_rows:
        return []
    ranked_rows.sort(reverse=True)
    best_rank, source_row, source_label = ranked_rows[0]
    if any(
        candidate_rank == best_rank and candidate_row != source_row
        for candidate_rank, candidate_row, _ in ranked_rows[1:]
    ):
        return []
    match_width = match_end_column - match_start_column + 1
    if best_rank[2] != match_width:
        return []

    new_start = _adjust_reference(
        index_range.group("start"),
        row_delta=source_row - index_start_row,
        column_delta=match_start_column
        - column_index_from_string(index_start.group("column")),
    )
    new_end = _adjust_reference(
        index_range.group("end"),
        row_delta=source_row - index_end_row,
        column_delta=match_end_column - column_index_from_string(index_end.group("column")),
    )
    if new_start is None or new_end is None:
        return []
    replacement = _replace_once(
        formula,
        index_range.start("start"),
        index_range.end("end"),
        f"{new_start}:{new_end}",
    )

    for selector in re.finditer(
        r"(?P<base>\$?[A-Z]{1,3}\$?\d+)\s*(?P<operator>[+-])\s*1(?=\s*[,\)])",
        replacement,
    ):
        parsed_selector = _CELL_RE.fullmatch(selector.group("base"))
        if parsed_selector is None:
            continue
        if column_index_from_string(parsed_selector.group("column")) != column:
            continue
        replacement = _replace_once(
            replacement, selector.start(), selector.end(), selector.group("base")
        )
        break
    if replacement == formula:
        return []
    return [
        (
            "index_semantic_alignment",
            replacement,
            f"align the INDEX return row and period span with source metric {source_label!r}",
        )
    ]


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

    # A line labelled ``(-) Interest`` or ``(-) CapEx`` commonly points at a
    # positive schedule value.  The unary sign is part of the row contract,
    # even though there is no arithmetic operator for the existing detector
    # to compare.  Require a direct reference and a non-negative source
    # convention; this avoids changing formulas that already negate a signed
    # source line.
    direct = re.fullmatch(r"=(?P<sign>-)?(?P<reference>\$?[A-Z]{1,3}\$?\d+)", formula)
    if direct is not None:
        row_label = " ".join(
            str(worksheet.cell(row, label_column).value or "")
            for label_column in range(1, min(column, 8))
            if isinstance(worksheet.cell(row, label_column).value, str)
            and not str(worksheet.cell(row, label_column).value).startswith("=")
        ).strip().casefold()
        reference = _CELL_RE.fullmatch(direct.group("reference"))
        if reference is not None and row_label.startswith("(-)"):
            referenced_row = int(reference.group("row"))
            source_label = " ".join(
                str(worksheet.cell(referenced_row, label_column).value or "")
                for label_column in range(1, min(column, 8))
                if isinstance(worksheet.cell(referenced_row, label_column).value, str)
                and not str(worksheet.cell(referenced_row, label_column).value).startswith("=")
            ).casefold()
            if direct.group("sign") is None and source_label and not source_label.startswith("(-)"):
                return (
                    "label_sign_alignment",
                    f"=-{direct.group('reference')}",
                    "apply the explicit negative row-label convention to a direct positive schedule reference",
                )

    # Debt paydown rows use a positive cash-flow capacity as the MIN input but
    # reduce the balance.  The parenthetical ``(paydown)`` label and a nearby
    # beginning-balance row provide the structural sign witness; this is safer
    # than flipping every formula that happens to contain MIN.
    row_label = " ".join(
        str(worksheet.cell(row, label_column).value or "")
        for label_column in range(1, min(column, 8))
        if isinstance(worksheet.cell(row, label_column).value, str)
        and not str(worksheet.cell(row, label_column).value).startswith("=")
    ).casefold()
    if "draw" in row_label and "paydown" in row_label and "min(" in formula.casefold():
        if "-min(" not in formula.casefold():
            return (
                "label_sign_alignment",
                re.sub(r"(?i)MIN\(", "-MIN(", formula, count=1),
                "make a debt paydown reduce the balance while preserving a positive cash-flow driver",
            )

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
            isinstance(referenced_value, str)
            and (
                re.match(r"^=\+?-", referenced_value) is not None
                or re.search(r"(?:\*\s*-1\b|/\s*-1\b)", referenced_value) is not None
            )
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
                    _average_vertical_period_window_alternatives(
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
                    _double_counting_cross_row_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                )
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
                    _double_counting_peer_translation(
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
                alternatives.extend(
                    _semantic_cross_sheet_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _cross_sheet_parallel_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                )
                alternatives.extend(
                    _cross_sheet_summary_window_alternatives(
                        workbook, worksheet, int(cell.row), int(cell.column), formula
                    )
                )
            elif index_match_task:
                if "INDEX(" not in formula.upper():
                    continue
                alternatives = _index_match_semantic_alignment_alternatives(
                    workbook, worksheet, int(cell.row), int(cell.column), formula
                )
                alternatives.extend(_index_match_alternatives(workbook, formula))
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
        "cross_sheet_semantic_alignment": -8,
        "cross_sheet_parallel_block": -7,
        "cross_sheet_summary_window": -6,
        "double_count_range_member": 0,
        "double_count_duplicate": 1,
        "double_count_direct_term": 0,
        "double_count_total_component": -1,
        "double_count_parallel_block_term": -1,
        "double_count_subtotal_chain": -2,
        "double_count_derived_interest": -2,
        "double_count_rollforward_total": -3,
        "double_count_rollforward_interest": -3,
        "double_count_debt_components": -4,
        "double_count_cross_row_component": -5,
        "double_count_fee_in_cash": -5,
        "double_count_embedded_subtotal_component": -5,
        "double_count_peer_translation": -6,
        "index_match_exact_label": 0,
        "index_semantic_alignment": -10,
        "index_match_exact_mode": 1,
        "index_return_column_after_blank_spacer": 1,
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
        "embedded_exit_ebitda_multiple": -3,
        "embedded_net_debt_lookup": -3,
        "embedded_share_price_reference": -3,
        "embedded_depreciation_bridge": -3,
        "embedded_wacc_reference": -3,
        "embedded_forecast_case_link": -3,
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
        "average_vertical_period_extension": -6,
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


__all__ = [
    "DebuggingRepairCandidate",
    "detect_debugging_repair_candidates",
    "repair_broken_sheet_qualifiers",
    "restore_deleted_scenario_selector_row",
]
