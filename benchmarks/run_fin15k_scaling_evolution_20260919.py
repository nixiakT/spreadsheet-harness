#!/usr/bin/env python3
"""Fin-1.5K evidence-scaling and coordinate-ablation experiment.

The experiment has two strictly separated stages:

1. Generate one fresh, frozen baseline trajectory pool on 500 nested,
   source-workbook-disjoint Fin-1.5K cases.  The 50 and 200 conditions are
   prefixes of that same pool.
2. Evolve nine independent candidates (3 evidence sizes x 3 coordinate
   scopes) against one fixed family-disjoint Fin-1.5K promotion gate, freeze
   them, and evaluate the frozen revisions on SpreadsheetBench V2.

SpreadsheetBench V2 is never read by the proposer, router, or promotion gate.
Its 24 Visualization tasks are excluded from the local value-only evaluation
and must be reported separately under the official Windows visual protocol.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import fcntl
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence


REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv/bin/python"
FIN15K = REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565"
V2 = REPO / "benchmarks/data/spreadsheetbench-v2"
OFFICIAL_EVALUATOR = (
    REPO / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
)
DEFAULT_ROOT = REPO / "benchmarks/results/fin15k-scaling-coevolution-20260919"
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
BASE_URL = "http://10.130.138.46:8010/v1"
SOLVER_MODEL = "qwen3.6-plus"
PROPOSER_MODEL = "dashscope/glm-5.2"
SIZES = (50, 200, 500)
SCOPES: Mapping[str, tuple[str, ...]] = {
    "general-only": ("harness",),
    "domain-only": ("domain",),
    "coevolution": ("harness", "domain", "joint"),
}
GENERAL_PLUGINS = (
    "act-code-plus-formula-validation",
    "observe-profile-compact",
    "observe-profile-full",
    "control-ours",
    "knowledge-structure",
    "knowledge-formula",
    "knowledge-manipulation",
    "knowledge-analysis",
    "knowledge-visualization",
    "knowledge-verification",
    "knowledge-memory",
    "verify-formula-runtime",
    "repair-date-text",
    "knowledge-coordination",
)
DOMAIN_PLUGINS = ("knowledge-financial-model",)
SCORE_WEIGHTS = {
    "accuracy": 1.0,
    "modification_accuracy": 0.25,
    "regression_accuracy": 0.10,
}
SEED = 20260919


def configured_solver_model() -> str:
    """Return the protocol-pinned solver override for a fresh root."""

    return os.environ.get("SPREADSHEET_FIN15K_SOLVER_MODEL", SOLVER_MODEL)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    ).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def append_event(root: Path, event: str, **payload: Any) -> None:
    row = {"time": now(), "event": event, **payload}
    print(json.dumps(row, ensure_ascii=False), flush=True)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def child_environment(*, source: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source or (REPO / "src"))
    # Provider clients are explicitly configured.  Environment proxies have
    # caused misleading connection errors on the private laboratory endpoint.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    return environment


def task_id(item: Mapping[str, Any]) -> str:
    return "Financial_Model/" + str(item["id"])


def _representatives(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Choose one deterministic task from each source workbook family."""

    by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        by_family[str(item["source_workbook"])].append(item)
    representatives: list[dict[str, Any]] = []
    for family, family_items in sorted(by_family.items()):
        chosen = min(
            family_items,
            key=lambda item: hashlib.sha256(
                f"{SEED}:{task_id(item)}".encode("utf-8")
            ).hexdigest(),
        )
        representatives.append(
            {
                "task_id": task_id(chosen),
                "id": str(chosen["id"]),
                "category": "Financial_Model",
                "complexity": str(chosen["complexity"]),
                "source_workbook": family,
                "input_path": str(
                    (FIN15K / "Financial_Model" / str(chosen["spreadsheet_path"])).resolve()
                ),
            }
        )
    return representatives


def _natural_quota(
    total: int, available: Mapping[str, int], *, require_each: bool = True
) -> dict[str, int]:
    keys = ("C1", "C2", "C3")
    population = sum(available.get(key, 0) for key in keys)
    exact = {key: total * available.get(key, 0) / population for key in keys}
    quota = {key: min(available.get(key, 0), int(math.floor(exact[key]))) for key in keys}
    if require_each:
        for key in keys:
            if available.get(key, 0) and quota[key] == 0 and total >= len(keys):
                quota[key] = 1
    while sum(quota.values()) > total:
        key = min(
            (name for name in keys if quota[name] > (1 if require_each else 0)),
            key=lambda name: (exact[name] - quota[name], name),
        )
        quota[key] -= 1
    while sum(quota.values()) < total:
        eligible = [key for key in keys if quota[key] < available.get(key, 0)]
        if not eligible:
            raise ValueError("Not enough workbook families for requested quota")
        key = max(eligible, key=lambda name: (exact[name] - quota[name], -keys.index(name)))
        quota[key] += 1
    return quota


