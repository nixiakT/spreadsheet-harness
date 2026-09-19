#!/usr/bin/env python3
"""Run the four requested SpreadsheetAgent benchmark groups reproducibly.

The scheduled groups are DeepSeek-V4-Flash and Qwen3-Coder-480B on both
SpreadsheetBench v1 and v2.  The paper's Qwen3-Coder-480B v1 result remains a
historical reference only because this run substitutes the unavailable
GLM-4.5V verifier.  Every task has an isolated harness output, log, state file,
and compact terminal result.

This supervisor never retries a completed model trajectory based on its score
or outcome.  On restart it harvests completed task outputs, resumes only when
no task run directory exists, and otherwise asks the harness to seal an
auditable interrupted trajectory without replaying it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import itertools
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

ROOT = Path(
    os.environ.get("SPREADSHEETAGENT_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
).resolve()
SCRIPT_ROOT = Path(__file__).resolve().parent
IMPORT_ROOT = SCRIPT_ROOT if (SCRIPT_ROOT / "spreadsheet_harness").is_dir() else ROOT / "src"
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

ARM = "spreadsheet-agent"
DEFAULT_API_KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
DEFAULT_BASE_URL = "http://10.130.138.46:8010/v1"
DEFAULT_VISION_MODEL = "dashscope/qwen3-vl-235b-a22b-thinking"
DEFAULT_VISUAL_EVALUATOR = Path(
    "/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py"
)
V1_DATASET = ROOT / "benchmarks/data/spreadsheetbench_912_v0.1"
V2_DATASET = ROOT / "benchmarks/data/spreadsheetbench-v2"
V2_EVALUATOR = (
    ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
)
MODELS = {
    "deepseek": "dashscope/deepseek-v4-flash",
    "qwen480": "dashscope/qwen3-coder-480b-a35b-instruct",
}
GROUPS = (
    {
        "name": "deepseek-v4-flash-v1",
        "version": "v1",
        "model_key": "deepseek",
    },
    {
        "name": "deepseek-v4-flash-v2",
        "version": "v2",
        "model_key": "deepseek",
    },
    {
        "name": "qwen3-coder-480b-v1",
        "version": "v1",
        "model_key": "qwen480",
    },
    {
        "name": "qwen3-coder-480b-v2",
        "version": "v2",
        "model_key": "qwen480",
    },
)
EXPECTED_V1_TASKS = 912
EXPECTED_INVALID_V1_TASK_IDS = ("55224", "55457", "55877")
EXPECTED_V2_CATEGORY_COUNTS = {
    "Debugging": 100,
    "Financial_Model": 100,
    "Template": 97,
    "Visualization": 24,
}
SENSITIVE_ENVIRONMENT_VARIABLES = (
    "SHEET_AGENT_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
)
PAPER_QWEN_V1_REFERENCE = {
    "scheduled_in_current_protocol": True,
    "reference_only": True,
    "reason": "paper_result_is_not_protocol_matched_to_the_current_vision_substitution",
    "source": "Towards Robust Real-World Spreadsheet Understanding with Multi-Agent Multi-Format Reasoning",
    "arxiv_id": "2604.12282",
    "table": 1,
    "model": "Qwen3-Coder-480B-A35B-Instruct",
    "metrics_percent": {"soft_overall": 41.67, "hard_overall": 34.65},
}
_WRITE_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "plan_sha256"}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def string_list_sha256(values: Sequence[str]) -> str:
    """Hash a list using a documented, unambiguous canonical encoding."""

    return hashlib.sha256(
        json.dumps(
            list(values),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def atomic_write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    encoded = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    with _WRITE_LOCK:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_key_file(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve(strict=True)
    metadata = resolved.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"API key file must be a regular file: {resolved}")
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"API key file must not grant group or other permissions: {resolved}")
    return resolved


def _task_slug(group: str, task_id: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._-") or "task"
    digest = hashlib.sha256(f"{group}\0{task_id}".encode()).hexdigest()[:12]
    return f"{readable[:96]}-{digest}"


def task_dir(root: str | Path, job: Mapping[str, Any]) -> Path:
    return Path(root) / "tasks" / str(job["group"]) / str(job["task_slug"])


def _source_inventory(visual_evaluator: Path) -> dict[str, dict[str, str]]:
    inventory: dict[str, dict[str, str]] = {}
    for path in sorted((ROOT / "src/spreadsheet_harness").glob("*.py")):
        key = str(path.relative_to(ROOT))
        inventory[key] = {"path": str(path), "sha256": sha256(path)}
    special = {
        "tools/run_spreadsheetagent_three.py": Path(__file__).resolve(),
        "evaluators/spreadsheetbench_v2.py": V2_EVALUATOR.resolve(strict=True),
        "evaluators/spreadsheetbench_v2_visual.py": visual_evaluator,
    }
    for key, path in special.items():
        inventory[key] = {"path": str(path), "sha256": sha256(path)}
    return inventory


def _git_revision(directory: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _launch_config(args: argparse.Namespace) -> dict[str, Any]:
    api_key_file = validate_key_file(args.api_key_file)
    vision_key_file = validate_key_file(args.vision_api_key_file or api_key_file)
    visual_evaluator = Path(args.visual_evaluator).expanduser().resolve(strict=True)
    if not visual_evaluator.is_file():
        raise RuntimeError(f"Visual evaluator must be a file: {visual_evaluator}")
    if args.workers < 1:
        raise RuntimeError("workers must be positive")
    return {
        "base_url": str(args.base_url).rstrip("/"),
        "api_key_file": str(api_key_file),
        "api_protocol": "chat-completions",
        "reasoning_effort": "medium",
        "temperature": 0.0,
        "top_p": 1.0,
        "enable_thinking": True,
        "vision_base_url": str(args.vision_base_url or args.base_url).rstrip("/"),
        "vision_api_key_file": str(vision_key_file),
        "vision_model": str(args.vision_model),
        "vision_api_protocol": "chat-completions",
        "vision_reasoning_effort": "medium",
        "vision_request_timeout_seconds": float(args.request_timeout),
        "vision_litellm_timeout_seconds": float(args.litellm_timeout),
        "vision_request_retries": int(args.request_retries),
        "vision_temperature": 0.0,
        "vision_top_p": 1.0,
        "vision_enable_thinking": True,
        "max_model_calls": 50,
        "max_turns_per_arm": 50,
        "max_total_tokens": int(args.max_total_tokens),
        "max_output_tokens": int(args.max_output_tokens),
        "task_timeout_seconds": float(args.task_timeout),
        "request_timeout_seconds": float(args.request_timeout),
        "litellm_timeout_seconds": float(args.litellm_timeout),
        "request_retries": int(args.request_retries),
        "visual_evaluator": str(visual_evaluator),
    }


def _load_tasks() -> tuple[Sequence[Any], Sequence[Any]]:
    from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1
    from spreadsheet_harness.spreadsheetbench_v2 import load_spreadsheetbench_v2_tasks

    v1 = load_spreadsheetbench_v1(V1_DATASET)
    v2 = load_spreadsheetbench_v2_tasks(V2_DATASET)
    if len(v1) != EXPECTED_V1_TASKS:
        raise RuntimeError(f"Expected {EXPECTED_V1_TASKS} v1 tasks, found {len(v1)}")
    counts = Counter(task.category for task in v2)
    if dict(counts) != EXPECTED_V2_CATEGORY_COUNTS:
        raise RuntimeError(
            "SpreadsheetBench v2 category counts changed: "
            f"expected {EXPECTED_V2_CATEGORY_COUNTS}, found {dict(counts)}"
        )
    return v1, v2


def initialize(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    plan_path = root / "plan.json"
    launch = _launch_config(args)
    if plan_path.exists():
        plan = load_plan(root)
        if plan.get("launch") != launch:
            raise RuntimeError("Existing plan launch configuration differs; use a new output root")
        return plan

    v1_tasks, v2_tasks = _load_tasks()
    invalid_v1_ids = [str(task.task_id) for task in v1_tasks if not task.instruction.strip()]
    if tuple(invalid_v1_ids) != EXPECTED_INVALID_V1_TASK_IDS:
        raise RuntimeError(
            "Pinned SpreadsheetBench v1 blank-instruction IDs changed: "
            f"expected {list(EXPECTED_INVALID_V1_TASK_IDS)}, found {invalid_v1_ids}"
        )
    invalid_v1_id_set = set(invalid_v1_ids)
    task_sets = {"v1": v1_tasks, "v2": v2_tasks}
    jobs: list[dict[str, Any]] = []
    for group in GROUPS:
        version = str(group["version"])
        for task in task_sets[version]:
            task_id = str(task.task_id)
            category = str(task.category) if version == "v2" else None
            jobs.append(
                {
                    "group": group["name"],
                    "version": version,
                    "model_key": group["model_key"],
                    "model": MODELS[str(group["model_key"])],
                    "task_id": task_id,
                    "task_slug": _task_slug(str(group["name"]), task_id),
                    "category": category,
                    "action": (
                        "dataset_invalid_zero"
                        if version == "v1" and task_id in invalid_v1_id_set
                        else "run"
                    ),
                    "evaluation_mode": (
                        "visual_generation_only"
                        if category == "Visualization"
                        else "official_inline"
                    ),
                    "instruction_sha256": hashlib.sha256(
                        str(task.instruction).encode("utf-8")
                    ).hexdigest(),
                }
            )
    expected_groups = {str(item["name"]) for item in GROUPS}
    if {job["group"] for job in jobs} != expected_groups:
        raise AssertionError("Plan groups do not match the four requested groups")

    visual_evaluator = Path(launch["visual_evaluator"])
    source_inventory = _source_inventory(visual_evaluator)
    dataset_hashes = {
        "v1/dataset.json": sha256(V1_DATASET / "dataset.json"),
        **{
            f"v2/{category}/dataset.json": sha256(V2_DATASET / category / "dataset.json")
            for category in EXPECTED_V2_CATEGORY_COUNTS
        },
    }
    plan: dict[str, Any] = {
        "schema_version": "spreadsheetagent-four-supervisor-v1",
        "created_at": now(),
        "identity": (
            "SpreadsheetAgent clean-room implementation of arXiv:2604.12282; "
            "v2 is a new method-transfer experiment"
        ),
        "paper": {
            "title": (
                "Towards Robust Real-World Spreadsheet Understanding with "
                "Multi-Agent Multi-Format Reasoning"
            ),
            "arxiv_id": "2604.12282",
            "author_repository_revision": ("b4ded1ebdb73ab66acfa8439ad2af54470e317e3"),
            "method": (
                "iterative extraction, vision and LaTeX verification, verifier "
                "feedback/refinement, then spreadsheet solving"
            ),
        },
        "scope": {
            "groups": [dict(item) for item in GROUPS],
            "arm": ARM,
            "qwen_v1_paper_historical_reference": PAPER_QWEN_V1_REFERENCE,
            "v2_nonvisual": ("297 tasks scored inline with the pinned official evaluator"),
            "v2_visualization": (
                "24 artifacts generated only; official Windows Excel/WPS plus "
                "VLM evaluation remains pending"
            ),
        },
        "method_roles": {
            "main_extraction_latex_solver": "group model",
            "vision_verifier": launch["vision_model"],
            "paper_original_main": "Qwen3-Coder-480B-A35B-Instruct",
            "paper_original_visual": "GLM-4.5V",
        },
        "protocol_deviations": [
            "The paper evaluates SpreadsheetBench v1; all v2 results are method transfer.",
            "Qwen3-Coder-480B v1 is rerun because the substituted vision verifier makes the paper Table 1 result non-comparable as a measured result.",
            "The requested 50-turn and reasoning-effort-medium settings replace the paper's nominal 20 tool-call rounds.",
            "The configured vision route replaces GLM-4.5V, which is unavailable on this LiteLLM deployment.",
            "Reasoning effort medium is requested and accepted by the relay; provider-side enforcement is not independently observable.",
            "Linux v2 Visualization runs generate artifacts but do not substitute for the official Windows visual evaluator.",
        ],
        "launch": launch,
        "datasets": {
            "v1": str(V1_DATASET.resolve(strict=True)),
            "v2": str(V2_DATASET.resolve(strict=True)),
            "dataset_json_sha256": dataset_hashes,
            "v1_blank_instructions": {
                "derivation": "not instruction.strip()",
                "action": "dataset_invalid_zero",
                "official_denominator": EXPECTED_V1_TASKS,
                "valid_instruction_count": EXPECTED_V1_TASKS - len(invalid_v1_ids),
                "count": len(invalid_v1_ids),
                "task_ids": invalid_v1_ids,
                "task_ids_sha256": string_list_sha256(invalid_v1_ids),
                "hash_encoding": "compact JSON UTF-8 string array in dataset order",
                "score_policy": (
                    "No inference; Soft=0 and Hard=0, matching the official no-op "
                    "comparison in which all three cases fail"
                ),
            },
        },
        "source": {
            "repository_root": str(ROOT),
            "repository_revision": _git_revision(ROOT),
            "files": source_inventory,
        },
        "selection_and_retry_policy": {
            "frozen_before_inference": True,
            "one_isolated_cli_output_per_task": True,
            "score_based_reruns": False,
            "terminal_model_error_reruns": False,
            "blank_instruction_model_requests": False,
            "provider_request_retries_within_trajectory": launch["request_retries"],
            "interrupted_trajectory": (
                "seal as not_scored when an auditable trajectory exists; never replay"
            ),
        },
        "jobs": jobs,
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    atomic_write_json(plan_path, plan)
    return plan


def load_plan(root: Path) -> dict[str, Any]:
    plan = load_json(root / "plan.json")
    if not isinstance(plan, dict):
        raise RuntimeError("plan.json must contain an object")
    if plan.get("schema_version") != "spreadsheetagent-four-supervisor-v1":
        raise RuntimeError("Unsupported SpreadsheetAgent supervisor plan schema")
    if plan.get("plan_sha256") != canonical_sha256(plan):
        raise RuntimeError("plan.json checksum mismatch")
    groups = {str(job["group"]) for job in plan.get("jobs", [])}
    if groups != {str(item["name"]) for item in GROUPS}:
        raise RuntimeError("plan.json does not contain exactly the requested four groups")
    invalid_record = plan.get("datasets", {}).get("v1_blank_instructions", {})
    invalid_ids = invalid_record.get("task_ids")
    if invalid_ids != list(EXPECTED_INVALID_V1_TASK_IDS):
        raise RuntimeError("plan.json has unexpected v1 blank-instruction task IDs")
    if invalid_record.get("count") != len(EXPECTED_INVALID_V1_TASK_IDS):
        raise RuntimeError("plan.json has an incorrect v1 blank-instruction count")
    if invalid_record.get("task_ids_sha256") != string_list_sha256(invalid_ids):
        raise RuntimeError("plan.json v1 blank-instruction ID checksum mismatch")
    invalid_jobs = [job for job in plan["jobs"] if job.get("action") == "dataset_invalid_zero"]
    invalid_ids_by_group = {
        group["name"]: [
            job.get("task_id") for job in invalid_jobs if job.get("group") == group["name"]
        ]
        for group in GROUPS
        if group["version"] == "v1"
    }
    if any(
        task_ids != list(EXPECTED_INVALID_V1_TASK_IDS)
        for task_ids in invalid_ids_by_group.values()
    ):
        raise RuntimeError("plan.json dataset_invalid_zero jobs do not match frozen IDs")
    if any(job.get("action") not in {"run", "dataset_invalid_zero"} for job in plan["jobs"]):
        raise RuntimeError("plan.json contains an unknown task action")
    return plan


def _verify_frozen_source(frozen: Path, plan: Mapping[str, Any]) -> None:
    identity = load_json(frozen / "identity.json")
    if identity.get("plan_sha256") != plan["plan_sha256"]:
        raise RuntimeError("Frozen source belongs to a different plan")
    for key, record in identity.get("files", {}).items():
        path = frozen / record["frozen_path"]
        if not path.is_file() or sha256(path) != record["sha256"]:
            raise RuntimeError(f"Frozen source checksum mismatch: {key}")


def freeze_worker_sources(root: Path, plan: Mapping[str, Any]) -> Path:
    frozen = root / "frozen-source"
    if frozen.exists():
        _verify_frozen_source(frozen, plan)
        return frozen

    temporary = Path(tempfile.mkdtemp(prefix=".frozen-source-", dir=root))
    try:
        package = temporary / "spreadsheet_harness"
        shutil.copytree(
            ROOT / "src/spreadsheet_harness",
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(Path(__file__).resolve(), temporary / "run_spreadsheetagent_three.py")
        evaluator_dir = temporary / "evaluators"
        evaluator_dir.mkdir()
        shutil.copy2(V2_EVALUATOR, evaluator_dir / "spreadsheetbench_v2.py")
        shutil.copy2(
            Path(plan["launch"]["visual_evaluator"]),
            evaluator_dir / "spreadsheetbench_v2_visual.py",
        )

        frozen_records: dict[str, dict[str, str]] = {}
        for key, source_record in plan["source"]["files"].items():
            if key.startswith("src/spreadsheet_harness/"):
                frozen_path = "spreadsheet_harness/" + Path(key).name
            elif key == "tools/run_spreadsheetagent_three.py":
                frozen_path = "run_spreadsheetagent_three.py"
            elif key == "evaluators/spreadsheetbench_v2.py":
                frozen_path = "evaluators/spreadsheetbench_v2.py"
            elif key == "evaluators/spreadsheetbench_v2_visual.py":
                frozen_path = "evaluators/spreadsheetbench_v2_visual.py"
            else:
                raise RuntimeError(f"Unknown source inventory key: {key}")
            source_path = Path(source_record["path"])
            if sha256(source_path) != source_record["sha256"]:
                raise RuntimeError(f"Source changed after plan creation: {source_path}")
            copy_path = temporary / frozen_path
            if sha256(copy_path) != source_record["sha256"]:
                raise RuntimeError(f"Frozen copy differs from source: {source_path}")
            frozen_records[key] = {
                "frozen_path": frozen_path,
                "sha256": source_record["sha256"],
            }
        atomic_write_json(
            temporary / "identity.json",
            {
                "created_at": now(),
                "plan_sha256": plan["plan_sha256"],
                "files": frozen_records,
            },
        )
        temporary.replace(frozen)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _verify_frozen_source(frozen, plan)
    return frozen


def _frozen_evaluator(root: Path, *, visual: bool) -> Path:
    filename = "spreadsheetbench_v2_visual.py" if visual else "spreadsheetbench_v2.py"
    return root / "frozen-source" / "evaluators" / filename


def build_command(root: Path, plan: Mapping[str, Any], job: Mapping[str, Any]) -> list[str]:
    launch = plan["launch"]
    output = task_dir(root, job) / "run"
    version = str(job["version"])
    visual = job.get("evaluation_mode") == "visual_generation_only"
    if version == "v1":
        command = [
            sys.executable,
            "-m",
            "spreadsheet_harness.cli",
            "benchmark",
            "v1-compare",
            "--dataset",
            str(plan["datasets"]["v1"]),
        ]
    elif visual:
        command = [
            sys.executable,
            "-m",
            "spreadsheet_harness.cli",
            "benchmark",
            "v2-visual-generate",
            "--dataset",
            str(plan["datasets"]["v2"]),
            "--visual-evaluator",
            str(_frozen_evaluator(root, visual=True)),
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "spreadsheet_harness.cli",
            "benchmark",
            "v2-compare",
            "--dataset",
            str(plan["datasets"]["v2"]),
            "--official-evaluator",
            str(_frozen_evaluator(root, visual=False)),
            "--category",
            str(job["category"]),
        ]
    command.extend(
        [
            "--task-id",
            str(job["task_id"]),
            "--arm",
            ARM,
            "--output",
            str(output),
            "--max-model-calls",
            str(launch["max_model_calls"]),
            "--max-turns-per-arm",
            str(launch["max_turns_per_arm"]),
            "--max-total-tokens",
            str(launch["max_total_tokens"]),
            "--max-output-tokens",
            str(launch["max_output_tokens"]),
            "--task-timeout",
            str(launch["task_timeout_seconds"]),
            "--base-url",
            str(launch["base_url"]),
            "--api-key-file",
            str(launch["api_key_file"]),
            "--model",
            str(job["model"]),
            "--api-protocol",
            str(launch["api_protocol"]),
            "--reasoning-effort",
            str(launch["reasoning_effort"]),
            "--request-timeout",
            str(launch["request_timeout_seconds"]),
            "--request-retries",
            str(launch["request_retries"]),
            "--litellm-timeout",
            str(launch["litellm_timeout_seconds"]),
            "--temperature",
            str(launch["temperature"]),
            "--top-p",
            str(launch["top_p"]),
            "--enable-thinking",
            "--vision-base-url",
            str(launch["vision_base_url"]),
            "--vision-api-key-file",
            str(launch["vision_api_key_file"]),
            "--vision-model",
            str(launch["vision_model"]),
            "--vision-api-protocol",
            str(launch["vision_api_protocol"]),
            "--vision-reasoning-effort",
            str(launch["vision_reasoning_effort"]),
            "--vision-request-timeout",
            str(launch["vision_request_timeout_seconds"]),
            "--vision-request-retries",
            str(launch["vision_request_retries"]),
            "--vision-litellm-timeout",
            str(launch["vision_litellm_timeout_seconds"]),
            "--vision-temperature",
            str(launch["vision_temperature"]),
            "--vision-top-p",
            str(launch["vision_top_p"]),
            "--vision-enable-thinking",
        ]
    )
    return command


def _expected_harness_run(output: Path, job: Mapping[str, Any]) -> Path:
    if job["version"] == "v1":
        return output / "runs" / str(job["task_id"]) / ARM
    return output / "runs" / str(job["category"]) / str(job["task_id"]).split("/", 1)[-1] / ARM


def _resume_arguments(output: Path, job: Mapping[str, Any]) -> list[str]:
    manifest = output / "manifest.json"
    results = output / "results.json"
    if not output.exists():
        return []
    if not manifest.is_file():
        if output.is_dir() and not any(output.iterdir()):
            output.rmdir()
            return []
        raise RuntimeError(f"Existing task output lacks a manifest and was preserved: {output}")
    if not results.exists():
        atomic_write_json(results, [])
    rows = load_json(results)
    if not isinstance(rows, list) or len(rows) > 1:
        raise RuntimeError(f"Task results must be an array with at most one row: {results}")
    if rows:
        return ["harvest"]
    pending_run = _expected_harness_run(output, job)
    if not pending_run.exists():
        return ["--resume"]
    if job["version"] == "v2" and not (pending_run / "trajectory.jsonl").is_file():
        raise RuntimeError(
            "Interrupted v2 task has a run directory but no auditable trajectory; "
            f"preserved without replay: {pending_run}"
        )
    return ["--resume", "--seal-interrupted-current"]


def _inside(path: Path, parent: Path) -> bool:
    resolved = path.resolve(strict=True)
    root = parent.resolve(strict=True)
    return resolved == root or root in resolved.parents


def harvest_result(
    root: Path,
    plan: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    returncode: int | None,
) -> dict[str, Any]:
    directory = task_dir(root, job)
    output = directory / "run"
    results_path = output / "results.json"
    manifest_path = output / "manifest.json"
    rows = load_json(results_path)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError(f"Expected exactly one result row: {results_path}")
    row = rows[0]
    if (row.get("task_id"), row.get("arm")) != (job["task_id"], ARM):
        raise RuntimeError(f"Harness result identity mismatch for {job['group']}::{job['task_id']}")
    if row.get("model") != job["model"]:
        raise RuntimeError(f"Harness result model mismatch: {row.get('model')!r}")
    if row.get("vision_model") != plan["launch"]["vision_model"]:
        raise RuntimeError(f"Harness result vision model mismatch: {row.get('vision_model')!r}")
    if not manifest_path.is_file():
        raise RuntimeError(f"Harness result has no manifest: {manifest_path}")

    row_status = str(row.get("status", "unknown"))
    visual = job.get("evaluation_mode") == "visual_generation_only"
    artifact_field = "visual_output_workbook" if visual else "output_workbook"
    artifact = row.get(artifact_field)
    artifact_record: dict[str, Any] | None = None
    if artifact:
        artifact_path = Path(str(artifact))
        if not artifact_path.is_file() or not _inside(artifact_path, output):
            raise RuntimeError(f"Result artifact is missing or outside task output: {artifact}")
        artifact_record = {"path": str(artifact_path), "sha256": sha256(artifact_path)}

    if visual and row_status == "generated" and artifact_record is not None:
        status = "visual_generated"
    elif not visual and row_status == "completed" and artifact_record is not None:
        status = "scored"
    else:
        status = "not_scored"
    metrics: dict[str, Any] = {}
    if job["version"] == "v1":
        metrics = {"soft": row.get("soft"), "hard": row.get("hard")}
    elif isinstance(row.get("official_score"), dict):
        metrics = {
            key: row["official_score"].get(key)
            for key in ("accuracy", "modification_accuracy", "regression_accuracy")
        }
    return {
        "schema_version": "spreadsheetagent-task-result-v1",
        "finished_at": now(),
        "plan_sha256": plan["plan_sha256"],
        "group": job["group"],
        "version": job["version"],
        "model": job["model"],
        "task_id": job["task_id"],
        "category": job.get("category"),
        "evaluation_mode": job["evaluation_mode"],
        "status": status,
        "harness_status": row_status,
        "outcome_kind": row.get("outcome_kind"),
        "error_type": row.get("error_type"),
        "error": row.get("error"),
        "metrics": metrics,
        "budget": row.get("budget"),
        "artifact": artifact_record,
        "harness_output": str(output),
        "harness_results_sha256": sha256(results_path),
        "harness_manifest_sha256": sha256(manifest_path),
        "harness_returncode": returncode,
    }


def _subprocess_environment(frozen: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in SENSITIVE_ENVIRONMENT_VARIABLES:
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(frozen)
    environment["SPREADSHEETAGENT_REPO_ROOT"] = str(ROOT)
    return environment


def _run_process(
    command: Sequence[str],
    *,
    log_path: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> int:
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            list(command),
            cwd=ROOT,
            env=dict(environment),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise RuntimeError(f"Harness process exceeded {timeout:g} seconds") from exc


def work(root: Path, plan: Mapping[str, Any], job: Mapping[str, Any]) -> None:
    directory = task_dir(root, job)
    directory.mkdir(parents=True, exist_ok=True)
    result_path = directory / "result.json"
    if result_path.exists():
        return
    atomic_write_json(
        directory / "state.json",
        {
            "status": "running",
            "started_at": now(),
            "pid": os.getpid(),
            "plan_sha256": plan["plan_sha256"],
            "group": job["group"],
            "task_id": job["task_id"],
        },
    )
    if job.get("action") == "dataset_invalid_zero":
        result = {
            "schema_version": "spreadsheetagent-task-result-v1",
            "finished_at": now(),
            "plan_sha256": plan["plan_sha256"],
            "group": job["group"],
            "version": job["version"],
            "model": job["model"],
            "task_id": job["task_id"],
            "category": job.get("category"),
            "evaluation_mode": job["evaluation_mode"],
            "status": "scored",
            "harness_status": "dataset_invalid_zero",
            "outcome_kind": "dataset_invalid_zero",
            "metrics": {"soft": 0.0, "hard": 0.0},
            "budget": {
                "limit": {
                    "model_calls": plan["launch"]["max_model_calls"],
                    "total_tokens": plan["launch"]["max_total_tokens"],
                    "elapsed_seconds": plan["launch"]["task_timeout_seconds"],
                },
                "used": {"model_calls": 0, "total_tokens": 0, "elapsed_seconds": 0.0},
                "termination": {
                    "reason": "dataset_invalid_zero",
                    "message": "Pinned dataset instruction is blank after strip; no inference",
                },
            },
            "artifact": None,
            "inference_skipped": True,
            "score_policy": (
                "Soft=0 and Hard=0 on the official 912-instruction denominator; "
                "all three official no-op case comparisons fail"
            ),
        }
        atomic_write_json(result_path, result)
        atomic_write_json(
            directory / "state.json",
            {
                "status": result["status"],
                "finished_at": result["finished_at"],
                "pid": os.getpid(),
                "plan_sha256": plan["plan_sha256"],
                "group": job["group"],
                "task_id": job["task_id"],
                "outcome_kind": result["outcome_kind"],
            },
        )
        return
    try:
        output = directory / "run"
        resume = _resume_arguments(output, job)
        returncode: int | None = None
        if resume != ["harvest"]:
            command = build_command(root, plan, job) + resume
            atomic_write_json(
                directory / "invocation.json",
                {
                    "created_at": now(),
                    "command": command,
                    "credential_transport": "owner_only_key_files",
                    "resume_mode": resume or ["fresh"],
                },
            )
            timeout = float(plan["launch"]["task_timeout_seconds"]) + 900.0
            returncode = _run_process(
                command,
                log_path=directory / "runner.log",
                environment=_subprocess_environment(root / "frozen-source"),
                timeout=timeout,
            )
        result = harvest_result(root, plan, job, returncode=returncode)
    except Exception as exc:
        result = {
            "schema_version": "spreadsheetagent-task-result-v1",
            "finished_at": now(),
            "plan_sha256": plan["plan_sha256"],
            "group": job["group"],
            "version": job["version"],
            "model": job["model"],
            "task_id": job["task_id"],
            "category": job.get("category"),
            "evaluation_mode": job["evaluation_mode"],
            "status": "worker_error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "harness_output": str(directory / "run"),
        }
    atomic_write_json(result_path, result)
    atomic_write_json(
        directory / "state.json",
        {
            "status": result["status"],
            "finished_at": result["finished_at"],
            "pid": os.getpid(),
            "plan_sha256": plan["plan_sha256"],
            "group": job["group"],
            "task_id": job["task_id"],
        },
    )


def _metric_summary(rows: Sequence[Mapping[str, Any]], key: str, expected: int) -> dict[str, Any]:
    values = [
        float(row["metrics"][key])
        for row in rows
        if row.get("status") == "scored"
        and isinstance(row.get("metrics"), dict)
        and isinstance(row["metrics"].get(key), (int, float))
    ]
    return {
        "scored_mean": fmean(values) if values else None,
        "scored_denominator": len(values),
        "official_full_denominator_mean": fmean(values) if len(values) == expected else None,
    }


def _group_report(
    group: str,
    jobs: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_task = {str(row["task_id"]): row for row in rows}
    status_counts = Counter(str(row.get("status", "unknown")) for row in rows)
    base: dict[str, Any] = {
        "group": group,
        "model": jobs[0]["model"],
        "version": jobs[0]["version"],
        "expected_tasks": len(jobs),
        "terminal_tasks": len(rows),
        "pending_tasks": len(jobs) - len(rows),
        "status_counts": dict(status_counts),
        "all_tasks_terminal": len(rows) == len(jobs),
        "missing_task_ids": [job["task_id"] for job in jobs if job["task_id"] not in by_task],
    }
    if jobs[0]["version"] == "v1":
        invalid_jobs = [job for job in jobs if job.get("action") == "dataset_invalid_zero"]
        invalid_rows = [
            row for row in rows if row.get("outcome_kind") == "dataset_invalid_zero"
        ]
        valid_jobs = [job for job in jobs if job.get("action") != "dataset_invalid_zero"]
        valid_rows = [
            row for row in rows if row.get("outcome_kind") != "dataset_invalid_zero"
        ]
        base.update(
            {
                "study_complete": status_counts["scored"] == len(jobs),
                "official_denominator": len(jobs),
                "valid_instruction_count": len(valid_jobs),
                "dataset_invalid_zero": {
                    "expected": len(invalid_jobs),
                    "terminal": len(invalid_rows),
                    "task_ids": [job["task_id"] for job in invalid_jobs],
                    "all_inference_skipped": len(invalid_rows) == len(invalid_jobs)
                    and all(row.get("inference_skipped") is True for row in invalid_rows),
                },
                "metrics": {
                    "official_912_denominator": {
                        key: _metric_summary(rows, key, len(jobs))
                        for key in ("soft", "hard")
                    },
                    "valid_909_instructions": {
                        key: _metric_summary(valid_rows, key, len(valid_jobs))
                        for key in ("soft", "hard")
                    },
                },
            }
        )
        return base

    numerical_jobs = [job for job in jobs if job["evaluation_mode"] == "official_inline"]
    visual_jobs = [job for job in jobs if job["evaluation_mode"] == "visual_generation_only"]
    numerical_rows = [row for row in rows if row.get("evaluation_mode") == "official_inline"]
    visual_rows = [row for row in rows if row.get("evaluation_mode") == "visual_generation_only"]
    category_reports: dict[str, Any] = {}
    for category in EXPECTED_V2_CATEGORY_COUNTS:
        category_jobs = [job for job in jobs if job.get("category") == category]
        category_rows = [row for row in rows if row.get("category") == category]
        category_reports[category] = {
            "expected": len(category_jobs),
            "terminal": len(category_rows),
            "scored": sum(row.get("status") == "scored" for row in category_rows),
            "visual_generated": sum(
                row.get("status") == "visual_generated" for row in category_rows
            ),
        }
    base.update(
        {
            "method_transfer_from_paper_v1": True,
            "nonvisual_official_evaluation": {
                "expected": len(numerical_jobs),
                "scored": sum(row.get("status") == "scored" for row in numerical_rows),
                "complete": sum(row.get("status") == "scored" for row in numerical_rows)
                == len(numerical_jobs),
                "metrics": {
                    key: _metric_summary(numerical_rows, key, len(numerical_jobs))
                    for key in (
                        "accuracy",
                        "modification_accuracy",
                        "regression_accuracy",
                    )
                },
            },
            "visualization_generation": {
                "expected": len(visual_jobs),
                "generated": sum(row.get("status") == "visual_generated" for row in visual_rows),
                "complete": sum(row.get("status") == "visual_generated" for row in visual_rows)
                == len(visual_jobs),
                "evaluation_state": "pending_official_windows_visual_evaluation",
            },
            "category_counts": category_reports,
            "full_v2_official_evaluation_complete": False,
        }
    )
    base["local_generation_and_nonvisual_scoring_complete"] = (
        base["nonvisual_official_evaluation"]["complete"]
        and base["visualization_generation"]["complete"]
    )
    return base


def report(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    progress_jobs: list[dict[str, Any]] = []
    for job in plan["jobs"]:
        result_path = task_dir(root, job) / "result.json"
        if result_path.is_file():
            row = load_json(result_path)
            if row.get("plan_sha256") != plan["plan_sha256"]:
                raise RuntimeError(f"Task result belongs to another plan: {result_path}")
            rows.append(row)
            status = row.get("status", "unknown")
        else:
            status = "pending"
        progress_jobs.append(
            {
                "group": job["group"],
                "task_id": job["task_id"],
                "category": job.get("category"),
                "status": status,
                "result": str(result_path),
            }
        )

    groups: dict[str, Any] = {}
    for group in (str(item["name"]) for item in GROUPS):
        group_jobs = [job for job in plan["jobs"] if job["group"] == group]
        group_rows = [row for row in rows if row["group"] == group]
        groups[group] = _group_report(group, group_jobs, group_rows)
        atomic_write_json(root / "reports" / group / "summary.json", groups[group])

    v1_complete = all(
        groups[group]["study_complete"]
        for group in ("deepseek-v4-flash-v1", "qwen3-coder-480b-v1")
    )
    v2_local_complete = all(
        groups[group]["local_generation_and_nonvisual_scoring_complete"]
        for group in ("deepseek-v4-flash-v2", "qwen3-coder-480b-v2")
    )
    summary = {
        "schema_version": "spreadsheetagent-four-summary-v1",
        "generated_at": now(),
        "plan_sha256": plan["plan_sha256"],
        "paper_qwen_v1_reference": PAPER_QWEN_V1_REFERENCE,
        "groups": groups,
        "terminal_jobs": len(rows),
        "expected_jobs": len(plan["jobs"]),
        "all_jobs_terminal": len(rows) == len(plan["jobs"]),
        "requested_local_work_complete": v1_complete and v2_local_complete,
        "full_v2_official_evaluation_complete": False,
        "remaining_external_step": (
            "Evaluate each v2 group's 24 Visualization artifacts with the official "
            "Windows Excel/WPS plus VLM evaluator."
        ),
    }
    atomic_write_json(
        root / "progress.json",
        {
            "schema_version": "spreadsheetagent-four-progress-v1",
            "generated_at": summary["generated_at"],
            "plan_sha256": plan["plan_sha256"],
            "terminal_jobs": len(rows),
            "expected_jobs": len(plan["jobs"]),
            "status_counts": dict(Counter(str(item["status"]) for item in progress_jobs)),
            "jobs": progress_jobs,
        },
    )
    atomic_write_json(root / "summary.json", summary)
    return summary


def run(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    selected_groups = set(args.only_group or (str(item["name"]) for item in GROUPS))
    known_groups = {str(item["name"]) for item in GROUPS}
    unknown_groups = sorted(selected_groups - known_groups)
    if unknown_groups:
        raise RuntimeError("Unknown requested groups: " + ", ".join(unknown_groups))
    with ExitStack() as stack:
        for group in sorted(selected_groups):
            lock = stack.enter_context(
                (root / f"supervisor-{group}.lock").open("w", encoding="utf-8")
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_lock = stack.enter_context((root / "plan.lock").open("w", encoding="utf-8"))
        fcntl.flock(plan_lock, fcntl.LOCK_EX)
        try:
            plan = initialize(root, args)
            frozen = freeze_worker_sources(root, plan)
        finally:
            fcntl.flock(plan_lock, fcntl.LOCK_UN)
        environment = _subprocess_environment(frozen)

        def dispatch(job: Mapping[str, Any]) -> int:
            directory = task_dir(root, job)
            directory.mkdir(parents=True, exist_ok=True)
            worker_command = [
                sys.executable,
                str(frozen / "run_spreadsheetagent_three.py"),
                "worker",
                "--root",
                str(root),
                "--group",
                str(job["group"]),
                "--task-id",
                str(job["task_id"]),
            ]
            with (directory / "worker.log").open("a", encoding="utf-8") as log:
                completed = subprocess.run(
                    worker_command,
                    cwd=ROOT,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            if completed.returncode and not (directory / "result.json").exists():
                atomic_write_json(
                    directory / "result.json",
                    {
                        "schema_version": "spreadsheetagent-task-result-v1",
                        "finished_at": now(),
                        "plan_sha256": plan["plan_sha256"],
                        "group": job["group"],
                        "version": job["version"],
                        "model": job["model"],
                        "task_id": job["task_id"],
                        "category": job.get("category"),
                        "evaluation_mode": job["evaluation_mode"],
                        "status": "worker_error",
                        "error_type": "WorkerProcessFailure",
                        "error": (f"worker exited {completed.returncode}; see worker.log"),
                    },
                )
            return completed.returncode

        pending_jobs = [
            job
            for job in plan["jobs"]
            if job["group"] in selected_groups
            and not (task_dir(root, job) / "result.json").exists()
        ]
        buckets = [
            [job for job in pending_jobs if job["group"] == group["name"]] for group in GROUPS
        ]
        ordered_jobs = [
            job for bundle in itertools.zip_longest(*buckets) for job in bundle if job is not None
        ]
        report(root, plan)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(dispatch, job) for job in ordered_jobs}
            while futures:
                _, futures = concurrent.futures.wait(
                    futures,
                    timeout=30,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                report(root, plan)
        summary = report(root, plan)
        atomic_write_json(
            root / "supervisor-finished.json",
            {
                "finished_at": now(),
                "selected_groups": sorted(selected_groups),
                **summary,
            },
        )
        return summary


def _worker(root: Path, group: str, task_id: str) -> None:
    import fcntl

    plan = load_plan(root)
    matches = [job for job in plan["jobs"] if job["group"] == group and job["task_id"] == task_id]
    if len(matches) != 1:
        raise RuntimeError(f"Unknown or ambiguous worker task: {group}::{task_id}")
    job = matches[0]
    directory = task_dir(root, job)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        work(root, plan, job)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("init", "run", "report", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--only-group",
        action="append",
        choices=tuple(str(item["name"]) for item in GROUPS),
        help="Run only the selected planned group; repeat to select multiple groups",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-file", type=Path, default=DEFAULT_API_KEY_FILE)
    parser.add_argument("--vision-base-url")
    parser.add_argument("--vision-api-key-file", type=Path)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--visual-evaluator", type=Path, default=DEFAULT_VISUAL_EVALUATOR)
    parser.add_argument("--max-total-tokens", type=int, default=10_000_000)
    parser.add_argument("--max-output-tokens", type=int, default=32_768)
    parser.add_argument("--task-timeout", type=float, default=21_600)
    parser.add_argument("--request-timeout", type=float, default=600)
    parser.add_argument("--litellm-timeout", type=float, default=600)
    parser.add_argument("--request-retries", type=int, choices=range(0, 6), default=5)
    parser.add_argument("--group", help=argparse.SUPPRESS)
    parser.add_argument("--task-id", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.mode == "init":
        plan = initialize(root, args)
        freeze_worker_sources(root, plan)
        summary = report(root, plan)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "run":
        summary = run(root, args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not args.only_group:
            complete = summary["requested_local_work_complete"]
        else:
            complete = all(
                summary["groups"][group][
                    "study_complete"
                    if summary["groups"][group]["version"] == "v1"
                    else "local_generation_and_nonvisual_scoring_complete"
                ]
                for group in args.only_group
            )
        return 0 if complete else 2
    if args.mode == "report":
        summary = report(root, load_plan(root))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not args.group or not args.task_id:
        raise RuntimeError("worker mode requires --group and --task-id")
    _worker(root, args.group, args.task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
