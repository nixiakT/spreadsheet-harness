#!/usr/bin/env python3
"""Read-only structural audit for the frozen paper24 held-out matrix.

This auditor deliberately does not load or print score values.  It verifies
the preregistration chain, six-arm/24-family cell inventory, per-cell runtime
manifests, frozen skill hashes, artifact hashes, and absence of held-out task
identifiers or label paths in search evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917"
DEFAULT_PARENT = REPO / "benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917"
SEARCH_ROOTS = {
    "general_only": REPO / "benchmarks/results/paper36-search-h-only-qwen36plus-20260917",
    "domain_only": REPO / "benchmarks/results/paper36-search-d-only-qwen36plus-20260917",
    "alternating": REPO / "benchmarks/results/paper36-search-alternating-qwen36plus-20260917",
}
ARMS = (
    "initial",
    "general_only",
    "domain_only",
    "alternating",
    "alt_h_only",
    "alt_d_only",
)
SCORED_STATUSES = frozenset({"scored", "skipped-scored"})
MODEL_VISIBLE_EVENTS = frozenset(
    {"model.requested", "model.responded", "tool.returned", "tool.failed"}
)
SKIP_SEARCH_SUFFIXES = frozenset(
    {".xlsx", ".xlsm", ".png", ".jpg", ".jpeg", ".pdf", ".pyc"}
)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def verified_recovery_policy(root: Path) -> dict[str, Any] | None:
    """Only a sealed, artifact-bound amendment can authorize a timeout change."""

    path = root / "infrastructure-recovery-amendment-v2.json"
    if not path.is_file():
        return None
    amendment = load(path)
    prior_path = root / "infrastructure-recovery-amendment.json"
    prior = load(prior_path)
    contract = amendment["contract"]["preserved_v1_execution_contract"]
    if contract != prior["contract"]:
        raise ValueError("v2 did not preserve the v1 execution contract")
    if amendment["supersedes"]["sha256"] != digest(prior_path):
        raise ValueError("superseded recovery amendment changed")
    for entry in (amendment, prior):
        if digest(Path(entry["runner"])) != entry["runner_sha256"]:
            raise ValueError("sealed recovery runner changed")
    for entry in contract["frozen_artifacts"].values():
        if digest(Path(entry["path"])) != entry["sha256"]:
            raise ValueError("recovery-bound artifact changed")
    correction = amendment["contract"]["source_integrity_correction"]
    if digest(Path(correction["controller_path"])) != correction["controller_sha256"]:
        raise ValueError("sealed execution controller changed")
    if contract["only_execution_change"] != {"task_timeout_seconds": {"from": 3600, "to": 7200}}:
        raise ValueError("unexpected infrastructure execution change")
    runtime_path = Path(contract["frozen_artifacts"]["runtime_source"]["path"])
    runtime_lock = load(runtime_path)
    source_root = runtime_path.parent / "spreadsheet_harness"
    actual = {str(p.relative_to(source_root)): digest(p) for p in sorted(source_root.rglob("*.py"))}
    if actual != runtime_lock["source_files"]:
        raise ValueError("frozen runtime files changed")
    return {
        "path": str(path), "sha256": digest(path),
        "created_at": amendment["created_at"],
        "canonical_source_variant": correction["canonical_source_variant"],
        "eligible_cells": {
            (item["arm"], item["dataset"], item["task_id"])
            for item in amendment["initial_scope"]["pending_cells"]
        },
    }


def normalized_resources(resources, key, started, policy):
    """Normalize only the registered timeout and only for eligible reruns."""

    result = dict(resources)
    timeout = result.get("task_timeout_seconds")
    if timeout == 3600:
        result["task_timeout_seconds"] = 3600.0
        return result
    if (
        timeout == 7200 and policy is not None
        and key in policy["eligible_cells"]
        and started >= _iso_timestamp(policy["created_at"])
    ):
        result["task_timeout_seconds"] = 3600.0
        return result
    raise ValueError("task timeout is not authorized for this cell/start time")


def load_runner():
    path = REPO / "benchmarks/run_paper24_pilot_20260917.py"
    spec = importlib.util.spec_from_file_location("paper24_readonly_audit_adapter", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def expected_cells(manifest: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (arm, str(task["dataset"]), str(task["task_id"])): task
        for arm in ARMS
        for task in manifest["tasks"]
    }


def cell_dir(root: Path, arm: str, task: dict[str, Any]) -> Path:
    slug = str(task["task_id"]).replace("/", "_")
    return root / "runs/heldout-pilot" / arm / str(task["dataset"]) / "heldout-pilot" / slug


def read_status(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _iso_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _task_id_from_trajectory(path: Path) -> str | None:
    try:
        index = path.parts.index("Financial_Model")
        return path.parts[index + 1]
    except (ValueError, IndexError):
        return None


def _dataset_rows(manifest: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dataset, metadata in manifest["datasets"].items():
        root = REPO / str(metadata["root"])
        rows = load(root / "Financial_Model/dataset.json")
        result[str(dataset)] = {str(row["id"]): dict(row) for row in rows}
    return result


def _utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def describe_source_variants(
    variants: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return stable per-variant evidence and pairwise source-file diffs."""

    details: list[dict[str, Any]] = []
    for variant_digest, variant in variants.items():
        cells = sorted(
            variant["cells"],
            key=lambda cell: (cell["manifest_mtime"], cell["cell"]),
        )
        details.append(
            {
                "digest": variant_digest,
                "cell_count": len(cells),
                "first_manifest_utc": _utc_timestamp(cells[0]["manifest_mtime"]),
                "last_manifest_utc": _utc_timestamp(cells[-1]["manifest_mtime"]),
                "arm_counts": dict(sorted(Counter(cell["arm"] for cell in cells).items())),
                "status_counts": dict(
                    sorted(Counter(cell["status"] for cell in cells).items())
                ),
                "cells": [
                    {
                        "cell": cell["cell"],
                        "arm": cell["arm"],
                        "dataset": cell["dataset"],
                        "task_id": cell["task_id"],
                        "status": cell["status"],
                        "manifest_utc": _utc_timestamp(cell["manifest_mtime"]),
                    }
                    for cell in cells
                ],
            }
        )
    details.sort(key=lambda variant: (variant["first_manifest_utc"], variant["digest"]))

    source_files = {
        variant_digest: dict(variant["source_files"])
        for variant_digest, variant in variants.items()
    }
    pair_diffs: list[dict[str, Any]] = []
    for before, after in combinations(details, 2):
        before_files = source_files[before["digest"]]
        after_files = source_files[after["digest"]]
        changed_files = []
        for filename in sorted(set(before_files) | set(after_files)):
            before_hash = before_files.get(filename)
            after_hash = after_files.get(filename)
            if before_hash != after_hash:
                changed_files.append(
                    {
                        "filename": filename,
                        "before_sha256": before_hash,
                        "after_sha256": after_hash,
                    }
                )
        pair_diffs.append(
            {
                "before_digest": before["digest"],
                "after_digest": after["digest"],
                "changed_file_count": len(changed_files),
                "changed_files": changed_files,
            }
        )
    return details, pair_diffs