def _nested_evidence(
    representatives: Sequence[Mapping[str, Any]], sizes: Sequence[int]
) -> list[dict[str, Any]]:
    queues: dict[str, list[dict[str, Any]]] = {}
    for complexity in ("C1", "C2", "C3"):
        queue = [dict(item) for item in representatives if item["complexity"] == complexity]
        random.Random(f"{SEED}:{complexity}").shuffle(queue)
        queues[complexity] = queue
    available = {key: len(value) for key, value in queues.items()}
    selected: list[dict[str, Any]] = []
    consumed = Counter()
    for size in sizes:
        quota = _natural_quota(size, available)
        stage: list[dict[str, Any]] = []
        for complexity in ("C1", "C2", "C3"):
            need = quota[complexity] - consumed[complexity]
            if need < 0:
                raise ValueError("Nested quota decreased across evidence milestones")
            stage.extend(queues[complexity][consumed[complexity] : quota[complexity]])
            consumed[complexity] = quota[complexity]
        random.Random(SEED + size).shuffle(stage)
        selected.extend(stage)
        if len(selected) != size:
            raise AssertionError("Nested evidence size mismatch")
    return selected


def _selection_gate(
    representatives: Sequence[Mapping[str, Any]], excluded_families: set[str]
) -> list[dict[str, Any]]:
    """Fixed 15-case, family-disjoint gate with harder cases represented."""

    candidates = [
        dict(item) for item in representatives if item["source_workbook"] not in excluded_families
    ]
    by_complexity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        by_complexity[str(item["complexity"])].append(item)
    for complexity, values in by_complexity.items():
        random.Random(f"{SEED}:gate:{complexity}").shuffle(values)
    quota = {"C1": 10, "C2": 3, "C3": 2}
    selected = [
        item
        for complexity in ("C1", "C2", "C3")
        for item in by_complexity[complexity][: quota[complexity]]
    ]
    if len(selected) != 15:
        raise ValueError("Fin-1.5K has insufficient family-disjoint gate strata")
    random.Random(SEED + 15).shuffle(selected)
    return selected


def _v2_task_ids() -> list[str]:
    ids: list[str] = []
    for category in ("Debugging", "Financial_Model", "Template", "Visualization"):
        for item in load(V2 / category / "dataset.json"):
            ids.append(f"{category}/{item['id']}")
    return ids


def prepare(root: Path) -> dict[str, Any]:
    protocol_path = root / "protocol.json"
    if protocol_path.is_file():
        verify_protocol(root)
        return load(root / "split-manifest.json")
    preexisting = (
        [item for item in root.iterdir() if item.name != "PROTOCOL_ROLE.json"]
        if root.exists()
        else []
    )
    if preexisting:
        raise RuntimeError(f"Refusing to overwrite non-empty experiment root: {root}")
    if not KEY_FILE.is_file() or not KEY_FILE.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"LiteLLM key file is missing or empty: {KEY_FILE}")
    metadata_path = FIN15K / "Financial_Model/dataset.json"
    representatives = _representatives(load(metadata_path))
    evidence = _nested_evidence(representatives, SIZES)
    evidence_families = {str(item["source_workbook"]) for item in evidence}
    gate = _selection_gate(representatives, evidence_families)
    for index, item in enumerate(evidence):
        item["evidence_index"] = index
        item["input_sha256"] = digest(Path(str(item["input_path"])))
    for index, item in enumerate(gate):
        item["gate_index"] = index
        item["input_sha256"] = digest(Path(str(item["input_path"])))
    manifest = {
        "schema_version": "fin15k-scaling-split-v1",
        "seed": SEED,
        "sampling_unit": "source_workbook_family",
        "representative_policy": "one deterministic task per source workbook",
        "nested_sizes": list(SIZES),
        "evidence": evidence,
        "selection_gate": gate,
        "evidence_complexity": {
            str(size): dict(Counter(item["complexity"] for item in evidence[:size]))
            for size in SIZES
        },
        "gate_complexity": dict(Counter(item["complexity"] for item in gate)),
        "family_disjoint": not evidence_families
        & {str(item["source_workbook"]) for item in gate},
    }
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / "split-manifest.json", manifest)

    from spreadsheet_harness.continuous_evolution import _kernel_manifest_sha256
    from spreadsheet_harness.plugins import default_plugin_registry

    kernel_hash = _kernel_manifest_sha256(REPO, default_plugin_registry())
    hashes = {
        "controller": digest(REPO / "src/spreadsheet_harness/continuous_evolution.py"),
        "capability_attribution": digest(
            REPO / "src/spreadsheet_harness/capability_evolution.py"
        ),
        "benchmark_runner": digest(REPO / "src/spreadsheet_harness/spreadsheetbench_v2.py"),
        "proposer": digest(REPO / "tools/propose_method_candidate.py"),
        "paired_evaluator": digest(
            REPO / "tools/evaluate_continuous_financial_20260911.py"
        ),
        "experiment_runner": digest(Path(__file__).resolve()),
        "fin15k_metadata": digest(metadata_path),
        "v2_metadata": canonical_sha(
            {
                category: digest(V2 / category / "dataset.json")
                for category in ("Debugging", "Financial_Model", "Template", "Visualization")
            }
        ),
        "official_evaluator": digest(OFFICIAL_EVALUATOR),
        "split_manifest": digest(root / "split-manifest.json"),
    }
    protocol = {
        "schema_version": "fin15k-scaling-coevolution-protocol-v1",
        "created_at": now(),
        "dataset": "Fin-1.5K calibration_only",
        "evidence_sizes": list(SIZES),
        "evidence_nesting": "50 subset 200 subset 500",
        "selection_gate_cases": len(gate),
        "selection_gate_policy": "fixed across all nine cells; source-family-disjoint from evidence",
        "base_composition": "spreadsheet-harness-financial",
        "general_plugins": list(GENERAL_PLUGINS),
        "domain_plugins": list(DOMAIN_PLUGINS),
        "kernel_manifest_sha256": kernel_hash,
        "scopes": {name: list(values) for name, values in SCOPES.items()},
        "operators": ["revision", "recomposition", "synthesis"],
        "rounds_per_cell": 1,
        "candidates_per_round": 1,
        "solver": {
            "requested": "qwen36-35b-a3b",
            "actual": configured_solver_model(),
            "substitution_reason": "requested model returned LiteLLM 500 during preflight",
            "base_url": BASE_URL,
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "max_turns": 50,
            "max_output_tokens": None,
            "max_total_tokens": None,
        },
        "proposer": {
            "model": PROPOSER_MODEL,
            "base_url": BASE_URL,
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
        },
        "spreadsheetbench_v2": {
            "total_tasks": 321,
            "local_nonvisual_tasks": 297,
            "visual_tasks_reported_separately": 24,
            "test_feedback_used_for_evolution": False,
        },
        "hashes": hashes,
    }
    atomic_json(protocol_path, protocol)
    atomic_json(
        root / "status.json",
        {"state": "prepared", "time": now(), "completed_evidence_cases": 0},
    )
    append_event(root, "experiment.prepared", evidence_cases=500, gate_cases=15)
    return manifest


