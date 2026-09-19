#!/usr/bin/env python3
"""Build the relaxed Fin-1.5K/V2 development selection artifacts.

The source benchmark outputs are immutable.  This utility filters the already
paired official-evaluator report to the unsealed V2 development slice and
recomputes the decision with the explicit aggregate-mean promotion policy.
Held-out tasks are retained only as sealed identifiers and are never read.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from spreadsheet_harness.continuous_evolution import (
    ContinuousEvolutionConfig,
    _sha256_json,
    evaluate_validation_report,
)


REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = (
    REPO
    / "benchmarks/results/method-operator-ablation-v3-qwen36plus-glm52-20260918"
)
SOURCE_CONFIG = SOURCE_ROOT / "configs/revision-recomposition.json"
SOURCE_CANDIDATE = (
    SOURCE_ROOT
    / "workspaces/revision-recomposition/candidates/r000001-r001-replace"
)
SOURCE_REPORT = SOURCE_CANDIDATE / "validation-report.json"
OUTPUT_ROOT = REPO / "benchmarks/results/fin15k-v2-relaxed-selection-20260918"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_v2_task(task_id: str) -> bool:
    return any(marker in task_id for marker in ("/fina_Fina_eus_", "/fina_Fina_she_", "/fina_Fina_spr_"))


def filtered_config() -> ContinuousEvolutionConfig:
    document = read_json(SOURCE_CONFIG)
    contexts: list[dict[str, Any]] = []
    for raw in document["contexts"]:
        keep = [task_id for task_id in raw["task_ids"] if is_v2_task(task_id)]
        family_by_task = dict(zip(raw["task_ids"], raw["workbook_families"], strict=True))
        item = json.loads(json.dumps(raw))
        item["task_ids"] = keep
        item["workbook_families"] = [family_by_task[task_id] for task_id in keep]
        item["runner"]["task_datasets"] = {
            task_id: dataset
            for task_id, dataset in raw["runner"]["task_datasets"].items()
            if task_id in keep
        }
        contexts.append(item)
    document["contexts"] = contexts
    declared = {task_id for item in contexts for task_id in item["task_ids"]}
    document["initial_evidence"] = [
        item for item in document["initial_evidence"] if item["task_id"] in declared
    ]
    document["promotion"] = {
        "bootstrap_samples": 4000,
        "confidence": 0.9,
        "delta": 1e-6,
        "epsilon": 0.0,
        "gate_mode": "aggregate-mean",
        "min_pair_coverage": 0.6,
        "min_pairs_per_context": 2,
        "min_total_pairs": 3,
        "replay_delta": 0.0,
        "transfer_delta": 0.0,
    }
    return ContinuousEvolutionConfig.from_document(document)


def filtered_report(config: ContinuousEvolutionConfig) -> dict[str, Any]:
    source = read_json(SOURCE_REPORT)
    by_name = {item["name"]: item for item in source["contexts"]}
    contexts: list[dict[str, Any]] = []
    declared: set[str] = set()
    for context in config.contexts:
        declared.update(context.task_ids)
        source_context = by_name[context.name]
        pairs = [pair for pair in source_context["pairs"] if pair["id"] in context.task_ids]
        hard_failures = [
            task_id
            for task_id in source_context.get("hard_failures", [])
            if task_id in context.task_ids
        ]
        contexts.append(
            {
                "name": context.name,
                "kind": context.kind,
                "task_set_sha256": context.task_set_sha256,
                "pairs": pairs,
                "hard_failures": hard_failures,
            }
        )
    return {
        "schema_version": "continuous-plugin-validation-report-v1",
        "incumbent_revision_sha256": source["incumbent_revision_sha256"],
        "candidate_revision_sha256": source["candidate_revision_sha256"],
        "evaluation_binding_sha256": config.binding_sha256,
        "contexts_sha256": config.contexts_sha256,
        "heldout_task_ids_sha256": _sha256_json(list(config.heldout_task_ids)),
        "contexts": contexts,
        "candidate_evidence": [
            item for item in source["candidate_evidence"] if item["task_id"] in declared
        ],
        "score_weights": source["score_weights"],
        "hard_failures": any(item["hard_failures"] for item in contexts),
    }


def ablation_rows() -> list[dict[str, Any]]:
    variants = {
        "revision": SOURCE_ROOT / "workspaces/revision-only/candidates/r000001-r001-edit",
        "recomposition": SOURCE_CANDIDATE,
        "synthesis": SOURCE_ROOT / "workspaces/full/candidates/r000001-r001-synthesize",
    }
    output: list[dict[str, Any]] = []
    for operator, candidate in variants.items():
        report = read_json(candidate / "validation-report.json")
        pairs = [
            pair
            for context in report["contexts"]
            for pair in context["pairs"]
            if is_v2_task(pair["id"])
            and pair.get("baseline_status") == "scored"
            and pair.get("candidate_status") == "scored"
        ]
        metrics: dict[str, dict[str, float]] = {}
        for metric in ("weighted_score", "accuracy", "modification_accuracy", "regression_accuracy"):
            if metric == "weighted_score":
                baseline = [float(pair["baseline"]) for pair in pairs]
                evolved = [float(pair["candidate"]) for pair in pairs]
            else:
                baseline = [float(pair["baseline_metrics"][metric]) for pair in pairs]
                evolved = [float(pair["candidate_metrics"][metric]) for pair in pairs]
            baseline_mean = sum(baseline) / len(baseline)
            evolved_mean = sum(evolved) / len(evolved)
            metrics[metric] = {
                "baseline_mean": baseline_mean,
                "candidate_mean": evolved_mean,
                "delta": evolved_mean - baseline_mean,
            }
        revision = read_json(candidate / "revision.json")
        output.append(
            {
                "operator": operator,
                "candidate_revision_sha256": revision["revision_sha256"],
                "paired_case_count": len(pairs),
                "metrics": metrics,
                "source_validation_report": str((candidate / "validation-report.json").resolve()),
            }
        )
    return sorted(output, key=lambda item: item["metrics"]["weighted_score"]["delta"], reverse=True)


def main() -> int:
    config = filtered_config()
    report = filtered_report(config)
    decision = evaluate_validation_report(
        report,
        config=config,
        incumbent_revision=str(report["incumbent_revision_sha256"]),
        candidate_revision=str(report["candidate_revision_sha256"]),
    )
    rows = ablation_rows()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUTPUT_ROOT / "config.json", config.to_dict())
    atomic_json(OUTPUT_ROOT / "validation-report.json", report)
    atomic_json(OUTPUT_ROOT / "decision.json", decision.to_dict())
    atomic_json(
        OUTPUT_ROOT / "operator-ablation.json",
        {
            "schema_version": "fin15k-v2-operator-ablation-v1",
            "selection_split": "unsealed Fin-1.5K V2 development/transfer/regression",
            "heldout_used_for_selection": False,
            "rows": rows,
        },
    )
    with (OUTPUT_ROOT / "operator-ablation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "operator",
                "paired_cases",
                "baseline_weighted_score",
                "candidate_weighted_score",
                "delta_weighted_score",
                "delta_accuracy",
                "delta_modification_accuracy",
                "delta_regression_accuracy",
            ]
        )
        for row in rows:
            metrics = row["metrics"]
            writer.writerow(
                [
                    row["operator"],
                    row["paired_case_count"],
                    metrics["weighted_score"]["baseline_mean"],
                    metrics["weighted_score"]["candidate_mean"],
                    metrics["weighted_score"]["delta"],
                    metrics["accuracy"]["delta"],
                    metrics["modification_accuracy"]["delta"],
                    metrics["regression_accuracy"]["delta"],
                ]
            )
    atomic_json(
        OUTPUT_ROOT / "protocol.json",
        {
            "schema_version": "fin15k-v2-relaxed-selection-protocol-v1",
            "source_config": str(SOURCE_CONFIG.resolve()),
            "source_config_sha256": digest(SOURCE_CONFIG),
            "source_validation_report": str(SOURCE_REPORT.resolve()),
            "source_validation_report_sha256": digest(SOURCE_REPORT),
            "candidate_revision_sha256": report["candidate_revision_sha256"],
            "solver_model": "qwen3.6-plus",
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "max_turns": 50,
            "score": "(accuracy + 0.25*modification_accuracy + 0.10*regression_accuracy)/1.35",
            "promotion_policy": config.policy.to_dict(),
            "valid_pair_count": sum(item["pair_count"] for item in decision.context_results),
            "declared_pair_count": sum(len(item.task_ids) for item in config.contexts),
            "heldout_used_for_selection": False,
            "kernel_changed": False,
        },
    )
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
