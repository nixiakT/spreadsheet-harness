#!/usr/bin/env python3
"""Source-aware infrastructure recovery for the frozen paper24 pilot.

This supersedes the v1 recovery runner without changing the preregistered
protocol.  In addition to v1's checks, a cell may be skipped or restored only
when its runtime source manifest is the canonical frozen variant.  A live cell
from any other source variant is atomically quarantined before it is rerun.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO = Path(__file__).resolve().parents[1]
V1_RUNNER = REPO / "benchmarks/recover_paper24_infrastructure_20260918.py"
V1_AMENDMENT = "infrastructure-recovery-amendment.json"
V2_AMENDMENT = "infrastructure-recovery-amendment-v2.json"
INVALID_SOURCE_VARIANT = "<invalid-runtime-manifest>"
CONTROLLER = REPO / "benchmarks/run_true_coevolution_20260911.py"
CONTROLLER_SHA256 = "8f0a38d75060ef2dd20a54a4239063a6020658f1ea2c901424bf8f0be62d71ab"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


v1 = load_module("paper24_infrastructure_recovery_v1_adapter", V1_RUNNER)


def source_variant(output: Path) -> str | None:
    """Return the recorded source variant, None if absent, or an invalid marker."""

    manifest = output / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        source_files = (document.get("implementation") or {}).get("source_files")
        if not isinstance(source_files, dict) or not source_files:
            return INVALID_SOURCE_VARIANT
        return hashlib.sha256(
            json.dumps(source_files, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except (OSError, ValueError, TypeError):
        return INVALID_SOURCE_VARIANT


def quarantine_live_cell(output: Path, variant: str) -> Path:
    """Atomically preserve an untrusted live cell outside archive discovery."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    variant_slug = "invalid" if variant == INVALID_SOURCE_VARIANT else variant[:12]
    destination = output.with_name(
        f"{output.name}.source-quarantine.{variant_slug}.{timestamp}"
    )
    counter = 0
    while destination.exists():
        counter += 1
        destination = output.with_name(
            f"{output.name}.source-quarantine.{variant_slug}.{timestamp}.{counter}"
        )
    output.rename(destination)
    return destination