def verify_protocol(root: Path) -> dict[str, Any]:
    protocol = load(root / "protocol.json")
    split = root / "split-manifest.json"
    if digest(split) != protocol["hashes"]["split_manifest"]:
        raise RuntimeError("Frozen scaling split changed")
    if digest(FIN15K / "Financial_Model/dataset.json") != protocol["hashes"]["fin15k_metadata"]:
        raise RuntimeError("Fin-1.5K metadata changed")
    for key, relative in {
        "controller": "src/spreadsheet_harness/continuous_evolution.py",
        "capability_attribution": "src/spreadsheet_harness/capability_evolution.py",
        "benchmark_runner": "src/spreadsheet_harness/spreadsheetbench_v2.py",
        "proposer": "tools/propose_method_candidate.py",
        "paired_evaluator": "tools/evaluate_continuous_financial_20260911.py",
        "experiment_runner": "benchmarks/run_fin15k_scaling_evolution_20260919.py",
    }.items():
        if digest(REPO / relative) != protocol["hashes"][key]:
            raise RuntimeError(f"Frozen experiment component changed: {relative}")
    return protocol


def _safe_task_name(task: Mapping[str, Any]) -> str:
    return f"{int(task['evidence_index']):04d}-{str(task['id']).replace('/', '_')}"


