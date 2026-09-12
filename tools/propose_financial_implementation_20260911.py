#!/usr/bin/env python3
"""Deterministic implementation proposal for the continuous plugin controller."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    request_path, response_path = Path(sys.argv[1]), Path(sys.argv[2])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    route = request["route"]
    source_path = (
        Path(__file__).resolve().parents[1]
        / "src/spreadsheet_harness/financial_model_repairs.py"
    )
    old_source = source_path.read_text(encoding="utf-8")
    new_source = old_source.replace(
        '    r"=SUM\\(\\$?(?P<column>[A-Z]{1,3})\\$?(?P<start>\\d+):"',
        '    r"=\\+?SUM\\(\\$?(?P<column>[A-Z]{1,3})\\$?(?P<start>\\d+):"',
    ).replace(
        '    r"=SUMIF\\([^,]+,[^,]+,\\$?(?P<start>[A-Z]{1,3})\\$?(?P<row>\\d+):"',
        '    r"=\\+?SUMIF\\([^,]+,[^,]+,\\$?(?P<start>[A-Z]{1,3})\\$?(?P<row>\\d+):"',
    )
    import difflib

    patch = "diff --git a/src/spreadsheet_harness/financial_model_repairs.py b/src/spreadsheet_harness/financial_model_repairs.py\n"
    patch += "".join(
        difflib.unified_diff(
            old_source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile="a/src/spreadsheet_harness/financial_model_repairs.py",
            tofile="b/src/spreadsheet_harness/financial_model_repairs.py",
            n=3,
        )
    )
    document = {
        "candidates": [
            {
                "candidate_id": "financial-runtime-plus-formulas-r1",
                "base_revision_sha256": request["base_revision_sha256"],
                "operation": "edit",
                "target_plugin": route["target_plugin"],
                "surface": route["surface"],
                "operator": "unified-diff",
                "patch": patch,
            }
        ]
    }
    response_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
