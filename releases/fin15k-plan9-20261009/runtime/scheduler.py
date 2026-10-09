#!/usr/bin/env python3
"""Run nine frozen plugin candidates and their common baseline, globally bounded.

Never generates candidates, changes frozen source, modifies the official evaluator,
or retries a normal algorithmic failure to obtain a better score.  Every attempt
is a fresh, artifact-local CLI process and an immutable, separately audited run.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures as futures
import fcntl
import hashlib
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from spreadsheet_harness.config import ProviderConfig, _read_api_key_file
from spreadsheet_harness.continuous_evolution import (
    RevisionStore,
    _kernel_manifest,
    _sha256_json,
    _tree_manifest,
)
from spreadsheet_harness.errors import HarnessError, redact_sensitive_text
from spreadsheet_harness.plugins import load_candidate_plugin_registry
from spreadsheet_harness.skills import SkillRegistry
from spreadsheet_harness.spreadsheetbench_v2 import (
    SPREADSHEETBENCH_V2_EVALUATOR_SHA256,
    SPREADSHEETBENCH_V2_MANIFEST_SCHEMA,
    SPREADSHEETBENCH_V2_PROTOCOL,
    _dataset_identity,
    _implementation_record_valid,
    _manifest_sha256,
    load_spreadsheetbench_v2_tasks,
)

ROOT = Path(__file__).resolve().parents[3]
CATEGORIES = ("Debugging", "Financial_Model", "Template")
COUNTS = {"Debugging": 100, "Financial_Model": 100, "Template": 97}
BENCHMARK_ARM = "spreadsheet-harness-financial"
SCHEMA = "fin15k-plugin-plan9-v2-scheduler-v1"
PROXY_ENV_VARS = frozenset(
    {
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    }
)
GLOBAL_LIMITER_ENV_VARS = frozenset(
    {
        "SHEET_AGENT_GLOBAL_LIMITER_FILE",
        "SHEET_AGENT_GLOBAL_REQUESTS_PER_MINUTE",
        "SHEET_AGENT_GLOBAL_TOKENS_PER_MINUTE",
    }
)
INFRASTRUCTURE_TYPES = frozenset(
    {
        "CodeIsolationError",
        "ScoringInfrastructureError",
        "RecalculationIntegrityError",
        "FileNotFoundError",
        "PermissionError",
        "OSError",
        "TimeoutExpired",
    }
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_identity(pid: int) -> str | None:
    """PID alone is not sufficient when resuming after a scheduler interruption."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rindex(")") + 2 :].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True)
class Arm:
    label: str
    directory: Path
    artifact: Path
    composition_file: Path
    revision: dict[str, Any]


def frozen_arms(
    bundle: Path, evidence_sizes: tuple[int, ...] = (50, 200, 500),
    *, include_baseline: bool = True,
) -> tuple[list[Arm], dict[str, Any]]:
    """Check exact full trees, composition/revision identities and a shared kernel."""
    manifest_path = bundle / "candidate-manifest.json"
    manifest = read_json(manifest_path)
    rows = manifest.get("candidates", [])
    expected = {(size, mode) for size in (50, 200, 500) for mode in ("h-only", "d-only", "joint")}
    if (
        len(rows) != 9
        or {(row.get("evidence_size"), row.get("mechanism")) for row in rows} != expected
    ):
        raise HarnessError("Bundle must contain exactly the frozen 50/200/500 x h/d/joint matrix")
    if not evidence_sizes or not set(evidence_sizes) <= {50, 200, 500}:
        raise HarnessError("Requested evidence sizes must be a nonempty subset of 50/200/500")
    store = RevisionStore(bundle / "materialization")
    baseline_dir = bundle / "baseline"
    baseline_revision = read_json(baseline_dir / "revision.json")
    if baseline_revision.get("revision_sha256") != manifest.get("baseline_revision_sha256"):
        raise HarnessError("Bundle baseline revision differs from its manifest")
    arms: list[Arm] = []
    if include_baseline:
        arms.append(Arm(
            "baseline",
            baseline_dir,
            baseline_dir / "artifact",
            baseline_dir / "composition.json",
            baseline_revision,
        ))
    for row in sorted(
        (item for item in rows if item["evidence_size"] in evidence_sizes),
        key=lambda item: (item["evidence_size"], item["mechanism"]),
    ):
        path = Path(row["candidate_dir"])
        directory = (path if path.is_absolute() else bundle / path).resolve(strict=True)
        if not directory.is_relative_to(bundle) or directory.is_symlink():
            raise HarnessError("Frozen candidate path is outside the bundle")
        revision = read_json(directory / "revision.json")
        if revision.get("revision_sha256") != row.get("revision_sha256"):
            raise HarnessError("Frozen candidate revision differs from its matrix manifest")
        if revision.get("parent_revision_sha256") != baseline_revision["revision_sha256"]:
            raise HarnessError("Every candidate must have the same immediate frozen baseline")
        arms.append(
            Arm(
                f"{row['evidence_size']}-{row['mechanism']}",
                directory,
                directory / "artifact",
                directory / "composition.json",
                revision,
            )
        )
    baseline_kernel = None
    for arm in arms:
        store.verify_materialized_candidate(arm.directory, arm.revision)
        registry = load_candidate_plugin_registry(arm.artifact)
        kernel = _kernel_manifest(arm.artifact, registry)
        if _sha256_json(kernel) != arm.revision.get("kernel_manifest_sha256"):
            raise HarnessError(f"Frozen kernel identity mismatch: {arm.label}")
        if baseline_kernel is None:
            baseline_kernel = kernel
        elif kernel != baseline_kernel:
            raise HarnessError(f"Candidate modified the shared immutable kernel: {arm.label}")
    if not arms:
        raise HarnessError("At least one candidate arm must be selected")
    return arms, {
        "candidate_manifest_sha256": file_sha256(manifest_path),
        "baseline_kernel_sha256": _sha256_json(baseline_kernel),
        "include_baseline": include_baseline,
    }


def interleaved_tasks(tasks: list[Any]) -> list[Any]:
    """Interleave categories without using outcomes or hidden answer values."""
    queues = {
        category: deque(task for task in tasks if task.category == category)
        for category in CATEGORIES
    }
    result = []
    while any(queues.values()):
        for category in CATEGORIES:
            if queues[category]:
                result.append(queues[category].popleft())
    return result


