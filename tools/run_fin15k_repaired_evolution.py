#!/usr/bin/env python3
"""Run profile-guided evolution on profile-backed 50/200/500 evidence views.

This wrapper keeps the formal experiment runner and hashes unchanged while
injecting the scale-specific plugin profile into each generated evolution
config.  The repaired root uses the original baseline directories through a
read-only symlink and records replacements separately.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
BASE = importlib.import_module("benchmarks.run_fin15k_scaling_evolution_20260919")


def _profiled_build_config(root: Path, size: int, scope_name: str) -> Path:
    path = BASE._ORIGINAL_BUILD_CONFIG(root, size, scope_name)
    document = json.loads(path.read_text(encoding="utf-8"))
    mechanism = {
        "general-only": "H-only",
        "domain-only": "D-only",
        "coevolution": "joint",
    }[scope_name]
    document["plugin_profile_path"] = str(
        (root / f"plugin-profile-{size}" / "plugin-profile.json").resolve()
    )
    # Evolution must use the same solver as the native Fin-1.5K pool.  The
    # historical formal runner defaulted to qwen3.6-plus even when the pool
    # was explicitly generated with DeepSeek-V4-Flash, which made candidate
    # deltas incomparable and caused long-tail budget failures.
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    solver_model = str((protocol.get("solver") or {}).get("actual") or "DeepSeek-V4-Flash")
    binding = dict(document.get("evaluation_binding") or {})
    binding["solver_model"] = solver_model
    binding["evaluator_task_retries"] = 1
    binding["evaluator_task_timeout_seconds"] = 1800
    binding["candidate_screening"] = "retry-infrastructure-before-gate"
    document["max_candidates_per_round"] = max(
        3, int(document.get("max_candidates_per_round", 1))
    )
    binding["evolution_mechanism"] = mechanism
    # Local task pools may use all global evaluator slots.  The shared flock
    # limiter below keeps aggregate provider/UNO concurrency bounded at six,
    # so raising the per-adapter pool from the historical four only reduces
    # idle time when another context/cell is waiting on a long-tail task.
    binding["parallelism"] = 6
    # H-only, D-only and joint share the same incumbent revision and gate.
    # The evaluator digest removes routing-only fields, so this namespace
    # allows one completed baseline result to serve all three mechanisms.
    binding["incumbent_cache_root"] = str(
        root / "selection-incumbent-cache" / "shared"
    )
    binding["evidence_source"] = "repaired nested Fin-1.5K trajectory pool"
    binding["heldout_feedback_used"] = False
    document["evaluation_binding"] = binding
    BASE.atomic_json(path, document)
    # Keep the sidecar audit record synchronized with the profile-augmented
    # config; the frozen runner hash remains the original formal runner.
    sidecar = path.with_suffix(".protocol.json")
    side = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
    from spreadsheet_harness.continuous_evolution import ContinuousEvolutionConfig

    side["config_sha256"] = ContinuousEvolutionConfig.from_document(document).sha256
    side["config_file_sha256"] = BASE.digest(path)
    side["plugin_profile_path"] = document["plugin_profile_path"]
    BASE.atomic_json(sidecar, side)
    # A repaired run is a protocol migration: the controller/evaluator
    # implementation is frozen by the source snapshot, while the old
    # workspace may have been initialized with a pre-parallel config.  Update
    # the workspace's durable policy/state identity to the regenerated config
    # before resuming; evidence, candidates and singleton caches remain
    # untouched and are still content-addressed.
    workspace = root / "workspaces" / f"fin15k-{size}-{scope_name}"
    state_path = workspace / "state.json"
    policy_path = workspace / "policy.json"
    if state_path.is_file() and policy_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        from spreadsheet_harness.continuous_evolution import _kernel_manifest_sha256
        from spreadsheet_harness.plugins import default_plugin_registry

        expected_kernel = _kernel_manifest_sha256(BASE.REPO, default_plugin_registry())
        if state.get("attempted_rounds", 0) == 0 and state.get("accepted_rounds", 0) == 0:
            state["config_sha256"] = side["config_sha256"]
            state["kernel_manifest_sha256"] = expected_kernel
            policy["kernel_manifest_sha256"] = expected_kernel
            policy["plugin_profile_path"] = document["plugin_profile_path"]
            policy["evaluation_binding"] = document.get("evaluation_binding", {})
            BASE.atomic_json(state_path, state)
            BASE.atomic_json(policy_path, policy)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument(
        "--size", type=int, action="append", choices=(50, 200, 500), required=True
    )
    parser.add_argument("--baseline-parallelism", type=int, default=4)
    parser.add_argument("--evolution-parallelism", type=int, default=3)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.snapshot_root is not None:
        # Freeze the code/skills tree used by all six evolution workers.  The
        # live checkout may be edited by another benchmark process while this
        # long-running experiment is in flight.
        BASE.REPO = args.snapshot_root.expanduser().resolve()
    BASE.verify_protocol(root)
    # All six cells may be launched concurrently.  Give every evaluator
    # subprocess the same small pool of flock-backed slots so local task
    # parallelism does not multiply into an unbounded provider/UNO storm.
    slots = root / "evaluator-global-slots"
    slots.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SPREADSHEET_EVOLUTION_GLOBAL_SLOTS_DIR", str(slots))
    os.environ.setdefault("SPREADSHEET_EVOLUTION_GLOBAL_SLOTS", "6")
    BASE._ORIGINAL_BUILD_CONFIG = BASE.build_config
    BASE.build_config = _profiled_build_config
    results: list[dict[str, object]] = []
    for size in args.size:
        BASE.append_event(root, "repaired.evolution.size.started", size=size)
        # The source runner is still finishing unrelated infrastructure cases,
        # but every selected repaired case already has a frozen trajectory.
        # Validate those paths/digests without rerunning the baseline or
        # racing on the shared status.json.
        BASE._trajectory_refs(root, size)
        rows = BASE.evolve_size(root, size, parallelism=args.evolution_parallelism)
        results.extend(rows)
        BASE.append_event(root, "repaired.evolution.size.completed", size=size)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
