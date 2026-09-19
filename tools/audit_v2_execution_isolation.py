"""Offline differential check: V2 requests/artifacts against a pinned git arms.py."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest
from openpyxl import load_workbook

from spreadsheet_harness import arms
from spreadsheet_harness.session import WorkbookSession

ROOT = Path(__file__).resolve().parents[1]


def load_test_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def exercise(module, test_helpers, workbook: Path, output: Path, arm: str,
             category: str | None, *, candidate_mode: str | None = None) -> dict:
    fake = test_helpers.FakeAgent
    fake.calls, fake.outputs, fake.stage_outputs, fake.stage_traces, fake.mutate_stages = [], [], {}, {}, set()
    session = WorkbookSession.create(workbook, output)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "SpreadsheetAgent", fake)
        patch.setattr(module, "SpreadsheetToolRegistry", test_helpers.FakeTools)
        extra = {"v1_execution_mode": candidate_mode} if candidate_mode is not None else {}
        result = module.run_arm(
            arm, test_helpers._config(), session, None, "Calculate totals in Sales!D2:D3.",
            8192, 300, object(), max_turns_per_arm=50, task_category=category, **extra,
        )
    request_fields = (
        "stage", "prompt", "base_instructions", "max_turns", "max_output_tokens",
        "forced_tool_prefix", "require_workbook_change", "allow_unchanged_terminal",
        "require_formula_runtime_validation", "required_tool_termination",
        "max_read_only_code_calls_before_edit", "recover_output_limit",
    )
    requests = []
    for call in fake.calls:
        item = {key: call.get(key) for key in request_fields}
        item["tools"] = sorted(call["tools"].allowed_tools or ())
        item["skills"] = [s.name for s in call["skills"].discover()] if call.get("skills") else []
        requests.append(item)
    book = load_workbook(session.workbook_path)
    try:
        cells = {ws.title: [(c.coordinate, str(c.value), c.number_format)
                           for row in ws for c in row if c.value is not None] for ws in book}
    finally:
        book.close()
    return {"requests": requests, "cells": cells, "stages": [s["name"] for s in result.stages]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", default="HEAD")
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", args.reference], cwd=ROOT, text=True).strip()
    source = subprocess.check_output(["git", "show", f"{revision}:src/spreadsheet_harness/arms.py"], cwd=ROOT)
    name = "spreadsheet_harness._v2_isolation_reference"
    reference = types.ModuleType(name)
    reference.__file__ = str(ROOT / "src/spreadsheet_harness/arms.py")
    sys.modules[name] = reference
    exec(compile(source, reference.__file__, "exec"), reference.__dict__)
    helpers = load_test_module("_v2_isolation_test_helpers", ROOT / "tests/test_arms.py")
    fixtures = load_test_module("_v2_isolation_test_fixtures", ROOT / "tests/conftest.py")
    rows = []
    with tempfile.TemporaryDirectory(prefix="v2-isolation-") as temp:
        root = Path(temp)
        workbook = fixtures.sample_workbook.__wrapped__(root)
        for arm in ("bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial"):
            for category in ("Template", "Financial_Model", "Debugging", "Visualization"):
                old = exercise(reference, helpers, workbook, root / f"old-{arm}-{category}", arm, category)
                new = exercise(arms, helpers, workbook, root / f"new-{arm}-{category}", arm, category)
                rows.append({"arm": arm, "category": category, "equal": old == new,
                             "stages": new["stages"],
                             "changed_fields": [k for k in old if old[k] != new[k]]})
        old_bare = exercise(reference, helpers, workbook, root / "old-bare-v1", "bare", None)
        new_bare = exercise(arms, helpers, workbook, root / "new-bare-v1", "bare", None, candidate_mode="direct")
        rows.append({"arm": "bare", "category": "V1 direct mode baseline control",
                     "equal": old_bare == new_bare, "stages": new_bare["stages"],
                     "changed_fields": [k for k in old_bare if old_bare[k] != new_bare[k]]})
    report = {"reference_commit": revision, "reference_arms_sha256": hashlib.sha256(source).hexdigest(),
              "current_arms_sha256": hashlib.sha256(Path(arms.__file__).read_bytes()).hexdigest(),
              "all_equal": all(r["equal"] for r in rows), "checks": rows,
              "scope": "Synthetic workbook + mocked model; exact request and cell-state comparison, not benchmark accuracy."}
    print(json.dumps(report, indent=2))
    if not report["all_equal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
