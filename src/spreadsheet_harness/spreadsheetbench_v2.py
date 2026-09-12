"""Paired SpreadsheetBench 2 execution with the pinned official evaluator."""

from __future__ import annotations

import atexit
import hashlib
import importlib.util
import json
import re
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from .arms import _debugging_detector_hint, postprocess_debugging_artifact, run_arm
from .benchmark import _atomic_write_json, _sha256
from .budget import RunBudget
from .config import ProviderConfig
from .errors import AgentExecutionFailure, HarnessError
from .openpyxl_compat import load_workbook as compat_load_workbook
from .pacing import RelayPacer
from .plugins import (
    ARM_COMPOSITIONS,
    PLUGEOLVE_SEED_COMPOSITION,
    CompositionSpec,
    default_plugin_registry,
)
from .render import (
    recalculate_workbook,
    transplant_ooxml_formula_cached_values,
)
from .session import WorkbookSession
from .skills import SkillRegistry
from .spreadsheetbench_harbor import (
    HARBOR_PROVENANCE_FILENAME,
    normalize_spreadsheetbench_harbor,
)

SPREADSHEETBENCH_V2_DATASET_REVISION = "9dea60025792fbac5928ce9f44812362dccbeecd"
SPREADSHEETBENCH_V2_ARCHIVE_SHA256 = (
    "17147ef9578cd57ce76c9a719d19da7821f3e5cb0d8f776c820f699fdcdb761c"
)
SPREADSHEETBENCH_V2_EVALUATOR_REVISION = "83d415ce87b1d6b8e8eafcc26957f5d13d37210f"
SPREADSHEETBENCH_V2_EVALUATOR_SHA256 = (
    "04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0"
)
SPREADSHEETBENCH_V2_PROTOCOL = "paired_official_evaluator_v4"
SPREADSHEETBENCH_V2_MANIFEST_SCHEMA = 4
SPREADSHEETBENCH_V2_CATEGORIES = (
    "Debugging",
    "Financial_Model",
    "Template",
    "Visualization",
)
DEFAULT_V2_EVALUATOR = (
    Path("benchmarks")
    / "vendor"
    / "spreadsheetbench2-official-83d415c"
    / "evaluation"
    / "evaluation.py"
)


