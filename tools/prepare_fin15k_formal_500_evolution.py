#!/usr/bin/env python3
"""Prepare a profile-guided 500-case evolution root from the formal pool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


COMPONENTS = {
    "controller": "src/spreadsheet_harness/continuous_evolution.py",
    "capability_attribution": "src/spreadsheet_harness/capability_evolution.py",
    "benchmark_runner": "src/spreadsheet_harness/spreadsheetbench_v2.py",
    "proposer": "tools/propose_method_candidate.py",
    "paired_evaluator": "tools/evaluate_continuous_financial_20260911.py",
    "experiment_runner": "benchmarks/run_fin15k_scaling_evolution_20260919.py",
}


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_kernel_hash(snapshot: Path) -> str:
    """Hash the immutable kernel represented by an evolution snapshot.

    The formal baseline protocol freezes the kernel that produced the native
    trajectories.  A derived evolution run may deliberately use a repaired
    controller/evaluator snapshot, so it needs its own kernel identity rather
    than silently carrying the baseline hash forward.  Keeping this migration
    in the derived protocol makes the distinction auditable while retaining
    the baseline protocol unchanged.
    """

    import sys

    repo_src = snapshot / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    from spreadsheet_harness.continuous_evolution import _kernel_manifest_sha256
    from spreadsheet_harness.plugins import default_plugin_registry

    return _kernel_manifest_sha256(snapshot, default_plugin_registry())


def task_dir(root: Path, item: dict[str, Any]) -> Path:
    return root / "baseline-trajectories" / (
        f"{int(item['evidence_index']):04d}-{str(item['id']).replace('/', '_')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--template-protocol", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_root.expanduser().resolve()
    output = args.output_root.expanduser().resolve()
    snapshot = args.snapshot_root.expanduser().resolve()
    split = read(source / "split-manifest.json")
    evidence = list(split.get("evidence", ()))[:500]
    if len(evidence) != 500:
        raise RuntimeError(f"Expected 500 frozen evidence cases, found {len(evidence)}")
    incomplete: list[str] = []
    for item in evidence:
        cell_path = task_dir(source, item) / "cell.json"
        cell = read(cell_path) if cell_path.is_file() else {}
        trajectory = Path(str(cell.get("trajectory", "")))
        if cell.get("status") != "complete" or not trajectory.is_file():
            incomplete.append(str(item["task_id"]))
    if incomplete:
        raise RuntimeError(
            f"Formal 500 pool is incomplete ({500 - len(incomplete)}/500); "
            f"examples: {incomplete[:8]}"
        )
    if output.exists() and any(output.iterdir()):
        # Idempotent restart is safe once the same source binding exists.
        existing = output / "protocol.json"
        if existing.is_file() and read(existing).get("source_formal_root") == str(source):
            protocol = read(existing)
            expected_kernel = snapshot_kernel_hash(snapshot)
            previous_kernel = protocol.get("kernel_manifest_sha256")
            if previous_kernel != expected_kernel:
                migrations = list(protocol.get("kernel_manifest_migrations") or [])
                migrations.append(
                    {
                        "from": previous_kernel,
                        "to": expected_kernel,
                        "reason": "derived evolution snapshot kernel; formal baseline hash remains frozen in source root",
                    }
                )
                protocol["kernel_manifest_migrations"] = migrations
                protocol["kernel_manifest_sha256"] = expected_kernel
                write(existing, protocol)
                print(
                    json.dumps(
                        {
                            "output_root": str(output),
                            "state": "already_prepared_kernel_migrated",
                            "kernel_manifest_sha256": expected_kernel,
                        }
                    )
                )
            else:
                print(json.dumps({"output_root": str(output), "state": "already_prepared"}))
            return 0
        raise RuntimeError(f"Refusing non-empty output root: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "baseline-trajectories").symlink_to(
        source / "baseline-trajectories", target_is_directory=True
    )
    shutil.copy2(source / "split-manifest.json", output / "split-manifest.json")
    if (source / "PROTOCOL_ROLE.json").is_file():
        role = read(source / "PROTOCOL_ROLE.json")
    else:
        role = {}
    role.update(
        {
            "schema_version": "fin15k-evolution-run-role-v1",
            "role": "formal_native_500_profile_guided",
            "solver_model": "DeepSeek-V4-Flash",
            "proposer_model": "dashscope/glm-5.2",
            "source_trajectories": "formal native 500 pool",
            "final_claim_eligible": True,
            "heldout_feedback_used": False,
        }
    )
    write(output / "PROTOCOL_ROLE.json", role)
    protocol = read(source / "protocol.json")
    template = read(args.template_protocol.expanduser().resolve())
    protocol["hashes"] = dict(protocol["hashes"])
    for key, relative in COMPONENTS.items():
        component = snapshot / relative
        if not component.is_file():
            raise RuntimeError(f"Snapshot component missing: {component}")
        protocol["hashes"][key] = digest(component)
    protocol["hashes"]["split_manifest"] = digest(output / "split-manifest.json")
    baseline_kernel = template["kernel_manifest_sha256"]
    evolution_kernel = snapshot_kernel_hash(snapshot)
    protocol["kernel_manifest_sha256"] = evolution_kernel
    protocol["baseline_kernel_manifest_sha256"] = baseline_kernel
    protocol["kernel_manifest_migrations"] = [
        {
            "from": baseline_kernel,
            "to": evolution_kernel,
            "reason": "derived evolution snapshot kernel; formal baseline hash remains frozen in source root",
        }
    ]
    protocol["dataset"] = "Fin-1.5K calibration_only (formal native 500 profile-guided run)"
    protocol["evidence_sizes"] = [500]
    protocol["evidence_nesting"] = (
        "same frozen native pool as the 50/200 prefixes; full 500 condition"
    )
    protocol["source_formal_root"] = str(source)
    protocol["source_snapshot"] = str(snapshot)
    write(output / "protocol.json", protocol)
    write(
        output / "status.json",
        {
            "state": "prepared_native_500",
            "source_root": str(source),
            "evidence_target": 500,
            "completed_evidence_cases": 500,
        },
    )
    print(json.dumps({"output_root": str(output), "evidence": 500}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