def make_jobs(arms: list[Arm], tasks: list[Any]) -> list[dict[str, str]]:
    jobs = []
    for index, task in enumerate(tasks):
        rotated = arms[index % len(arms) :] + arms[: index % len(arms)]
        for arm in rotated:
            jobs.append(
                {
                    "key": f"{arm.label}/{task.task_id}",
                    "arm": arm.label,
                    "task_id": task.task_id,
                    "category": task.category,
                    "item_id": task.item_id,
                }
            )
    return jobs


def public_provider(args: argparse.Namespace) -> dict[str, Any]:
    parsed = urlsplit(args.base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        raise HarnessError(
            "Provider base URL must be an HTTP(S) endpoint without embedded credentials/query"
        )
    config = ProviderConfig(
        base_url=args.base_url.rstrip("/"),
        api_key="configured-by-owner-only-key-file",
        model=args.model,
        api_protocol=args.api_protocol,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.request_timeout,
        max_retries=args.request_retries,
        store_responses=False,
        request_interval_seconds=args.request_interval_seconds,
        litellm_timeout_seconds=args.litellm_timeout or args.request_timeout,
        temperature=args.temperature,
        top_p=args.top_p,
        seed=args.seed,
        presence_penalty=args.presence_penalty,
        top_k=args.top_k,
        min_p=args.min_p,
        repetition_penalty=args.repetition_penalty,
        enable_thinking=args.enable_thinking,
    )
    return config.public_dict()


def global_limiter_config(args: argparse.Namespace) -> dict[str, Any] | None:
    values = (
        args.global_limiter_file,
        args.global_requests_per_minute,
        args.global_tokens_per_minute,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise HarnessError(
            "--global-limiter-file, --global-requests-per-minute and "
            "--global-tokens-per-minute must be supplied together"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values[1:]
    ):
        raise HarnessError("Global requests/tokens per minute must be positive integers")
    path = Path(args.global_limiter_file).expanduser().resolve()
    if any(path.is_relative_to(root.expanduser().resolve()) for root in (args.bundle, args.dataset)):
        raise HarnessError("Global limiter file must be outside the frozen bundle and dataset")
    if path.exists() and not path.is_file():
        raise HarnessError("Global limiter path must be a regular file, not a directory")
    return {
        "file": str(path),
        "requests_per_minute": args.global_requests_per_minute,
        "tokens_per_minute": args.global_tokens_per_minute,
        "policy": "cross_process_rolling_window_v1",
    }


def verify_global_limiter_support(arms: list[Arm]) -> None:
    """Inspect frozen source without importing or modifying candidate code."""
    for arm in arms:
        path = arm.artifact / "src/spreadsheet_harness/pacing.py"
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise HarnessError(
                f"Frozen artifact lacks readable global limiter support: {arm.label}"
            ) from exc
        limiter = next(
            (
                node for node in module.body
                if isinstance(node, ast.ClassDef) and node.name == "GlobalRelayLimiter"
            ),
            None,
        )
        factory = next(
            (
                node for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == "relay_pacer"
            ),
            None,
        )
        methods = (
            {node.name for node in limiter.body if isinstance(node, ast.FunctionDef)}
            if limiter else set()
        )
        factory_strings = (
            {
                node.value for node in ast.walk(factory)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            if factory else set()
        )
        if (
            not {"__init__", "acquire", "record_usage"}.issubset(methods)
            or not GLOBAL_LIMITER_ENV_VARS.issubset(factory_strings)
        ):
            raise HarnessError(
                f"Frozen artifact lacks explicit cross-process limiter support: {arm.label}"
            )


def prepare_binding(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[Arm], list[dict[str, str]]]:
    global_limiter = global_limiter_config(args)
    evidence_sizes = tuple(args.evidence_size or (50, 200, 500))
    if len(set(evidence_sizes)) != len(evidence_sizes):
        raise HarnessError("--evidence-size values must be unique")
    arms, bundle_identity = frozen_arms(
        args.bundle, evidence_sizes, include_baseline=not args.no_baseline
    )
    if global_limiter is not None:
        verify_global_limiter_support(arms)
    if args.results.is_relative_to(args.bundle) or args.results.is_relative_to(args.dataset):
        raise HarnessError("Results must be outside the frozen bundle and dataset")
    if file_sha256(args.official_evaluator) != SPREADSHEETBENCH_V2_EVALUATOR_SHA256:
        raise HarnessError("Official evaluator checksum mismatch; evaluator may not be modified")
    loaded = load_spreadsheetbench_v2_tasks(args.dataset, categories=CATEGORIES)
    if dict(Counter(task.category for task in loaded)) != COUNTS:
        raise HarnessError(
            "Formal V2 matrix requires Debugging100 / Financial_Model100 / Template97"
        )
    selected = interleaved_tasks(loaded)
    if args.limit is not None:
        selected = selected[: args.limit]
    if not selected:
        raise HarnessError("The selected task set is empty")
    dataset_files = {
        f"{category}/dataset.json": file_sha256(args.dataset / category / "dataset.json")
        for category in CATEGORIES
    }
    for task in loaded:
        for path in (task.input_path, task.golden_path):
            dataset_files[path.relative_to(args.dataset).as_posix()] = file_sha256(path)
    task_identities = {
        task.task_id: {
            "task_id": task.task_id,
            "category": task.category,
            "item_id": task.item_id,
            "instruction_sha256": hashlib.sha256(task.instruction.encode()).hexdigest(),
            "input_sha256": dataset_files[task.input_path.relative_to(args.dataset).as_posix()],
            "golden_sha256": dataset_files[task.golden_path.relative_to(args.dataset).as_posix()],
            "answer_position_sha256": hashlib.sha256(task.answer_position.encode()).hexdigest(),
            "arm_order": [BENCHMARK_ARM],
        }
        for task in selected
    }
    resources = {
        "max_model_calls": args.max_model_calls,
        "max_turns_per_arm": args.max_turns,
        "max_total_tokens": None,
        "max_output_tokens_per_call": args.max_output_tokens,
        "task_timeout_seconds": args.task_timeout,
        "request_interval_seconds": args.request_interval_seconds,
        "arm_order_seed": 20260820,
    }
    binding = {
        "schema_version": SCHEMA,
        "scheduler_source_sha256": file_sha256(Path(__file__)),
        "bundle": str(args.bundle),
        **bundle_identity,
        "dataset": str(args.dataset),
        "dataset_files_sha256": dataset_files,
        "dataset_identity": _dataset_identity(args.dataset),
        "task_identities": task_identities,
        "resources": resources,
        "official_evaluator": str(args.official_evaluator),
        "official_evaluator_sha256": SPREADSHEETBENCH_V2_EVALUATOR_SHA256,
        "python": str(args.python),
        "python_executable_sha256": file_sha256(args.python),
        "provider": public_provider(args),
        "global_limiter": global_limiter,
        "api_key_file": str(args.api_key_file),
        "limits": {
            "workers": args.workers,
            "request_timeout": args.request_timeout,
            "request_retries": args.request_retries,
            "max_model_calls": args.max_model_calls,
            "max_turns_per_arm": args.max_turns,
            "max_total_tokens": None,
            "max_output_tokens": args.max_output_tokens,
            "task_timeout": args.task_timeout,
            "task_attempts_maximum": 2,
            "hard_process_timeout": args.task_timeout + args.hard_timeout_grace,
            "circuit_consecutive": args.circuit_consecutive,
            "circuit_initial_window": 20,
            "circuit_initial_provider_failure_rate": 0.8,
        },
        "task_count_per_arm": len(selected),
        "category_counts": dict(Counter(task.category for task in selected)),
        "full_category_counts": COUNTS,
        "evidence_sizes": list(evidence_sizes),
        "arm_count": len(arms),
        "include_baseline": not args.no_baseline,
        "expected_cells": len(selected) * len(arms),
        "limit": args.limit,
        "task_ids": [task.task_id for task in selected],
        "arms": [
            {
                "label": arm.label,
                "directory": str(arm.directory),
                "artifact": str(arm.artifact),
                "composition_file": str(arm.composition_file),
                "composition": arm.revision["composition"],
                "resolved_composition_sha256": arm.revision["resolved_composition_sha256"],
                "revision_sha256": arm.revision["revision_sha256"],
                "artifact_manifest_sha256": arm.revision["artifact_manifest_sha256"],
                "artifact_manifest": arm.revision["artifact_manifest"],
                "skills": [
                    {"name": skill.name, "sha256": skill.sha256}
                    for skill in SkillRegistry([arm.artifact / "skills"]).discover()
                ],
                "kernel_manifest_sha256": arm.revision["kernel_manifest_sha256"],
            }
            for arm in arms
        ],
        "completion_rule": "status==completed AND outcome_kind==scored; provider salvage is separate",
        "selection_policy": "latest permitted attempt, never best score; algorithm failures are not retried",
        "bytecode_policy": "fresh attempt-private PYTHONPYCACHEPREFIX and PYTHONDONTWRITEBYTECODE=1; never read artifact-local pyc",
        "heldout_feedback_to_proposer": False,
    }
    binding["binding_sha256"] = _sha256_json(binding)
    return binding, arms, make_jobs(arms, selected)


def child_environment(
    artifact: Path, cache: Path, scratch: Path, global_limiter: dict[str, Any] | None = None
) -> dict[str, str]:
    # Keep runtime/system essentials, but remove all experiment flags and ambient credentials.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHEET_", "OPENAI_", "ANTHROPIC_", "AZURE_OPENAI_"))
        and key not in PROXY_ENV_VARS
        and key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
            "PYTHONPYCACHEPREFIX",
        }
    }
    env.update(
        {
            "PYTHONPATH": str(artifact / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "SHEET_AGENT_STORE_RESPONSES": "false",
            "XDG_CACHE_HOME": str(cache),
            "TMPDIR": str(scratch),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPYCACHEPREFIX": str(scratch / "private-pycache"),
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    if global_limiter is not None:
        env.update(
            {
                "SHEET_AGENT_GLOBAL_LIMITER_FILE": global_limiter["file"],
                "SHEET_AGENT_GLOBAL_REQUESTS_PER_MINUTE": str(global_limiter["requests_per_minute"]),
                "SHEET_AGENT_GLOBAL_TOKENS_PER_MINUTE": str(global_limiter["tokens_per_minute"]),
            }
        )
    return env


def command_for(args: argparse.Namespace, arm: Arm, job: dict[str, str], output: Path) -> list[str]:
    command = [
        str(args.python),
        "-m",
        "spreadsheet_harness.cli",
        "benchmark",
        "v2-compare",
        "--dataset",
        str(args.dataset),
        "--official-evaluator",
        str(args.official_evaluator),
        "--category",
        job["category"],
        "--task-id",
        job["task_id"],
        "--output",
        str(output),
        "--arm",
        BENCHMARK_ARM,
        "--composition-file",
        f"{BENCHMARK_ARM}={arm.composition_file}",
        "--skill-root",
        str(arm.artifact / "skills"),
        "--max-model-calls",
        str(args.max_model_calls),
        "--max-turns-per-arm",
        str(args.max_turns),
        "--max-total-tokens",
        "unlimited",
        "--max-output-tokens",
        str(args.max_output_tokens),
        "--task-timeout",
        str(args.task_timeout),
        "--request-timeout",
        str(args.request_timeout),
        "--request-retries",
        str(args.request_retries),
        "--request-interval-seconds",
        str(args.request_interval_seconds),
        "--litellm-timeout",
        str(args.litellm_timeout or args.request_timeout),
        "--base-url",
        args.base_url,
        "--api-key-file",
        str(args.api_key_file),
        "--model",
        args.model,
        "--api-protocol",
        args.api_protocol,
        "--reasoning-effort",
        args.reasoning_effort,
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
    ]
    for name in ("seed", "presence_penalty", "top_k", "min_p", "repetition_penalty"):
        value = getattr(args, name)
        if value is not None:
            command += ["--" + name.replace("_", "-"), str(value)]
    if args.enable_thinking is not None:
        command.append("--enable-thinking" if args.enable_thinking else "--disable-thinking")
    return command


def finite_score(record: dict[str, Any]) -> bool:
    score = record.get("official_score")
    return isinstance(score, dict) and all(
        isinstance(score.get(name), (int, float))
        and not isinstance(score.get(name), bool)
        and math.isfinite(score[name])
        and 0 <= score[name] <= 1
        for name in ("accuracy", "modification_accuracy", "regression_accuracy")
    )


def classify_record(record: dict[str, Any], *, returncode: int = 0) -> dict[str, Any]:
    kind, status = record.get("outcome_kind"), record.get("status")
    error_type = str(record.get("error_type", ""))
    score = record.get("official_score") if finite_score(record) else None
    contradictory_normal = (
        status == "completed"
        and kind == "scored"
        and any(
            record.get(field) not in (None, False, "")
            for field in (
                "artifact_failure_salvaged", "error_type", "error", "model_failure_reason"
            )
        )
    )
    if contradictory_normal:
        # A score and a success label must not hide a provider/agent failure.
        # Quarantine ambiguous records instead of retrying for a better score.
        label, retryable = "integrity_failure", False
    elif status == "completed" and kind == "scored" and score is not None and returncode == 0:
        label, retryable = "normal", False
    elif kind == "scored_after_provider_failure" and score is not None:
        label, retryable = "provider_salvage", True
    elif error_type.startswith("Provider"):
        label, retryable = "provider_failure", True
    elif error_type in INFRASTRUCTURE_TYPES:
        label, retryable = "infrastructure_failure", True
    else:
        # Includes model_execution_failure, scored_after_agent_failure and normal zero scores.
        label, retryable = "algorithm_failure", False
    return {
        "classification": label,
        "normal_completed": label == "normal",
        "retryable": retryable,
        "score": score,
        "passed": bool(record.get("passed")) if label == "normal" else None,
        "status": status,
        "outcome_kind": kind,
        "error_type": error_type or None,
        "error": str(record.get("error", ""))[:4000] or None,
    }


def verify_child_result(
    run_dir: Path, arm: Arm, job: dict[str, str], binding: dict[str, Any]
) -> dict[str, Any]:
    records = read_json(run_dir / "results.json")
    manifest = read_json(run_dir / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("manifest_sha256") != _manifest_sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    ):
        raise HarnessError("Child manifest cryptographic checksum mismatch")
    if (
        manifest.get("schema_version") != SPREADSHEETBENCH_V2_MANIFEST_SCHEMA
        or manifest.get("protocol") != SPREADSHEETBENCH_V2_PROTOCOL
    ):
        raise HarnessError("Child used a different benchmark protocol")
    if not isinstance(records, list) or len(records) != 1:
        raise HarnessError("Single-task child results must contain exactly one row")
    record = records[0]
    if not isinstance(record, dict):
        raise HarnessError("Child result must be an object")
    expected = next(row for row in binding["arms"] if row["label"] == arm.label)
    source_expected = {
        path.removeprefix("src/spreadsheet_harness/"): digest
        for path, digest in expected["artifact_manifest"].items()
        if path.startswith("src/spreadsheet_harness/")
        and (
            path.endswith(".py")
            or path == "src/spreadsheet_harness/generated_plugins/manifest.json"
        )
    }
    implementation = manifest.get("implementation", {})
    if not _implementation_record_valid(implementation):
        raise HarnessError("Child implementation manifest checksum is invalid")
    if implementation.get("source_files") != source_expected:
        raise HarnessError("Child imported a different Python artifact or dynamic manifest")
    if implementation.get("skills") != expected["skills"]:
        raise HarnessError("Child loaded different skill names/contents")
    if manifest.get("provider") != binding["provider"]:
        raise HarnessError("Child provider/generation settings differ from their frozen binding")
    composition = manifest.get("compositions", {}).get(BENCHMARK_ARM, {})
    if composition.get("composition_sha256") != expected["resolved_composition_sha256"]:
        raise HarnessError("Child used a different plugin composition")
    if _sha256_json(composition.get("composition")) != expected["resolved_composition_sha256"]:
        raise HarnessError("Child composition content does not match its checksum")
    evaluator = manifest.get("official_evaluator", {})
    if (
        evaluator.get("sha256") != binding["official_evaluator_sha256"]
        or evaluator.get("unmodified") is not True
        or evaluator.get("path") != binding["official_evaluator"]
    ):
        raise HarnessError("Child official evaluator differs from its frozen binding")
    expected_dataset = {
        **binding["dataset_identity"],
        "dataset_json_sha256": {
            job["category"]: binding["dataset_files_sha256"][f"{job['category']}/dataset.json"]
        },
    }
    if manifest.get("dataset") != expected_dataset:
        raise HarnessError("Child loaded a different category dataset")
    if manifest.get("resources") != binding["resources"]:
        raise HarnessError("Child resource limits differ from their frozen binding")
    if manifest.get("tasks") != [binding["task_identities"][job["task_id"]]] or manifest.get(
        "arms"
    ) != [BENCHMARK_ARM]:
        raise HarnessError(
            "Child task input/golden/instruction identities differ from their frozen binding"
        )
    if record.get("task_id") != job["task_id"] or record.get("arm") != BENCHMARK_ARM:
        raise HarnessError("Child result identity differs from its scheduled cell")
    if record.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise HarnessError("Child result does not bind to its manifest")
    if record.get("status") == "completed":
        workbook = (
            run_dir
            / "runs"
            / job["category"]
            / job["item_id"]
            / BENCHMARK_ARM
            / "artifacts/output.xlsx"
        )
        recorded_workbook = Path(str(record.get("output_workbook", "")))
        if (
            recorded_workbook.resolve() != workbook.resolve()
            or not workbook.is_file()
            or file_sha256(workbook) != record.get("output_sha256")
        ):
            raise HarnessError("Child output workbook path/content checksum mismatch")
    return record


def kill_process_group(process: subprocess.Popen[Any]) -> None:
    """Only the process group created by this scheduler's individual task is targeted."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
    except ProcessLookupError:
        pass


def _redacted_log(pipe: Any, path: Path, secret: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for line in iter(pipe.readline, b""):
            handle.write(
                redact_sensitive_text(line.decode("utf-8", errors="replace"), secrets=(secret,))
            )
            handle.flush()


def execute_attempt(
    args: argparse.Namespace,
    binding: dict[str, Any],
    arm: Arm,
    job: dict[str, str],
    attempt: int,
    secret: str,
    active: dict[str, dict[str, Any]],
    active_lock: threading.Lock,
) -> dict[str, Any]:
    cell = args.results / "cells" / arm.label / job["category"] / job["item_id"]
    directory = cell / f"attempt-{attempt:02d}"
    directory.mkdir(parents=True, exist_ok=False)
    scratch = directory / "tmp"
    scratch.mkdir()
    cache = args.results / "cache" / binding["binding_sha256"] / arm.label
    cache.mkdir(parents=True, exist_ok=True)
    run = directory / "run"
    meta = {
        **job,
        "attempt": attempt,
        "binding_sha256": binding["binding_sha256"],
        "status": "running",
        "started_at": utc_now(),
        "attempt_directory": str(directory),
        "output": str(run),
        "artifact": str(arm.artifact),
        "pid": None,
    }
    atomic_json(directory / "attempt.json", meta)
    started = time.monotonic()
    process = None
    timed_out = False
    try:
        if _tree_manifest(arm.artifact) != arm.revision["artifact_manifest"]:
            raise HarnessError("Frozen artifact changed before task launch")
        if read_json(arm.composition_file) != arm.revision["composition"]:
            raise HarnessError("Frozen composition changed before task launch")
        process = subprocess.Popen(
            command_for(args, arm, job, run),
            cwd=arm.artifact,
            env=child_environment(arm.artifact, cache, scratch, binding.get("global_limiter")),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        meta.update({"pid": process.pid, "process_start_ticks": process_identity(process.pid)})
        atomic_json(directory / "attempt.json", meta)
        with active_lock:
            active[job["key"]] = dict(meta)
        logger = threading.Thread(
            target=_redacted_log,
            args=(process.stdout, directory / "runner.log", secret),
            daemon=True,
        )
        logger.start()
        try:
            returncode = process.wait(timeout=args.task_timeout + args.hard_timeout_grace)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_group(process)
            returncode = process.returncode
        logger.join(timeout=10)
        if logger.is_alive():
            # A descendant can outlive the CLI and retain its stdout. This is
            # still our task-owned process group, never a shared worker group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            logger.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if timed_out:
            verdict = {
                "classification": "infrastructure_failure",
                "normal_completed": False,
                "retryable": True,
                "score": None,
                "error_type": "HardTaskTimeout",
                "error": "Scheduler terminated its task process group at the frozen hard deadline",
            }
        else:
            try:
                record = verify_child_result(run, arm, job, binding)
                verdict = classify_record(record, returncode=returncode)
                verdict["result_sha256"] = file_sha256(run / "results.json")
                verdict["manifest_sha256"] = file_sha256(run / "manifest.json")
            except FileNotFoundError:
                # A bare crash is not proof of provider/infrastructure failure.
                verdict = {
                    "classification": "unclassified_failure",
                    "normal_completed": False,
                    "retryable": False,
                    "score": None,
                    "error_type": "IncompleteChildOutput",
                    "error": f"Child exited {returncode} without a complete audited result",
                }
        meta.update({**verdict, "returncode": returncode, "hard_timeout": timed_out})
    except Exception as exc:
        if process is not None:
            kill_process_group(process)
        meta.update(
            {
                "classification": "infrastructure_failure"
                if isinstance(exc, OSError)
                else "integrity_failure",
                "normal_completed": False,
                "retryable": isinstance(exc, OSError),
                "score": None,
                "error_type": type(exc).__name__,
                "error": redact_sensitive_text(str(exc), secrets=(secret,))[:4000],
            }
        )
    finally:
        with active_lock:
            active.pop(job["key"], None)
    meta.update(
        {
            "status": "finished",
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    if meta.get("error"):
        meta["error"] = redact_sensitive_text(str(meta["error"]), secrets=(secret,))
    atomic_json(directory / "attempt.json", meta)
    return meta


def collect_attempts(
    results: Path, binding: dict[str, Any], jobs: list[dict[str, str]], arms: list[Arm]
) -> dict[str, list[dict[str, Any]]]:
    allowed = {job["key"] for job in jobs}
    by_job = {job["key"]: job for job in jobs}
    by_arm = {arm.label: arm for arm in arms}
    histories: dict[str, list[dict[str, Any]]] = {key: [] for key in allowed}
    for path in sorted((results / "cells").glob("*/*/*/attempt-*/attempt.json")):
        meta = read_json(path)
        key = meta.get("key")
        if key not in allowed or meta.get("binding_sha256") != binding["binding_sha256"]:
            raise HarnessError("Existing attempt has an unknown cell or incompatible binding")
        job = by_job[key]
        arm = by_arm[job["arm"]]
        expected_directory = (
            results
            / "cells"
            / arm.label
            / job["category"]
            / job["item_id"]
            / f"attempt-{meta.get('attempt', 0):02d}"
        )
        if (
            path != expected_directory / "attempt.json"
            or any(meta.get(field) != value for field, value in job.items())
            or meta.get("attempt_directory") != str(expected_directory)
            or meta.get("output") != str(expected_directory / "run")
            or meta.get("artifact") != str(arm.artifact)
        ):
            raise HarnessError("Existing attempt identity/paths differ from their scheduled cell")
        if path.is_symlink() or expected_directory.resolve() != expected_directory:
            raise HarnessError("Attempt histories must not redirect through symlinks")
        if meta.get("status") == "running":
            pid = meta.get("pid")
            if (
                pid
                and meta.get("process_start_ticks")
                and process_identity(pid) == meta["process_start_ticks"]
            ):
                raise HarnessError(
                    "An earlier child is still running; refusing duplicate evaluation"
                )
            if (expected_directory / "run/results.json").is_file():
                record = verify_child_result(expected_directory / "run", arm, job, binding)
                verdict = classify_record(record, returncode=0)
                meta.update(
                    {
                        **verdict,
                        "result_sha256": file_sha256(expected_directory / "run/results.json"),
                        "manifest_sha256": file_sha256(expected_directory / "run/manifest.json"),
                        "returncode": 0,
                        "recovered_verified_result": True,
                    }
                )
            else:
                meta.update(
                    {
                        "classification": "infrastructure_failure",
                        "retryable": True,
                        "normal_completed": False,
                        "score": None,
                        "error_type": "SchedulerInterrupted",
                        "error": "Prior scheduler interrupted before persisting a completed task",
                    }
                )
            meta.update({"status": "finished", "finished_at": utc_now()})
            atomic_json(path, meta)
        elif meta.get("status") != "finished":
            raise HarnessError("Existing attempt has an invalid persistence status")
        if meta.get("result_sha256") or meta.get("manifest_sha256"):
            run_dir = expected_directory / "run"
            if file_sha256(run_dir / "results.json") != meta.get("result_sha256") or file_sha256(
                run_dir / "manifest.json"
            ) != meta.get("manifest_sha256"):
                raise HarnessError("Persisted child result/manifest content changed")
            record = verify_child_result(run_dir, arm, job, binding)
            verdict = classify_record(record, returncode=meta.get("returncode", 0))
            if any(
                meta.get(field) != verdict[field]
                for field in (
                    "classification",
                    "normal_completed",
                    "retryable",
                    "score",
                    "passed",
                    "outcome_kind",
                    "error_type",
                )
            ):
                raise HarnessError(
                    "Persisted attempt classification differs from its audited child result"
                )
        elif meta.get("normal_completed") or meta.get("classification") in {
            "normal",
            "provider_salvage",
            "provider_failure",
        }:
            raise HarnessError(
                "A scored/provider attempt lacks immutable audited child result hashes"
            )
        histories[key].append(meta)
    for history in histories.values():
        if len(history) > 2 or [row.get("attempt") for row in history] != list(
            range(1, len(history) + 1)
        ):
            raise HarnessError("Attempt history exceeds its two-attempt budget or has gaps")
        if len(history) == 2 and not history[0].get("retryable"):
            raise HarnessError("Attempt history illegally retried a normal/algorithmic outcome")
    return histories


def import_finished_attempts(
    source: Path, destination: Path, binding: dict[str, Any], jobs: list[dict[str, str]]
) -> int:
    """Copy only audited finished cells from a prior compatible run.

    Running/incomplete attempts are deliberately excluded and will be scheduled anew.
    The child manifest is re-audited by collect_attempts under the new arm selection.
    """
    allowed = {job["key"]: job for job in jobs}
    copied = 0
    source_cells = source / "cells"
    if not source_cells.is_dir():
        raise HarnessError("--import-finished-results must point to a results directory")
    for path in sorted(source_cells.glob("*/*/*/attempt-*/attempt.json")):
        meta = read_json(path)
        key = meta.get("key")
        if key not in allowed or meta.get("status") != "finished":
            continue
        if meta.get("classification") in {"infrastructure_failure", "provider_failure"}:
            # Preserve audited provider results only; retryable infrastructure failures are
            # intentionally allowed to run again in the new schedule.
            continue
        job = allowed[key]
        attempt = int(meta.get("attempt", 0))
        if attempt != 1:
            raise HarnessError("Imported attempt histories must start at attempt 1")
        target = destination / "cells" / job["arm"] / job["category"] / job["item_id"] / "attempt-01"
        if target.exists():
            raise HarnessError(f"Imported target already exists: {target}")
        shutil.copytree(path.parent, target)
        imported = read_json(target / "attempt.json")
        expected_output = target / "run"
        imported.update({
            "binding_sha256": binding["binding_sha256"],
            "attempt_directory": str(target),
            "output": str(expected_output),
            "artifact": next(
                row["artifact"] for row in binding["arms"] if row["label"] == job["arm"]
            ),
        })
        atomic_json(target / "attempt.json", imported)
        copied += 1
    return copied


def needs_attempt(history: list[dict[str, Any]]) -> bool:
    return not history or len(history) < 2 and history[-1].get("retryable") is True


def score_summary(rows: list[dict[str, Any]], expected: int) -> dict[str, Any]:
    normal = [row for row in rows if row.get("normal_completed")]
    score = sum(row["score"]["accuracy"] for row in normal) / len(normal) if normal else None
    return {
        "expected": expected,
        "attempted_cells": len(rows),
        "normal_completed": len(normal),
        "passed": sum(bool(row.get("passed")) for row in normal),
        "classifications": dict(Counter(row["classification"] for row in rows)),
        "normal_scored_accuracy": score,
        "full_accuracy": score if len(normal) == expected and expected else None,
    }


def paired_summary(
    baseline: dict[str, Any], candidate: dict[str, Any], expected: int
) -> dict[str, Any]:
    common = sorted(set(baseline) & set(candidate))
    pairs = [
        (baseline[task_id], candidate[task_id])
        for task_id in common
        if baseline[task_id].get("normal_completed") and candidate[task_id].get("normal_completed")
    ]
    baseline_score = (
        sum(left["score"]["accuracy"] for left, _ in pairs) / len(pairs) if pairs else None
    )
    candidate_score = (
        sum(right["score"]["accuracy"] for _, right in pairs) / len(pairs) if pairs else None
    )
    return {
        "expected": expected,
        "normal_intersection": len(pairs),
        "coverage": len(pairs) / expected if expected else None,
        "baseline_accuracy": baseline_score,
        "candidate_accuracy": candidate_score,
        "delta": candidate_score - baseline_score if pairs else None,
        "gains": sum(
            right["score"]["accuracy"] > left["score"]["accuracy"] for left, right in pairs
        ),
        "losses": sum(
            right["score"]["accuracy"] < left["score"]["accuracy"] for left, right in pairs
        ),
        "ties": sum(
            right["score"]["accuracy"] == left["score"]["accuracy"] for left, right in pairs
        ),
        "full_paired_complete": len(pairs) == expected and expected > 0,
    }


def progress_document(
    binding: dict[str, Any],
    histories: dict[str, list[dict[str, Any]]],
    active: dict[str, Any],
    status: str,
    pause_reason: str | None = None,
) -> dict[str, Any]:
    latest = [history[-1] for history in histories.values() if history]
    exhausted = [
        history[-1] for history in histories.values() if history and not needs_attempt(history)
    ]
    aggregate = Counter(row["classification"] for row in latest)
    per_arm = {}
    per_category = {
        category: score_summary(
            [row for row in latest if row["category"] == category],
            binding["category_counts"].get(category, 0) * binding["arm_count"],
        )
        for category in CATEGORIES
    }
    rows_by_arm = {}
    for arm in binding["arms"]:
        rows = [row for row in latest if row["arm"] == arm["label"]]
        rows_by_arm[arm["label"]] = {row["task_id"]: row for row in rows}
        per_arm[arm["label"]] = {
            **score_summary(rows, binding["task_count_per_arm"]),
            "categories": {
                category: score_summary(
                    [row for row in rows if row["category"] == category],
                    binding["category_counts"].get(category, 0),
                )
                for category in CATEGORIES
            },
        }
    baseline = rows_by_arm.get("baseline", {})
    paired = {}
    for label, candidate in rows_by_arm.items():
        if label != "baseline":
            paired[label] = {
                **paired_summary(baseline, candidate, binding["task_count_per_arm"]),
                "categories": {
                    category: paired_summary(
                        {key: row for key, row in baseline.items() if row["category"] == category},
                        {key: row for key, row in candidate.items() if row["category"] == category},
                        binding["category_counts"].get(category, 0),
                    )
                    for category in CATEGORIES
                },
            }
    return {
        "schema_version": SCHEMA,
        "binding_sha256": binding["binding_sha256"],
        "global_limiter": binding.get("global_limiter"),
        "pid": os.getpid(),
        "process_start_ticks": process_identity(os.getpid()),
        "updated_at": utc_now(),
        "status": status,
        "pause_reason": pause_reason,
        "expected_cells": binding["expected_cells"],
        "attempted_cells": len(latest),
        "finished_cells": len(exhausted),
        "normal_completed": aggregate["normal"],
        "provider_failure": aggregate["provider_failure"],
        "provider_salvage": aggregate["provider_salvage"],
        "all_classifications": dict(aggregate),
        "attempts": sum(len(history) for history in histories.values()),
        "active_count": len(active),
        "active": list(active.values()),
        "per_arm": per_arm,
        "per_category": per_category,
        "paired_against_baseline": paired,
        "all_normal_complete": aggregate["normal"] == binding["expected_cells"],
        "queue_complete": len(exhausted) == binding["expected_cells"],
    }


def circuit_reason(attempts: list[dict[str, Any]], consecutive_limit: int) -> str | None:
    classifications = [row["classification"] for row in attempts]
    provider = {"provider_failure", "provider_salvage"}
    consecutive = 0
    for classification in reversed(classifications):
        if classification not in provider:
            break
        consecutive += 1
    if consecutive >= consecutive_limit:
        return f"{consecutive} consecutive terminal provider failures; new tasks paused"
    first = classifications[:20]
    if (
        len(first) == 20
        and first.count("normal") == 0
        and sum(value in provider for value in first) / 20 >= 0.8
    ):
        return (
            "First 20 attempts had >=80% provider failures and no normal result; new tasks paused"
        )
    if attempts:
        last = attempts[-1]
        failure_types = {"infrastructure_failure", "integrity_failure", "unclassified_failure"}
        repeated = 0
        for row in reversed(attempts):
            if row["classification"] not in failure_types or row.get("error_type") != last.get(
                "error_type"
            ):
                break
            repeated += 1
        if repeated >= consecutive_limit:
            return f"{repeated} consecutive infrastructure/integrity failures of type {last.get('error_type')}; new tasks paused"
    return None


def verify_end_binding(args: argparse.Namespace, binding: dict[str, Any], arms: list[Arm]) -> None:
    for arm in arms:
        if _tree_manifest(arm.artifact) != arm.revision["artifact_manifest"]:
            raise HarnessError(f"Frozen artifact changed while evaluation was running: {arm.label}")
        if (
            read_json(arm.composition_file) != arm.revision["composition"]
            or read_json(arm.directory / "revision.json") != arm.revision
        ):
            raise HarnessError(
                f"Frozen composition/revision changed while evaluation was running: {arm.label}"
            )
    if (
        file_sha256(args.bundle / "candidate-manifest.json") != binding["candidate_manifest_sha256"]
        or file_sha256(args.official_evaluator) != binding["official_evaluator_sha256"]
    ):
        raise HarnessError("Frozen bundle manifest or official evaluator changed during evaluation")
    for relative, expected in binding["dataset_files_sha256"].items():
        if file_sha256(args.dataset / relative) != expected:
            raise HarnessError("Dataset content changed while evaluation was running")


def run(args: argparse.Namespace) -> int:
    binding, arms, jobs = prepare_binding(args)
    if args.resume:
        if not args.results.is_dir() or read_json(args.results / "run-config.json") != binding:
            raise HarnessError("Resume requires an exactly matching immutable run binding")
    else:
        args.results.mkdir(parents=True, exist_ok=False)
        atomic_json(args.results / "run-config.json", binding)
        atomic_json(args.results / "jobs.json", jobs)
        if args.import_finished_results is not None:
            imported = import_finished_attempts(
                args.import_finished_results.expanduser().resolve(strict=True),
                args.results,
                binding,
                jobs,
            )
            print(json.dumps({"imported_finished_attempts": imported}), flush=True)
    with (args.results / "scheduler.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HarnessError(
                "Another scheduler holds this run; refusing duplicate tasks"
            ) from exc
        histories = collect_attempts(args.results, binding, jobs, arms)
        active: dict[str, dict[str, Any]] = {}
        lock = threading.Lock()
        if args.prepare_only:
            atomic_json(
                args.results / "progress.json",
                progress_document(binding, histories, active, "prepared"),
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "binding_sha256": binding["binding_sha256"],
                        "arms": len(arms),
                        "tasks_per_arm": len(jobs) // len(arms),
                        "cells": len(jobs),
                        "model_calls": 0,
                    }
                ),
                flush=True,
            )
            return 0
        secret = _read_api_key_file(args.api_key_file)
        by_arm = {arm.label: arm for arm in arms}
        queue = deque(job for job in jobs if needs_attempt(histories[job["key"]]))
        circuit_attempts: list[dict[str, Any]] = []
        pause_reason = None
        stop_requested = False

        def stop(_signal: int, _frame: Any) -> None:
            nonlocal stop_requested, pause_reason
            stop_requested = True
            pause_reason = (
                "Scheduler interruption requested; new tasks paused, active tasks draining"
            )

        previous = {
            signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            atomic_json(
                args.results / "progress.json",
                progress_document(binding, histories, active, "running"),
            )
            with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                pending: dict[futures.Future[Any], dict[str, str]] = {}
                while queue or pending:
                    while (
                        queue
                        and len(pending) < args.workers
                        and not pause_reason
                        and not stop_requested
                    ):
                        job = queue.popleft()
                        attempt = len(histories[job["key"]]) + 1
                        pending[
                            pool.submit(
                                execute_attempt,
                                args,
                                binding,
                                by_arm[job["arm"]],
                                job,
                                attempt,
                                secret,
                                active,
                                lock,
                            )
                        ] = job
                    if not pending:
                        break
                    completed, _ = futures.wait(
                        pending, timeout=5, return_when=futures.FIRST_COMPLETED
                    )
                    for future in completed:
                        job = pending.pop(future)
                        meta = future.result()
                        histories[job["key"]].append(meta)
                        circuit_attempts.append(meta)
                        if meta["classification"] == "integrity_failure":
                            pause_reason = "Frozen child binding or execution integrity check failed; new tasks paused"
                        pause_reason = pause_reason or circuit_reason(
                            circuit_attempts, args.circuit_consecutive
                        )
                        if needs_attempt(histories[job["key"]]):
                            queue.append(job)
                        print(
                            json.dumps(
                                {
                                    "arm": job["arm"],
                                    "task_id": job["task_id"],
                                    "attempt": meta["attempt"],
                                    "classification": meta["classification"],
                                    "normal_completed": meta["normal_completed"],
                                    "elapsed_seconds": meta["elapsed_seconds"],
                                    "pause_reason": pause_reason,
                                }
                            ),
                            flush=True,
                        )
                    with lock:
                        snapshot = dict(active)
                    atomic_json(
                        args.results / "progress.json",
                        progress_document(
                            binding,
                            histories,
                            snapshot,
                            "draining" if pause_reason else "running",
                            pause_reason,
                        ),
                    )
            try:
                verify_end_binding(args, binding, arms)
            except HarnessError as exc:
                pause_reason = f"Integrity validation failed: {redact_sensitive_text(str(exc), secrets=(secret,))}"
            final_status = "paused" if pause_reason else "finished"
            report = progress_document(binding, histories, {}, final_status, pause_reason)
            atomic_json(args.results / "progress.json", report)
            atomic_json(args.results / "summary.json", report)
            return 3 if pause_reason else 0 if report["all_normal_complete"] else 2
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--bundle", type=Path)
    result.add_argument("--results", type=Path, required=True)
    result.add_argument(
        "--dataset", type=Path, default=ROOT / "benchmarks/data/spreadsheetbench-v2"
    )
    result.add_argument(
        "--official-evaluator",
        type=Path,
        default=ROOT
        / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py",
    )
    result.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python")
    result.add_argument("--model")
    result.add_argument("--base-url")
    result.add_argument("--api-key-file", type=Path)
    result.add_argument(
        "--api-protocol", choices=("responses", "chat-completions"), default="chat-completions"
    )
    result.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default="medium",
    )
    result.add_argument("--temperature", type=float, default=0.0)
    result.add_argument("--top-p", type=float, default=1.0)
    result.add_argument("--seed", type=int, default=20261009)
    result.add_argument("--presence-penalty", type=float)
    result.add_argument("--top-k", type=int)
    result.add_argument("--min-p", type=float)
    result.add_argument("--repetition-penalty", type=float)
    thinking = result.add_mutually_exclusive_group()
    thinking.add_argument(
        "--enable-thinking", dest="enable_thinking", action="store_true", default=None
    )
    thinking.add_argument("--disable-thinking", dest="enable_thinking", action="store_false")
    result.add_argument("--workers", type=int, default=20)
    result.add_argument("--request-timeout", type=float, default=600)
    result.add_argument("--request-retries", type=int, choices=(0, 1), default=1)
    result.add_argument("--request-interval-seconds", type=float, default=0.0)
    result.add_argument("--global-limiter-file", type=Path, help="Shared quota state outside bundle/dataset")
    result.add_argument("--global-requests-per-minute", type=int, help="Explicit rolling-window request quota")
    result.add_argument("--global-tokens-per-minute", type=int, help="Explicit rolling-window token quota")
    result.add_argument("--litellm-timeout", type=float)
    result.add_argument("--max-model-calls", type=int, default=50)
    result.add_argument("--max-turns", type=int, default=50)
    result.add_argument("--max-output-tokens", type=int, default=32768)
    result.add_argument("--task-timeout", type=float, default=3600)
    result.add_argument("--hard-timeout-grace", type=float, default=180)
    result.add_argument("--circuit-consecutive", type=int, default=10)
    result.add_argument(
        "--evidence-size", type=int, choices=(50, 200, 500), action="append",
        help="Run only candidates generated from these trace sizes; repeat for several sizes",
    )
    result.add_argument(
        "--no-baseline", action="store_true",
        help="Run selected candidate arms only; disables baseline pairing for this result set",
    )
    result.add_argument(
        "--import-finished-results", type=Path,
        help="Import finished compatible cells from an earlier run; running cells are excluded",
    )
    result.add_argument(
        "--limit", type=int, help="Smoke only: use N tasks per arm, category-interleaved"
    )
    result.add_argument("--prepare-only", action="store_true")
    result.add_argument("--resume", action="store_true")
    result.add_argument("--status", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    args.results = args.results.expanduser().resolve()
    if args.status:
        if any(
            getattr(args, name) is not None for name in (
                "global_limiter_file", "global_requests_per_minute", "global_tokens_per_minute"
            )
        ):
            cli.error("--status reads frozen limiter settings; do not supply limiter overrides")
        binding = read_json(args.results / "run-config.json")
        if binding.get("binding_sha256") != _sha256_json(
            {key: value for key, value in binding.items() if key != "binding_sha256"}
        ):
            raise HarnessError("Status run binding checksum mismatch")
        report = read_json(args.results / "progress.json")
        if report.get("binding_sha256") != binding["binding_sha256"]:
            raise HarnessError("Status progress does not match its frozen run binding")
        report["global_limiter"] = binding.get("global_limiter")
        identity = process_identity(report.get("pid", -1))
        report["scheduler_alive"] = identity is not None and identity == report.get(
            "process_start_ticks"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not all((args.bundle, args.model, args.base_url, args.api_key_file)):
        cli.error(
            "--bundle, --model, --base-url and --api-key-file are required except for --status"
        )
    for name in (
        "workers",
        "request_timeout",
        "task_timeout",
        "max_model_calls",
        "max_turns",
        "max_output_tokens",
        "circuit_consecutive",
    ):
        if getattr(args, name) <= 0 or not math.isfinite(getattr(args, name)):
            cli.error(f"--{name.replace('_', '-')} must be positive and finite")
    if args.workers > 20 or args.max_model_calls < args.max_turns:
        cli.error("Global workers must be <=20 and max-model-calls >= max-turns")
    if (
        (args.limit is not None and args.limit <= 0)
        or args.hard_timeout_grace < 0
        or not math.isfinite(args.hard_timeout_grace)
    ):
        cli.error("--limit must be positive and --hard-timeout-grace nonnegative")
    try:
        limiter = global_limiter_config(args)
    except HarnessError as exc:
        cli.error(str(exc))
    if limiter is not None:
        args.global_limiter_file = Path(limiter["file"])
    for name in ("bundle", "dataset", "official_evaluator", "api_key_file"):
        setattr(args, name, getattr(args, name).expanduser().resolve(strict=True))
    # Do not resolve away a venv executable symlink: its lexical location selects the venv.
    args.python = Path(os.path.abspath(args.python.expanduser()))
    if not args.python.is_file():
        cli.error("--python must be an existing absolute interpreter path")
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarnessError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": redact_sensitive_text(str(exc)),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
