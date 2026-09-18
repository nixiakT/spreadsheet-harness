"""Kernel-level contracts for durable workbook mutations.

This module intentionally knows nothing about a planner, financial model, or plugin.  It only
answers the execution question: did a declared mutation survive persistence and retain its
action-specific post-condition after reopening the artifact?
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openpyxl.utils import column_index_from_string, range_boundaries

from .openpyxl_compat import load_workbook


class PlannerActionResult(int):
    """Int-compatible proposed/executed/verified result for a planner mutation batch."""

    def __new__(
        cls,
        verified_count: int = 0,
        *,
        proposed: Sequence[Mapping[str, Any]] = (),
        executed: Sequence[Mapping[str, Any]] = (),
        verified: Sequence[Mapping[str, Any]] = (),
        failures: Sequence[str] = (),
    ) -> PlannerActionResult:
        return int.__new__(cls, int(verified_count))

    def __init__(
        self,
        verified_count: int = 0,
        *,
        proposed: Sequence[Mapping[str, Any]] = (),
        executed: Sequence[Mapping[str, Any]] = (),
        verified: Sequence[Mapping[str, Any]] = (),
        failures: Sequence[str] = (),
    ) -> None:
        self.proposed = [dict(action) for action in proposed]
        self.executed = [dict(action) for action in executed]
        self.verified = [dict(action) for action in verified]
        self.failures = [str(reason) for reason in failures]

    @property
    def fast_path_eligible(self) -> bool:
        return bool(self.executed) and not self.failures and len(self.verified) == len(self.executed)

    @property
    def status(self) -> str:
        if self.fast_path_eligible:
            return "verified"
        if self.executed:
            return "executed"
        return "proposed" if self.proposed else "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fast_path_eligible": self.fast_path_eligible,
            "proposed": self.proposed,
            "executed": self.executed,
            "verified": self.verified,
            "verification_failures": self.failures,
        }


def _planner_result_can_bypass(result: Any) -> bool:
    """Read the contract while tolerating legacy integer test doubles."""

    eligible = getattr(result, "fast_path_eligible", None)
    return bool(result) if eligible is None else bool(eligible)


def cell_value(value: Any) -> Any:
    """Return a stable, JSON-friendly cell representation."""

    if hasattr(value, "text"):
        return {
            "type": type(value).__name__,
            "text": str(getattr(value, "text", "")),
            "ref": str(getattr(value, "ref", "")),
        }
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def workbook_snapshot(workbook: Any) -> dict[str, Any]:
    """Capture values, sheet inventory, and merged ranges for mutation verification."""

    snapshot: dict[str, Any] = {"sheets": list(workbook.sheetnames), "cells": {}}
    cells: dict[str, Any] = snapshot["cells"]
    for sheet in workbook.worksheets:
        prefix = f"{sheet.title}!"
        for coordinate, cell in getattr(sheet, "_cells", {}).items():
            value = cell_value(getattr(cell, "value", None))
            if value is not None:
                cells[f"{prefix}{getattr(cell, 'coordinate', coordinate)}"] = value
        cells[f"{prefix}__merged__"] = sorted(str(item) for item in sheet.merged_cells.ranges)
    return snapshot


def _target_snapshot(snapshot: Mapping[str, Any], sheet_name: str, target: str) -> dict[str, Any]:
    cells = snapshot.get("cells")
    if not isinstance(cells, Mapping):
        return {}
    row_range = re.fullmatch(r"(?P<start>\d+):(?P<end>\d+)", target)
    column_range = re.fullmatch(r"(?P<start>[A-Z]+):(?P<end>[A-Z]+)", target, re.I)
    if row_range:
        bounds = (1, int(row_range.group("start")), 16_384, int(row_range.group("end")))
    elif column_range:
        bounds = (
            column_index_from_string(column_range.group("start")),
            1,
            column_index_from_string(column_range.group("end")),
            1_048_576,
        )
    else:
        try:
            bounds = range_boundaries(target)
        except (TypeError, ValueError):
            return {}
    min_col, min_row, max_col, max_row = bounds
    prefix = f"{sheet_name}!"
    selected: dict[str, Any] = {}
    for key, value in cells.items():
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        match = re.fullmatch(r"([A-Z]+)(\d+)", key[len(prefix) :], re.I)
        if match is None:
            continue
        column = column_index_from_string(match.group(1))
        row = int(match.group(2))
        if min_col <= column <= max_col and min_row <= row <= max_row:
            selected[match.group(1).upper() + str(row)] = value
    return selected


def verify_persisted_mutations(
    workbook_path: str | Path,
    *,
    before: Mapping[str, Any],
    expected_workbook: Any,
    actions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Reopen *workbook_path* and verify every action's durable post-condition."""

    verified: list[dict[str, Any]] = []
    failures: list[str] = []
    persisted = None
    try:
        path = Path(workbook_path)
        persisted = load_workbook(path, data_only=False, keep_vba=path.suffix.casefold() == ".xlsm")
        after = workbook_snapshot(persisted)
        expected = workbook_snapshot(expected_workbook)
        if after == before:
            failures.append("workbook snapshot unchanged after planner execution")
        for action in actions:
            sheet_name = action.get("sheet")
            target = action.get("target")
            if not isinstance(sheet_name, str) or not isinstance(target, str):
                if after != before:
                    verified.append(dict(action))
                continue
            if sheet_name not in persisted.sheetnames:
                failures.append(f"target sheet missing after reload: {sheet_name}")
                continue
            previous = _target_snapshot(before, sheet_name, target)
            expected_values = _target_snapshot(expected, sheet_name, target)
            actual = _target_snapshot(after, sheet_name, target)
            # Explicit cell writes carry a precise expected value, avoiding a broad-range
            # snapshot accidentally validating an unrelated mutation.
            if "expected_value" in action:
                previous = {target: action.get("before_value")}
                expected_values = {target: action.get("expected_value")}
                actual = {target: cell_value(persisted[sheet_name][target].value)}
            if expected_values == previous:
                failures.append(
                    f"no mutation at {sheet_name}!{target}: persisted value equals the pre-execution value"
                )
            elif actual != expected_values:
                failures.append(
                    f"post-condition mismatch at {sheet_name}!{target}: "
                    f"expected {expected_values!r}, got {actual!r}"
                )
            else:
                verified.append(dict(action))
    except Exception as exc:  # persistence failures must route to the executor
        failures.append(f"planner artifact verification failed: {type(exc).__name__}: {exc}")
    finally:
        if persisted is not None:
            persisted.close()
    return verified, failures
