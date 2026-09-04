"""Pinned SpreadsheetBench v1 loader and official Soft/Hard aggregation.

The original benchmark generates one solution from case 1 and replays that
solution on sibling cases.  This module owns the data/evaluator boundary; the
agent/replay runner deliberately lives separately so incomplete provider runs
cannot silently become zeroes here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .arms import run_arm
from .budget import RunBudget
from .config import ProviderConfig
from .errors import AgentExecutionFailure, HarnessError
from .pacing import RelayPacer
from .plugins import (
    ARM_COMPOSITIONS,
    PLUGEOLVE_SEED_COMPOSITION,
    CompositionSpec,
    default_plugin_registry,
    execution_plan,
)
from .render import recalculate_workbook
from .session import WorkbookSession
from .skills import SkillRegistry
from .tools import SpreadsheetToolRegistry

V1_SOURCE_REPOSITORY = "https://github.com/RUCKBReasoning/SpreadsheetBench"
V1_SOURCE_REVISION = "49b73a94775fb489063f60ca1865e3a650079a79"
V1_ARCHIVE_FILENAME = "spreadsheetbench_912_v0.1.tar.gz"
V1_ARCHIVE_SHA256 = "9cf7228b54f1edcdd4b372eb736774adf29cb4f804c9920229bac6c154833399"
V1_DATASET_JSON_SHA256 = "e5137ecbec4273d91344a0c8feb2aff2d4a93d5881ac40e490250dfd8db227de"
V1_OFFICIAL_EVALUATOR_SHA256 = (
    "4ae77cee8df01d1f34684fceab972810d696886533d33be2e89373de6b4d3de3"
)
V1_INSTRUCTION_COUNT = 912
V1_CASES_PER_INSTRUCTION = 3
V1_AVAILABLE_INPUT_CASE_COUNT = 2726
_REPLAYABLE_TOOLS = frozenset(
    {
        "write_range",
        "fill_formula",
        "format_range",
        "clear_range",
        "delete_rows",
        "delete_columns",
        "manage_sheet",
        "undo_last",
        "code_interpreter",
    }
)
V1_RUN_SCHEMA = "spreadsheetbench-v1-sibling-replay-run-v1"
V1_RUN_PROTOCOL = "generate-case1-replay-siblings-official-soft-hard-v1"


@dataclass(frozen=True)
class SpreadsheetBenchV1Case:
    index: int
    input_path: Path | None
    golden_path: Path | None


@dataclass(frozen=True)
class SpreadsheetBenchV1Instruction:
    task_id: str
    instruction: str
    instruction_type: str
    answer_position: str
    answer_sheet: str | None
    cases: tuple[SpreadsheetBenchV1Case, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_spreadsheetbench_v1(
    dataset_root: str | Path,
    *,
    require_pinned_dataset: bool = True,
) -> list[SpreadsheetBenchV1Instruction]:
    """Load all 912 instructions while retaining absent official case files."""

    root = Path(dataset_root).expanduser().resolve(strict=True)
    dataset_path = root / "dataset.json"
    if require_pinned_dataset and _sha256(dataset_path) != V1_DATASET_JSON_SHA256:
        raise ValueError("SpreadsheetBench v1 dataset.json does not match the pinned archive")
    rows = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("SpreadsheetBench v1 dataset.json must be a JSON array")
    if require_pinned_dataset and len(rows) != V1_INSTRUCTION_COUNT:
        raise ValueError(
            f"Pinned SpreadsheetBench v1 expected {V1_INSTRUCTION_COUNT} instructions, "
            f"got {len(rows)}"
        )

    tasks: list[SpreadsheetBenchV1Instruction] = []
    seen: set[str] = set()
    available_inputs = 0
    for row in rows:
        task_id = str(row["id"])
        if task_id in seen:
            raise ValueError(f"Duplicate SpreadsheetBench v1 task ID: {task_id}")
        seen.add(task_id)
        task_dir = root / "spreadsheet" / task_id
        cases: list[SpreadsheetBenchV1Case] = []
        for case_index in range(1, V1_CASES_PER_INSTRUCTION + 1):
            input_candidate = task_dir / f"{case_index}_{task_id}_input.xlsx"
            golden_candidate = task_dir / f"{case_index}_{task_id}_answer.xlsx"
            input_path = input_candidate if input_candidate.is_file() else None
            golden_path = golden_candidate if golden_candidate.is_file() else None
            available_inputs += int(input_path is not None)
            cases.append(SpreadsheetBenchV1Case(case_index, input_path, golden_path))
        tasks.append(
            SpreadsheetBenchV1Instruction(
                task_id=task_id,
                instruction=str(row["instruction"]),
                instruction_type=str(row.get("instruction_type", "")),
                answer_position=str(row["answer_position"]),
                answer_sheet=(str(row["answer_sheet"]) if row.get("answer_sheet") else None),
                cases=tuple(cases),
            )
        )
    if require_pinned_dataset and available_inputs != V1_AVAILABLE_INPUT_CASE_COUNT:
        raise ValueError(
            "Pinned SpreadsheetBench v1 available input count changed: "
            f"expected {V1_AVAILABLE_INPUT_CASE_COUNT}, got {available_inputs}"
        )
    return tasks


def _official_transform(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, dt.time):
        return str(value)[:-3]
    if isinstance(value, dt.datetime):
        excel_start = dt.datetime(1899, 12, 30)
        delta = value - excel_start
        return round(delta.days + delta.seconds / 86400.0, 0)
    if isinstance(value, str):
        try:
            return round(float(value), 2)
        except ValueError:
            return value
    return value


def _official_equal(expected: Any, actual: Any) -> bool:
    expected = _official_transform(expected)
    actual = _official_transform(actual)
    if expected in (None, "") and actual in (None, ""):
        return True
    return type(expected) is type(actual) and expected == actual


def _official_cells(cell_range: str) -> list[str]:
    if ":" not in cell_range:
        return [cell_range]
    start, end = cell_range.split(":")
    start_col = "".join(char for char in start if not char.isdigit())
    start_row = int("".join(char for char in start if char.isdigit()))
    end_col = "".join(char for char in end if not char.isdigit())
    end_row = int("".join(char for char in end if char.isdigit()))

    def column_number(name: str) -> int:
        number = 0
        for char in name:
            number = number * 26 + ord(char.upper()) - ord("A") + 1
        return number

    return [
        f"{get_column_letter(column)}{row}"
        for column in range(column_number(start_col), column_number(end_col) + 1)
        for row in range(start_row, end_row + 1)
    ]


def official_compare_v1(
    golden_path: str | Path,
    candidate_path: str | Path,
    answer_position: str,
) -> bool:
    """Reproduce ``evaluation/evaluation.py`` value-only comparison semantics."""

    candidate = Path(candidate_path)
    if not candidate.is_file():
        return False
    golden_book = load_workbook(golden_path, data_only=True)
    candidate_book = load_workbook(candidate, data_only=True)
    try:
        results: list[bool] = []
        for raw_range in answer_position.split(","):
            if "!" in raw_range:
                sheet_name, cell_range = raw_range.split("!")
                sheet_name = sheet_name.lstrip("'").rstrip("'")
            else:
                sheet_name = golden_book.sheetnames[0]
                cell_range = raw_range
            sheet_name = sheet_name.lstrip("'").rstrip("'")
            cell_range = cell_range.lstrip("'").rstrip("'")
            if sheet_name not in candidate_book.sheetnames:
                results.append(False)
                continue
            expected_sheet = golden_book[sheet_name]
            actual_sheet = candidate_book[sheet_name]
            results.append(
                all(
                    _official_equal(expected_sheet[cell].value, actual_sheet[cell].value)
                    for cell in _official_cells(cell_range)
                )
            )
        return all(results)
    finally:
        golden_book.close()
        candidate_book.close()


def score_v1_instruction(
    task: SpreadsheetBenchV1Instruction,
    outputs: Mapping[int, str | Path],
) -> dict[str, Any]:
    """Score one instruction, failing closed when a runnable case is unfinished."""

    case_rows: list[dict[str, Any]] = []
    incomplete = False
    official_results: list[int] = []
    for case in task.cases:
        if case.input_path is None or case.golden_path is None:
            case_rows.append(
                {
                    "case_index": case.index,
                    "status": "dataset_missing",
                    "passed": False,
                }
            )
            official_results.append(0)
            continue
        output = outputs.get(case.index)
        if output is None or not Path(output).is_file():
            case_rows.append(
                {"case_index": case.index, "status": "not_scored", "passed": None}
            )
            incomplete = True
            continue
        try:
            passed = official_compare_v1(case.golden_path, output, task.answer_position)
        except Exception as exc:
            case_rows.append(
                {
                    "case_index": case.index,
                    "status": "not_scored",
                    "passed": None,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            incomplete = True
            continue
        case_rows.append({"case_index": case.index, "status": "scored", "passed": passed})
        official_results.append(int(passed))

    if incomplete:
        soft = hard = None
    else:
        if len(official_results) != V1_CASES_PER_INSTRUCTION:
            raise RuntimeError("Complete v1 instruction did not produce three official outcomes")
        soft = sum(official_results) / V1_CASES_PER_INSTRUCTION
        hard = int(all(official_results))
    return {
        "task_id": task.task_id,
        "status": "completed" if not incomplete else "not_scored",
        "case_results": case_rows,
        "soft": soft,
        "hard": hard,
    }


def successful_v1_replay_calls(trajectory_path: str | Path) -> list[dict[str, Any]]:
    """Extract successful, state-changing tool calls in their original order."""

    path = Path(trajectory_path).expanduser().resolve(strict=True)
    pending: tuple[str, dict[str, Any]] | None = None
    calls: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        name = event.get("event")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if name == "tool.called":
            if pending is not None:
                raise ValueError(f"Nested tool call at trajectory line {line_number}")
            tool_name = str(payload.get("name", ""))
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError(f"Tool call arguments are not an object at line {line_number}")
            pending = (tool_name, arguments)
            continue
        if name not in {"tool.returned", "tool.failed"} or pending is None:
            continue
        tool_name, arguments = pending
        returned_name = str(payload.get("name", ""))
        if returned_name != tool_name:
            raise ValueError(
                f"Tool return mismatch at line {line_number}: {returned_name!r} != {tool_name!r}"
            )
        pending = None
        if name == "tool.failed" or tool_name not in _REPLAYABLE_TOOLS:
            continue
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        if tool_name == "code_interpreter" and result.get("workbook_changed") is not True:
            continue
        calls.append({"name": tool_name, "arguments": arguments})
    if pending is not None:
        raise ValueError("Trajectory ends with an unresolved tool call")
    return calls


def replay_v1_calls(
    source_path: str | Path,
    run_dir: str | Path,
    calls: list[Mapping[str, Any]],
    *,
    instruction: str = "",
    planner_plan: str | None = None,
    repair_date_text: bool = False,
    require_code_isolation: bool = True,
) -> dict[str, Any]:
    """Replay one frozen case-1 harness solution on a sibling workbook."""

    session = WorkbookSession.create(source_path, run_dir)
    planner_actions = 0
    if planner_plan is not None:
        from .arms import _apply_safe_planner_actions

        planner_actions = _apply_safe_planner_actions(
            session,
            instruction=instruction,
            normalized_plan=planner_plan,
            deterministic_evidence="{}",
            task_category=None,
        )
    registry = SpreadsheetToolRegistry(
        session,
        enable_code=any(call.get("name") == "code_interpreter" for call in calls),
        require_code_isolation=require_code_isolation,
    )
    replayed = 0
    failure: dict[str, Any] | None = None
    for index, call in enumerate(calls, 1):
        name = str(call.get("name", ""))
        arguments = call.get("arguments")
        if name not in _REPLAYABLE_TOOLS or not isinstance(arguments, dict):
            raise ValueError(f"Invalid frozen replay call at position {index}")
        outcome = registry.invoke(name, dict(arguments)).data
        if outcome.get("ok") is not True:
            failure = {
                "call_index": index,
                "name": name,
                "error": outcome.get("error"),
                "error_type": outcome.get("type"),
            }
            break
        replayed += 1
    repaired_date_cells = 0
    if repair_date_text:
        from .arms import _repair_date_text_in_date_formatted_cells

        repaired_date_cells = _repair_date_text_in_date_formatted_cells(session)
    return {
        "status": "scored" if failure is None else "model_execution_failure",
        "planner_actions_replayed": planner_actions,
        "date_text_repairs": repaired_date_cells,
        "calls_total": len(calls),
        "calls_replayed": replayed,
        "failure": failure,
        "output_workbook": str(session.workbook_path),
        "output_sha256": _sha256(session.workbook_path),
        "trajectory": str(session.paths.trajectory),
    }


def v1_planner_replay_plan(
    agent_result: Mapping[str, Any],
    trajectory_path: str | Path,
) -> str | None:
    """Return the case-1 planner text only when its safe actions were applied."""

    applied = False
    for raw_line in Path(trajectory_path).read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        if event.get("event") == "harness.planner_actions.applied":
            payload = event.get("payload")
            if isinstance(payload, dict) and int(payload.get("count", 0) or 0) > 0:
                applied = True
                break
    if not applied:
        return None
    stages = agent_result.get("stages")
    if not isinstance(stages, list):
        raise ValueError("Applied planner actions are missing stage evidence")
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("name") != "plan":
            continue
        nested = stage.get("agent")
        text = nested.get("final_text") if isinstance(nested, dict) else None
        if isinstance(text, str) and text.strip():
            return text
    raise ValueError("Applied planner actions are missing their frozen plan text")


def summarize_v1_scores(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate only a complete, unique 912-instruction official run."""

    ids = [str(row["task_id"]) for row in rows]
    complete = (
        len(rows) == V1_INSTRUCTION_COUNT
        and len(set(ids)) == V1_INSTRUCTION_COUNT
        and all(row.get("status") == "completed" for row in rows)
    )
    scored = [row for row in rows if row.get("status") == "completed"]
    return {
        "study_complete": complete,
        "instructions": len(rows),
        "scored_instructions": len(scored),
        "not_scored_instructions": len(rows) - len(scored),
        "soft": fmean(float(row["soft"]) for row in scored) if complete else None,
        "hard": fmean(float(row["hard"]) for row in scored) if complete else None,
    }


