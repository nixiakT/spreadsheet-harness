#!/usr/bin/env python3
"""Fast failure-focused Fin-1.5K screen for H-only/D-only/joint mechanisms."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import run_fin15k_profiled_evolution_20260920 as profiled
from spreadsheet_harness.continuous_evolution import ContinuousEvolutionConfig, ContinuousEvolutionEngine, _kernel_manifest_sha256
from spreadsheet_harness.plugins import default_plugin_registry


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def choose_contexts(source_root: Path, source_config: dict[str, Any], *, limit: int = 2) -> list[dict[str, Any]]:
    # Prefer historically poor tasks when old paired output exists; otherwise
    # keep the frozen context order.  This screen is diagnostic, not final
    # evidence, so it is allowed to use historical development outputs.
    scores: dict[str, float] = {}
    # Prefer scores from the current formal Fin-1.5K baseline.  Historical
    # paired outputs are only a fallback for retrospective probes.
    for cell_path in (source_root / "baseline-trajectories").glob("*/cell.json"):
        try:
            cell = load(cell_path)
            task_id = str((cell.get("task") or {}).get("task_id", ""))
            summary_path = Path(str(cell.get("summary", "")))
            if task_id and summary_path.is_file():
                summary = load(summary_path)
                arm = (summary.get("arms") or {}).get("spreadsheet-harness-financial") or {}
                score = arm.get("scored_accuracy")
                if isinstance(score, (int, float)):
                    scores[task_id] = float(score)
        except (OSError, json.JSONDecodeError):
            continue
    old = Path("benchmarks/results/continuous-financial-plugin-evaluations/fin15k-50-general-only")
    for context_name in ("replay", "transfer", "regression"):
        for result_path in old.glob(f"*/{context_name}/candidate/results.json"):
            try:
                for row in load(result_path):
                    score = (row.get("official_score") or {}).get("accuracy")
                    if isinstance(score, (int, float)):
                        scores[str(row.get("task_id"))] = float(score)
            except (OSError, json.JSONDecodeError):
                continue
    contexts = []
    for raw in source_config.get("contexts", []):
        task_ids = list(raw["task_ids"])
        families = list(raw["workbook_families"])
        poor = [
            index for index in range(len(task_ids))
            if scores.get(task_ids[index], 0.5) <= 0.0
        ]
        successful = [
            index for index in range(len(task_ids))
            if scores.get(task_ids[index], 0.5) >= 1.0
        ]
        # Replay/transfer need both repair pressure and positive anchors.
        # Regression is deliberately success-heavy: it protects behavior that
        # should not move while the candidate addresses failures elsewhere.
        if raw.get("kind") == "regression":
            poor = []
        selected: list[int] = []
        for index in poor[: max(1, limit // 2)]:
            if index not in selected:
                selected.append(index)
        for index in successful:
            if len(selected) >= limit:
                break
            if index not in selected:
                selected.append(index)
        if len(selected) < limit:
            order = sorted(
                range(len(task_ids)),
                key=lambda index: (scores.get(task_ids[index], 0.5), index),
            )
            for index in order:
                if len(selected) >= limit:
                    break
                if index not in selected:
                    selected.append(index)
        tasks = [task_ids[index] for index in selected]
        selected_families = [families[index] for index in selected]
        runner = dict(raw.get("runner") or {})
        datasets = runner.get("task_datasets") or {}
        runner["task_datasets"] = {task: datasets.get(task) for task in tasks}
        contexts.append(
            {
                "name": raw["name"],
                "kind": raw["kind"],
                "task_ids": tasks,
                "workbook_families": selected_families,
                "weight": raw.get("weight", 1.0),
                "runner": runner,
            }
        )
    return contexts


def run(source_root: Path, root: Path, mechanism: str, *, size: int = 50) -> dict[str, Any]:
    source_scope = {"h-only": "general-only", "d-only": "domain-only", "joint": "coevolution"}[mechanism]
    source_config_path = source_root / "configs" / f"fin15k-{size}-{source_scope}.json"
    if not source_config_path.is_file():
        source_config_path = profiled.build_config(source_root, root, size=size, mechanism=mechanism)
    raw = load(source_config_path)
    raw["contexts"] = choose_contexts(source_root, raw, limit=2)
    declared = {task: family for context in raw["contexts"] for task, family in zip(context["task_ids"], context["workbook_families"])}
    evidence = [item for item in raw.get("initial_evidence", []) if item.get("task_id") not in declared]
    raw["initial_evidence"] = evidence
    raw["evidence_task_families"] = {task: family for task, family in (raw.get("evidence_task_families") or {}).items() if task not in declared}
    raw["max_rounds"] = 1
    raw["max_candidates_per_round"] = 1
    raw["kernel_manifest_sha256"] = _kernel_manifest_sha256(Path(__file__).resolve().parents[1], default_plugin_registry())
    binding = dict(raw.get("evaluation_binding") or {})
    binding.update(
        {
            "max_model_calls": 12,
            "max_turns": 12,
            "task_timeout_seconds": 300,
            "request_timeout_seconds": 300,
            "litellm_timeout_seconds": 300,
            "parallelism": 1,
            "evolution_mechanism": mechanism,
        }
    )
    binding["incumbent_cache_root"] = str(root / "selection-incumbent-cache" / f"screen-{mechanism}")
    raw["evaluation_binding"] = binding
    raw["promotion"] = {
        **dict(raw.get("promotion") or {}),
        "gate_mode": "aggregate-mean",
        "delta": 0.000001,
        "min_pairs_per_context": 2,
        "min_total_pairs": 3,
        "min_pair_coverage": 0.5,
    }
    profile_path = root / "profile-replayed/plugin-profile.json"
    ledger_path = root / "profile-replayed/plugin-task-ledger.jsonl"
    raw["plugin_profile_path"] = str(profile_path)
    config_path = root / "configs" / f"screen-{mechanism}.json"
    write(config_path, raw)
    config = ContinuousEvolutionConfig.load(config_path)
    workspace = root / "workspaces" / f"screen-{mechanism}"
    router = profiled.ProfileMechanismRouter(profile_path, ledger_path, mechanism)
    engine = ContinuousEvolutionEngine(config, workspace, router=router)
    state = engine.run(rounds=1)
    frozen = engine.freeze(config)
    result = {"mechanism": mechanism, "workspace": str(workspace), "state": state, "frozen": frozen, "screen": True}
    write(root / "cells" / f"screen-{mechanism}.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path("benchmarks/results/fin15k-scaling-coevolution-20260919"))
    parser.add_argument("--root", type=Path, default=Path("benchmarks/results/fin15k-plugin-screen-20260920"))
    parser.add_argument("--mechanism", choices=("h-only", "d-only", "joint"), required=True)
    args = parser.parse_args()
    run(args.source_root.resolve(), args.root.resolve(), args.mechanism)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
