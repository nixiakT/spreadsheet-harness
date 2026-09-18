#!/usr/bin/env python3
"""Evaluate Trace2Skill V2 cell-based splits with the official comparator.

Visualization is inventoried but deliberately left ``not_scored`` because its
official protocol requires Windows Excel/WPS COM rendering plus a VLM judge.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR_EVAL = (
    ROOT
    / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
)


def load_official_module():
    spec = importlib.util.spec_from_file_location("spreadsheetbench_v2_official", VENDOR_EVAL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load official evaluator: {VENDOR_EVAL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_one(payload: tuple[str, dict, str, str]) -> dict:
    category, item, category_root_text, outputs_text = payload
    official = load_official_module()
    category_root = Path(category_root_text)
    outputs = Path(outputs_text)
    task_id = f"{category}/{item['id']}"
    input_file = category_root / item["spreadsheet_path"]
    golden_file = category_root / item["golden_response_path"]
    output_file = outputs / task_id.replace("/", "_") / "initial_output.xlsx"
    with_font_color = category == "Debugging" and "Color" in item["spreadsheet_path"]
    with_formula = category == "Debugging" and "Embedded" in item["spreadsheet_path"]

    result, message, correct, total, regression, modification = (
        official.compare_workbooks_with_regression(
            str(input_file),
            str(golden_file),
            str(output_file),
            item["answer_position"],
            with_font_color=with_font_color,
            with_formula=with_formula,
        )
    )
    missing = not output_file.is_file()
    reg_ratio = regression["correct"] / regression["total"] if regression["total"] else 0.0
    mod_ratio = modification["correct"] / modification["total"] if modification["total"] else 0.0
    cell_ratio = correct / total if total else 0.0
    normalized_regression = 1.0 if round(reg_ratio, 4) >= 0.998 else round(reg_ratio, 4)
    normalized_modification = round(mod_ratio, 4)
    exact = float(normalized_regression == 1.0 and normalized_modification == 1.0)
    return {
        "id": task_id,
        "category": category,
        "output_file": str(output_file),
        "missing": missing,
        "error_message": "" if result else message,
        "regression_accuracy": normalized_regression,
        "modification_accuracy": normalized_modification,
        "cell_accuracy": cell_ratio,
        "accuracy": exact,
        "correct_cells": correct,
        "total_cells": total,
        "regression": regression,
        "modification": modification,
    }


def evaluate(data_root: Path, outputs: Path, workers: int = 1) -> dict:
    rows: list[dict] = []
    visualization_rows: list[dict] = []
    categories = ("Debugging", "Financial_Model", "Template", "Visualization")
    payloads: list[tuple[str, dict, str, str]] = []

    for category in categories:
        category_root = data_root / category
        dataset = json.loads((category_root / "dataset.json").read_text(encoding="utf-8"))
        for item in dataset:
            task_id = f"{category}/{item['id']}"
            if category == "Visualization":
                output_file = outputs / task_id.replace("/", "_") / "initial_output.xlsx"
                exists = output_file.is_file()
                valid_xlsx = exists and zipfile.is_zipfile(output_file)
                visualization_rows.append(
                    {
                        "id": task_id,
                        "category": category,
                        "output_file": str(output_file),
                        "output_exists": exists,
                        "output_valid": valid_xlsx,
                        "status": "not_scored",
                        "official_score": None,
                        "reason": (
                            "official Visualization evaluation requires Windows "
                            "Excel/WPS COM rendering plus the glm-4.6v VLM judge"
                        ),
                    }
                )
                continue
            if not item.get("answer_position"):
                raise ValueError(f"non-Visualization task lacks answer_position: {task_id}")
            payloads.append((category, item, str(category_root), str(outputs)))

    if workers <= 1:
        for index, payload in enumerate(payloads, 1):
            rows.append(evaluate_one(payload))
            if index % 10 == 0 or index == len(payloads):
                print(f"evaluated {index}/{len(payloads)}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(evaluate_one, payload): payload[1]["id"] for payload in payloads}
            for index, future in enumerate(as_completed(futures), 1):
                rows.append(future.result())
                if index % 10 == 0 or index == len(payloads):
                    print(f"evaluated {index}/{len(payloads)}", flush=True)
        rows.sort(key=lambda row: row["id"])

    present = [row for row in rows if not row["missing"]]
    by_category: dict[str, dict] = {}
    for category in ("Debugging", "Financial_Model", "Template"):
        subset = [row for row in rows if row["category"] == category]
        available = [row for row in subset if not row["missing"]]
        by_category[category] = {
            "tasks": len(subset),
            "outputs": len(available),
            "missing_outputs": len(subset) - len(available),
            "exact_tasks": int(sum(row["accuracy"] for row in subset)),
            "accuracy": mean([row["accuracy"] for row in subset]),
            "regression_accuracy": mean([row["regression_accuracy"] for row in available]),
            "modification_accuracy": mean([row["modification_accuracy"] for row in available]),
            "cell_accuracy": mean([row["cell_accuracy"] for row in subset]),
        }
    visual_valid = sum(row["output_valid"] for row in visualization_rows)
    visual_present = sum(row["output_exists"] for row in visualization_rows)
    by_category["Visualization"] = {
        "tasks": len(visualization_rows),
        "outputs": visual_present,
        "valid_xlsx_outputs": visual_valid,
        "missing_outputs": len(visualization_rows) - visual_present,
        "invalid_xlsx_outputs": visual_present - visual_valid,
        "accuracy": None,
        "evaluation_state": "pending_official_windows_visual_evaluation",
        "official_protocol": "Windows Excel/WPS COM + glm-4.6v; pass if score > 0.7",
    }

    summary = {
        "benchmark_tasks": 321,
        "spreadsheet_tasks_scored": len(rows),
        "visualization_tasks_unscored": len(visualization_rows),
        "outputs": len(present),
        "missing_outputs": len(rows) - len(present),
        "exact_tasks": int(sum(row["accuracy"] for row in rows)),
        "accuracy": mean([row["accuracy"] for row in rows]),
        "regression_accuracy": mean([row["regression_accuracy"] for row in present]),
        "modification_accuracy": mean([row["modification_accuracy"] for row in present]),
        "cell_accuracy": mean([row["cell_accuracy"] for row in rows]),
        "micro_cell_accuracy": (
            sum(row["correct_cells"] for row in rows)
            / sum(row["total_cells"] for row in rows)
            if sum(row["total_cells"] for row in rows)
            else 0.0
        ),
        "by_category": by_category,
    }
    return {
        "summary": summary,
        "visualization_results": visualization_rows,
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    result = evaluate(args.data_root.resolve(), args.outputs.resolve(), args.workers)
    args.results_file.parent.mkdir(parents=True, exist_ok=True)
    args.results_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