def audit(root: Path, parent_root: Path) -> dict[str, Any]:
    root = root.resolve()
    parent_root = parent_root.resolve()
    runner = load_runner()
    manifest = runner.verify(root, parent_root)
    protocol = load(root / "protocol.json")
    endpoint_lock = load(root / "endpoint-lock.json")
    expected = expected_cells(manifest)
    rows_by_dataset = _dataset_rows(manifest)
    issues: list[str] = []
    try:
        recovery_policy = verified_recovery_policy(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        recovery_policy = None
        issues.append(f"recovery amendment verification failed: {exc}")

    if tuple(protocol.get("unique_compositions") or ()) != ARMS:
        issues.append("protocol unique_compositions is not the frozen six-arm order")
    if set(endpoint_lock.get("skill_hashes") or {}) != set(ARMS):
        issues.append("endpoint lock does not contain exactly six arm skill trees")

    status_rows = read_status(root / "run-status.tsv")
    latest_status: dict[tuple[str, str, str], dict[str, str]] = {}
    attempts = Counter()
    unexpected_status_cells: set[tuple[str, str, str]] = set()
    for row in status_rows:
        key = (str(row["label"]), str(row["dataset"]), str(row["task"]))
        attempts[str(row["status"])] += 1
        if key not in expected:
            unexpected_status_cells.add(key)
        latest_status[key] = row

    source_variants: dict[str, dict[str, Any]] = {}
    provider_variants: set[str] = set()
    resource_variants: set[str] = set()
    raw_resource_variants: set[str] = set()
    timeout_counts: Counter = Counter()
    evaluator_variants: set[str] = set()
    cells: list[dict[str, Any]] = []
    scored_cells: set[tuple[str, str, str]] = set()
    attempted_cells: set[tuple[str, str, str]] = set()
    durations: dict[str, list[float]] = defaultdict(list)
    first_started: float | None = None

    for key, task in expected.items():
        arm, dataset, task_id = key
        directory = cell_dir(root, arm, task)
        manifest_path = directory / "manifest.json"
        summary_path = directory / "summary.json"
        results_path = directory / "results.json"
        trajectories = sorted(directory.rglob("trajectory.jsonl")) if directory.is_dir() else []
        outputs = sorted(directory.rglob("output.xlsx")) if directory.is_dir() else []
        artifact_paths = {
            "manifest": manifest_path,
            "summary": summary_path,
            "results": results_path,
            "trajectory": trajectories[0] if len(trajectories) == 1 else None,
            "output_workbook": outputs[0] if len(outputs) == 1 else None,
        }
        record: dict[str, Any] = {
            "arm": arm,
            "dataset": dataset,
            "task_id": task_id,
            "directory": str(directory.relative_to(root)),
            "artifacts": {
                name: (
                    {
                        "path": str(path.relative_to(root)),
                        "sha256": digest(path),
                        "bytes": path.stat().st_size,
                    }
                    if path is not None and path.is_file()
                    else None
                )
                for name, path in artifact_paths.items()
            },
            "status": latest_status.get(key, {}).get("status", "not-recorded"),
        }
        if directory.exists() or manifest_path.is_file() or key in latest_status:
            attempted_cells.add(key)
        if record["status"] in SCORED_STATUSES:
            scored_cells.add(key)
            missing = [
                name for name, artifact in record["artifacts"].items() if artifact is None
            ]
            if missing:
                issues.append(f"{key}: scored status lacks artifacts: {', '.join(missing)}")
        if len(trajectories) > 1:
            issues.append(f"{key}: live cell has {len(trajectories)} trajectories")
        if len(outputs) > 1:
            issues.append(f"{key}: live cell has {len(outputs)} output workbooks")

        if manifest_path.is_file():
            cell_manifest = load(manifest_path)
            task_rows = cell_manifest.get("tasks") or []
            if len(task_rows) != 1 or task_rows[0].get("task_id") != task_id:
                issues.append(f"{key}: cell manifest task identity mismatch")
            dataset_hash = (
                (cell_manifest.get("dataset") or {}).get("dataset_json_sha256") or {}
            ).get("Financial_Model")
            if dataset_hash != manifest["datasets"][dataset]["metadata_sha256"]:
                issues.append(f"{key}: cell dataset metadata hash mismatch")
            implementation = cell_manifest.get("implementation") or {}
            source_files = implementation.get("source_files") or {}
            source_key = hashlib.sha256(
                json.dumps(source_files, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if recovery_policy and source_key != recovery_policy["canonical_source_variant"]:
                issues.append(f"{key}: runtime source is not canonical")
            started = manifest_path.stat().st_mtime
            variant = source_variants.setdefault(
                source_key,
                {"source_files": dict(source_files), "cells": []},
            )
            variant["cells"].append(
                {
                    "cell": "/".join(key),
                    "arm": arm,
                    "dataset": dataset,
                    "task_id": task_id,
                    "status": record["status"],
                    "manifest_mtime": started,
                }
            )
            skill_hashes = {
                f"{skill['name']}/SKILL.md": skill["sha256"]
                for skill in implementation.get("skills") or []
            }
            if skill_hashes != endpoint_lock["skill_hashes"].get(arm):
                issues.append(f"{key}: runtime skill hashes do not match endpoint lock")
            provider = cell_manifest.get("provider") or {}
            generation = provider.get("generation") or {}
            if (
                provider.get("model") != protocol["solver"]
                or generation.get("temperature") != protocol["temperature"]
                or generation.get("top_p") != protocol["top_p"]
                or generation.get("enable_thinking") is not protocol["thinking"]
            ):
                issues.append(f"{key}: runtime provider settings differ from protocol")
            resources = cell_manifest.get("resources") or {}
            if resources.get("max_model_calls") != protocol["max_model_calls"]:
                issues.append(f"{key}: max_model_calls differs from protocol")
            provider_variants.add(
                hashlib.sha256(json.dumps(provider, sort_keys=True).encode()).hexdigest()
            )
            raw_resource_variants.add(
                hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()
            )
            timeout_counts[str(resources.get("task_timeout_seconds"))] += 1
            try:
                compared_resources = normalized_resources(resources, key, started, recovery_policy)
            except ValueError as exc:
                issues.append(f"{key}: {exc}")
                compared_resources = resources
            resource_variants.add(hashlib.sha256(json.dumps(compared_resources, sort_keys=True).encode()).hexdigest())
            evaluator_variants.add(
                str((cell_manifest.get("official_evaluator") or {}).get("sha256"))
            )
            first_started = started if first_started is None else min(first_started, started)
            status = latest_status.get(key)
            if status and status.get("status") in SCORED_STATUSES:
                elapsed = max(0.0, _iso_timestamp(status["timestamp"]) - started)
                durations[arm].append(elapsed)
        cells.append(record)

    if len(source_variants) > 1:
        issues.append(f"runtime package source changed across cells: {len(source_variants)} variants")
    if len(provider_variants) > 1:
        issues.append("runtime provider settings changed across cells")
    if len(resource_variants) > 1:
        issues.append("runtime resource settings changed across cells")
    if len(evaluator_variants) > 1:
        issues.append("official evaluator changed across cells")
    if unexpected_status_cells:
        issues.append(f"status journal contains {len(unexpected_status_cells)} unexpected cells")

    source_variant_details, source_variant_pair_diffs = describe_source_variants(
        source_variants
    )

    heldout_ids = [str(task["task_id"]).split("/", 1)[1] for task in manifest["tasks"]]
    search_references: list[str] = []
    search_files_after_heldout: list[str] = []
    for label, search_root in SEARCH_ROOTS.items():
        if not search_root.is_dir():
            issues.append(f"search root is missing: {search_root}")
            continue
        for path in search_root.rglob("*"):
            if not path.is_file():
                continue
            if first_started is not None and path.stat().st_mtime > first_started + 1:
                search_files_after_heldout.append(f"{label}:{path.relative_to(search_root)}")
            if path.suffix.lower() in SKIP_SEARCH_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(task_id in text for task_id in heldout_ids):
                search_references.append(f"{label}:{path.relative_to(search_root)}")
    if search_references:
        issues.append(f"search evidence references held-out IDs in {len(search_references)} files")
    if search_files_after_heldout:
        issues.append(
            f"search roots changed after held-out execution began: {len(search_files_after_heldout)} files"
        )

    model_visible_exposures: list[str] = []
    for path in root.glob("runs/heldout-pilot/**/trajectory.jsonl"):
        task_id = _task_id_from_trajectory(path)
        if task_id is None:
            issues.append(f"cannot resolve trajectory task: {path.relative_to(root)}")
            continue
        task = next(
            (item for item in manifest["tasks"] if str(item["task_id"]).endswith("/" + task_id)),
            None,
        )
        if task is None:
            issues.append(f"trajectory belongs to an unexpected task: {path.relative_to(root)}")
            continue
        row = rows_by_dataset[str(task["dataset"])][task_id]
        forbidden = {
            str(row.get("golden_response_path") or ""),
            Path(str(row.get("golden_response_path") or "")).name,
            str(row.get("answer_position") or ""),
        } - {""}
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(event.get("event") or "") not in MODEL_VISIBLE_EVENTS:
                continue
            material = json.dumps(event.get("payload"), ensure_ascii=False)
            if any(value in material for value in forbidden):
                model_visible_exposures.append(f"{path.relative_to(root)}:{line_number}")
    if model_visible_exposures:
        issues.append(
            "golden path or answer-position metadata appeared in model-visible events: "
            f"{len(model_visible_exposures)}"
        )

    report_files = {
        name: ((root / name).is_file())
        for name in (
            "report.json",
            "raw-records.jsonl",
            "table2.tex",
            "table3.tex",
            "TABLES.md",
        )
    }
    if report_files["report.json"] and not all(report_files.values()):
        issues.append("final report exists but one or more companion report files are missing")

    elapsed = (
        max(0.0, datetime.now(timezone.utc).timestamp() - first_started)
        if first_started is not None
        else 0.0
    )
    throughput = len(scored_cells) / (elapsed / 3600) if elapsed > 0 else None
    remaining = len(expected) - len(scored_cells)
    eta = remaining / throughput * 3600 if throughput else None
    duration_summary = {
        arm: {
            "count": len(values),
            "median_seconds": statistics.median(values) if values else None,
            "p90_seconds": _percentile(values, 0.9),
            "max_seconds": max(values) if values else None,
        }
        for arm, values in durations.items()
    }

    return {
        "schema_version": "paper24-structural-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_values_read_or_reported": False,
        "root": str(root),
        "protocol": {
            "pilot_split_sha256": protocol["pilot_split_sha256"],
            "parent_pilot_split_sha256": protocol["parent_pilot_split_sha256"],
            "parent_protocol_sha256": protocol["parent_protocol_sha256"],
            "script_sha256": protocol["script_sha256"],
            "amendment_runner_sha256": protocol["amendment_runner_sha256"],
        },
        "split": {
            "families": len(manifest["tasks"]),
            "unique_families": len(
                {(task["dataset"], task["source_workbook"]) for task in manifest["tasks"]}
            ),
            "strata": {
                "/".join(key): value
                for key, value in sorted(
                    Counter(
                        (str(task["dataset"]), str(task["complexity"]))
                        for task in manifest["tasks"]
                    ).items()
                )
            },
        },
        "matrix": {
            "expected_cells": len(expected),
            "attempted_cells": len(attempted_cells),
            "status_scored_cells": len(scored_cells),
            "remaining_cells": remaining,
            "unexpected_status_cells": ["/".join(key) for key in sorted(unexpected_status_cells)],
            "status_event_counts": dict(sorted(attempts.items())),
            "cells": cells,
        },
        "runtime_consistency": {
            "source_variants": len(source_variants),
            "source_variant_details": source_variant_details,
            "source_variant_pair_diffs": source_variant_pair_diffs,
            "provider_variants": len(provider_variants),
            "resource_variants": len(resource_variants),
            "raw_resource_variants": len(raw_resource_variants),
            "task_timeout_counts": dict(timeout_counts),
            "verified_recovery_amendment": (
                {k: v for k, v in recovery_policy.items() if k != "eligible_cells"}
                if recovery_policy else None
            ),
            "evaluator_variants": len(evaluator_variants),
        },
        "leakage": {
            "heldout_id_references_in_search_files": search_references,
            "search_files_modified_after_heldout_start": search_files_after_heldout,
            "model_visible_golden_or_answer_position_exposures": model_visible_exposures,
        },
        "runtime": {
            "elapsed_seconds": elapsed,
            "scored_cells_per_hour": throughput,
            "naive_eta_seconds_at_observed_throughput": eta,
            "completed_cell_duration_by_arm": duration_summary,
            "configured_parallelism_from_launcher": 12,
            "configured_cell_timeout_seconds": 3600,
            "configured_attempts": 2,
        },
        "report_files": report_files,
        "complete": len(scored_cells) == len(expected) and not issues and all(report_files.values()),
        "structurally_valid_so_far": not issues,
        "issues": issues,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--parent-root", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = audit(args.root, args.parent_root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    else:
        print(encoded, end="")
    if not report["structurally_valid_so_far"]:
        return 2
    if args.require_complete and not report["complete"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
