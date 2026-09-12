#!/usr/bin/env python3
"""Audit the four-paper SpreadsheetBench-v2 reproduction state.

This is deliberately read-only: it checks the fixed manifest, report rows,
evaluator hashes, and method identity labels without mutating benchmark data.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "benchmarks/results/paper_reproduction_v2_30_summary_20260905.json"
MANIFEST = ROOT / "benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json"
PINNED_EVAL = "04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0"
REQUIRED = {
    "spreadsheet_rl_thinking_official_checkpoint",
    "spreadsheetagent_paper_vision_clean_room_proxy",
    "sheetcompass_graph_memory_clean_room_proxy",
    "trace2skill_coderplus_model_substitution_supplement",
}


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    summary = json.loads(SUMMARY.read_text())
    expected = {str(r["task_id"]) for r in manifest["tasks"]}
    assert len(expected) == 30, f"expected split has {len(expected)} cases"
    assert len({r["task_id"] for r in manifest["tasks"] if r["category"] == "Debugging"}) == 10
    assert len({r["task_id"] for r in manifest["tasks"] if r["category"] == "Financial_Model"}) == 10
    assert len({r["task_id"] for r in manifest["tasks"] if r["category"] == "Template"}) == 10
    methods = summary.get("methods", {})
    missing_methods = REQUIRED - set(methods)
    assert not missing_methods, f"missing methods: {sorted(missing_methods)}"
    print(f"manifest: {len(expected)} cases (10/10/10), revision={summary.get('manifest')}")
    for name in sorted(REQUIRED):
        state = methods[name]
        ids = {str(r.get("task_id")) for r in state.get("rows", [])}
        missing = sorted(expected - ids)
        bad_hashes = sorted({str(r.get("official_evaluator_sha256")) for r in state.get("rows", []) if r.get("official_evaluator_sha256") and r.get("official_evaluator_sha256") != PINNED_EVAL})
        print(f"{name}: unique={state.get('unique_cases')} completed={state.get('completed')} errors={state.get('errors_or_incomplete')} missing={len(missing)} evaluator_hash_mismatches={len(bad_hashes)}")
        if missing:
            print("  missing IDs:", ", ".join(missing))
        if bad_hashes:
            print("  unexpected hashes:", ", ".join(bad_hashes))
    print("identity: Spreadsheet-RL=official-checkpoint/Linux-proxy; SpreadsheetAgent=clean-room proxy; SheetCompass=clean-room proxy; Trace2Skill=model substitution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