@dataclass(frozen=True)
class SpreadsheetBenchV2Task:
    category: str
    item_id: str
    instruction: str
    category_root: Path
    input_path: Path
    golden_path: Path
    answer_position: str
    source_row: Mapping[str, Any]

    @property
    def task_id(self) -> str:
        return f"{self.category}/{self.item_id}"


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def load_spreadsheetbench_v2_tasks(
    dataset_root: str | Path,
    *,
    categories: Sequence[str] | None = None,
) -> list[SpreadsheetBenchV2Task]:
    source = Path(dataset_root).expanduser().resolve(strict=True)
    root = source
    # The financial calibration releases are Harbor bundles rather than the
    # flat category-root layout used by the official v2 archive.  Normalize
    # them into a temporary canonical root so callers can use the same loader
    # and evaluator without manually unpacking 269/1565 task directories.
    harbor_directory = source.is_dir() and (
        (source / "harbor_bundles").is_dir()
        or any((child / "task.toml").is_file() for child in source.iterdir())
    )
    if source.is_file() or (
        harbor_directory
        and not any(
            (source / category / "dataset.json").is_file()
            for category in SPREADSHEETBENCH_V2_CATEGORIES
        )
    ):
        destination = Path(tempfile.mkdtemp(prefix="spreadsheetbench-harbor-"))
        root = normalize_spreadsheetbench_harbor(source, destination)
        atexit.register(shutil.rmtree, destination, ignore_errors=True)
    if not root.is_dir():
        raise HarnessError(f"SpreadsheetBench 2 dataset root must be a directory: {source}")
    if categories is None:
        available_categories = tuple(
            category
            for category in SPREADSHEETBENCH_V2_CATEGORIES
            if (root / category / "dataset.json").is_file()
        )
        selected_categories = available_categories or SPREADSHEETBENCH_V2_CATEGORIES
    else:
        selected_categories = tuple(categories)
    unknown = sorted(set(selected_categories) - set(SPREADSHEETBENCH_V2_CATEGORIES))
    if unknown:
        raise HarnessError("Unknown SpreadsheetBench 2 categories: " + ", ".join(unknown))
    tasks: list[SpreadsheetBenchV2Task] = []
    for category in selected_categories:
        category_root = (root / category).resolve(strict=True)
        dataset_path = category_root / "dataset.json"
        rows = json.loads(dataset_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise HarnessError(f"SpreadsheetBench 2 {category}/dataset.json must be an array")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise HarnessError(f"SpreadsheetBench 2 {category} row must be an object")
            item_id = str(row["id"])
            if item_id in seen:
                raise HarnessError(f"Duplicate SpreadsheetBench 2 task: {category}/{item_id}")
            seen.add(item_id)
            input_path = (category_root / str(row["spreadsheet_path"])).resolve(strict=True)
            golden_path = (category_root / str(row["golden_response_path"])).resolve(strict=True)
            if not _inside(input_path, category_root) or not _inside(golden_path, category_root):
                raise HarnessError(f"SpreadsheetBench 2 path escaped category root: {category}")
            if not input_path.is_file() or not golden_path.is_file():
                raise HarnessError(
                    f"SpreadsheetBench 2 workbook is not a file: {category}/{item_id}"
                )
            answer_position = str(row.get("answer_position", ""))
            if category != "Visualization" and not answer_position:
                raise HarnessError(
                    f"SpreadsheetBench 2 task has no answer_position: {category}/{item_id}"
                )
            tasks.append(
                SpreadsheetBenchV2Task(
                    category=category,
                    item_id=item_id,
                    instruction=str(row["instruction"]),
                    category_root=category_root,
                    input_path=input_path,
                    golden_path=golden_path,
                    answer_position=answer_position,
                    source_row=dict(row),
                )
            )
    return tasks


def _dataset_identity(source_root: Path) -> dict[str, Any]:
    """Return auditable source identity for official and normalized datasets."""

    if source_root.is_file():
        return {
            "name": source_root.name,
            "revision": "harbor-financial-calibration-v1",
            "format": "harbor-task-bundles-v1",
            "archive_sha256": _sha256(source_root),
        }
    provenance_path = source_root / HARBOR_PROVENANCE_FILENAME
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(f"Invalid Harbor provenance: {provenance_path}") from exc
        required = ("name", "revision", "format", "archive_sha256")
        if not isinstance(provenance, dict) or not set(required).issubset(provenance):
            raise HarnessError(f"Invalid Harbor provenance: {provenance_path}")
        archive_sha256 = provenance["archive_sha256"]
        if archive_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", archive_sha256):
            raise HarnessError(f"Invalid Harbor archive digest: {provenance_path}")
        return {key: provenance[key] for key in required}
    return {
        "name": "KAKA22/SpreadsheetBench-v2",
        "revision": SPREADSHEETBENCH_V2_DATASET_REVISION,
        "format": "spreadsheetbench-v2",
        "archive_sha256": SPREADSHEETBENCH_V2_ARCHIVE_SHA256,
    }


def select_spreadsheetbench_v2_tasks(
    tasks: Sequence[SpreadsheetBenchV2Task],
    requested_ids: Sequence[str],
) -> list[SpreadsheetBenchV2Task]:
    if not requested_ids:
        return list(tasks)
    by_key = {task.task_id: task for task in tasks}
    selected: list[SpreadsheetBenchV2Task] = []
    for requested in requested_ids:
        key = str(requested)
        if "/" not in key:
            matches = [task for task in tasks if task.item_id == key]
            if len(matches) != 1:
                raise HarnessError(
                    f"SpreadsheetBench 2 task ID {key!r} is ambiguous; use CATEGORY/ID"
                )
            selected.append(matches[0])
        else:
            try:
                selected.append(by_key[key])
            except KeyError as exc:
                raise HarnessError(f"Unknown SpreadsheetBench 2 task: {key}") from exc
    if len({task.task_id for task in selected}) != len(selected):
        raise HarnessError("SpreadsheetBench 2 task IDs must be unique")
    return selected


def _load_official_evaluator(path: str | Path) -> tuple[ModuleType, Path, str]:
    evaluator_path = Path(path).expanduser().resolve(strict=True)
    digest = _sha256(evaluator_path)
    if digest != SPREADSHEETBENCH_V2_EVALUATOR_SHA256:
        raise HarnessError(
            "SpreadsheetBench 2 evaluator checksum mismatch: "
            f"expected {SPREADSHEETBENCH_V2_EVALUATOR_SHA256}, got {digest}"
        )
    spec = importlib.util.spec_from_file_location(
        "spreadsheetbench_v2_official_evaluator", evaluator_path
    )
    if spec is None or spec.loader is None:
        raise HarnessError("Unable to load SpreadsheetBench 2 official evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "process_single_item", None)):
        raise HarnessError("SpreadsheetBench 2 evaluator lacks process_single_item")
    if hasattr(module, "openpyxl") and hasattr(module.openpyxl, "load_workbook"):
        module.openpyxl.load_workbook = compat_load_workbook
    return module, evaluator_path, digest


def _official_score(
    evaluator: ModuleType,
    task: SpreadsheetBenchV2Task,
    output_workbook: Path,
    staging_dir: Path,
    *,
    model_calls: int,
) -> dict[str, Any]:
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_output = staging_dir / f"{task.item_id}_output.xlsx"
    shutil.copy2(output_workbook, staged_output)
    result, missing = evaluator.process_single_item(
        dict(task.source_row),
        str(task.category_root),
        str(staging_dir),
        {task.item_id: model_calls},
        task.category,
    )
    if missing:
        raise HarnessError("Official evaluator reported a missing staged output")
    if not isinstance(result, dict):
        raise HarnessError("Official evaluator returned a non-object score")
    return dict(result)


def _parse_answer_position_segment(
    segment: str,
    *,
    default_sheet: str,
) -> tuple[str, str]:
    if "!" in segment:
        raw_sheet, raw_range = segment.split("!", 1)
    else:
        raw_sheet, raw_range = default_sheet, segment
    sheet_name = raw_sheet.strip()
    if len(sheet_name) >= 2 and sheet_name[0] == sheet_name[-1] == "'":
        sheet_name = sheet_name[1:-1].replace("''", "'")
    cell_range = raw_range.strip()
    if len(cell_range) >= 2 and cell_range[0] == cell_range[-1] == "'":
        cell_range = cell_range[1:-1]
    return sheet_name, cell_range


def _restore_unchanged_input_formula_caches(
    task: SpreadsheetBenchV2Task,
    output_workbook: Path,
) -> dict[str, Any]:
    """Restore input caches only where the submitted formula is unchanged.

    LibreOffice can rewrite cached values across an entire iterative workbook
    while materializing one edited formula.  Preserve the source workbook's
    cache for formula-equivalent cells without consulting answer positions or
    the golden workbook.  Formula edits and non-formula cells remain untouched.
    """

    if task.category == "Visualization":
        return {"selected_cells": 0, "restored_cells": 0, "skipped": True}
    try:
        source = compat_load_workbook(task.input_path, data_only=False)
        output = compat_load_workbook(output_workbook, data_only=False)
        selected: dict[str, set[str]] = {}
        try:

            def normalize(value: Any) -> str:
                if isinstance(value, str):
                    normalized = "".join(value.split()).replace("=+", "=").upper()
                    return normalized.replace(
                        "COM.SUN.STAR.SHEET.ADDIN.ANALYSIS.GETXIRR(", "XIRR("
                    ).replace(
                        "COM.SUN.STAR.SHEET.ADDIN.ANALYSIS.GETXNPV(", "XNPV("
                    )
                if hasattr(value, "text"):
                    return "ARRAY:" + normalize(str(getattr(value, "text", "")))
                return repr(value)

            for sheet_name in set(source.sheetnames) & set(output.sheetnames):
                source_sheet = source[sheet_name]
                output_sheet = output[sheet_name]
                for source_cell in getattr(source_sheet, "_cells", {}).values():
                    source_formula = source_cell.value
                    source_text = getattr(source_formula, "text", source_formula)
                    if not isinstance(source_text, str) or not source_text.startswith("="):
                        continue
                    output_formula = output_sheet[source_cell.coordinate].value
                    output_text = getattr(output_formula, "text", output_formula)
                    if not isinstance(output_text, str) or not output_text.startswith("="):
                        continue
                    if normalize(source_formula) == normalize(output_formula):
                        selected.setdefault(sheet_name, set()).add(source_cell.coordinate)
        finally:
            source.close()
            output.close()
        restored = transplant_ooxml_formula_cached_values(
            task.input_path,
            output_workbook,
            exclude_data_table_formulas=False,
            selected_coordinates=selected,
        )
        return {
            "selected_cells": sum(len(cells) for cells in selected.values()),
            "restored_cells": restored,
            "skipped": False,
        }
    except Exception as exc:
        return {
            "selected_cells": 0,
            "restored_cells": 0,
            "skipped": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }


def _composition_record(arm: str, spec: CompositionSpec) -> dict[str, Any]:
    resolved = default_plugin_registry().resolve(spec)
    return {
        "composition_sha256": resolved.sha256,
        "composition": resolved.to_dict(),
    }


def _implementation_record(skills: SkillRegistry) -> dict[str, Any]:
    """Bind a benchmark manifest to the loaded harness source and frozen skill prompts."""

    package_root = Path(__file__).resolve().parent
    source_files = {
        path.relative_to(package_root).as_posix(): _sha256(path)
        for path in sorted(package_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    _, skill_manifest = skills.render_for_prompt()
    frozen_skills = [
        {"name": str(item["name"]), "sha256": str(item["sha256"])} for item in skill_manifest
    ]
    payload: dict[str, Any] = {
        "policy": "python-package-and-frozen-skills-v1",
        "source_files": source_files,
        "skills": frozen_skills,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _implementation_record_valid(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    source_files = record.get("source_files")
    skills = record.get("skills")
    if (
        record.get("policy") != "python-package-and-frozen-skills-v1"
        or not isinstance(source_files, dict)
        or not source_files
        or not isinstance(skills, list)
    ):
        return False
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    if any(
        not isinstance(path, str)
        or not path
        or not isinstance(digest, str)
        or digest_pattern.fullmatch(digest) is None
        for path, digest in source_files.items()
    ):
        return False
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("sha256"), str)
        or digest_pattern.fullmatch(str(item.get("sha256"))) is None
        for item in skills
    ):
        return False
    payload = {key: value for key, value in record.items() if key != "sha256"}
    actual = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return record.get("sha256") == actual


def _manifest_sha256(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _balanced_arm_orders(
    tasks: Sequence[SpreadsheetBenchV2Task],
    arm_order_seed: int,
    arms: Sequence[str] = ("bare", "ours"),
) -> dict[str, tuple[str, ...]]:
    """Counterbalance arm order without using task content or outcomes."""
    selected = tuple(arms)
    if not selected or len(set(selected)) != len(selected):
        raise HarnessError("SpreadsheetBench 2 arms must be non-empty and unique")
    ranked = sorted(
        tasks,
        key=lambda task: hashlib.sha256(
            f"arm-order:{arm_order_seed}:{task.task_id}".encode()
        ).hexdigest(),
    )
    positions = {task.task_id: index for index, task in enumerate(ranked)}
    orders: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        # Rotate a hash-stable permutation so each arm receives each position
        # approximately equally over a pilot/full split.
        keyed = sorted(
            selected,
            key=lambda arm: hashlib.sha256(
                f"arm-order:{arm_order_seed}:{task.task_id}:{arm}".encode()
            ).hexdigest(),
        )
        shift = positions[task.task_id] % len(keyed)
        orders[task.task_id] = tuple(keyed[shift:] + keyed[:shift])
    return orders


def _summarize_results(
    results: Sequence[Mapping[str, Any]],
    *,
    task_count: int,
    arms: Sequence[str],
) -> tuple[dict[str, Any], bool]:
    expected_rows = task_count * len(arms)
    study_complete = len(results) == expected_rows and all(
        row.get("status") == "completed" for row in results
    )
    by_arm: dict[str, Any] = {}
    for arm in arms:
        rows = [row for row in results if row.get("arm") == arm]
        scored = [row for row in rows if row.get("status") == "completed"]
        arm_complete = len(scored) == task_count and len(rows) == task_count
        metrics: dict[str, float | None] = {}
        for metric in ("accuracy", "modification_accuracy", "regression_accuracy"):
            scored_mean = (
                sum(float(row["official_score"][metric]) for row in scored) / len(scored)
                if scored
                else None
            )
            metrics[metric] = scored_mean if arm_complete else None
            metrics[f"scored_{metric}"] = scored_mean
        by_arm[arm] = {
            "expected": task_count,
            "completed": len(scored),
            "errors": len(rows) - len(scored),
            "completion_rate": len(scored) / task_count,
            "passed": sum(bool(row.get("passed")) for row in scored),
            "model_execution_failures": sum(
                row.get("outcome_kind") == "model_execution_failure" for row in scored
            ),
            **metrics,
            "total_tokens": sum(int(row["budget"]["used"]["total_tokens"]) for row in rows),
            "model_calls": sum(int(row["budget"]["used"]["model_calls"]) for row in rows),
        }
    metrics = ("accuracy", "modification_accuracy", "regression_accuracy")
    pairwise_delta: dict[str, Any] = {}
    for left in arms:
        for right in arms:
            if left == right:
                continue
            key = f"{left}-{right}"
            pairwise_delta[key] = {
                metric: (
                    float(by_arm[left][metric]) - float(by_arm[right][metric])
                    if study_complete
                    else None
                )
                for metric in metrics
            }
    # Keep the historical flat bare/ours shape for existing audits and reports;
    # multi-arm studies additionally expose the complete pairwise matrix.
    if tuple(arms) == ("bare", "ours"):
        paired_delta: Any = {
            metric: (
                float(by_arm["ours"][metric]) - float(by_arm["bare"][metric])
                if study_complete
                else None
            )
            for metric in metrics
        }
    else:
        paired_delta = pairwise_delta
    return {
        "arms": by_arm,
        "paired_delta": paired_delta,
        "pairwise_delta": pairwise_delta,
    }, study_complete


def _seal_interrupted_v2_row(
    *,
    output: Path,
    task: SpreadsheetBenchV2Task,
    arm: str,
    manifest_sha256: str,
    model: str,
    max_model_calls: int,
    max_total_tokens: int | None,
    task_timeout_seconds: float,
) -> dict[str, Any]:
    run_dir = output / "runs" / task.category / task.item_id / arm
    trajectory = run_dir / "trajectory.jsonl"
    if not trajectory.is_file():
        raise HarnessError(
            "Cannot seal interrupted SpreadsheetBench 2 arm without a trajectory: "
            f"{task.task_id}::{arm}"
        )
    events: list[dict[str, Any]] = []
    for line in trajectory.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    requested = [event for event in events if event.get("event") == "model.requested"]
    responded = [event for event in events if event.get("event") == "model.responded"]
    known_tokens = sum(
        int(event.get("payload", {}).get("usage", {}).get("total_tokens", 0)) for event in responded
    )
    ambiguous_requests = max(0, len(requested) - len(responded))
    started_at = (
        str(events[0].get("timestamp")) if events else datetime.now(timezone.utc).isoformat()
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    elapsed_seconds = 0.0
    if events:
        elapsed_seconds = max(
            0.0,
            (
                datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
            ).total_seconds(),
        )
    row: dict[str, Any] = {
        "task_id": task.task_id,
        "category": task.category,
        "item_id": task.item_id,
        "arm": arm,
        "protocol": SPREADSHEETBENCH_V2_PROTOCOL,
        "manifest_sha256": manifest_sha256,
        "model": model,
        "run_dir": str(run_dir),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "status": "error",
        "passed": False,
        "outcome_kind": "not_scored",
        "error_type": "InterruptedAmbiguousRequest",
        "error": "Runner stopped after a provider request was sent; the arm was not replayed",
        "ambiguous_inflight_requests": ambiguous_requests,
        "known_completed_model_calls": len(responded),
        "budget": {
            "limit": {
                "model_calls": max_model_calls,
                "total_tokens": max_total_tokens,
                "elapsed_seconds": task_timeout_seconds,
            },
            "used": {
                "model_calls": len(requested),
                "total_tokens": known_tokens,
                "elapsed_seconds": round(elapsed_seconds, 3),
            },
            "termination": {
                "reason": "interrupted_ambiguous_request",
                "message": "Run stopped with an unresolved provider request",
                "stage": "solve",
                "elapsed_seconds": round(elapsed_seconds, 3),
            },
        },
    }
    workbook = run_dir / "artifacts" / "output.xlsx"
    if workbook.is_file():
        row["output_workbook"] = str(workbook)
        row["output_sha256"] = _sha256(workbook)
    return row


def run_spreadsheetbench_v2_comparison(
    *,
    config: ProviderConfig,
    dataset_root: str | Path,
    evaluator_path: str | Path,
    output_dir: str | Path,
    skill_registry: SkillRegistry,
    tasks: Sequence[SpreadsheetBenchV2Task],
    arms: Sequence[str] = ("bare", "ours"),
    composition_overrides: Mapping[str, CompositionSpec] | None = None,
    max_model_calls: int = 50,
    max_turns_per_arm: int = 50,
    max_total_tokens: int | None = 200_000,
    max_output_tokens: int | None = 4_096,
    task_timeout_seconds: float = 1_800,
    request_interval_seconds: float | None = None,
    arm_order_seed: int = 20_260_820,
    resume: bool = False,
    seal_interrupted_current: bool = False,
    visual_generation_only: bool = False,
) -> dict[str, Any]:
    selected_arms = tuple(str(arm) for arm in arms)
    if not tasks or len({task.task_id for task in tasks}) != len(tasks):
        raise HarnessError("SpreadsheetBench 2 tasks must be non-empty and unique")
    known_arms = {
        "bare",
        "ours",
        "native",
        "paper",
        "spreadsheet-rl-minimal",
        "spreadsheet-rl-native",
        "paper-vision",
        "spreadsheet-harness-basic",
        "spreadsheet-harness-financial",
    }
    if not selected_arms or len(set(selected_arms)) != len(selected_arms):
        raise HarnessError("SpreadsheetBench 2 arms must be non-empty and unique")
    unknown_arms = sorted(set(selected_arms) - known_arms)
    if unknown_arms:
        raise HarnessError("Unknown SpreadsheetBench 2 arms: " + ", ".join(unknown_arms))
    if max_model_calls < max_turns_per_arm:
        raise HarnessError("max_model_calls must be at least max_turns_per_arm")
    has_visualization = any(task.category == "Visualization" for task in tasks)
    if has_visualization and not visual_generation_only:
        raise HarnessError(
            "Visualization requires the official Windows Excel/WPS plus glm-4.6v evaluator"
        )
    if visual_generation_only and any(task.category != "Visualization" for task in tasks):
        raise HarnessError("Visual generation mode accepts only Visualization tasks")
    source_root = Path(dataset_root).expanduser().resolve(strict=True)
    if not source_root.is_dir() and not source_root.is_file():
        raise HarnessError(f"SpreadsheetBench 2 dataset path is invalid: {source_root}")
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and not resume:
        raise HarnessError(f"Fresh SpreadsheetBench 2 output already exists: {output}")
    if resume and not output.is_dir():
        raise HarnessError(f"SpreadsheetBench 2 resume output does not exist: {output}")
    if not resume:
        output.mkdir(parents=True)
    if visual_generation_only:
        evaluator = None
        resolved_evaluator_path = Path(evaluator_path).expanduser().resolve(strict=True)
        evaluator_sha256 = _sha256(resolved_evaluator_path)
    else:
        evaluator, resolved_evaluator_path, evaluator_sha256 = _load_official_evaluator(
            evaluator_path
        )
    frozen_skills = skill_registry.freeze()
    overrides = dict(composition_overrides or {})
    specs = {
        arm: overrides.get(
            arm,
            PLUGEOLVE_SEED_COMPOSITION if arm == "ours" else ARM_COMPOSITIONS[arm],
        )
        for arm in selected_arms
    }
    compositions = {arm: _composition_record(arm, specs[arm]) for arm in selected_arms}
    arm_orders = _balanced_arm_orders(tasks, arm_order_seed, selected_arms)
    # Use the task-owned canonical category roots.  This also works when the
    # caller supplied a Harbor archive and the loader materialized it into a
    # temporary canonical root.
    category_roots = {task.category: task.category_root for task in tasks}
    dataset_manifests = {
        category: _sha256(category_root / "dataset.json")
        for category, category_root in sorted(category_roots.items())
    }
    dataset_identity = _dataset_identity(source_root)
    manifest: dict[str, Any] = {
        "schema_version": SPREADSHEETBENCH_V2_MANIFEST_SCHEMA,
        "protocol": SPREADSHEETBENCH_V2_PROTOCOL,
        "dataset": {
            **dataset_identity,
            "dataset_json_sha256": dataset_manifests,
        },
        "official_evaluator": {
            "repository": "RUCKBReasoning/SpreadsheetBench-2",
            "revision": (
                "5c160265aa93c15b38e4034cbf1e09ab498335d9"
                if visual_generation_only
                else SPREADSHEETBENCH_V2_EVALUATOR_REVISION
            ),
            "path": str(resolved_evaluator_path),
            "sha256": evaluator_sha256,
            "entrypoint": (
                "run_visual_vlm_checklist_eval.py"
                if visual_generation_only
                else "process_single_item"
            ),
            "unmodified": True,
            "execution_requirement": (
                "Windows Excel/WPS COM plus glm-4.6v; score > 0.7"
                if visual_generation_only
                else "local value-only evaluator"
            ),
        },
        "evaluation_state": (
            "pending_official_windows_visual_evaluation"
            if visual_generation_only
            else "evaluated_inline"
        ),
        "provider": config.public_dict(),
        "implementation": _implementation_record(frozen_skills),
        "arms": list(selected_arms),
        "compositions": compositions,
        "resources": {
            "max_model_calls": max_model_calls,
            "max_turns_per_arm": max_turns_per_arm,
            "max_total_tokens": max_total_tokens,
            "max_output_tokens_per_call": max_output_tokens,
            "task_timeout_seconds": task_timeout_seconds,
            "request_interval_seconds": (
                config.request_interval_seconds
                if request_interval_seconds is None
                else request_interval_seconds
            ),
            "arm_order_seed": arm_order_seed,
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "category": task.category,
                "item_id": task.item_id,
                "instruction_sha256": hashlib.sha256(task.instruction.encode()).hexdigest(),
                "input_sha256": _sha256(task.input_path),
                "golden_sha256": _sha256(task.golden_path),
                "answer_position_sha256": hashlib.sha256(task.answer_position.encode()).hexdigest(),
                "arm_order": list(arm_orders[task.task_id]),
            }
            for task in tasks
        ],
    }
    manifest_sha256 = _manifest_sha256(manifest)
    manifest["manifest_sha256"] = manifest_sha256
    if resume:
        recorded_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if recorded_manifest != manifest:
            raise HarnessError("SpreadsheetBench 2 resume manifest does not match this command")
        results = json.loads((output / "results.json").read_text(encoding="utf-8"))
        if not isinstance(results, list):
            raise HarnessError("SpreadsheetBench 2 results.json must be an array")
    else:
        _atomic_write_json(output / "manifest.json", manifest)
        results = []

    interval = (
        config.request_interval_seconds
        if request_interval_seconds is None
        else float(request_interval_seconds)
    )
    pacer = RelayPacer(interval)
    work = [(task, arm) for task in tasks for arm in arm_orders[task.task_id]]
    if len(results) > len(work):
        raise HarnessError("SpreadsheetBench 2 results contain too many rows")
    for index, row in enumerate(results):
        task, arm = work[index]
        if (row.get("task_id"), row.get("arm")) != (task.task_id, arm):
            raise HarnessError("SpreadsheetBench 2 results are not a valid execution prefix")
    if resume and len(results) < len(work):
        pending_task, pending_arm = work[len(results)]
        pending_dir = output / "runs" / pending_task.category / pending_task.item_id / pending_arm
        if pending_dir.exists():
            if not seal_interrupted_current:
                raise HarnessError(
                    "Resume found an interrupted arm; pass --seal-interrupted-current "
                    "to preserve it as not_scored without replay"
                )
            results.append(
                _seal_interrupted_v2_row(
                    output=output,
                    task=pending_task,
                    arm=pending_arm,
                    manifest_sha256=manifest_sha256,
                    model=config.model,
                    max_model_calls=max_model_calls,
                    max_total_tokens=max_total_tokens,
                    task_timeout_seconds=task_timeout_seconds,
                )
            )
            _atomic_write_json(output / "results.json", results)
    for task, arm in work[len(results) :]:
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        run_dir = output / "runs" / task.category / task.item_id / arm
        budget = RunBudget(
            max_model_calls=max_model_calls,
            max_total_tokens=max_total_tokens,
            max_elapsed_seconds=task_timeout_seconds,
        )
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "category": task.category,
            "item_id": task.item_id,
            "arm": arm,
            "protocol": SPREADSHEETBENCH_V2_PROTOCOL,
            "manifest_sha256": manifest_sha256,
            "model": config.model,
            "run_dir": str(run_dir),
            "started_at": started_at.isoformat(),
        }
        session: WorkbookSession | None = None
        try:
            session = WorkbookSession.create(
                task.input_path,
                run_dir,
                run_id=f"v2-{task.category}-{task.item_id}-{arm}",
                recorder_secrets=(config.api_key,),
            )
            session.recorder.record(
                "spreadsheetbench_v2.configured",
                {
                    "protocol": SPREADSHEETBENCH_V2_PROTOCOL,
                    "manifest_sha256": manifest_sha256,
                    "task_id": task.task_id,
                    "arm": arm,
                    "official_evaluator_sha256": evaluator_sha256,
                },
            )
            execution_failure: AgentExecutionFailure | None = None
            try:
                agent_result = run_arm(
                    arm=arm,
                    config=config,
                    session=session,
                    skills=frozen_skills,
                    instruction=task.instruction,
                    max_output_tokens=max_output_tokens,
                    max_elapsed_seconds=task_timeout_seconds,
                    budget=budget,
                    pacer=pacer,
                    max_turns_per_arm=max_turns_per_arm,
                    composition=specs[arm],
                    task_category=task.category,
                )
            except AgentExecutionFailure as exc:
                agent_result = exc.agent_result
                if agent_result is None or not callable(getattr(agent_result, "to_dict", None)):
                    raise HarnessError(
                        "Agent execution failure omitted auditable agent evidence"
                    ) from exc
                execution_failure = exc
            # Detection is workbook-local and safe to share with every arm for
            # runner-level policies such as skipping LibreOffice on color-only
            # repairs.  Only the harness arms apply the corresponding mutation
            # postprocessor; ablation arms retain their own agent behavior.
            debugging_hint = _debugging_detector_hint(
                session.paths.input,
                task.instruction,
                task_category=task.category,
            ) if task.category == "Debugging" else task.instruction
            if arm in {"ours", "spreadsheet-harness-basic"}:
                postprocess_debugging_artifact(session, source_name=debugging_hint)
            color_only_debugging = "inconsistent color" in debugging_hint.casefold()
            if visual_generation_only:
                recalculation = {
                    "ok": True,
                    "skipped": True,
                    "reason": "preserve_chart_objects_for_windows_com_evaluation",
                }
            elif color_only_debugging:
                recalculation = {
                    "ok": True,
                    "skipped": True,
                    "reason": "preserve_ooxml_formula_caches_and_font_theme_for_color_only_task",
                }
            else:
                recalculation = recalculate_workbook(
                    session.workbook_path,
                    session.workbook_path,
                    # Keep the untouched input workbook as a semantic seed.
                    # LibreOffice/openpyxl can flatten one-cell CSE formulas
                    # (and circular-formula caches need their pre-edit seed),
                    # so recalculation must be able to restore only unchanged
                    # source formula metadata without overwriting edited cells.
                    cache_seed=session.paths.input,
                    timeout_seconds=min(120.0, task_timeout_seconds),
                )
            # calculateAll() must remain the final value-producing operation.
            # Formula-equivalent downstream cells can legitimately change when
            # an edited assumption changes, so restoring their input caches
            # would silently undo the dependency-consistent recalculation.
            if not visual_generation_only and not color_only_debugging:
                recalculation["unchanged_input_formula_cache_restore"] = (
                    {
                        "selected_cells": 0,
                        "restored_cells": 0,
                        "skipped": True,
                        "reason": "preserve_dependency_consistent_calculate_all_values",
                    }
                )
            used = budget.to_dict()["used"]
            if visual_generation_only:
                visual_dir = output / "visual_outputs" / arm
                visual_dir.mkdir(parents=True, exist_ok=True)
                visual_output = visual_dir / f"1_{task.item_id}_output.xlsx"
                shutil.copy2(session.workbook_path, visual_output)
                row.update(
                    {
                        "status": "generated",
                        "passed": None,
                        "outcome_kind": "pending_official_visual_evaluation",
                        "official_score": None,
                        "official_evaluator_sha256": evaluator_sha256,
                        "agent": agent_result.to_dict(),
                        "recalculation": recalculation,
                        "output_workbook": str(session.workbook_path),
                        "output_sha256": _sha256(session.workbook_path),
                        "visual_output_workbook": str(visual_output),
                        "visual_output_sha256": _sha256(visual_output),
                    }
                )
            else:
                if evaluator is None:
                    raise AssertionError("Numerical evaluation module is unavailable")
                score = _official_score(
                    evaluator,
                    task,
                    session.workbook_path,
                    output / "official_outputs" / arm / task.category,
                    model_calls=int(used["model_calls"]),
                )
                row.update(
                    {
                        "status": "completed",
                        "passed": score.get("accuracy") == 1.0,
                        "outcome_kind": (
                            "model_execution_failure" if execution_failure else "scored"
                        ),
                        "official_score": score,
                        "official_evaluator_sha256": evaluator_sha256,
                        "agent": agent_result.to_dict(),
                        "recalculation": recalculation,
                        "output_workbook": str(session.workbook_path),
                        "output_sha256": _sha256(session.workbook_path),
                    }
                )
            if execution_failure is not None:
                row.update(
                    {
                        "error_type": type(execution_failure).__name__,
                        "error": str(execution_failure).replace(config.api_key, "[REDACTED]"),
                        "model_failure_reason": execution_failure.reason,
                    }
                )
            session.recorder.record(
                (
                    "spreadsheetbench_v2.visual_generated"
                    if visual_generation_only
                    else "spreadsheetbench_v2.evaluated"
                ),
                {
                    "task_id": task.task_id,
                    "arm": arm,
                    "passed": row["passed"],
                    "outcome_kind": row["outcome_kind"],
                    "model_failure_reason": row.get("model_failure_reason"),
                    "official_score": row.get("official_score"),
                    "official_evaluator_sha256": evaluator_sha256,
                },
            )
        except Exception as exc:
            row.update(
                {
                    "status": "error",
                    "passed": False,
                    "outcome_kind": "not_scored",
                    "error_type": type(exc).__name__,
                    "error": str(exc).replace(config.api_key, "[REDACTED]"),
                }
            )
            if session is not None and session.workbook_path.is_file():
                row["output_workbook"] = str(session.workbook_path)
                row["output_sha256"] = _sha256(session.workbook_path)
        finally:
            row["budget"] = budget.to_dict()
            row["elapsed_seconds"] = round(time.monotonic() - started_clock, 3)
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            results.append(row)
            _atomic_write_json(output / "results.json", results)

    if visual_generation_only:
        expected_rows = len(tasks) * len(selected_arms)
        generated_rows = sum(row.get("status") == "generated" for row in results)
        summary_body = {
            "arms": {
                arm: {
                    "expected": len(tasks),
                    "generated": sum(
                        row.get("arm") == arm and row.get("status") == "generated"
                        for row in results
                    ),
                    "errors": sum(
                        row.get("arm") == arm and row.get("status") != "generated"
                        for row in results
                    ),
                }
                for arm in selected_arms
            },
            "generation_complete": generated_rows == expected_rows,
            "pending_official_visual_evaluation": generated_rows,
        }
        study_complete = False
    else:
        summary_body, study_complete = _summarize_results(
            results, task_count=len(tasks), arms=selected_arms
        )
    summary = {
        "schema_version": 1,
        "protocol": SPREADSHEETBENCH_V2_PROTOCOL,
        "manifest_sha256": manifest_sha256,
        "dataset_revision": str(manifest["dataset"]["revision"]),
        "official_evaluator_sha256": evaluator_sha256,
        "task_count": len(tasks),
        **summary_body,
        "study_complete": study_complete,
    }
    _atomic_write_json(output / "summary.json", summary)
    return summary


def audit_spreadsheetbench_v2_comparison(
    results_dir: str | Path,
    *,
    dataset_root: str | Path,
    evaluator_path: str | Path,
) -> dict[str, Any]:
    root = Path(results_dir).expanduser().resolve(strict=True)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((root / "results.json").read_text(encoding="utf-8"))
    evaluator, _, evaluator_sha256 = _load_official_evaluator(evaluator_path)
    reasons: list[str] = []
    if not isinstance(manifest, dict):
        raise ValueError("SpreadsheetBench 2 manifest must be an object")
    if not isinstance(results, list):
        raise ValueError("SpreadsheetBench 2 results must be an array")
    recorded_manifest_sha256 = manifest.pop("manifest_sha256", None)
    actual_manifest_sha256 = _manifest_sha256(manifest)
    manifest["manifest_sha256"] = recorded_manifest_sha256
    if recorded_manifest_sha256 != actual_manifest_sha256:
        reasons.append("manifest_sha256_mismatch")
    manifest_schema = manifest.get("schema_version")
    if manifest_schema == SPREADSHEETBENCH_V2_MANIFEST_SCHEMA:
        if manifest.get("protocol") != SPREADSHEETBENCH_V2_PROTOCOL:
            reasons.append("protocol_mismatch")
        if not _implementation_record_valid(manifest.get("implementation")):
            reasons.append("implementation_manifest_invalid")
    recorded_evaluator = manifest.get("official_evaluator", {})
    if not isinstance(recorded_evaluator, dict):
        reasons.append("official_evaluator_manifest_missing")
    elif recorded_evaluator.get("sha256") != evaluator_sha256:
        reasons.append("official_evaluator_sha256_mismatch")
    manifest_tasks = manifest.get("tasks", [])
    if not isinstance(manifest_tasks, list):
        reasons.append("manifest_tasks_missing")
        manifest_tasks = []
    tasks = {
        task.task_id: task
        for task in load_spreadsheetbench_v2_tasks(
            dataset_root,
            categories=tuple(
                sorted(
                    {
                        str(item["category"])
                        for item in manifest_tasks
                        if isinstance(item, dict) and "category" in item
                    }
                )
            ),
        )
    }
    manifest_task_ids: list[str] = []
    for item in manifest_tasks:
        if not isinstance(item, dict):
            reasons.append("non_object_manifest_task")
            continue
        task_id = str(item.get("task_id"))
        manifest_task_ids.append(task_id)
        task = tasks.get(task_id)
        if task is None:
            reasons.append(f"{task_id}:manifest_task_unavailable")
            continue
        identities = {
            "category": task.category,
            "item_id": task.item_id,
            "instruction_sha256": hashlib.sha256(task.instruction.encode()).hexdigest(),
            "input_sha256": _sha256(task.input_path),
            "golden_sha256": _sha256(task.golden_path),
            "answer_position_sha256": hashlib.sha256(task.answer_position.encode()).hexdigest(),
        }
        for field, expected_value in identities.items():
            if item.get(field) != expected_value:
                reasons.append(f"{task_id}:{field}_mismatch")
    if len(manifest_task_ids) != len(set(manifest_task_ids)):
        reasons.append("duplicate_manifest_task")
    arms = tuple(str(arm) for arm in manifest.get("arms", []))
    if not arms or len(arms) != len(set(arms)):
        reasons.append("invalid_manifest_arms")
    expected = {(task_id, arm) for task_id in manifest_task_ids for arm in arms}
    seen: set[tuple[str, str]] = set()
    completed: set[tuple[str, str]] = set()
    audited_rows: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            reasons.append("non_object_result_row")
            continue
        row_reasons: list[str] = []
        key = (str(row.get("task_id")), str(row.get("arm")))
        if key in seen:
            row_reasons.append("duplicate_arm_task")
        seen.add(key)
        if key not in expected:
            row_reasons.append("unexpected_arm_task")
        task = tasks.get(key[0])
        if task is None:
            row_reasons.append("unknown_task")
        output_path = (
            root / "runs" / task.category / task.item_id / key[1] / "artifacts" / "output.xlsx"
            if task is not None
            else root / ".unavailable"
        )
        fresh_score: dict[str, Any] | None = None
        if row.get("status") == "completed":
            completed.add(key)
            if not output_path.is_file():
                row_reasons.append("output_missing")
            else:
                recorded_output = Path(str(row.get("output_workbook", ""))).resolve()
                if recorded_output != output_path.resolve():
                    row_reasons.append("output_path_mismatch")
                if _sha256(output_path) != row.get("output_sha256"):
                    row_reasons.append("output_sha256_mismatch")
                if task is not None:
                    fresh_score = _official_score(
                        evaluator,
                        task,
                        output_path,
                        root / ".audit" / key[1] / task.category,
                        model_calls=int(
                            row.get("budget", {}).get("used", {}).get("model_calls", 0)
                        ),
                    )
                    if fresh_score != row.get("official_score"):
                        row_reasons.append("official_score_mismatch")
        elif row.get("outcome_kind") != "not_scored":
            row_reasons.append("incomplete_row_not_marked_not_scored")
        if row.get("manifest_sha256") != recorded_manifest_sha256:
            row_reasons.append("row_manifest_mismatch")
        reasons.extend(f"{row.get('task_id')}::{row.get('arm')}:{reason}" for reason in row_reasons)
        audited_rows.append(
            {
                "task_id": row.get("task_id"),
                "arm": row.get("arm"),
                "audit_valid": not row_reasons,
                "reasons": row_reasons,
                "fresh_official_score": fresh_score,
            }
        )
    missing = expected - seen
    if missing:
        reasons.append(f"missing_arm_tasks:{len(missing)}")
    category_counts: dict[str, int] = {}
    for task_id in manifest_task_ids:
        task = tasks.get(task_id)
        if task is not None:
            category_counts[task.category] = category_counts.get(task.category, 0) + 1
    full_category_counts = {
        "Template": 97,
        "Financial_Model": 100,
        "Debugging": 100,
        "Visualization": 24,
    }
    study_complete = expected == completed
    report = {
        "schema_version": 1,
        "protocol": manifest.get("protocol", SPREADSHEETBENCH_V2_PROTOCOL),
        "audit_valid": not reasons,
        "study_complete": study_complete,
        "benchmark_complete": study_complete and category_counts == full_category_counts,
        "reasons": reasons,
        "manifest_sha256": recorded_manifest_sha256,
        "official_evaluator_sha256": evaluator_sha256,
        "category_task_counts": category_counts,
        "expected_arm_tasks": len(expected),
        "completed_arm_tasks": len(expected & completed),
        "not_scored_arm_tasks": len(expected - completed),
        "rows": audited_rows,
    }
    _atomic_write_json(root / "audit.json", report)
    return report


__all__ = [
    "DEFAULT_V2_EVALUATOR",
    "SPREADSHEETBENCH_V2_CATEGORIES",
    "SPREADSHEETBENCH_V2_DATASET_REVISION",
    "SPREADSHEETBENCH_V2_EVALUATOR_REVISION",
    "SPREADSHEETBENCH_V2_EVALUATOR_SHA256",
    "SpreadsheetBenchV2Task",
    "audit_spreadsheetbench_v2_comparison",
    "load_spreadsheetbench_v2_tasks",
    "run_spreadsheetbench_v2_comparison",
    "select_spreadsheetbench_v2_tasks",
]
