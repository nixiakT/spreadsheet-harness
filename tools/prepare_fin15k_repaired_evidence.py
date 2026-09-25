#!/usr/bin/env python3
"""Prepare deterministic repaired 50/200 evidence views.

The formal 500-case runner keeps infrastructure outcomes in the audit trail.
For a time-bounded development run we may replace only those unavailable
prefix entries with completed representatives from the same frozen 500 pool.
The replacement map is recorded, and the repaired 50 prefix is nested in the
repaired 200 prefix.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def task_dir(root: Path, item: dict[str, Any]) -> Path:
    return root / "baseline-trajectories" / (
        f"{int(item['evidence_index']):04d}-{str(item['id']).replace('/', '_')}"
    )


def is_complete(root: Path, item: dict[str, Any]) -> bool:
    cell = task_dir(root, item) / "cell.json"
    if not cell.is_file():
        return False
    row = read(cell)
    trajectory = Path(str(row.get("trajectory", "")))
    return row.get("status") == "complete" and trajectory.is_file()


def choose_repaired(
    source_root: Path, evidence: list[dict[str, Any]], size: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return a nested repaired prefix and its replacement records."""

    if size not in (50, 200):
        raise ValueError("only repaired sizes 50 and 200 are supported")
    used: set[str] = set()
    replacements: list[dict[str, Any]] = []

    def repair_prefix(
        items: list[dict[str, Any]], target: int, replacement_start: int
    ) -> list[dict[str, Any]]:
        nonlocal used
        selected: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for item in items:
            key = str(item["task_id"])
            if is_complete(source_root, item):
                selected.append(dict(item))
                used.add(key)
            else:
                missing.append(dict(item))
        available = [
            dict(item)
            for item in evidence
            if str(item["task_id"]) not in used
            and is_complete(source_root, item)
            and int(item["evidence_index"]) >= replacement_start
        ]
        for missing_item in missing:
            same_complexity = [
                item
                for item in available
                if item.get("complexity") == missing_item.get("complexity")
            ]
            candidate = (same_complexity or available)[0] if (same_complexity or available) else None
            if candidate is None:
                raise RuntimeError(f"not enough completed replacement cases for {target}")
            available.remove(candidate)
            key = str(candidate["task_id"])
            used.add(key)
            selected.append(candidate)
            replacements.append(
                {
                    "size": target,
                    "original_task_id": str(missing_item["task_id"]),
                    "original_evidence_index": int(missing_item["evidence_index"]),
                    "replacement_task_id": key,
                    "replacement_evidence_index": int(candidate["evidence_index"]),
                    "complexity": str(candidate.get("complexity", "")),
                    "reason": (
                        "infrastructure/unscored baseline; completed same-complexity pool member"
                        if not is_complete(source_root, missing_item)
                        else "completed pool member reserved outside the original prefix"
                    ),
                }
            )
        if len(selected) != target:
            raise AssertionError((target, len(selected)))
        return selected

    # Draw 50-case replacements from the post-200 tail so that repairing the
    # 50 prefix does not displace otherwise usable original 50..199 entries.
    repaired50 = repair_prefix(evidence[:50], 50, 200)
    if size == 50:
        return repaired50, replacements

    # Keep the repaired 50 prefix nested, then retain completed original
    # entries from positions 50..199 and fill only their unavailable entries.
    repaired200 = list(repaired50)
    original_tail = [dict(item) for item in evidence[50:200]]
    selected_tail = [
        item
        for item in original_tail
        if is_complete(source_root, item) and str(item["task_id"]) not in used
    ]
    for item in selected_tail:
        repaired200.append(item)
        used.add(str(item["task_id"]))
    missing_count = 200 - len(repaired200)
    if missing_count:
        available = [
            dict(item)
            for item in evidence
            if int(item["evidence_index"]) >= 200
            and str(item["task_id"]) not in used
            and is_complete(source_root, item)
        ]
        selected_tail_ids = {str(item["task_id"]) for item in selected_tail}
        # This includes unavailable originals and complete originals displaced
        # by a replacement that was needed to repair the 50 prefix.
        missing_original = [
            item for item in original_tail if str(item["task_id"]) not in selected_tail_ids
        ]
        if len(missing_original) != missing_count:
            raise AssertionError((len(missing_original), missing_count))
        for missing_item in missing_original:
            same = [item for item in available if item.get("complexity") == missing_item.get("complexity")]
            candidate = (same or available)[0] if (same or available) else None
            if candidate is None:
                raise RuntimeError("not enough completed replacement cases for 200")
            available.remove(candidate)
            used.add(str(candidate["task_id"]))
            repaired200.append(candidate)
            replacements.append(
                {
                    "size": 200,
                    "original_task_id": str(missing_item["task_id"]),
                    "original_evidence_index": int(missing_item["evidence_index"]),
                    "replacement_task_id": str(candidate["task_id"]),
                    "replacement_evidence_index": int(candidate["evidence_index"]),
                    "complexity": str(candidate.get("complexity", "")),
                    "reason": (
                        "infrastructure/unscored baseline; completed same-complexity pool member"
                        if not is_complete(source_root, missing_item)
                        else "completed pool member displaced by nested-prefix repair"
                    ),
                }
            )
    if len(repaired200) != 200:
        raise AssertionError(len(repaired200))
    return repaired200, replacements


