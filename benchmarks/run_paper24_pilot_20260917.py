#!/usr/bin/env python3
"""Time-bounded amendment of the frozen 36-family financial pilot.

The amendment is made without consulting any held-out score: it takes the
first four SHA-256-ordered workbook families from every dataset/complexity
stratum of the already frozen 36-family manifest.  The resulting evaluation
has 24 independent workbook families and six plugin compositions (144 cells).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
PARENT_ROOT = REPO / "benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917"
DEFAULT_ROOT = REPO / "benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917"
PER_STRATUM = 4


def load_pilot36():
    path = REPO / "benchmarks/run_paper36_pilot_20260917.py"
    spec = importlib.util.spec_from_file_location("paper24_pilot36_adapter", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.PER_STRATUM = PER_STRATUM
    return module


pilot = load_pilot36()


def prepare(root: Path, parent_root: Path) -> dict:
    manifest_path = root / "pilot-split.json"
    if manifest_path.is_file():
        return verify(root, parent_root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("Refusing to prepare into a non-empty pilot directory")

    parent_manifest_path = parent_root / "pilot-split.json"
    parent_protocol_path = parent_root / "protocol.json"
    parent = pilot.load(parent_manifest_path)
    parent_protocol = pilot.load(parent_protocol_path)
    if parent_protocol.get("pilot_split_sha256") != pilot.digest(parent_manifest_path):
        raise RuntimeError("The frozen 36-family parent manifest does not match its protocol")

    grouped: dict[tuple[str, str], list[dict]] = {}
    for task in parent["tasks"]:
        grouped.setdefault((task["dataset"], task["complexity"]), []).append(task)
    if len(grouped) != 6 or any(len(tasks) != 6 for tasks in grouped.values()):
        raise RuntimeError("The parent is not the expected balanced 36-family split")

    selected = []
    for (dataset, complexity), tasks in sorted(grouped.items()):
        ordered = sorted(
            tasks,
            key=lambda task: pilot.selection_key(
                dataset, complexity, task["source_workbook"]
            ),
        )
        selected.extend(ordered[:PER_STRATUM])

    manifest = {
        "schema_version": "family-pilot-time-amendment-v1",
        "seed": parent["seed"],
        "selection": (
            "first four SHA-256-ordered families per dataset/complexity from "
            "the frozen 36-family parent; no held-out scores consulted"
        ),
        "families_per_dataset_complexity": PER_STRATUM,
        "task_count": len(selected),
        "datasets": parent["datasets"],
        "exclusions": parent["exclusions"],
        "parent": {
            "root": str(parent_root),
            "pilot_split_sha256": pilot.digest(parent_manifest_path),
            "protocol_sha256": pilot.digest(parent_protocol_path),
        },
        "tasks": selected,
        "policy": {
            **parent["policy"],
            "label": (
                "accelerated preregistered 24-family pilot; not the full "
                "269/1565 population"
            ),
            "amendment_reason": "time-bounded completion requested before held-out execution",
            "heldout_scores_consulted_before_amendment": False,
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    pilot.atomic_json(manifest_path, manifest)
    pilot.atomic_json(
        root / "protocol.json",
        {
            "schema_version": "family-pilot-table23-protocol-v2",
            "pilot_split_sha256": pilot.digest(manifest_path),
            "parent_pilot_split_sha256": pilot.digest(parent_manifest_path),
            "parent_protocol_sha256": pilot.digest(parent_protocol_path),
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
            "unique_compositions": list(pilot.TABLE2.values())
            + ["alt_h_only", "alt_d_only"],
            "table2": pilot.TABLE2,
            "table3": pilot.TABLE3,
            # The underlying runner verifies this before any held-out call.
            "script_sha256": pilot.digest(Path(pilot.__file__)),
            "amendment_runner_sha256": pilot.digest(Path(__file__)),
        },
    )
    return verify(root, parent_root)


def verify(root: Path, parent_root: Path) -> dict:
    protocol = pilot.load(root / "protocol.json")
    manifest = pilot.load(root / "pilot-split.json")
    parent_manifest_path = parent_root / "pilot-split.json"
    parent_protocol_path = parent_root / "protocol.json"
    if protocol["amendment_runner_sha256"] != pilot.digest(Path(__file__)):
        raise RuntimeError("The 24-family amendment runner changed after preregistration")
    if protocol["parent_pilot_split_sha256"] != pilot.digest(parent_manifest_path):
        raise RuntimeError("The frozen parent split changed after the amendment")
    if protocol["parent_protocol_sha256"] != pilot.digest(parent_protocol_path):
        raise RuntimeError("The frozen parent protocol changed after the amendment")
    checked = pilot.verify_manifest(root)
    counts = Counter((task["dataset"], task["complexity"]) for task in manifest["tasks"])
    if len(checked["tasks"]) != 24 or set(counts.values()) != {PER_STRATUM}:
        raise RuntimeError("The amended pilot is not balanced at four families per stratum")
    return checked


def correct_labels(root: Path, report: dict) -> None:
    report["schema_version"] = "family-pilot-table23-report-v2"
    report["pilot_label"] = "accelerated preregistered 24-family held-out pilot"
    pilot.atomic_json(root / "report.json", report)
    tables = root / "TABLES.md"
    if tables.is_file():
        text = tables.read_text(encoding="utf-8")
        text = text.replace(
            "# Accelerated 36-family held-out pilot",
            "# Accelerated 24-family held-out pilot",
        ).replace(
            "This is a preregistered pilot (18 independent workbook families per dataset),",
            "This is a preregistered pilot (12 independent workbook families per dataset),",
        )
        pilot.atomic_text(tables, text)


def run(args: argparse.Namespace) -> dict:
    verify(args.root, args.parent_root)
    delegate = SimpleNamespace(
        root=args.root,
        shared_root=args.shared_root,
        h_search=args.h_search,
        d_search=args.d_search,
        alt_search=args.alt_search,
        parallelism=args.parallelism,
        task_attempts=args.task_attempts,
    )
    report = pilot.run(delegate)
    correct_labels(args.root, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--parent-root", type=Path, default=PARENT_ROOT)
    parser.add_argument("--shared-root", type=Path, default=pilot.DEFAULT_SHARED)
    parser.add_argument("--h-search", type=Path, default=pilot.DEFAULT_H)
    parser.add_argument("--d-search", type=Path, default=pilot.DEFAULT_D)
    parser.add_argument("--alt-search", type=Path, default=pilot.DEFAULT_ALT)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--parallelism", type=int, default=12)
    parser.add_argument("--task-attempts", type=int, default=2)
    args = parser.parse_args()
    if args.parallelism < 1 or args.task_attempts < 1:
        parser.error("parallelism and task-attempts must be positive")
    return args


def main() -> int:
    args = parse_args()
    args.root = args.root.resolve()
    args.parent_root = args.parent_root.resolve()
    if args.prepare:
        manifest = prepare(args.root, args.parent_root)
        print(json.dumps({"prepared": str(args.root), "tasks": len(manifest["tasks"])}, indent=2))
        return 0
    if args.run:
        report = run(args)
        print(json.dumps({"complete": report["complete"], "root": str(args.root)}, indent=2))
        return 0
    raise SystemExit("Choose --prepare or --run")


if __name__ == "__main__":
    raise SystemExit(main())
