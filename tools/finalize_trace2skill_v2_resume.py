#!/usr/bin/env python3
"""Build a 321-task execution-status manifest from resumed Trace2Skill runs."""
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    dataset = json.loads((args.dataset / "dataset.json").read_text(encoding="utf-8"))
    experiment = args.experiment.resolve()
    latest: dict[str, dict] = {}
    sources = sorted(experiment.glob("runner_results*.json"), key=lambda p: p.stat().st_mtime)
    for source in sources:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for result in payload.get("results", []):
            latest[str(result.get("id"))] = result

    results = []
    for item in dataset:
        task_id = str(item["id"])
        output = experiment / "outputs" / str(item["spreadsheet_path"]) / "initial_output.xlsx"
        exists = output.is_file()
        valid = exists and zipfile.is_zipfile(output)
        prior = latest.get(task_id, {})
        error = ""
        if not exists:
            error = prior.get("error") or "Output file was not created"
        elif not valid:
            error = "Output workbook is not a valid ZIP/XLSX file"
        results.append(
            {
                "id": task_id,
                "category": item.get("trace2skill_source_category"),
                "success": valid,
                "output_file": str(output),
                "output_exists": exists,
                "output_valid": valid,
                "error": error,
                "last_runner_result": prior,
            }
        )

    successful = sum(row["success"] for row in results)
    payload = {
        "model": args.model,
        "timestamp": datetime.now().isoformat(),
        "total_instances": len(results),
        "successful_instances": successful,
        "failed_instances": len(results) - successful,
        "success_rate": successful / len(results),
        "runner_sources": [str(p) for p in sources],
        "results": results,
    }
    args.results_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("total_instances", "successful_instances", "failed_instances", "success_rate")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