def make_profile_inputs(out: Path, source: Path, selections: dict[int, list[dict[str, Any]]]) -> None:
    for size, items in selections.items():
        target = out / f"profile-input-{size}" / "baseline-trajectories"
        target.mkdir(parents=True, exist_ok=True)
        for item in items:
            link = target / task_dir(source, item).name
            if not link.exists():
                link.symlink_to(task_dir(source, item), target_is_directory=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_root.resolve()
    out = args.output_root.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing non-empty output root: {out}")
    split = read(source / "split-manifest.json")
    evidence = [dict(item) for item in split["evidence"]]
    repaired50, repl50 = choose_repaired(source, evidence, 50)
    # Recompute independently from the same deterministic source while making
    # the 50 prefix nested in 200; choose_repaired(200) handles that itself.
    repaired200, repl200 = choose_repaired(source, evidence, 200)
    selections = {50: repaired50, 200: repaired200}
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline-trajectories").symlink_to(source / "baseline-trajectories", target_is_directory=True)
    if (source / "PROTOCOL_ROLE.json").is_file():
        shutil.copy2(source / "PROTOCOL_ROLE.json", out / "PROTOCOL_ROLE.json")
    repaired_split = dict(split)
    repaired_split["evidence"] = repaired200
    repaired_split["nested_sizes"] = [50, 200]
    repaired_split["evidence_complexity"] = {
        str(n): dict(Counter(item["complexity"] for item in selections[n])) for n in (50, 200)
    }
    repaired_split["replacement_policy"] = "completed same-complexity members from frozen 500 pool"
    repaired_split["source_split_manifest"] = str(source / "split-manifest.json")
    write(out / "split-manifest.json", repaired_split)
    protocol = dict(read(source / "protocol.json"))
    protocol["hashes"] = dict(protocol["hashes"])
    # The repaired split is intentionally a separate, auditable development
    # protocol; all code/data/evaluator hashes remain those of the formal root.
    import hashlib

    protocol["hashes"]["split_manifest"] = hashlib.sha256((out / "split-manifest.json").read_bytes()).hexdigest()
    protocol["evidence_sizes"] = [50, 200]
    protocol["evidence_nesting"] = "repaired 50 subset 200; replacements recorded"
    protocol["dataset"] = "Fin-1.5K calibration_only (repaired evidence development run)"
    protocol["source_formal_root"] = str(source)
    # The repaired development view is created after the native plugin-trace
    # kernel was added; bind it to the live kernel while leaving the formal
    # root's historical protocol immutable.
    from spreadsheet_harness.continuous_evolution import _kernel_manifest_sha256
    from spreadsheet_harness.plugins import default_plugin_registry

    protocol["kernel_manifest_sha256"] = _kernel_manifest_sha256(
        source.parents[2], default_plugin_registry()
    )
    write(out / "protocol.json", protocol)
    write(out / "status.json", {"state": "prepared_repaired", "source_root": str(source), "evidence_target": 200})
    write(out / "replacement-manifest.json", {"source_root": str(source), "replacements": repl200, "repaired50": repaired50, "repaired200": repaired200})
    make_profile_inputs(out, source, selections)
    print(json.dumps({"output_root": str(out), "sizes": {"50": len(repaired50), "200": len(repaired200)}, "replacements": len(repl200)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
