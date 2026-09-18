#!/usr/bin/env python3
"""Accelerated, family-disjoint Table 2/3 pilot for financial co-evolution.

The pilot freezes 18 workbook families per dataset (six per complexity), then
evaluates six unique plugin compositions. It never exposes held-out trajectories
to any search controller. Missing infrastructure remains missing, never a zero.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917"
DEFAULT_SHARED = REPO / "benchmarks/results/paper36-shared-baseline-qwen36plus-20260917"
DEFAULT_H = REPO / "benchmarks/results/paper36-search-h-only-qwen36plus-20260917"
DEFAULT_D = REPO / "benchmarks/results/paper36-search-d-only-qwen36plus-20260917"
DEFAULT_ALT = REPO / "benchmarks/results/paper36-search-alternating-qwen36plus-20260917"
DATASETS = {
    "Fin-269": REPO / "benchmarks/data/normalized-harbor/v06-financial-269",
    "Fin-1.5K": REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565",
}
SEED = 20260917
PER_STRATUM = 6
METRICS = ("accuracy", "modification_accuracy", "regression_accuracy")
TABLE2 = {
    "initial": "initial",
    "general_only": "general_only",
    "domain_only": "domain_only",
    "alternating": "alternating",
}
TABLE3 = {"c00": "initial", "c10": "alt_h_only", "c01": "alt_d_only", "c11": "alternating"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_lines(values: Sequence[str]) -> str:
    material = "".join(f"{value}\n" for value in sorted(values)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_controller():
    path = REPO / "benchmarks/run_true_coevolution_20260911.py"
    name = "paper36_true_coevolution_adapter"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def exclusion_sources(shared_root: Path) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    """Return families that were actually exposed to optimization.

    Merely listing a task in an old sealed/test split is not exposure.  We
    exclude the current search tasks plus every task for which an earlier
    experiment produced a trajectory.  This distinction is important for the
    small Fin-269 C2/C3 family population.
    """

    sources: dict[str, set[str]] = defaultdict(set)
    provenance: list[dict[str, Any]] = []
    aliases = {
        "v06": "Fin-269",
        "enhanced-v2": "Fin-1.5K",
        "Fin-269": "Fin-269",
        "Fin-1.5K": "Fin-1.5K",
    }
    shared_manifest = shared_root / "split-manifest.json"
    if not shared_manifest.is_file():
        raise RuntimeError(f"Shared formal split is missing: {shared_manifest}")
    shared = load(shared_manifest)
    shared_task_ids: list[str] = []
    added = 0
    for item in shared.get("tasks") or []:
        if str(item.get("role")) == "heldout":
            continue
        dataset = aliases.get(str(item.get("dataset") or ""))
        source = str(item.get("source_workbook") or "")
        if dataset and source:
            shared_task_ids.append(str(item["task_id"]))
            if source not in sources[dataset]:
                sources[dataset].add(source)
                added += 1
    provenance.append(
        {
            "path": str(shared_manifest.relative_to(REPO)),
            "sha256": digest(shared_manifest),
            "evidence": "non-heldout tasks in the formal search split",
            "task_count": len(shared_task_ids),
            "task_ids_sha256": digest_lines(shared_task_ids),
            "added_families": added,
        }
    )

    task_catalog: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for dataset, dataset_root in DATASETS.items():
        for row in load(dataset_root / "Financial_Model/dataset.json"):
            task_catalog[str(row["id"])].append((dataset, str(row["source_workbook"])))

    historical_roots = [
        REPO / "benchmarks/results/evidence-gated-v4e-qwen36plus-20260917",
        REPO / "benchmarks/results/attributed-evolution-v3b-20260917",
        REPO / "benchmarks/results/true-coevolution-deepseekpro-fast-20260911",
    ]
    for result_root in historical_roots:
        if not result_root.is_dir():
            continue
        trajectory_paths = sorted(result_root.rglob("trajectory.jsonl"))
        task_ids: set[str] = set()
        for trajectory in trajectory_paths:
            task_id = next(
                (
                    trajectory.parts[index + 1]
                    for index, part in enumerate(trajectory.parts[:-1])
                    if part == "Financial_Model"
                    and trajectory.parts[index + 1].startswith("fina_")
                ),
                None,
            )
            if task_id is None:
                task_id = next(
                    (
                        part.removeprefix("Financial_Model_")
                        for part in trajectory.parts
                        if part.startswith("Financial_Model_fina_")
                    ),
                    None,
                )
            if task_id is None or task_id not in task_catalog:
                raise RuntimeError(f"Cannot resolve historical trajectory task: {trajectory}")
            task_ids.add(task_id)
        added = 0
        for task_id in sorted(task_ids):
            matches = task_catalog[task_id]
            if len(matches) != 1:
                raise RuntimeError(f"Ambiguous historical task id {task_id}: {matches}")
            dataset, source = matches[0]
            if source not in sources[dataset]:
                sources[dataset].add(source)
                added += 1
        provenance.append(
            {
                "path": str(result_root.relative_to(REPO)),
                "evidence": "tasks with materialized trajectory.jsonl",
                "trajectory_count": len(trajectory_paths),
                "task_count": len(task_ids),
                "task_ids_sha256": digest_lines(task_ids),
                "added_families": added,
            }
        )
    return sources, provenance


def selection_key(dataset: str, complexity: str, source: str) -> str:
    return hashlib.sha256(f"{SEED}\0{dataset}\0{complexity}\0{source}".encode()).hexdigest()


def prepare(root: Path, shared_root: Path) -> dict[str, Any]:
    manifest_path = root / "pilot-split.json"
    if manifest_path.is_file():
        return verify_manifest(root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("Refusing to prepare into a non-empty directory without pilot-split.json")
    root.mkdir(parents=True, exist_ok=True)
    excluded, exclusion_provenance = exclusion_sources(shared_root)
    selected: list[dict[str, Any]] = []
    datasets: dict[str, Any] = {}
    for dataset, dataset_root in DATASETS.items():
        metadata_path = dataset_root / "Financial_Model/dataset.json"
        rows = load(metadata_path)
        datasets[dataset] = {
            "root": str(dataset_root.relative_to(REPO)),
            "metadata_sha256": digest(metadata_path),
            "population_tasks": len(rows),
            "population_families": len({str(row["source_workbook"]) for row in rows}),
        }
        by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_source[str(row["source_workbook"])].append(row)
        used: set[str] = set()
        remaining = {"C1", "C2", "C3"}
        # Allocate the currently scarcest stratum first.  A workbook family
        # can contain tasks at multiple complexities, so fixed C1-first greedy
        # selection can make a valid family-disjoint split look impossible.
        while remaining:
            eligible_by_complexity = {
                complexity: [
                    source
                    for source, family_rows in by_source.items()
                    if source not in excluded[dataset]
                    and source not in used
                    and any(str(row.get("complexity")) == complexity for row in family_rows)
                ]
                for complexity in remaining
            }
            complexity = min(
                remaining,
                key=lambda value: (len(eligible_by_complexity[value]), value),
            )
            eligible = eligible_by_complexity[complexity]
            eligible.sort(key=lambda source: selection_key(dataset, complexity, source))
            if len(eligible) < PER_STRATUM:
                raise RuntimeError(f"Not enough clean {dataset}/{complexity} families")
            for source in eligible[:PER_STRATUM]:
                family_rows = sorted(
                    (row for row in by_source[source] if str(row.get("complexity")) == complexity),
                    key=lambda row: str(row["id"]),
                )
                row = family_rows[0]
                input_path = dataset_root / "Financial_Model" / str(row["spreadsheet_path"])
                golden_path = dataset_root / "Financial_Model" / str(row["golden_response_path"])
                selected.append(
                    {
                        "dataset": dataset,
                        "dataset_root": str(dataset_root.relative_to(REPO)),
                        "task_id": "Financial_Model/" + str(row["id"]),
                        "complexity": complexity,
                        "source_workbook": source,
                        "role": "heldout-pilot",
                        "input_sha256": digest(input_path),
                        "golden_sha256": digest(golden_path),
                    }
                )
                used.add(source)
            remaining.remove(complexity)
    manifest = {
        "schema_version": "paper36-family-pilot-v1",
        "seed": SEED,
        "selection": "sha256 order within dataset/complexity after frozen family exclusions",
        "families_per_dataset_complexity": PER_STRATUM,
        "task_count": len(selected),
        "datasets": datasets,
        "exclusions": exclusion_provenance,
        "tasks": selected,
        "policy": {
            "proposal_access": False,
            "search_selection_access": False,
            "rollback_access": False,
            "one_task_per_workbook_family": True,
            "label": "accelerated preregistered pilot; not the full 269/1565 population",
        },
    }
    atomic_json(manifest_path, manifest)
    atomic_json(
        root / "protocol.json",
        {
            "schema_version": "paper36-table23-protocol-v1",
            "pilot_split_sha256": digest(manifest_path),
            "solver": "qwen3.6-plus",
            "generator": "dashscope/glm-5.2",
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "max_model_calls": 50,
            "max_total_tokens": None,
            "max_output_tokens": None,
            "family_bootstrap_samples": 10000,
            "confidence": 0.95,
            "unique_compositions": list(TABLE2.values()) + ["alt_h_only", "alt_d_only"],
            "table2": TABLE2,
            "table3": TABLE3,
            "script_sha256": digest(Path(__file__)),
        },
    )
    return manifest


def verify_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "pilot-split.json"
    protocol = load(root / "protocol.json")
    if digest(manifest_path) != protocol["pilot_split_sha256"]:
        raise RuntimeError("Pilot split changed after preregistration")
    if digest(Path(__file__)) != protocol["script_sha256"]:
        raise RuntimeError("Pilot runner changed after preregistration")
    manifest = load(manifest_path)
    if len(manifest["tasks"]) != 2 * 3 * PER_STRATUM:
        raise RuntimeError("Pilot split has the wrong size")
    seen = set()
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for task in manifest["tasks"]:
        key = (task["dataset"], task["source_workbook"])
        if key in seen:
            raise RuntimeError("Pilot contains repeated workbook families")
        seen.add(key)
        counts[(task["dataset"], task["complexity"])] += 1
        dataset_root = REPO / task["dataset_root"]
        row_id = task["task_id"].split("/", 1)[1]
        rows = load(dataset_root / "Financial_Model/dataset.json")
        row = next(item for item in rows if str(item["id"]) == row_id)
        if digest(dataset_root / "Financial_Model" / row["spreadsheet_path"]) != task["input_sha256"]:
            raise RuntimeError("Held-out input changed")
        if digest(dataset_root / "Financial_Model" / row["golden_response_path"]) != task["golden_sha256"]:
            raise RuntimeError("Held-out golden workbook changed")
    if any(value != PER_STRATUM for value in counts.values()) or len(counts) != 6:
        raise RuntimeError("Pilot is not balanced by dataset and complexity")
    return manifest


def skill_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): digest(path)
        for path in sorted(root.rglob("SKILL.md"))
    }


def endpoint(search_root: Path) -> dict[str, Any]:
    path = search_root / "search-final.json"
    if not path.is_file():
        raise RuntimeError(f"Search endpoint is not ready: {path}")
    result = load(path)
    if result.get("solver_model") != "qwen3.6-plus" or result.get("heldout_opened") is not False:
        raise RuntimeError(f"Invalid or contaminated search endpoint: {path}")
    result["search_final_path"] = str(path)
    result["search_final_sha256"] = digest(path)
    return result


def fatal_infrastructure_events(run_dir: Path) -> list[str]:
    fatal: list[str] = []
    for path in sorted(run_dir.rglob("trajectory.jsonl")):
        last_outcome: str | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = str(event.get("event") or "").casefold()
            if name in {"model.failed", "model.responded"}:
                last_outcome = name
        if last_outcome == "model.failed":
            fatal.append(str(path.relative_to(run_dir)))
    return fatal


def _copy_overlay(baseline: Path, destination: Path, overlays: Sequence[tuple[Path, str]]) -> None:
    if destination.exists():
        return
    shutil.copytree(baseline, destination)
    for source_root, skill in overlays:
        shutil.copy2(source_root / skill / "SKILL.md", destination / skill / "SKILL.md")


def materialize_arms(
    root: Path, shared_root: Path, h_root: Path, d_root: Path, alt_root: Path
) -> dict[str, Path]:
    h, d, alt = endpoint(h_root), endpoint(d_root), endpoint(alt_root)
    expected_coordinates = {
        "general_only": ["harness"],
        "domain_only": ["domain"],
        "alternating": ["harness", "domain"],
    }
    for label, result in (("general_only", h), ("domain_only", d), ("alternating", alt)):
        if result.get("coordinate_sequence") != expected_coordinates[label]:
            raise RuntimeError(
                f"Search {label} has the wrong accepted-coordinate sequence: "
                f"{result.get('coordinate_sequence')}"
            )
    if len({h_root.resolve(), d_root.resolve(), alt_root.resolve()}) != 3:
        raise RuntimeError("Table 2 searches must use three independent result roots")
    baseline = shared_root / "skill-roots/round-00-h0d0"
    h_endpoint, d_endpoint, alt_endpoint = map(
        lambda value: Path(value["endpoint_skill_root"]), (h, d, alt)
    )
    if not all(path.is_dir() for path in (baseline, h_endpoint, d_endpoint, alt_endpoint)):
        raise RuntimeError("One or more frozen skill roots are missing")
    base_tree = skill_tree(baseline)
    for label, result in (("general_only", h), ("domain_only", d), ("alternating", alt)):
        if skill_tree(Path(result["baseline_skill_root"])) != base_tree:
            raise RuntimeError(f"Search {label} did not start from the shared frozen baseline")
    arms_root = root / "skill-roots"
    arms = {name: arms_root / name for name in (*TABLE2.values(), "alt_h_only", "alt_d_only")}
    _copy_overlay(baseline, arms["initial"], ())
    _copy_overlay(baseline, arms["general_only"], ((h_endpoint, "spreadsheet-structure"),))
    _copy_overlay(baseline, arms["domain_only"], ((d_endpoint, "spreadsheet-financial-model"),))
    _copy_overlay(
        baseline,
        arms["alternating"],
        (
            (alt_endpoint, "spreadsheet-structure"),
            (alt_endpoint, "spreadsheet-financial-model"),
        ),
    )
    _copy_overlay(baseline, arms["alt_h_only"], ((alt_endpoint, "spreadsheet-structure"),))
    _copy_overlay(baseline, arms["alt_d_only"], ((alt_endpoint, "spreadsheet-financial-model"),))
    base_tree = skill_tree(arms["initial"])
    expected_changes = {
        "initial": set(),
        "general_only": {"spreadsheet-structure/SKILL.md"},
        "domain_only": {"spreadsheet-financial-model/SKILL.md"},
        "alternating": {"spreadsheet-structure/SKILL.md", "spreadsheet-financial-model/SKILL.md"},
        "alt_h_only": {"spreadsheet-structure/SKILL.md"},
        "alt_d_only": {"spreadsheet-financial-model/SKILL.md"},
    }
    arm_hashes = {}
    for name, path in arms.items():
        tree = skill_tree(path)
        changed = {key for key in set(base_tree) | set(tree) if base_tree.get(key) != tree.get(key)}
        if changed != expected_changes[name]:
            raise RuntimeError(f"Arm {name} violates the frozen-coordinate contract: {sorted(changed)}")
        arm_hashes[name] = tree
    lock = {
        "schema_version": "paper36-endpoint-lock-v1",
        "shared_baseline": str(shared_root),
        "searches": {"general_only": h, "domain_only": d, "alternating": alt},
        "skill_hashes": arm_hashes,
    }
    lock_path = root / "endpoint-lock.json"
    if lock_path.exists() and load(lock_path) != lock:
        raise RuntimeError("Endpoint lock differs from the existing frozen lock")
    atomic_json(lock_path, lock)
    return arms


def percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap(values: Sequence[float], *, seed: int, samples: int = 10000) -> list[float]:
    if not values:
        return [math.nan, math.nan]
    rng = random.Random(seed)
    n = len(values)
    estimates = [sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples)]
    return [percentile(estimates, 0.025), percentile(estimates, 0.975)]


def exact_mcnemar(left: Sequence[float], right: Sequence[float]) -> dict[str, Any]:
    left_wins = sum(a > b for a, b in zip(left, right, strict=True))
    right_wins = sum(b > a for a, b in zip(left, right, strict=True))
    discordant = left_wins + right_wins
    if not discordant:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(min(left_wins, right_wins) + 1))
        p_value = min(1.0, 2.0 * tail / (2**discordant))
    return {
        "left_wins": left_wins,
        "right_wins": right_wins,
        "ties": len(left) - discordant,
        "discordant": discordant,
        "two_sided_exact_p": p_value,
    }


def holm(items: Sequence[tuple[str, float]]) -> dict[str, float]:
    ordered = sorted(items, key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - index) * value))
        adjusted[name] = running
    return adjusted


def interaction_values(
    c00: Sequence[float],
    c10: Sequence[float],
    c01: Sequence[float],
    c11: Sequence[float],
) -> list[float]:
    return [
        evolved_pair - h_only - d_only + initial
        for initial, h_only, d_only, evolved_pair in zip(
            c00, c10, c01, c11, strict=True
        )
    ]


def summarize(controller, experiment, tasks, arms: Mapping[str, Path], root: Path) -> dict[str, Any]:
    rows = {arm: experiment.result_rows("heldout-pilot", arm, tasks) for arm in arms}
    for arm in rows:
        rows[arm].sort(key=lambda item: (item["dataset"], item["task_id"]))
    infrastructure_failures: list[dict[str, Any]] = []
    for arm, arm_rows in rows.items():
        for task, row in zip(tasks, experiment.result_rows("heldout-pilot", arm, tasks), strict=True):
            fatal = fatal_infrastructure_events(experiment.task_dir("heldout-pilot", arm, task))
            if fatal:
                infrastructure_failures.append(
                    {
                        "arm": arm,
                        "dataset": task.dataset,
                        "task_id": task.task_id,
                        "fatal_events": fatal,
                    }
                )
    if infrastructure_failures:
        atomic_json(root / "infrastructure-failures.json", infrastructure_failures)
        raise RuntimeError(
            f"Refusing to report {len(infrastructure_failures)} infrastructure-failed cells as scores"
        )
    report: dict[str, Any] = {
        "schema_version": "paper36-table23-report-v1",
        "complete": all(len(items) == len(tasks) for items in rows.values()),
        "task_count": len(tasks),
        "arms": {},
        "table2": {},
        "table3": {},
        "raw_rows": rows,
    }
    for arm, arm_rows in rows.items():
        report["arms"][arm] = {}
        for dataset in DATASETS:
            subset = [row for row in arm_rows if row["dataset"] == dataset]
            report["arms"][arm][dataset] = {
                metric: {
                    "mean": sum(float(row[metric]) for row in subset) / len(subset),
                    "family_bootstrap_95": bootstrap(
                        [float(row[metric]) for row in subset],
                        seed=SEED + sum(map(ord, arm + dataset + metric)),
                    ),
                }
                for metric in METRICS
            }
    for dataset in DATASETS:
        task_order = [
            row["task_id"] for row in rows["initial"] if row["dataset"] == dataset
        ]
        indexed = {
            arm: {row["task_id"]: row for row in arm_rows if row["dataset"] == dataset}
            for arm, arm_rows in rows.items()
        }
        report["table2"][dataset] = {}
        p_values = []
        for label, arm in TABLE2.items():
            values = [float(indexed[arm][task]["accuracy"]) for task in task_order]
            base = [float(indexed["initial"][task]["accuracy"]) for task in task_order]
            deltas = [value - baseline for value, baseline in zip(values, base, strict=True)]
            entry = {
                "accuracy": sum(values) / len(values),
                "accuracy_family_bootstrap_95": bootstrap(
                    values, seed=SEED + sum(map(ord, dataset + label))
                ),
                "delta_vs_initial": sum(deltas) / len(deltas),
                "delta_family_bootstrap_95": bootstrap(
                    deltas, seed=SEED + 1000 + sum(map(ord, dataset + label))
                ),
            }
            if arm != "initial":
                entry["mcnemar_vs_initial"] = exact_mcnemar(values, base)
                p_values.append((label, entry["mcnemar_vs_initial"]["two_sided_exact_p"]))
            report["table2"][dataset][label] = entry
        adjusted = holm(p_values)
        for label, value in adjusted.items():
            report["table2"][dataset][label]["mcnemar_vs_initial"]["holm_p"] = value

        components = {
            label: [float(indexed[arm][task]["accuracy"]) for task in task_order]
            for label, arm in TABLE3.items()
        }
        interaction = interaction_values(
            components["c00"],
            components["c10"],
            components["c01"],
            components["c11"],
        )
        report["table3"][dataset] = {
            label: {
                "accuracy": sum(values) / len(values),
                "accuracy_family_bootstrap_95": bootstrap(
                    values, seed=SEED + 2000 + sum(map(ord, dataset + label))
                ),
            }
            for label, values in components.items()
        }
        report["table3"][dataset]["interaction"] = {
            "percentage_points": 100 * sum(interaction) / len(interaction),
            "family_bootstrap_95_percentage_points": [
                100 * value
                for value in bootstrap(
                    interaction, seed=SEED + 3000 + sum(map(ord, dataset))
                )
            ],
        }
    atomic_json(root / "report.json", report)
    raw_lines = [
        json.dumps({"arm": arm, **row}, ensure_ascii=False, sort_keys=True)
        for arm, arm_rows in rows.items()
        for row in arm_rows
    ]
    atomic_text(root / "raw-records.jsonl", "\n".join(raw_lines) + "\n")
    write_tables(root, report)
    return report


def pct(value: float) -> str:
    return f"{100 * value:.1f}"


def pct_ci(entry: Mapping[str, Any]) -> str:
    low, high = entry["accuracy_family_bootstrap_95"]
    return f"{pct(float(entry['accuracy']))} [{pct(float(low))}, {pct(float(high))}]"


def write_tables(root: Path, report: Mapping[str, Any]) -> None:
    datasets = ("Fin-269", "Fin-1.5K")
    table2_rows = [
        ("Financial (initialization)", "initial"),
        ("General-only", "general_only"),
        ("Domain-only", "domain_only"),
        ("Evolved (alternating)", "alternating"),
    ]
    table2 = [
        "\\begin{tabular}{lcc}",
        "\\toprule",
        "Variant & Fin-269 & Fin-1.5K \\\\",
        "\\midrule",
    ]
    for title, label in table2_rows:
        values = [pct_ci(report["table2"][dataset][label]) for dataset in datasets]
        table2.append(f"{title} & {values[0]} & {values[1]} \\\\")
    table2.extend(["\\bottomrule", "\\end{tabular}"])
    (root / "table2.tex").write_text("\n".join(table2) + "\n", encoding="utf-8")

    table3_rows = [
        ("Initial pair", "c00"),
        ("Evolved $H$, initial $D$", "c10"),
        ("Initial $H$, evolved $D$", "c01"),
        ("Evolved pair", "c11"),
    ]
    table3 = [
        "\\begin{tabular}{lcc}",
        "\\toprule",
        "Configuration & Fin-269 & Fin-1.5K \\\\",
        "\\midrule",
    ]
    for title, label in table3_rows:
        values = [pct_ci(report["table3"][dataset][label]) for dataset in datasets]
        table3.append(f"{title} & {values[0]} & {values[1]} \\\\")
    interactions = [report["table3"][dataset]["interaction"]["percentage_points"] for dataset in datasets]
    table3.extend(
        [
            "\\midrule",
            f"Interaction $I$ & {interactions[0]:+.1f} & {interactions[1]:+.1f} \\\\",
            "\\bottomrule",
            "\\end{tabular}",
        ]
    )
    (root / "table3.tex").write_text("\n".join(table3) + "\n", encoding="utf-8")

    markdown = [
        "# Accelerated 36-family held-out pilot",
        "",
        "This is a preregistered pilot (18 independent workbook families per dataset), not a full 269/1,565-task evaluation.",
        "Scores are percentages with family-bootstrap 95% confidence intervals in brackets.",
        "",
        "## Table 2",
        "",
        "| Variant | Fin-269 | Fin-1.5K |",
        "|---|---:|---:|",
    ]
    for title, label in table2_rows:
        values = [pct_ci(report["table2"][dataset][label]) for dataset in datasets]
        markdown.append(f"| {title} | {values[0]} | {values[1]} |")
    markdown.extend(
        [
            "",
            "### Paired exact-pass tests versus initialization",
            "",
            "| Dataset | Variant | Candidate wins | Initial wins | Ties | Exact p | Holm p |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for dataset in datasets:
        for label in ("general_only", "domain_only", "alternating"):
            test = report["table2"][dataset][label]["mcnemar_vs_initial"]
            markdown.append(
                f"| {dataset} | {label} | {test['left_wins']} | {test['right_wins']} | "
                f"{test['ties']} | {test['two_sided_exact_p']:.4g} | {test['holm_p']:.4g} |"
            )
    markdown.extend(["", "## Table 3", "", "| Configuration | Fin-269 | Fin-1.5K |", "|---|---:|---:|"])
    for title, label in table3_rows:
        values = [pct_ci(report["table3"][dataset][label]) for dataset in datasets]
        markdown.append(f"| {title} | {values[0]} | {values[1]} |")
    markdown.append(f"| Interaction I (pp) | {interactions[0]:+.1f} | {interactions[1]:+.1f} |")
    markdown.extend(["", "Interaction 95% CI (percentage points):"])
    for dataset in datasets:
        low, high = report["table3"][dataset]["interaction"][
            "family_bootstrap_95_percentage_points"
        ]
        markdown.append(f"- {dataset}: [{low:+.1f}, {high:+.1f}]")
    atomic_text(root / "TABLES.md", "\n".join(markdown) + "\n")


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    manifest = verify_manifest(root)
    arms = materialize_arms(root, args.shared_root, args.h_search, args.d_search, args.alt_search)
    controller = load_controller()
    tasks = [
        controller.Task(
            dataset=item["dataset"],
            dataset_root=REPO / item["dataset_root"],
            task_id=item["task_id"],
            complexity=item["complexity"],
            source_workbook=item["source_workbook"],
            role="heldout-pilot",
        )
        for item in manifest["tasks"]
    ]
    experiment_args = SimpleNamespace(
        result_root=root,
        parallelism=args.parallelism,
        task_attempts=args.task_attempts,
        task_timeout=3600,
        request_interval=0.5,
        base_url="http://10.130.138.46:8010/v1",
        api_key_file=Path("/tmp/spreadsheet-harness-litellm.key"),
        model="qwen3.6-plus",
    )
    experiment = controller.Experiment(experiment_args, tasks)
    experiment.run_matrix("heldout-pilot", arms, tasks)
    return summarize(controller, experiment, tasks, arms, root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED)
    parser.add_argument("--h-search", type=Path, default=DEFAULT_H)
    parser.add_argument("--d-search", type=Path, default=DEFAULT_D)
    parser.add_argument("--alt-search", type=Path, default=DEFAULT_ALT)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--parallelism", type=int, default=8)
    parser.add_argument("--task-attempts", type=int, default=3)
    args = parser.parse_args()
    if args.parallelism < 1 or args.task_attempts < 1:
        parser.error("parallelism and task-attempts must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.prepare:
        manifest = prepare(args.root.resolve(), args.shared_root.resolve())
        print(json.dumps({"prepared": str(args.root), "tasks": len(manifest["tasks"])}, indent=2))
        return 0
    if args.run:
        report = run(args)
        print(json.dumps({"complete": report["complete"], "root": str(args.root)}, indent=2))
        return 0
    raise SystemExit("Choose --prepare or --run")


if __name__ == "__main__":
    raise SystemExit(main())
