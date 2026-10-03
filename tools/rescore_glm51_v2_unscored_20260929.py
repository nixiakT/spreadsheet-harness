from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "benchmarks/results/v2-glm51-public-20260928/glm-5.1"
DATA = ROOT / "benchmarks/data/spreadsheetbench-v2"
EVAL = ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"


def load_eval():
    spec = importlib.util.spec_from_file_location("sb2_eval", EVAL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def score(mod, task, workbook: Path):
    if not workbook.is_file():
        return None, "missing_output"
    source = DATA / task["category"]
    input_path = source / task["spreadsheet_path"]
    if not input_path.is_file() or workbook.read_bytes() == input_path.read_bytes():
        return None, "missing_or_unchanged_output"
    with tempfile.TemporaryDirectory(prefix="glm51-v2-rescore-") as td:
        td = Path(td)
        staged = td / f"{task['id']}_output.xlsx"
        shutil.copy2(workbook, staged)
        result, missing = mod.process_single_item(task, str(source), str(td), {}, task["category"])
        return (result if not missing else None), ("scored" if not missing else "missing")


def main():
    mod = load_eval()
    tasks = {}
    for cat in ("Template", "Financial_Model", "Debugging"):
        for row in json.loads((DATA / cat / "dataset.json").read_text()):
            tasks[f"{cat}/{row['id']}"] = dict(row, category=cat)
    reports = []
    for arm in ("bare", "spreadsheet-harness-basic", "spreadsheet-harness-financial"):
        for cat in ("Template", "Financial_Model", "Debugging"):
            root = RUN / arm / cat
            rows = []
            for task_dir in sorted(root.iterdir()):
                runs = sorted(task_dir.glob("run*/results.json"), key=lambda p: p.stat().st_mtime)
                if not runs:
                    continue
                payload = json.loads(runs[-1].read_text())
                row = payload[0] if isinstance(payload, list) else payload
                if row.get("outcome_kind") in {"scored", "scored_after_provider_failure"} and isinstance((row.get("official_score") or {}).get("accuracy"), (int, float)):
                    continue
                tid = f"{cat}/{task_dir.name}"
                workbook = Path(row.get("output_workbook", ""))
                if not workbook.is_file():
                    candidates = sorted(task_dir.glob("run*/runs/*/*/*/artifacts/output.xlsx"), key=lambda p: p.stat().st_mtime)
                    workbook = candidates[-1] if candidates else Path("/nonexistent")
                score_result, status = score(mod, tasks[tid], workbook)
                rows.append({"task_id": tid, "workbook": str(workbook), "status": status, "official_score": score_result})
            out = RUN / f"rescore-{arm}-{cat}-20260929.json"
            out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
            reports.append({"arm": arm, "category": cat, "total": len(rows), "scored": sum(r["status"] == "scored" for r in rows), "rerun": [r["task_id"] for r in rows if r["status"] != "scored"]})
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