def active_pilot_processes(root: Path, proc_root: Path = Path("/proc")) -> list[int]:
    """Reject overlap with a coordinator or orphaned worker for this pilot."""

    active = set(v1.active_primary_pilot_pids()) if proc_root == Path("/proc") else set()
    for path in proc_root.glob("[0-9]*/cmdline"):
        try:
            arguments = [value.decode(errors="replace") for value in path.read_bytes().split(b"\0") if value]
            if "spreadsheet_harness.cli" not in arguments or "v2-compare" not in arguments:
                continue
            output = Path(arguments[arguments.index("--output") + 1]).resolve()
            if output.is_relative_to(root.resolve()):
                active.add(int(path.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    return sorted(active)


def manifest_matches_cell(output: Path, task, label: str, root: Path, manifest, lock) -> bool:
    """Verify identity and frozen settings before skipping/restoring an archive."""

    try:
        doc = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        tasks = doc["tasks"]
        expected = next(item for item in manifest["tasks"] if item["dataset"] == task.dataset and item["task_id"] == task.task_id)
        if len(tasks) != 1 or any(tasks[0].get(key) != expected[key] for key in ("task_id", "input_sha256", "golden_sha256")):
            return False
        metadata_hash = doc["dataset"]["dataset_json_sha256"]["Financial_Model"]
        if metadata_hash != manifest["datasets"][task.dataset]["metadata_sha256"]:
            return False
        skills = {f"{item['name']}/SKILL.md": item["sha256"] for item in doc["implementation"]["skills"]}
        if skills != lock["skill_hashes"][label]:
            return False
        provider = doc["provider"]
        if provider["model"] != "qwen3.6-plus" or provider["api_protocol"] != "chat-completions":
            return False
        if provider["generation"] != {"temperature": 0.0, "top_p": 1.0, "seed": 41, "enable_thinking": True}:
            return False
        resources = dict(doc["resources"])
        timeout = resources.pop("task_timeout_seconds")
        return timeout in (3600, 7200) and resources == {
            "max_model_calls": 50, "max_turns_per_arm": 50,
            "max_total_tokens": None, "max_output_tokens_per_call": None,
            "request_interval_seconds": 0.5, "arm_order_seed": 20260911,
        }
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return False


def source_aware_experiment_class(base_class, controller, *, compatible=None):
    """Wrap the frozen Experiment so its skip path is source-aware."""

    class SourceAwareRecoveryExperiment(base_class):
        def valid(self, candidate: Path, output: Path) -> bool:
            return v1.is_valid_scored_cell(controller, candidate) and (
                compatible is None or compatible(candidate, output)
            )

        def recover_scored_archive(self, output: Path) -> bool:
            if self.valid(output, output):
                return True
            canonical_archives = [
                path
                for path in sorted(output.parent.glob(f"{output.name}.incomplete.*"))
                if self.valid(path, output)
            ]
            if not canonical_archives:
                return False
            if output.exists():
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                output.rename(output.with_name(f"{output.name}.superseded.{timestamp}"))
            canonical_archives[-1].rename(output)
            return True

        def run_one(
            self,
            phase: str,
            label: str,
            skill_root: Path,
            task,
            max_model_calls: int | None = None,
        ) -> None:
            output = self.task_dir(phase, label, task)
            variant = source_variant(output)
            if variant is not None and (
                variant != v1.CANONICAL_SOURCE_VARIANT
                or (compatible is not None and not compatible(output, output))
            ):
                quarantine_live_cell(output, variant)
            super().run_one(phase, label, skill_root, task, max_model_calls)
            if not self.valid(output, output):
                raise RuntimeError(
                    "Recovery returned without a canonical valid scored cell: "
                    f"{label} {task.task_id}"
                )

    SourceAwareRecoveryExperiment.__name__ = "SourceAwareRecoveryExperiment"
    return SourceAwareRecoveryExperiment


def superseding_contract(
    root: Path,
    protocol: dict[str, Any],
    endpoint_lock: Path,
    *,
    task_attempts: int,
) -> dict[str, Any]:
    return {
        "schema_version": "paper24-infrastructure-recovery-contract-v2",
        "preserved_v1_execution_contract": v1.recovery_contract(
            root,
            protocol,
            endpoint_lock,
            task_attempts=task_attempts,
        ),
        "source_integrity_correction": {
            "valid_skip_requires": [
                "canonical runtime source variant",
                "valid scored summary structure",
                "no unrecovered final model failure",
            ],
            "canonical_source_variant": v1.CANONICAL_SOURCE_VARIANT,
            "noncanonical_live_cell_action": (
                "atomically rename to a source-quarantine path that cannot match "
                "the .incomplete.* archive glob, then rerun"
            ),
            "archive_restore_policy": "restore canonical valid scored archives only",
            "postcondition": "every returned cell is canonical and valid scored",
            "heldout_score_values_used": False,
            "skip_restore_checks_cell_identity_skills_and_execution": True,
            "shared_v1_v2_process_lock": True,
            "orphaned_worker_overlap_forbidden": True,
            "controller_path": str(CONTROLLER),
            "controller_sha256": CONTROLLER_SHA256,
        },
    }


def _v1_amendment_reference(root: Path) -> dict[str, Any]:
    path = root / V1_AMENDMENT
    if not path.is_file():
        raise RuntimeError("The v1 infrastructure recovery amendment is missing")
    document = json.loads(path.read_text(encoding="utf-8"))
    runner = Path(str(document.get("runner") or ""))
    if not runner.is_file() or document.get("runner_sha256") != v1.digest(runner):
        raise RuntimeError("The v1 recovery amendment or its runner is no longer sealed")
    return {
        "path": str(path),
        "sha256": v1.digest(path),
        "schema_version": document.get("schema_version"),
        "runner_sha256": document.get("runner_sha256"),
    }


def write_or_verify_superseding_amendment(
    root: Path,
    contract: dict[str, Any],
    scored,
    pending,
) -> Path:
    path = root / V2_AMENDMENT
    supersedes = _v1_amendment_reference(root)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("contract") != contract:
            raise RuntimeError("Source-aware recovery contract differs from frozen amendment")
        if existing.get("supersedes") != supersedes:
            raise RuntimeError("The superseded v1 amendment changed")
        if existing.get("runner_sha256") != v1.digest(Path(__file__).resolve()):
            raise RuntimeError("Source-aware recovery runner changed after amendment")
        return path

    noncanonical = []
    for label, task in pending:
        output = root / (
            f"runs/heldout-pilot/{label}/{task.dataset}/heldout-pilot/"
            f"{task.task_id.replace('/', '_')}"
        )
        variant = source_variant(output)
        if variant is not None and variant != v1.CANONICAL_SOURCE_VARIANT:
            noncanonical.append(
                {
                    "arm": label,
                    "dataset": task.dataset,
                    "task_id": task.task_id,
                    "source_variant": variant,
                }
            )
    v1.atomic_json(
        path,
        {
            "schema_version": "paper24-infrastructure-recovery-amendment-v2",
            "created_at": v1.utc_now(),
            "runner": str(Path(__file__).resolve()),
            "runner_sha256": v1.digest(Path(__file__).resolve()),
            "supersedes": supersedes,
            "reason": (
                "The v1 partition rejected noncanonical cells, but its inherited "
                "run_one skip path did not inspect runtime source hashes."
            ),
            "contract": contract,
            "initial_scope": {
                "valid_canonical_scored_cells_skipped": len(scored),
                "cells_eligible_for_recovery": len(pending),
                "noncanonical_live_cells": noncanonical,
                "pending_cells": [
                    {
                        "arm": label,
                        "dataset": task.dataset,
                        "task_id": task.task_id,
                    }
                    for label, task in pending
                ],
            },
        },
    )
    return path


def execute(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    if v1.digest(CONTROLLER) != CONTROLLER_SHA256:
        raise RuntimeError("Recovery execution controller changed after sealing")
    if not args.audit_only:
        active = active_pilot_processes(root)
        if active:
            raise RuntimeError("Refusing recovery while pilot processes are live: " + ", ".join(map(str, active)))
    manifest = v1.paper24.verify(root, args.parent_root.resolve())
    protocol = v1.paper24.pilot.load(root / "protocol.json")
    v1.verify_protocol(protocol)
    frozen_runtime = v1.verify_frozen_runtime(root)
    endpoint_lock = root / "endpoint-lock.json"
    endpoint_lock_sha256 = v1.digest(endpoint_lock)
    arms = v1.load_frozen_arms(
        root,
        args.shared_root.resolve(),
        args.h_search.resolve(),
        args.d_search.resolve(),
        args.alt_search.resolve(),
    )
    controller = v1.paper24.pilot.load_controller()
    tasks = v1.task_objects(controller, manifest)
    endpoint_document = v1.paper24.pilot.load(endpoint_lock)
    cell_specs = {
        root / f"runs/heldout-pilot/{label}/{task.dataset}/heldout-pilot/{task.slug}": (label, task)
        for label in v1.EXPECTED_ARMS for task in tasks
    }

    def compatible(candidate, output):
        label, task = cell_specs[output]
        return manifest_matches_cell(candidate, task, label, root, manifest, endpoint_document)

    experiment_class = source_aware_experiment_class(controller.Experiment, controller, compatible=compatible)
    experiment = experiment_class(
        SimpleNamespace(
            result_root=root,
            parallelism=args.parallelism,
            task_attempts=args.task_attempts,
            task_timeout=v1.RECOVERY_TASK_TIMEOUT,
            request_interval=0.5,
            base_url="http://10.130.138.46:8010/v1",
            api_key_file=Path("/tmp/spreadsheet-harness-litellm.key"),
            model="qwen3.6-plus",
        ),
        tasks,
    )
    scored, pending = v1.partition_cells(controller, experiment, tasks, arms)
    incompatible = [(label, task) for label, task in scored if not compatible(experiment.task_dir("heldout-pilot", label, task), experiment.task_dir("heldout-pilot", label, task))]
    scored = [cell for cell in scored if cell not in incompatible]
    pending.extend(incompatible)
    contract = superseding_contract(
        root,
        protocol,
        endpoint_lock,
        task_attempts=args.task_attempts,
    )
    amendment = write_or_verify_superseding_amendment(
        root, contract, scored, pending
    )
    audit = {
        "amendment": str(amendment),
        "valid_canonical_scored_cells": len(scored),
        "pending_cells": len(pending),
        "total_cells": len(scored) + len(pending),
    }
    if args.audit_only:
        return {"status": "audit-only", **audit}
    active = active_pilot_processes(root)
    if active:
        raise RuntimeError(
            "Refusing to overlap recovery with the primary run: "
            + ", ".join(map(str, active))
        )
    # Workers drained while the original parent was SIGSTOPed cannot journal
    # their completion. Reconcile only structurally valid canonical cells.
    for label, task in scored:
        experiment.record_status("heldout-pilot", label, task, "skipped-scored", 0, 0)
    if pending:
        previous_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(frozen_runtime)
        try:
            v1.run_pending(experiment, arms, pending, parallelism=args.parallelism)
        finally:
            if previous_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous_pythonpath
    final_scored, final_pending = v1.partition_cells(
        controller, experiment, tasks, arms
    )
    if final_pending:
        raise RuntimeError(
            f"Source-aware recovery ended with {len(final_pending)} pending cells"
        )
    if v1.digest(endpoint_lock) != endpoint_lock_sha256:
        raise RuntimeError("Endpoint lock changed during infrastructure recovery")
    if v1.digest(CONTROLLER) != CONTROLLER_SHA256:
        raise RuntimeError("Recovery execution controller changed during evaluation")
    if any(not compatible(experiment.task_dir("heldout-pilot", label, task), experiment.task_dir("heldout-pilot", label, task)) for label, task in final_scored):
        raise RuntimeError("Recovered cell identity or frozen execution settings differ")
    report = v1.paper24.pilot.summarize(controller, experiment, tasks, arms, root)
    v1.paper24.correct_labels(root, report)
    result = {
        "schema_version": "paper24-infrastructure-recovery-result-v2",
        "completed_at": v1.utc_now(),
        "amendment": str(amendment),
        "valid_canonical_scored_cells_before": len(scored),
        "recovered_or_restored_cells": len(final_scored) - len(scored),
        "valid_canonical_scored_cells_after": len(final_scored),
        "report": str(root / "report.json"),
        "complete": bool(report.get("complete")),
    }
    v1.atomic_json(root / "infrastructure-recovery-result-v2.json", result)
    return result


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=v1.DEFAULT_ROOT)
    parser.add_argument("--parent-root", type=Path, default=v1.DEFAULT_PARENT)
    parser.add_argument("--shared-root", type=Path, default=v1.DEFAULT_SHARED)
    parser.add_argument("--h-search", type=Path, default=v1.DEFAULT_H)
    parser.add_argument("--d-search", type=Path, default=v1.DEFAULT_D)
    parser.add_argument("--alt-search", type=Path, default=v1.DEFAULT_ALT)
    parser.add_argument("--parallelism", type=int, default=12)
    parser.add_argument("--task-attempts", type=int, default=2)
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args(argv)
    if args.parallelism < 1 or args.task_attempts < 1:
        parser.error("parallelism and task-attempts must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    lock_path = args.root.resolve() / ".infrastructure-recovery.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another source-aware recovery runner is active") from exc
        try:
            result = execute(args)
        except Exception as exc:
            v1.atomic_json(
                args.root.resolve() / "infrastructure-recovery-v2-last.json",
                {
                    "schema_version": "paper24-infrastructure-recovery-status-v2",
                    "updated_at": v1.utc_now(),
                    "status": "incomplete",
                    "error": str(exc),
                },
            )
            raise
        v1.atomic_json(
            args.root.resolve() / "infrastructure-recovery-v2-last.json",
            {
                "schema_version": "paper24-infrastructure-recovery-status-v2",
                "updated_at": v1.utc_now(),
                "status": result.get("status", "complete"),
                "result": result,
            },
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