def _baseline_cell(root: Path, task: Mapping[str, Any]) -> dict[str, Any]:
    output = root / "baseline-trajectories" / _safe_task_name(task)
    record_path = output / "cell.json"
    previous = None
    if record_path.is_file():
        previous = load(record_path)
        trajectory = Path(str(previous.get("trajectory", "")))
        if trajectory.is_file() and previous.get("status") == "complete":
            return previous
    output.mkdir(parents=True, exist_ok=True)
    run_dir = output / "run"
    if run_dir.exists():
        # Preserve interrupted and scored-but-invalid requests for auditability;
        # the official runner correctly refuses to overwrite an old output.
        attempt = 2
        while (output / f"run-{attempt}").exists():
            attempt += 1
        run_dir = output / f"run-{attempt}"
    command = [
        str(PYTHON),
        "-m",
        "spreadsheet_harness.cli",
        "benchmark",
        "v2-compare",
        "--dataset",
        str(FIN15K),
        "--official-evaluator",
        str(OFFICIAL_EVALUATOR),
        "--category",
        "Financial_Model",
        "--task-id",
        str(task["task_id"]),
        "--output",
        str(run_dir),
        "--arm",
        "spreadsheet-harness-financial",
        "--skill-root",
        str(REPO / "skills"),
        "--max-model-calls",
        "50",
        "--max-turns-per-arm",
        "50",
        "--max-total-tokens",
        "unlimited",
        "--max-output-tokens",
        "unlimited",
        "--task-timeout",
        "7200",
        "--request-timeout",
        "600",
        "--request-retries",
        "2",
        "--request-interval-seconds",
        "0.8",
        "--litellm-timeout",
        "600",
        "--base-url",
        BASE_URL,
        "--api-key-file",
        str(KEY_FILE),
        "--model",
        configured_solver_model(),
        "--api-protocol",
        "chat-completions",
        "--reasoning-effort",
        "medium",
        "--temperature",
        "0",
        "--top-p",
        "1",
        "--seed",
        "41",
        "--enable-thinking",
    ]
    with (output / "runner.log").open("a", encoding="utf-8") as handle:
        completed = subprocess.run(
            command,
            cwd=REPO,
            env=child_environment(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=7500,
            check=False,
        )
    trajectories = list(
        run_dir.glob("runs/*/*/spreadsheet-harness-financial/trajectory.jsonl")
    )
    trajectory = trajectories[0] if len(trajectories) == 1 else None
    summary_path = run_dir / "summary.json"
    summary = load(summary_path) if summary_path.is_file() else {}
    record = {
        "schema_version": "fin15k-baseline-cell-v1",
        "task": dict(task),
        "status": "complete" if trajectory is not None and summary.get("study_complete") else "infrastructure",
        "returncode": completed.returncode,
        "trajectory": str(trajectory) if trajectory else None,
        "trajectory_sha256": digest(trajectory) if trajectory else None,
        "summary": str(summary_path) if summary_path.is_file() else None,
        "finished_at": now(),
    }
    if previous is not None:
        history = list(previous.get("prior_attempts") or ())
        history.append({key: previous.get(key) for key in ("status", "returncode", "trajectory")})
        record["prior_attempts"] = history
    atomic_json(record_path, record)
    return record


def run_baseline_to(root: Path, limit: int, *, parallelism: int) -> list[dict[str, Any]]:
    verify_protocol(root)
    split = load(root / "split-manifest.json")
    tasks = split["evidence"][:limit]
    completed_by_task: dict[str, dict[str, Any]] = {}
    append_event(root, "baseline.batch.started", target=limit, parallelism=parallelism)
    retry_tasks = list(tasks)
    for attempt in range(1, 4):
        if not retry_tasks:
            break
        with futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
            jobs = {pool.submit(_baseline_cell, root, task): task for task in retry_tasks}
            for job in futures.as_completed(jobs):
                record = job.result()
                task_key = str(jobs[job]["task_id"])
                completed_by_task[task_key] = record
                append_event(
                    root,
                    "baseline.case.finished",
                    task_id=task_key,
                    status=record["status"],
                    attempt=attempt,
                )
        retry_tasks = [
            task
            for task in tasks
            if completed_by_task.get(str(task["task_id"]), {}).get("status") != "complete"
        ]
    completed = list(completed_by_task.values())
    good = [record for record in completed if record["status"] == "complete"]
    atomic_json(
        root / "status.json",
        {
            "state": "baseline_ready" if len(good) == limit else "baseline_incomplete",
            "time": now(),
            "target_evidence_cases": limit,
            "completed_evidence_cases": len(good),
        },
    )
    if len(good) != limit:
        missing = sorted(
            record["task"]["task_id"] for record in completed if record["status"] != "complete"
        )
        raise RuntimeError(
            f"Only {len(good)}/{limit} baseline trajectories completed; retry: {missing[:8]}"
        )
    append_event(root, "baseline.batch.completed", target=limit)
    return completed


def _trajectory_refs(root: Path, size: int) -> list[dict[str, str]]:
    split = load(root / "split-manifest.json")
    references: list[dict[str, str]] = []
    for task in split["evidence"][:size]:
        record = load(root / "baseline-trajectories" / _safe_task_name(task) / "cell.json")
        trajectory = Path(str(record.get("trajectory", "")))
        if record.get("status") != "complete" or not trajectory.is_file():
            raise RuntimeError(f"Missing frozen trajectory for {task['task_id']}")
        if record.get("trajectory_sha256") != digest(trajectory):
            raise RuntimeError(f"Baseline trajectory changed: {task['task_id']}")
        references.append(
            {
                "path": str(trajectory),
                "task_id": str(task["task_id"]),
                "task_type": "Financial_Model",
                "workbook_family": str(task["source_workbook"]),
            }
        )
    return references


def _contexts(gate: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for offset, (name, kind) in enumerate(
        (("replay", "replay"), ("transfer", "transfer"), ("regression", "regression"))
    ):
        selected = [dict(gate[index]) for index in range(offset, len(gate), 3)]
        contexts.append(
            {
                "name": name,
                "kind": kind,
                "task_ids": [item["task_id"] for item in selected],
                "workbook_families": [item["source_workbook"] for item in selected],
                "weight": 1.0,
                "runner": {
                    "task_datasets": {item["task_id"]: str(FIN15K) for item in selected}
                },
            }
        )
    return contexts


def build_config(root: Path, size: int, scope_name: str) -> Path:
    verify_protocol(root)
    if size not in SIZES or scope_name not in SCOPES:
        raise ValueError("Unknown scaling cell")
    split = load(root / "split-manifest.json")
    evidence = _trajectory_refs(root, size)
    from spreadsheet_harness.continuous_evolution import (
        ContinuousEvolutionConfig,
        _kernel_manifest_sha256,
    )
    from spreadsheet_harness.plugins import default_plugin_registry

    kernel_hash = _kernel_manifest_sha256(REPO, default_plugin_registry())
    frozen_hash = load(root / "protocol.json")["kernel_manifest_sha256"]
    if kernel_hash != frozen_hash:
        raise RuntimeError("Live kernel changed after protocol preparation")
    config = {
        "repository_root": str(REPO),
        "composition": "spreadsheet-harness-financial",
        "groups": {
            "harness": list(GENERAL_PLUGINS),
            "domain": list(DOMAIN_PLUGINS),
        },
        "contexts": _contexts(split["selection_gate"]),
        # IDs only: no V2 instruction, workbook or score enters evolution.
        "heldout_task_ids": _v2_task_ids(),
        "initial_evidence": evidence,
        "evidence_task_families": {
            item["task_id"]: item["workbook_family"] for item in evidence
        },
        "evaluation_binding": {
            "protocol": "fin15k-fixed-family-gate-v1",
            "provider": BASE_URL,
            "solver_model": configured_solver_model(),
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "max_model_calls": 50,
            "max_turns": 50,
            "max_output_tokens": None,
            "max_total_tokens": None,
            "task_timeout_seconds": 7200,
            "request_timeout_seconds": 600,
            "litellm_timeout_seconds": 600,
            "parallelism": 4,
            "seed": 41,
            "default_dataset_root": str(FIN15K),
            "evaluator_path": str(OFFICIAL_EVALUATOR),
            # Each scaling cell has its own cache namespace.  Sharing one
            # lock file across the concurrently running 50/200/500 cells
            # serialized otherwise independent incumbent evaluations and, on
            # a busy provider, looked like a hung evaluator.  The namespace
            # remains reusable across retries of the same cell.
            "incumbent_cache_root": str(
                root / "selection-incumbent-cache" / f"fin15k-{size}-{scope_name}"
            ),
            "score_weights": SCORE_WEIGHTS,
            "family_aggregation": "mean-tasks-then-uniform-families",
        },
        "promotion": {
            "delta": 1e-6,
            "replay_delta": 0.0,
            "transfer_delta": 0.0,
            "epsilon": 0.0,
            "confidence": 0.90,
            "bootstrap_samples": 4000,
            "min_pairs_per_context": 2,
            "gate_mode": "aggregate-mean",
            "min_total_pairs": 9,
            "min_pair_coverage": 0.60,
        },
        "first_group": "harness",
        "max_rounds": 1,
        "max_candidates_per_round": 1,
        "proposer_command": [
            str(PYTHON),
            str(REPO / "tools/propose_method_candidate.py"),
            "{request}",
            "{response}",
            "--base-url",
            BASE_URL,
            "--api-key-file",
            str(KEY_FILE),
            "--model",
            PROPOSER_MODEL,
            "--timeout",
            "1800",
        ],
        "evaluator_command": [
            str(PYTHON),
            str(REPO / "tools/evaluate_continuous_financial_20260911.py"),
            "{request}",
            "{response}",
        ],
        "command_timeout_seconds": 43200,
        "static_checks": [
            [str(PYTHON), "-m", "compileall", "-q", "{source}"]
        ],
        "allowed_operators": ["revision", "recomposition", "synthesis"],
        "allowed_update_scopes": list(SCOPES[scope_name]),
        "kernel_manifest_sha256": kernel_hash,
    }
    loaded = ContinuousEvolutionConfig.from_document(config)
    path = root / "configs" / f"fin15k-{size}-{scope_name}.json"
    atomic_json(path, config)
    atomic_json(
        path.with_suffix(".protocol.json"),
        {
            "schema_version": "fin15k-scaling-cell-v1",
            "size": size,
            "scope": scope_name,
            "allowed_update_scopes": list(SCOPES[scope_name]),
            "config_sha256": loaded.sha256,
            "config_file_sha256": digest(path),
            "initial_evidence_sha256": canonical_sha(
                [
                    {**item, "sha256": digest(Path(item["path"]))}
                    for item in evidence
                ]
            ),
            "test_feedback_used": False,
        },
    )
    return path


def _evolve_cell(root: Path, size: int, scope_name: str) -> dict[str, Any]:
    config = build_config(root, size, scope_name)
    workspace = root / "workspaces" / f"fin15k-{size}-{scope_name}"
    log_path = root / "logs" / f"fin15k-{size}-{scope_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON),
        "-m",
        "spreadsheet_harness.cli",
        "evolve",
        "continuous-run",
        str(config),
        str(workspace),
        "--rounds",
        "1",
    ]
    completed = None
    for attempt in range(1, 4):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== resumable evolution attempt {attempt} ===\n")
            handle.flush()
            completed = subprocess.run(
                command,
                cwd=REPO,
                env=child_environment(),
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=45000,
                check=False,
            )
        if completed.returncode == 0:
            break
        append_event(
            root,
            "evolution.cell.retry",
            size=size,
            scope=scope_name,
            attempt=attempt,
            returncode=completed.returncode,
        )
    if completed is None or completed.returncode:
        raise RuntimeError(f"Evolution failed for {size}/{scope_name}; see {log_path}")
    state = load(workspace / "state.json")
    if not (workspace / "frozen.json").is_file():
        freeze = subprocess.run(
            [
                str(PYTHON),
                "-m",
                "spreadsheet_harness.cli",
                "evolve",
                "continuous-freeze",
                str(config),
                str(workspace),
            ],
            cwd=REPO,
            env=child_environment(),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if freeze.returncode:
            raise RuntimeError(f"Freeze failed for {size}/{scope_name}: {freeze.stderr[-500:]}")
    frozen = load(workspace / "frozen.json")
    result = {
        "size": size,
        "scope": scope_name,
        "status": state["status"],
        "attempted_rounds": state["attempted_rounds"],
        "accepted_rounds": state["accepted_rounds"],
        "initial_revision_sha256": state["history"][0],
        "frozen_revision_sha256": frozen["revision_sha256"],
        "workspace": str(workspace),
        "config": str(config),
    }
    atomic_json(root / "cells" / f"fin15k-{size}-{scope_name}.json", result)
    return result


def evolve_size(root: Path, size: int, *, parallelism: int = 3) -> list[dict[str, Any]]:
    append_event(root, "evolution.size.started", size=size)
    rows: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=min(parallelism, len(SCOPES))) as pool:
        jobs = {
            pool.submit(_evolve_cell, root, size, scope): scope for scope in SCOPES
        }
        for job in futures.as_completed(jobs):
            row = job.result()
            rows.append(row)
            append_event(
                root,
                "evolution.cell.finished",
                size=size,
                scope=jobs[job],
                accepted_rounds=row["accepted_rounds"],
            )
    append_event(root, "evolution.size.completed", size=size)
    return rows


def _frozen_variants(root: Path) -> dict[str, Path]:
    variants: dict[str, Path] = {}
    baseline_revision: str | None = None
    baseline_workspace: Path | None = None
    for size in SIZES:
        for scope in SCOPES:
            cell_path = root / "cells" / f"fin15k-{size}-{scope}.json"
            if not cell_path.is_file():
                raise RuntimeError(f"Evolution cell is not complete: {size}/{scope}")
            cell = load(cell_path)
            workspace = Path(cell["workspace"])
            revision = str(cell["frozen_revision_sha256"])
            variants[f"fin15k-{size}-{scope}"] = workspace / "revisions" / revision
            if baseline_revision is None:
                baseline_revision = str(cell["initial_revision_sha256"])
                baseline_workspace = workspace
            elif baseline_revision != str(cell["initial_revision_sha256"]):
                raise RuntimeError("Nine evolution cells do not share one baseline revision")
    assert baseline_workspace is not None and baseline_revision is not None
    variants = {
        "baseline": baseline_workspace / "revisions" / baseline_revision,
        **variants,
    }
    return variants


def _nonvisual_v2_tasks() -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    for category in ("Debugging", "Financial_Model", "Template"):
        for item in load(V2 / category / "dataset.json"):
            tasks.append(
                {
                    "category": category,
                    "task_id": f"{category}/{item['id']}",
                    "id": str(item["id"]),
                    "source_workbook": str(item.get("source_workbook") or item["id"]),
                }
            )
    if len(tasks) != 297:
        raise RuntimeError(f"Expected 297 nonvisual V2 tasks, found {len(tasks)}")
    return tasks


def _v2_cell(
    root: Path,
    variant: str,
    revision_dir: Path,
    task: Mapping[str, str],
    *,
    evaluation_name: str = "full",
) -> dict[str, Any]:
    output = (
        root
        / "v2-evaluation"
        / evaluation_name
        / variant
        / task["category"]
        / task["id"].replace("/", "_")
    )
    record_path = output / "cell.json"
    previous = None
    if record_path.is_file():
        previous = load(record_path)
        if previous.get("status") == "scored":
            return previous
    output.mkdir(parents=True, exist_ok=True)
    run_dir = output / "run"
    if run_dir.exists():
        attempt = 2
        while (output / f"run-{attempt}").exists():
            attempt += 1
        run_dir = output / f"run-{attempt}"
    artifact = revision_dir / "artifact"
    composition = revision_dir / "composition.json"
    command = [
        str(PYTHON),
        "-m",
        "spreadsheet_harness.cli",
        "benchmark",
        "v2-compare",
        "--dataset",
        str(V2),
        "--official-evaluator",
        str(OFFICIAL_EVALUATOR),
        "--category",
        task["category"],
        "--task-id",
        task["task_id"],
        "--output",
        str(run_dir),
        "--arm",
        "ours",
        "--composition-file",
        f"ours={composition}",
        "--skill-root",
        str(artifact / "skills"),
        "--max-model-calls",
        "50",
        "--max-turns-per-arm",
        "50",
        "--max-total-tokens",
        "unlimited",
        "--max-output-tokens",
        "unlimited",
        "--task-timeout",
        "7200",
        "--request-timeout",
        "600",
        "--request-retries",
        "2",
        "--request-interval-seconds",
        "0.8",
        "--litellm-timeout",
        "600",
        "--base-url",
        BASE_URL,
        "--api-key-file",
        str(KEY_FILE),
        "--model",
        configured_solver_model(),
        "--api-protocol",
        "chat-completions",
        "--reasoning-effort",
        "medium",
        "--temperature",
        "0",
        "--top-p",
        "1",
        "--seed",
        "41",
        "--enable-thinking",
    ]
    with (output / "runner.log").open("a", encoding="utf-8") as handle:
        completed = subprocess.run(
            command,
            cwd=artifact,
            env=child_environment(source=artifact / "src"),
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=7500,
            check=False,
        )
    results_path = run_dir / "results.json"
    raw_results = load(results_path) if results_path.is_file() else []
    rows = [item for item in raw_results if isinstance(item, dict)]
    official = rows[0].get("official_score") if len(rows) == 1 else None
    metrics = {
        key: official.get(key) if isinstance(official, dict) else None
        for key in SCORE_WEIGHTS
    }
    scored = all(isinstance(value, (int, float)) for value in metrics.values())
    weighted = (
        sum(SCORE_WEIGHTS[key] * float(value) for key, value in metrics.items())
        / sum(SCORE_WEIGHTS.values())
        if scored
        else None
    )
    record = {
        "schema_version": "fin15k-scaling-v2-cell-v1",
        "variant": variant,
        "revision_sha256": revision_dir.name,
        "task": dict(task),
        "status": "scored" if scored else "infrastructure",
        "returncode": completed.returncode,
        "metrics": metrics,
        "weighted_score": weighted,
        "result_path": str(results_path) if results_path.is_file() else None,
        "finished_at": now(),
    }
    if previous is not None:
        history = list(previous.get("prior_attempts") or ())
        history.append({key: previous.get(key) for key in ("status", "returncode", "result_path")})
        record["prior_attempts"] = history
    atomic_json(record_path, record)
    return record


def evaluate_v2(
    root: Path,
    *,
    parallelism: int,
    task_limit: int | None = None,
    evaluation_name: str | None = None,
) -> dict[str, Any]:
    verify_protocol(root)
    variants = _frozen_variants(root)
    tasks = _nonvisual_v2_tasks()
    if task_limit is not None:
        if task_limit < 1 or task_limit > len(tasks):
            raise ValueError("v2 task limit must be between 1 and 297")
        # A deterministic stratified prefix is useful for a canary, but a
        # publication claim must use task_limit=None and all 297 tasks.
        by_category: dict[str, list[dict[str, str]]] = defaultdict(list)
        for task in tasks:
            by_category[task["category"]].append(task)
        for category, values in by_category.items():
            random.Random(f"{SEED}:v2:{category}").shuffle(values)
        chosen: list[dict[str, str]] = []
        category_order = ("Debugging", "Financial_Model", "Template")
        while len(chosen) < task_limit:
            for category in category_order:
                if by_category[category] and len(chosen) < task_limit:
                    chosen.append(by_category[category].pop())
        tasks = chosen
    if evaluation_name is None:
        evaluation_name = "full" if task_limit is None else f"canary-{task_limit}"
    jobs = [
        (variant, revision, task)
        for variant, revision in variants.items()
        for task in tasks
    ]
    random.Random(SEED).shuffle(jobs)
    append_event(
        root,
        "v2.started",
        variants=len(variants),
        tasks=len(tasks),
        jobs=len(jobs),
        parallelism=parallelism,
    )
    rows_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    retry_jobs = list(jobs)
    for attempt in range(1, 4):
        if not retry_jobs:
            break
        with futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
            pending = {
                pool.submit(
                    _v2_cell,
                    root,
                    variant,
                    revision,
                    task,
                    evaluation_name=evaluation_name,
                ): (variant, task["task_id"])
                for variant, revision, task in retry_jobs
            }
            for job in futures.as_completed(pending):
                record = job.result()
                key = pending[job]
                rows_by_cell[key] = record
                append_event(
                    root,
                    "v2.cell.finished",
                    variant=key[0],
                    task_id=key[1],
                    status=record["status"],
                    attempt=attempt,
                )
        retry_jobs = [
            item
            for item in jobs
            if rows_by_cell.get((item[0], item[2]["task_id"]), {}).get("status")
            != "scored"
        ]
    report = summarize_v2(
        root,
        variants=tuple(variants),
        expected_tasks=len(tasks),
        evaluation_name=evaluation_name,
    )
    append_event(
        root,
        "v2.completed",
        report=str(root / f"v2-report-{evaluation_name}.json"),
    )
    return report


def _bootstrap_ci(values: Sequence[float], seed: str, samples: int = 4000) -> list[float] | None:
    if not values:
        return None
    generator = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    means = sorted(
        fmean(generator.choice(values) for _ in values) for _ in range(samples)
    )
    return [means[int(0.025 * (samples - 1))], means[int(0.975 * (samples - 1))]]


def summarize_v2(
    root: Path,
    *,
    variants: Sequence[str] | None = None,
    expected_tasks: int = 297,
    evaluation_name: str = "full",
) -> dict[str, Any]:
    records = [
        load(path)
        for path in (root / "v2-evaluation" / evaluation_name).glob("*/*/*/cell.json")
    ]
    if variants is None:
        variants = sorted({str(item["variant"]) for item in records})
    by_variant = {
        variant: {
            item["task"]["task_id"]: item
            for item in records
            if item["variant"] == variant and item["status"] == "scored"
        }
        for variant in variants
    }
    baseline = by_variant.get("baseline", {})
    summaries: list[dict[str, Any]] = []
    for variant in variants:
        scored = by_variant.get(variant, {})
        values = [float(item["weighted_score"]) for item in scored.values()]
        paired_ids = sorted(set(baseline) & set(scored))
        family_delta: dict[str, list[float]] = defaultdict(list)
        for task in paired_ids:
            row = scored[task]
            family_delta[str(row["task"]["source_workbook"])].append(
                float(row["weighted_score"]) - float(baseline[task]["weighted_score"])
            )
        paired_family_deltas = [fmean(value) for value in family_delta.values()]
        category = {}
        for name in ("Debugging", "Financial_Model", "Template"):
            subset = [
                float(item["weighted_score"])
                for item in scored.values()
                if item["task"]["category"] == name
            ]
            category[name] = {
                "scored": len(subset),
                "mean_weighted_score": fmean(subset) if subset else None,
            }
        summaries.append(
            {
                "variant": variant,
                "scored": len(values),
                "expected": expected_tasks,
                "coverage": len(values) / expected_tasks,
                "mean_weighted_score": fmean(values) if values else None,
                "paired_tasks": len(paired_ids),
                "paired_families": len(paired_family_deltas),
                "mean_delta_over_common_baseline": (
                    fmean(paired_family_deltas) if paired_family_deltas else None
                ),
                "delta_95pct_family_bootstrap_ci": _bootstrap_ci(
                    paired_family_deltas, variant
                ),
                "categories": category,
            }
        )
    report = {
        "schema_version": "fin15k-scaling-v2-report-v1",
        "generated_at": now(),
        "evaluation_name": evaluation_name,
        "expected_nonvisual_tasks": expected_tasks,
        "visualization_tasks_excluded": 24,
        "rows": summaries,
        "claim_policy": (
            "Scaling is reported as observed; monotonicity is not assumed or cherry-picked. "
            "Only complete 297-task rows support the full nonvisual V2 claim."
        ),
    }
    report_path = root / f"v2-report-{evaluation_name}.json"
    csv_path = root / f"v2-report-{evaluation_name}.csv"
    atomic_json(report_path, report)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "variant",
                "scored",
                "expected",
                "coverage",
                "mean_weighted_score",
                "paired_tasks",
                "paired_families",
                "mean_delta_over_common_baseline",
                "delta_ci_low",
                "delta_ci_high",
            ]
        )
        for row in summaries:
            interval = row["delta_95pct_family_bootstrap_ci"] or [None, None]
            writer.writerow(
                [
                    row["variant"],
                    row["scored"],
                    row["expected"],
                    row["coverage"],
                    row["mean_weighted_score"],
                    row["paired_tasks"],
                    row["paired_families"],
                    row["mean_delta_over_common_baseline"],
                    interval[0],
                    interval[1],
                ]
            )
    return report


