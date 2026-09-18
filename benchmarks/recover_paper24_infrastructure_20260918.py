#!/usr/bin/env python3
"""Infrastructure-only recovery for the frozen 24-family held-out pilot.

This runner is deliberately separate from search.  It verifies the frozen
split, protocol, endpoints, and materialized skill trees; skips only cells with
a valid scored summary; and reruns the remaining cells with a longer elapsed
time allowance.  It never generates a candidate or changes an acceptance
gate.  Once every cell is valid, it calls the original pilot summarizer.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence


REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917"
DEFAULT_PARENT = REPO / "benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917"
DEFAULT_SHARED = REPO / "benchmarks/results/paper36-shared-baseline-qwen36plus-20260917"
DEFAULT_H = REPO / "benchmarks/results/paper36-search-h-only-qwen36plus-20260917"
DEFAULT_D = REPO / "benchmarks/results/paper36-search-d-only-qwen36plus-20260917"
DEFAULT_ALT = REPO / "benchmarks/results/paper36-search-alternating-qwen36plus-20260917"
ORIGINAL_TASK_TIMEOUT = 3600
RECOVERY_TASK_TIMEOUT = 7200
ARM_ORDER_SEED = 20260911
CANONICAL_SOURCE_VARIANT = "31ff9b96bbe399810f6e1b974c1f27ad0b361257077c10109f646c200068f3ac"
FROZEN_RUNTIME = DEFAULT_ROOT / "frozen-runtime-source"
EXPECTED_TASKS = 24
EXPECTED_ARMS = (
    "initial",
    "general_only",
    "domain_only",
    "alternating",
    "alt_h_only",
    "alt_d_only",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


paper24 = load_module(
    "paper24_infrastructure_recovery_adapter",
    REPO / "benchmarks/run_paper24_pilot_20260917.py",
)


def verify_protocol(protocol: Mapping[str, Any]) -> None:
    expected = {
        "solver": "qwen3.6-plus",
        "temperature": 0.0,
        "top_p": 1.0,
        "thinking": True,
        "max_model_calls": 50,
        "max_total_tokens": None,
        "max_output_tokens": None,
    }
    mismatches = {
        key: {"expected": value, "actual": protocol.get(key)}
        for key, value in expected.items()
        if protocol.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"Frozen protocol execution contract changed: {mismatches}")
    if protocol.get("unique_compositions") != list(EXPECTED_ARMS):
        raise RuntimeError("Frozen protocol arm order or identities changed")


def load_frozen_arms(
    root: Path,
    shared_root: Path,
    h_search: Path,
    d_search: Path,
    alt_search: Path,
) -> dict[str, Path]:
    """Validate and return the already materialized arms without rewriting them."""

    pilot = paper24.pilot
    lock_path = root / "endpoint-lock.json"
    if not lock_path.is_file():
        raise RuntimeError("Frozen endpoint lock is missing")
    lock = pilot.load(lock_path)
    if lock.get("schema_version") != "paper36-endpoint-lock-v1":
        raise RuntimeError("Unexpected endpoint lock schema")
    if Path(str(lock.get("shared_baseline"))).resolve() != shared_root.resolve():
        raise RuntimeError("Frozen shared baseline path changed")

    current_searches = {
        "general_only": pilot.endpoint(h_search),
        "domain_only": pilot.endpoint(d_search),
        "alternating": pilot.endpoint(alt_search),
    }
    if lock.get("searches") != current_searches:
        raise RuntimeError("Frozen search-final endpoints changed after held-out launch")
    expected_sequences = {
        "general_only": ["harness"],
        "domain_only": ["domain"],
        "alternating": ["harness", "domain"],
    }
    for label, sequence in expected_sequences.items():
        endpoint = current_searches[label]
        if endpoint.get("coordinate_sequence") != sequence:
            raise RuntimeError(f"Frozen endpoint coordinate sequence changed: {label}")
        if endpoint.get("heldout_opened") is not False:
            raise RuntimeError(f"Search endpoint was contaminated by held-out data: {label}")

    locked_hashes = lock.get("skill_hashes")
    if not isinstance(locked_hashes, dict) or set(locked_hashes) != set(EXPECTED_ARMS):
        raise RuntimeError("Frozen endpoint lock has unexpected arm identities or order")
    arms = {name: root / "skill-roots" / name for name in EXPECTED_ARMS}
    for name, path in arms.items():
        if not path.is_dir():
            raise RuntimeError(f"Frozen materialized arm is missing: {path}")
        if pilot.skill_tree(path) != locked_hashes[name]:
            raise RuntimeError(f"Frozen materialized arm changed: {name}")
    return arms


def task_objects(controller, manifest: Mapping[str, Any]) -> list[Any]:
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
    if len(tasks) != EXPECTED_TASKS:
        raise RuntimeError(f"Expected {EXPECTED_TASKS} frozen tasks, found {len(tasks)}")
    return tasks


def is_valid_scored_cell(controller, output: Path) -> bool:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_files = (document.get("implementation") or {}).get("source_files") or {}
        source_variant = hashlib.sha256(
            json.dumps(source_files, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except (OSError, ValueError, TypeError):
        return False
    return (
        source_variant == CANONICAL_SOURCE_VARIANT
        and controller.is_scored_summary(output / "summary.json")
        and not controller.has_unrecovered_model_failure(output)
    )


def verify_frozen_runtime(root: Path) -> Path:
    runtime = root / "frozen-runtime-source"
    lock_path = runtime / "lock.json"
    if not lock_path.is_file():
        raise RuntimeError("Canonical frozen runtime source lock is missing")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("canonical_source_variant") != CANONICAL_SOURCE_VARIANT:
        raise RuntimeError("Frozen runtime source variant is not canonical")
    source_root = runtime / "spreadsheet_harness"
    expected = lock.get("source_files") or {}
    actual = {
        str(path.relative_to(source_root)): digest(path)
        for path in sorted(source_root.rglob("*.py"))
    }
    if actual != expected:
        raise RuntimeError("Frozen runtime source files differ from their lock")
    return runtime


def partition_cells(
    controller,
    experiment,
    tasks: Sequence[Any],
    arms: Mapping[str, Path],
) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
    scored: list[tuple[str, Any]] = []
    pending: list[tuple[str, Any]] = []
    for label in EXPECTED_ARMS:
        if label not in arms:
            raise RuntimeError(f"Frozen arm missing from recovery: {label}")
        for task in tasks:
            cell = (label, task)
            output = experiment.task_dir("heldout-pilot", label, task)
            (scored if is_valid_scored_cell(controller, output) else pending).append(cell)
    expected = EXPECTED_TASKS * len(EXPECTED_ARMS)
    if len(scored) + len(pending) != expected:
        raise RuntimeError("Recovery cell partition is incomplete")
    return scored, pending


def recovery_contract(
    root: Path,
    protocol: Mapping[str, Any],
    endpoint_lock: Path,
    *,
    task_attempts: int,
) -> dict[str, Any]:
    return {
        "schema_version": "paper24-infrastructure-recovery-contract-v1",
        "pilot_root": str(root),
        "reason": (
            "Recover only held-out cells without a valid scored summary after the "
            "preregistered 3600-second execution allowance."
        ),
        "classification_policy": (
            "Held-out outcomes are used only to distinguish valid scored cells from "
            "infrastructure-incomplete cells; they are never used for candidate selection, "
            "gate changes, or hyperparameter changes."
        ),
        "only_execution_change": {
            "task_timeout_seconds": {
                "from": ORIGINAL_TASK_TIMEOUT,
                "to": RECOVERY_TASK_TIMEOUT,
            }
        },
        "unchanged_execution": {
            "model": protocol["solver"],
            "max_model_calls": protocol["max_model_calls"],
            "max_turns_per_arm": 50,
            "max_total_tokens": protocol["max_total_tokens"],
            "max_output_tokens": protocol["max_output_tokens"],
            "seed": 41,
            "temperature": protocol["temperature"],
            "top_p": protocol["top_p"],
            "thinking": protocol["thinking"],
            "reasoning_effort": "medium",
            "request_timeout_seconds": 1800,
            "litellm_timeout_seconds": 1800,
            "request_retries": 5,
            "request_interval_seconds": 0.5,
            "arm_order_seed": ARM_ORDER_SEED,
            "task_attempts_per_recovery_invocation": task_attempts,
        },
        "frozen_artifacts": {
            "runtime_source": {
                "path": str(root / "frozen-runtime-source/lock.json"),
                "sha256": digest(root / "frozen-runtime-source/lock.json"),
                "canonical_source_variant": CANONICAL_SOURCE_VARIANT,
            },
            "pilot_split": {
                "path": str(root / "pilot-split.json"),
                "sha256": digest(root / "pilot-split.json"),
            },
            "protocol": {
                "path": str(root / "protocol.json"),
                "sha256": digest(root / "protocol.json"),
            },
            "endpoint_lock": {
                "path": str(endpoint_lock),
                "sha256": digest(endpoint_lock),
            },
        },
        "frozen_scope": {
            "task_count": EXPECTED_TASKS,
            "arms": list(EXPECTED_ARMS),
            "cell_count": EXPECTED_TASKS * len(EXPECTED_ARMS),
        },
        "method_invariants": {
            "search_reopened": False,
            "candidate_generation": False,
            "acceptance_gate_changed": False,
            "tasks_changed": False,
            "arms_changed": False,
            "endpoint_changed": False,
            "summarizer": "run_paper36_pilot_20260917.summarize",
        },
    }


def write_or_verify_amendment(
    root: Path,
    contract: Mapping[str, Any],
    scored: Sequence[tuple[str, Any]],
    pending: Sequence[tuple[str, Any]],
) -> Path:
    path = root / "infrastructure-recovery-amendment.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("contract") != contract:
            raise RuntimeError("Infrastructure recovery contract differs from frozen amendment")
        if existing.get("runner_sha256") != digest(Path(__file__).resolve()):
            raise RuntimeError("Infrastructure recovery runner changed after amendment")
        return path
    atomic_json(
        path,
        {
            "schema_version": "paper24-infrastructure-recovery-amendment-v1",
            "created_at": utc_now(),
            "runner": str(Path(__file__).resolve()),
            "runner_sha256": digest(Path(__file__).resolve()),
            "contract": contract,
            "initial_scope": {
                "valid_scored_cells_skipped": len(scored),
                "unscored_cells_eligible_for_recovery": len(pending),
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


def active_primary_pilot_pids() -> list[int]:
    active: list[int] = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = [
                value.decode("utf-8", errors="replace")
                for value in path.read_bytes().split(b"\0")
                if value
            ]
        except (OSError, ValueError):
            continue
        if any(Path(value).name == "run_paper24_pilot_20260917.py" for value in arguments) and (
            "--run" in arguments
        ):
            active.append(int(path.parent.name))
    return sorted(active)


def run_pending(
    experiment,
    arms: Mapping[str, Path],
    pending: Sequence[tuple[str, Any]],
    *,
    parallelism: int,
) -> None:
    print(
        f"[{utc_now()}] infrastructure recovery: {len(pending)} unscored cells; "
        f"task_timeout={RECOVERY_TASK_TIMEOUT}",
        flush=True,
    )
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(
                experiment.run_one,
                "heldout-pilot",
                label,
                arms[label],
                task,
                50,
            ): (label, task)
            for label, task in pending
        }
        for future in concurrent.futures.as_completed(futures):
            label, task = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{label} {task.task_id}: {exc}")
    if failures:
        raise RuntimeError("Infrastructure recovery failures:\n" + "\n".join(failures))


def execute(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    parent = args.parent_root.resolve()
    manifest = paper24.verify(root, parent)
    protocol = paper24.pilot.load(root / "protocol.json")
    verify_protocol(protocol)
    frozen_runtime = verify_frozen_runtime(root)
    endpoint_lock = root / "endpoint-lock.json"
    endpoint_lock_sha256 = digest(endpoint_lock)
    arms = load_frozen_arms(
        root,
        args.shared_root.resolve(),
        args.h_search.resolve(),
        args.d_search.resolve(),
        args.alt_search.resolve(),
    )
    controller = paper24.pilot.load_controller()
    tasks = task_objects(controller, manifest)
    experiment = controller.Experiment(
        SimpleNamespace(
            result_root=root,
            parallelism=args.parallelism,
            task_attempts=args.task_attempts,
            task_timeout=RECOVERY_TASK_TIMEOUT,
            request_interval=0.5,
            base_url="http://10.130.138.46:8010/v1",
            api_key_file=Path("/tmp/spreadsheet-harness-litellm.key"),
            model="qwen3.6-plus",
        ),
        tasks,
    )
    scored, pending = partition_cells(controller, experiment, tasks, arms)
    contract = recovery_contract(
        root,
        protocol,
        endpoint_lock,
        task_attempts=args.task_attempts,
    )
    amendment = write_or_verify_amendment(root, contract, scored, pending)
    audit = {
        "amendment": str(amendment),
        "valid_scored_cells": len(scored),
        "pending_cells": len(pending),
        "total_cells": len(scored) + len(pending),
    }
    if args.audit_only:
        return {"status": "audit-only", **audit}
    active = active_primary_pilot_pids()
    if active:
        raise RuntimeError(
            "Refusing to overlap infrastructure recovery with the primary 3600-second run: "
            + ", ".join(map(str, active))
        )
    if pending:
        previous_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(frozen_runtime)
        try:
            run_pending(experiment, arms, pending, parallelism=args.parallelism)
        finally:
            if previous_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous_pythonpath
    final_scored, final_pending = partition_cells(controller, experiment, tasks, arms)
    if final_pending:
        raise RuntimeError(
            f"Infrastructure recovery ended with {len(final_pending)} unscored cells; "
            "no report was generated"
        )
    if digest(endpoint_lock) != endpoint_lock_sha256:
        raise RuntimeError("Endpoint lock changed during infrastructure recovery")
    report = paper24.pilot.summarize(controller, experiment, tasks, arms, root)
    paper24.correct_labels(root, report)
    result = {
        "schema_version": "paper24-infrastructure-recovery-result-v1",
        "completed_at": utc_now(),
        "amendment": str(amendment),
        "valid_scored_cells_before": len(scored),
        "recovered_or_restored_cells": len(final_scored) - len(scored),
        "valid_scored_cells_after": len(final_scored),
        "report": str(root / "report.json"),
        "complete": bool(report.get("complete")),
    }
    atomic_json(root / "infrastructure-recovery-result.json", result)
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--parent-root", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED)
    parser.add_argument("--h-search", type=Path, default=DEFAULT_H)
    parser.add_argument("--d-search", type=Path, default=DEFAULT_D)
    parser.add_argument("--alt-search", type=Path, default=DEFAULT_ALT)
    parser.add_argument("--parallelism", type=int, default=12)
    parser.add_argument("--task-attempts", type=int, default=2)
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args(argv)
    if args.parallelism < 1 or args.task_attempts < 1:
        parser.error("parallelism and task-attempts must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lock_path = args.root.resolve() / ".infrastructure-recovery.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another infrastructure recovery runner is active") from exc
        try:
            result = execute(args)
        except Exception as exc:
            atomic_json(
                args.root.resolve() / "infrastructure-recovery-last.json",
                {
                    "schema_version": "paper24-infrastructure-recovery-status-v1",
                    "updated_at": utc_now(),
                    "status": "incomplete",
                    "error": str(exc),
                },
            )
            raise
        atomic_json(
            args.root.resolve() / "infrastructure-recovery-last.json",
            {
                "schema_version": "paper24-infrastructure-recovery-status-v1",
                "updated_at": utc_now(),
                "status": result.get("status", "complete"),
                "result": result,
            },
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
