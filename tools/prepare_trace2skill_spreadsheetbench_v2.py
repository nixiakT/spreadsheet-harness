#!/usr/bin/env python3
"""Build a Trace2Skill-compatible 30-case view of SpreadsheetBench-v2.

The public Trace2Skill runner expects one instance directory containing the
input workbook(s).  SpreadsheetBench-v2 stores the same workbooks in category
trees and identifies each case in a JSON array, so this creates a lightweight
symlinked staging tree while preserving the original benchmark files.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("benchmarks/data/spreadsheetbench-v2"))
    ap.add_argument("--manifest", type=Path, default=Path("benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json"))
    ap.add_argument("--output", type=Path, default=Path("tmp/trace2skill_spreadsheetbench_v2_30"))
    args = ap.parse_args()
    dataset = args.dataset.resolve()
    manifest = json.loads(args.manifest.read_text())
    selected = [x["task_id"] for x in manifest["tasks"]]

    by_id = {}
    for category in ("Debugging", "Financial_Model", "Template"):
        for row in json.loads((dataset / category / "dataset.json").read_text()):
            by_id[f"{category}/{row['id']}"] = (category, row)

    root = args.output.resolve()
    (root / "spreadsheet").mkdir(parents=True, exist_ok=True)
    records = []
    for task_id in selected:
        if task_id not in by_id:
            raise SystemExit(f"Task not found in SpreadsheetBench-v2: {task_id}")
        category, row = by_id[task_id]
        slug = task_id.replace("/", "_")
        src = dataset / category / row["spreadsheet_path"]
        if not src.is_file():
            raise SystemExit(f"Input workbook not found: {src}")
        case_dir = root / "spreadsheet" / slug
        case_dir.mkdir(parents=True, exist_ok=True)
        # Use the verified runner's simple naming convention so each staged
        # instance has exactly one test case and the official-compatible
        # evaluator looks for initial_output.xlsx against golden.xlsx.
        dst = case_dir / "initial.xlsx"
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)
        golden_src = dataset / category / row["golden_response_path"]
        if not golden_src.is_file():
            raise SystemExit(f"Golden workbook not found: {golden_src}")
        golden_dst = case_dir / "golden.xlsx"
        if golden_dst.exists() or golden_dst.is_symlink():
            golden_dst.unlink()
        golden_dst.symlink_to(golden_src)
        rec = dict(row)
        rec["id"] = task_id
        rec["spreadsheet_path"] = slug
        rec["golden_response_path"] = f"spreadsheet/{slug}/golden.xlsx"
        rec["trace2skill_source_category"] = category
        records.append(rec)

    (root / "dataset.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
    metadata = {
        "source_dataset": str(dataset),
        "source_manifest": str(args.manifest.resolve()),
        "task_count": len(records),
        "task_ids": selected,
        "note": "Symlinked staging view for the public Trace2Skill runner; source workbooks are unchanged.",
    }
    (root / "STAGING_METADATA.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    print(f"Prepared {len(records)} cases at {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