def _atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _manifest_sha256(manifest: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _v1_arm_orders(
    tasks: Sequence[SpreadsheetBenchV1Instruction],
    arms: tuple[str, ...],
    seed: int,
) -> dict[str, tuple[str, ...]]:
    orders: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        offset = int.from_bytes(
            hashlib.sha256(f"{seed}:{task.task_id}".encode()).digest()[:4], "big"
        ) % len(arms)
        orders[task.task_id] = (*arms[offset:], *arms[:offset])
    return orders


def run_spreadsheetbench_v1_comparison(
    *,
    config: ProviderConfig,
    dataset_root: str | Path,
    output_dir: str | Path,
    skill_registry: SkillRegistry,
    tasks: Sequence[SpreadsheetBenchV1Instruction] | None = None,
    arms: Sequence[str] = ("ours",),
    composition_overrides: Mapping[str, CompositionSpec] | None = None,
    max_model_calls: int = 20,
    max_turns_per_arm: int = 20,
    max_total_tokens: int = 200_000,
    max_output_tokens: int = 4_096,
    task_timeout_seconds: float = 1_800,
    arm_order_seed: int = 20_260_829,
    resume: bool = False,
    seal_interrupted_current: bool = False,
) -> dict[str, Any]:
    """Run a resumable full v1 study with one generated solution per instruction."""

    selected_tasks = list(tasks or load_spreadsheetbench_v1(dataset_root))
    selected_arms = tuple(str(arm) for arm in arms)
    known_arms = {
        "bare",
        "ours",
        "spreadsheet-harness-basic",
        "spreadsheet-harness-financial",
    }
    if not selected_tasks or len({task.task_id for task in selected_tasks}) != len(
        selected_tasks
    ):
        raise HarnessError("SpreadsheetBench v1 tasks must be non-empty and unique")
    if not selected_arms or len(set(selected_arms)) != len(selected_arms):
        raise HarnessError("SpreadsheetBench v1 arms must be non-empty and unique")
    if set(selected_arms) - known_arms:
        raise HarnessError("Unsupported SpreadsheetBench v1 arm")
    if max_model_calls < max_turns_per_arm:
        raise HarnessError("max_model_calls must be at least max_turns_per_arm")

    root = Path(dataset_root).expanduser().resolve(strict=True)
    if _sha256(root / "dataset.json") != V1_DATASET_JSON_SHA256:
        raise HarnessError("SpreadsheetBench v1 runner requires the pinned dataset")
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and not resume:
        raise HarnessError(f"Fresh SpreadsheetBench v1 output already exists: {output}")
    if resume and not output.is_dir():
        raise HarnessError(f"SpreadsheetBench v1 resume output does not exist: {output}")
    if not resume:
        output.mkdir(parents=True)

    overrides = dict(composition_overrides or {})
    if set(overrides) - set(selected_arms):
        raise HarnessError("v1 composition override targets an unselected arm")
    registry = default_plugin_registry()
    specs = {
        arm: overrides.get(
            arm,
            PLUGEOLVE_SEED_COMPOSITION if arm == "ours" else ARM_COMPOSITIONS[arm],
        )
        for arm in selected_arms
    }
    compositions = {
        arm: {
            "composition_sha256": registry.resolve(spec).sha256,
            "composition": registry.resolve(spec).to_dict(),
        }
        for arm, spec in specs.items()
    }
    arm_orders = _v1_arm_orders(selected_tasks, selected_arms, arm_order_seed)
    manifest: dict[str, Any] = {
        "schema_version": V1_RUN_SCHEMA,
        "protocol": V1_RUN_PROTOCOL,
        "dataset": {
            "repository": V1_SOURCE_REPOSITORY,
            "revision": V1_SOURCE_REVISION,
            "archive_filename": V1_ARCHIVE_FILENAME,
            "archive_sha256": V1_ARCHIVE_SHA256,
            "dataset_json_sha256": V1_DATASET_JSON_SHA256,
            "instruction_count": V1_INSTRUCTION_COUNT,
            "available_input_cases": V1_AVAILABLE_INPUT_CASE_COUNT,
            "official_denominator_cases_per_instruction": V1_CASES_PER_INSTRUCTION,
        },
        "evaluator": {
            "sha256": V1_OFFICIAL_EVALUATOR_SHA256,
            "semantics": "clean-room exact value-only port",
        },
        "provider": config.public_dict(),
        "arms": list(selected_arms),
        "compositions": compositions,
        "resources": {
            "max_model_calls": max_model_calls,
            "max_turns_per_arm": max_turns_per_arm,
            "max_total_tokens": max_total_tokens,
            "max_output_tokens_per_call": max_output_tokens,
            "task_timeout_seconds": task_timeout_seconds,
            "arm_order_seed": arm_order_seed,
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "instruction_sha256": hashlib.sha256(task.instruction.encode()).hexdigest(),
                "answer_position_sha256": hashlib.sha256(
                    task.answer_position.encode()
                ).hexdigest(),
                "arm_order": list(arm_orders[task.task_id]),
                "cases": [
                    {
                        "case_index": case.index,
                        "input_sha256": _sha256(case.input_path) if case.input_path else None,
                        "golden_sha256": _sha256(case.golden_path) if case.golden_path else None,
                    }
                    for case in task.cases
                ],
            }
            for task in selected_tasks
        ],
    }
    manifest["manifest_sha256"] = _manifest_sha256(manifest)
    manifest_path = output / "manifest.json"
    results_path = output / "results.json"
    if resume:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if recorded != manifest:
            raise HarnessError("SpreadsheetBench v1 resume manifest does not match this command")
        results = json.loads(results_path.read_text(encoding="utf-8"))
        if not isinstance(results, list):
            raise HarnessError("SpreadsheetBench v1 results.json must be an array")
    else:
        _atomic_write_json(manifest_path, manifest)
        results = []

    work = [
        (task, arm)
        for task in selected_tasks
        for arm in arm_orders[task.task_id]
    ]
    if len(results) > len(work):
        raise HarnessError("SpreadsheetBench v1 results contain too many rows")
    for index, row in enumerate(results):
        task, arm = work[index]
        if (row.get("task_id"), row.get("arm")) != (task.task_id, arm):
            raise HarnessError("SpreadsheetBench v1 results are not a valid execution prefix")
    if resume and len(results) < len(work):
        pending_task, pending_arm = work[len(results)]
        pending_dir = output / "runs" / pending_task.task_id / pending_arm
        if pending_dir.exists():
            if not seal_interrupted_current:
                raise HarnessError(
                    "Resume found an interrupted v1 arm; pass seal_interrupted_current"
                )
            results.append(
                {
                    "task_id": pending_task.task_id,
                    "arm": pending_arm,
                    "status": "not_scored",
                    "outcome_kind": "interrupted",
                    "soft": None,
                    "hard": None,
                    "manifest_sha256": manifest["manifest_sha256"],
                }
            )
            _atomic_write_json(results_path, results)

    frozen_skills = skill_registry.freeze()
    pacer = RelayPacer(config.request_interval_seconds)
    for task, arm in work[len(results) :]:
        started = time.monotonic()
        run_root = output / "runs" / task.task_id / arm
        budget = RunBudget(
            max_model_calls=max_model_calls,
            max_total_tokens=max_total_tokens,
            max_elapsed_seconds=task_timeout_seconds,
        )
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "arm": arm,
            "model": config.model,
            "protocol": V1_RUN_PROTOCOL,
            "manifest_sha256": manifest["manifest_sha256"],
            "run_dir": str(run_root),
            "started_at": dt.datetime.now(timezone.utc).isoformat(),
        }
        try:
            first_case = task.cases[0]
            if first_case.input_path is None or first_case.golden_path is None:
                raise HarnessError("SpreadsheetBench v1 case 1 is absent")
            first_session = WorkbookSession.create(
                first_case.input_path,
                run_root / "case-1",
                run_id=f"v1-{task.task_id}-{arm}-case-1",
                recorder_secrets=(config.api_key,),
            )
            execution_failure: AgentExecutionFailure | None = None
            try:
                agent_result = run_arm(
                    arm=arm,  # type: ignore[arg-type]
                    config=config,
                    session=first_session,
                    skills=frozen_skills,
                    instruction=task.instruction,
                    max_output_tokens=max_output_tokens,
                    max_elapsed_seconds=task_timeout_seconds,
                    budget=budget,
                    pacer=pacer,
                    max_turns_per_arm=max_turns_per_arm,
                    composition=specs[arm],
                    task_category=None,
                )
            except AgentExecutionFailure as exc:
                if exc.agent_result is None:
                    raise
                agent_result = exc.agent_result
                execution_failure = exc
            first_recalculation = recalculate_workbook(
                first_session.workbook_path,
                first_session.workbook_path,
                timeout_seconds=min(120.0, task_timeout_seconds),
            )
            agent_dict = agent_result.to_dict()
            planner_plan = v1_planner_replay_plan(
                agent_dict, first_session.paths.trajectory
            )
            calls = successful_v1_replay_calls(first_session.paths.trajectory)
            outputs: dict[int, Path] = {1: first_session.workbook_path}
            replay_rows: list[dict[str, Any]] = []
            plan = execution_plan(registry.resolve(specs[arm]))
            for case in task.cases[1:]:
                if case.input_path is None or case.golden_path is None:
                    replay_rows.append(
                        {"case_index": case.index, "status": "dataset_missing"}
                    )
                    continue
                replay = replay_v1_calls(
                    case.input_path,
                    run_root / f"case-{case.index}",
                    calls,
                    instruction=task.instruction,
                    planner_plan=planner_plan,
                    repair_date_text=plan.repair_date_text,
                )
                replay_output = Path(replay["output_workbook"])
                replay["recalculation"] = recalculate_workbook(
                    replay_output,
                    replay_output,
                    timeout_seconds=min(120.0, task_timeout_seconds),
                )
                replay["case_index"] = case.index
                replay_rows.append(replay)
                outputs[case.index] = replay_output
            score = score_v1_instruction(task, outputs)
            row.update(
                {
                    "status": score["status"],
                    "outcome_kind": (
                        "model_execution_failure" if execution_failure else "scored"
                    ),
                    "soft": score["soft"],
                    "hard": score["hard"],
                    "case_results": score["case_results"],
                    "agent": agent_dict,
                    "first_case_recalculation": first_recalculation,
                    "planner_plan_replayed": planner_plan is not None,
                    "replay_call_count": len(calls),
                    "replays": replay_rows,
                    "output_workbook": str(first_session.workbook_path),
                    "output_sha256": _sha256(first_session.workbook_path),
                }
            )
            if execution_failure is not None:
                row["model_failure_reason"] = execution_failure.reason
        except Exception as exc:
            row.update(
                {
                    "status": "not_scored",
                    "outcome_kind": "not_scored",
                    "soft": None,
                    "hard": None,
                    "error_type": type(exc).__name__,
                    "error": str(exc).replace(config.api_key, "[REDACTED]"),
                }
            )
        row["budget"] = budget.to_dict()
        row["elapsed_seconds"] = round(time.monotonic() - started, 3)
        row["finished_at"] = dt.datetime.now(timezone.utc).isoformat()
        results.append(row)
        _atomic_write_json(results_path, results)
        print(
            json.dumps(
                {
                    "event": "spreadsheetbench_v1.arm_instruction_finished",
                    "task_id": task.task_id,
                    "arm": arm,
                    "status": row["status"],
                    "soft": row.get("soft"),
                    "hard": row.get("hard"),
                    "finished": len(results),
                    "expected": len(work),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    arm_summaries = {
        arm: summarize_v1_scores([row for row in results if row.get("arm") == arm])
        for arm in selected_arms
    }
    summary = {
        "study_complete": all(item["study_complete"] for item in arm_summaries.values()),
        "expected_arm_instructions": len(work),
        "recorded_arm_instructions": len(results),
        "arms": arm_summaries,
        "manifest_sha256": manifest["manifest_sha256"],
    }
    _atomic_write_json(output / "summary.json", summary)
    return summary


def audit_spreadsheetbench_v1_comparison(
    result_dir: str | Path,
    *,
    dataset_root: str | Path,
) -> dict[str, Any]:
    """Fresh-rescore every available v1 artifact and verify frozen identities."""

    output = Path(result_dir).expanduser().resolve(strict=True)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((output / "results.json").read_text(encoding="utf-8"))
    reasons: list[str] = []
    if not isinstance(manifest, dict) or manifest.get("schema_version") != V1_RUN_SCHEMA:
        reasons.append("invalid_manifest_schema")
        manifest = manifest if isinstance(manifest, dict) else {}
    if not isinstance(results, list):
        reasons.append("results_not_array")
        results = []
    if manifest.get("manifest_sha256") != _manifest_sha256(manifest):
        reasons.append("manifest_sha256_mismatch")
    try:
        tasks = load_spreadsheetbench_v1(dataset_root)
    except Exception as exc:
        reasons.append(f"dataset_load:{type(exc).__name__}")
        tasks = []
    by_id = {task.task_id: task for task in tasks}
    manifest_tasks = manifest.get("tasks")
    if not isinstance(manifest_tasks, list):
        reasons.append("manifest_tasks_missing")
        manifest_tasks = []
    selected_ids = [str(item.get("task_id")) for item in manifest_tasks if isinstance(item, dict)]
    if len(selected_ids) != len(set(selected_ids)):
        reasons.append("duplicate_manifest_task")
    for item in manifest_tasks:
        if not isinstance(item, dict):
            reasons.append("non_object_manifest_task")
            continue
        task_id = str(item.get("task_id"))
        task = by_id.get(task_id)
        if task is None:
            reasons.append(f"{task_id}:manifest_task_unavailable")
            continue
        expected_instruction = hashlib.sha256(task.instruction.encode()).hexdigest()
        expected_position = hashlib.sha256(task.answer_position.encode()).hexdigest()
        if item.get("instruction_sha256") != expected_instruction:
            reasons.append(f"{task_id}:instruction_sha256_mismatch")
        if item.get("answer_position_sha256") != expected_position:
            reasons.append(f"{task_id}:answer_position_sha256_mismatch")
        recorded_cases = item.get("cases")
        if not isinstance(recorded_cases, list) or len(recorded_cases) != len(task.cases):
            reasons.append(f"{task_id}:manifest_cases_mismatch")
            continue
        for recorded_case, case in zip(recorded_cases, task.cases, strict=True):
            if not isinstance(recorded_case, dict) or recorded_case.get("case_index") != case.index:
                reasons.append(f"{task_id}:manifest_case_index_mismatch")
                continue
            expected_input = _sha256(case.input_path) if case.input_path else None
            expected_golden = _sha256(case.golden_path) if case.golden_path else None
            if recorded_case.get("input_sha256") != expected_input:
                reasons.append(f"{task_id}:case_{case.index}_input_sha256_mismatch")
            if recorded_case.get("golden_sha256") != expected_golden:
                reasons.append(f"{task_id}:case_{case.index}_golden_sha256_mismatch")
    arms = tuple(str(arm) for arm in manifest.get("arms", []))
    expected = {(task_id, arm) for task_id in selected_ids for arm in arms}
    seen: set[tuple[str, str]] = set()
    audit_rows: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            reasons.append("non_object_result_row")
            continue
        key = (str(row.get("task_id")), str(row.get("arm")))
        row_reasons: list[str] = []
        if key in seen:
            row_reasons.append("duplicate_arm_instruction")
        seen.add(key)
        if key not in expected:
            row_reasons.append("unexpected_arm_instruction")
        if row.get("manifest_sha256") != manifest.get("manifest_sha256"):
            row_reasons.append("row_manifest_mismatch")
        task = by_id.get(key[0])
        fresh: dict[str, Any] | None = None
        if task is None:
            row_reasons.append("task_unavailable")
        elif row.get("status") == "completed":
            outputs: dict[int, Path] = {}
            for case in task.cases:
                candidate = output / "runs" / task.task_id / key[1] / f"case-{case.index}" / "artifacts" / "output.xlsx"
                if candidate.is_file():
                    outputs[case.index] = candidate
            first_output = outputs.get(1)
            if first_output is not None and row.get("output_sha256") != _sha256(first_output):
                row_reasons.append("output_sha256_mismatch")
            fresh = score_v1_instruction(task, outputs)
            for field in ("status", "soft", "hard", "case_results"):
                if fresh.get(field) != row.get(field):
                    row_reasons.append(f"fresh_{field}_mismatch")
        audit_rows.append(
            {
                "task_id": key[0],
                "arm": key[1],
                "audit_valid": not row_reasons,
                "reasons": row_reasons,
                "fresh_score": fresh,
            }
        )
        reasons.extend(f"{key[0]}::{key[1]}:{reason}" for reason in row_reasons)
    missing = expected - seen
    if missing:
        reasons.append(f"missing_arm_instructions:{len(missing)}")
    completed = {
        (str(row.get("task_id")), str(row.get("arm")))
        for row in results
        if isinstance(row, dict) and row.get("status") == "completed"
    }
    incomplete = expected - completed
    return {
        "schema_version": "spreadsheetbench-v1-sibling-replay-audit-v1",
        "protocol": V1_RUN_PROTOCOL,
        "audit_valid": not reasons,
        "study_complete": not incomplete and len(selected_ids) == V1_INSTRUCTION_COUNT,
        "reasons": reasons,
        "manifest_sha256": manifest.get("manifest_sha256"),
        "expected_arm_instructions": len(expected),
        "result_rows": len(results),
        "completed_arm_instructions": len(expected & completed),
        "not_scored_arm_instructions": len(incomplete),
        "rows": audit_rows,
    }


__all__ = [
    "SpreadsheetBenchV1Case",
    "SpreadsheetBenchV1Instruction",
    "audit_spreadsheetbench_v1_comparison",
    "load_spreadsheetbench_v1",
    "official_compare_v1",
    "replay_v1_calls",
    "run_spreadsheetbench_v1_comparison",
    "score_v1_instruction",
    "successful_v1_replay_calls",
    "summarize_v1_scores",
    "v1_planner_replay_plan",
]