def status(root: Path) -> dict[str, Any]:
    split = load(root / "split-manifest.json") if (root / "split-manifest.json").is_file() else {}
    baseline = list((root / "baseline-trajectories").glob("*/cell.json"))
    baseline_rows = [load(path) for path in baseline]
    cells = list((root / "cells").glob("*.json"))
    v2_canary_cells = list((root / "v2-evaluation/canary-30").glob("*/*/*/cell.json"))
    v2_full_cells = list((root / "v2-evaluation/full").glob("*/*/*/cell.json"))
    payload = {
        "root": str(root),
        "prepared": (root / "protocol.json").is_file(),
        "evidence_target": len(split.get("evidence", [])),
        "baseline_records": len(baseline_rows),
        "baseline_complete": sum(item.get("status") == "complete" for item in baseline_rows),
        "evolution_cells_complete": len(cells),
        "evolution_cells_expected": 9,
        "v2_canary_cells": len(v2_canary_cells),
        "v2_canary_cells_expected": 300,
        "v2_full_cells": len(v2_full_cells),
        "v2_full_cells_expected": 2970,
        "last_status": load(root / "status.json") if (root / "status.json").is_file() else None,
    }
    return payload


def run_all(
    root: Path,
    *,
    baseline_parallelism: int,
    evolution_parallelism: int,
    v2_parallelism: int,
    v2_task_limit: int | None,
) -> None:
    prepare(root)
    for size in SIZES:
        run_baseline_to(root, size, parallelism=baseline_parallelism)
        evolve_size(root, size, parallelism=evolution_parallelism)
        evaluate_v2(root, parallelism=v2_parallelism, task_limit=v2_task_limit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--limit", type=int, choices=SIZES, default=500)
    baseline.add_argument("--parallelism", type=int, default=8)
    evolve = commands.add_parser("evolve")
    evolve.add_argument("--size", type=int, choices=SIZES)
    evolve.add_argument("--parallelism", type=int, default=3)
    v2 = commands.add_parser("v2")
    v2.add_argument("--parallelism", type=int, default=8)
    v2.add_argument("--task-limit", type=int)
    v2.add_argument("--evaluation-name")
    commands.add_parser("status")
    commands.add_parser("summarize")
    run = commands.add_parser("run")
    run.add_argument("--baseline-parallelism", type=int, default=8)
    run.add_argument("--evolution-parallelism", type=int, default=3)
    run.add_argument("--v2-parallelism", type=int, default=8)
    run.add_argument("--v2-task-limit", type=int)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.command == "prepare":
        prepare(root)
    elif args.command == "baseline":
        prepare(root)
        run_baseline_to(root, args.limit, parallelism=args.parallelism)
    elif args.command == "evolve":
        prepare(root)
        sizes = SIZES if args.size is None else (args.size,)
        for size in sizes:
            run_baseline_to(root, size, parallelism=max(1, args.parallelism))
            evolve_size(root, size, parallelism=args.parallelism)
    elif args.command == "v2":
        evaluate_v2(
            root,
            parallelism=args.parallelism,
            task_limit=args.task_limit,
            evaluation_name=args.evaluation_name,
        )
    elif args.command == "status":
        print(json.dumps(status(root), ensure_ascii=False, indent=2))
    elif args.command == "summarize":
        print(json.dumps(summarize_v2(root), ensure_ascii=False, indent=2))
    else:
        run_all(
            root,
            baseline_parallelism=args.baseline_parallelism,
            evolution_parallelism=args.evolution_parallelism,
            v2_parallelism=args.v2_parallelism,
            v2_task_limit=args.v2_task_limit,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
