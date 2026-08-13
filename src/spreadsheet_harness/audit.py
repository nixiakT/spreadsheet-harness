"""Read-only integrity audit for completed SpreadsheetBench comparisons."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .arms import COMPARISON_FORCED_TOOL_PREFIX_POLICY, comparison_stage_turn_caps
from .benchmark import SpreadsheetTask, _source_fingerprint, compare_workbooks
from .comparison import (
    COMPARISON_CONFIGURATION_POLICIES,
    COMPARISON_MANIFEST_SCHEMA_VERSION,
    COMPARISON_PROTOCOL_VERSION,
    _allowed_observed_terminals_policy,
    _request_attempt_audit,
    _stage_allowed_tools_policy,
)


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"not a regular file: {path}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _scoring_metadata_sha256(task: SpreadsheetTask) -> str:
    encoded = json.dumps(
        {
            "answer_position": task.answer_position,
            "answer_sheet": task.answer_sheet,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_sha256(encoded)


def _absolute_path(value: Any, *, base: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.abspath(candidate))


def _has_symlink(root: Path, target: Path) -> bool:
    """Check every result-owned component without following the target first."""

    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _load_manifest(path: Path, reasons: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _add_reason(reasons, "comparison_manifest_missing")
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        _add_reason(reasons, "comparison_manifest_invalid")
        return {}
    if not isinstance(value, dict):
        _add_reason(reasons, "comparison_manifest_invalid")
        return {}
    return value


def _load_result_rows(path: Path, reasons: list[str]) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        _add_reason(reasons, "results_file_missing")
        return []
    except (OSError, UnicodeError):
        _add_reason(reasons, "results_file_unreadable")
        return []

    if raw and not raw.endswith(b"\n"):
        _add_reason(reasons, "results_file_non_terminated")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        _add_reason(reasons, "results_file_unreadable")
        return []

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            _add_reason(reasons, f"invalid_jsonl_line:{line_number}")
            continue
        if not isinstance(row, dict):
            _add_reason(reasons, f"non_object_jsonl_line:{line_number}")
            continue
        rows.append(row)
    return rows


def _manifest_task_reasons(
    manifest: dict[str, Any], tasks: list[SpreadsheetTask], reasons: list[str]
) -> dict[str, list[str]]:
    task_reasons = {task.task_id: [] for task in tasks}
    expected_ids = [task.task_id for task in tasks]
    if manifest.get("task_count") != len(tasks):
        _add_reason(reasons, "manifest_task_count_mismatch")
    if manifest.get("task_ids") != expected_ids:
        _add_reason(reasons, "manifest_task_ids_mismatch")

    raw_entries = manifest.get("tasks")
    if not isinstance(raw_entries, list):
        _add_reason(reasons, "manifest_tasks_invalid")
        raw_entries = []
    entries: dict[str, list[dict[str, Any]]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or raw_entry.get("task_id") is None:
            _add_reason(reasons, "manifest_task_entry_invalid")
            continue
        entries.setdefault(str(raw_entry["task_id"]), []).append(raw_entry)

    expected_id_set = set(expected_ids)
    for task_id in sorted(set(entries) - expected_id_set):
        _add_reason(reasons, f"manifest_unknown_task:{task_id}")

    for task in tasks:
        matching = entries.get(task.task_id, [])
        if len(matching) != 1:
            reason = "manifest_task_missing" if not matching else "manifest_task_duplicate"
            task_reasons[task.task_id].append(reason)
            _add_reason(reasons, f"{reason}:{task.task_id}")
            continue
        entry = matching[0]
        expected_hashes: dict[str, str | None] = {
            "instruction_sha256": _text_sha256(task.instruction),
            "scoring_metadata_sha256": _scoring_metadata_sha256(task),
            "input_sha256": None,
            "golden_sha256": None,
        }
        for field, path in (
            ("input_sha256", task.input_path),
            ("golden_sha256", task.golden_path),
        ):
            try:
                expected_hashes[field] = _file_sha256(Path(path))
            except OSError:
                code = f"task_{field.removesuffix('_sha256')}_unreadable"
                task_reasons[task.task_id].append(code)
                _add_reason(reasons, f"{code}:{task.task_id}")
        for field, expected in expected_hashes.items():
            if expected is not None and entry.get(field) != expected:
                code = f"manifest_task_hash_mismatch:{field}"
                task_reasons[task.task_id].append(code)
                _add_reason(reasons, f"{code}:{task.task_id}")
    return task_reasons


def _expected_artifact_hash(row: dict[str, Any]) -> tuple[str | None, bool]:
    values: list[str] = []
    direct = row.get("output_sha256")
    if isinstance(direct, str):
        values.append(direct)
    recalculation = row.get("recalculation")
    if isinstance(recalculation, dict) and isinstance(recalculation.get("output_sha256"), str):
        values.append(str(recalculation["output_sha256"]))
    unique = set(values)
    return (values[0] if len(unique) == 1 else None, len(unique) > 1)


def _valid_source_fingerprint(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    aggregate = value.get("sha256")
    files = value.get("files")
    if (
        not isinstance(aggregate, str)
        or len(aggregate) != 64
        or not isinstance(files, list)
        or not files
    ):
        return False
    combined = hashlib.sha256()
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            return False
        path = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path in seen
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return False
        seen.add(path)
        combined.update(path.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    return combined.hexdigest() == aggregate


def _audit_manifest_contract(manifest: dict[str, Any], reasons: list[str]) -> None:
    configuration = manifest.get("configuration")
    required_configuration = {
        "model",
        "api_protocol",
        "requested_reasoning_effort",
        "reasoning_effort",
        "request_interval_seconds",
        "litellm_timeout_seconds",
        "generation",
        "max_model_calls",
        "max_turns_per_arm",
        "max_total_tokens",
        "max_output_tokens_per_call",
        "task_timeout_seconds",
        "recalculate",
    }
    if not isinstance(configuration, dict) or not required_configuration.issubset(
        configuration
    ):
        _add_reason(reasons, "comparison_manifest_configuration_invalid")
    elif any(
        configuration.get(field) != expected
        for field, expected in COMPARISON_CONFIGURATION_POLICIES.items()
    ):
        _add_reason(reasons, "comparison_manifest_policy_mismatch")
    if not _valid_source_fingerprint(manifest.get("harness_source")):
        _add_reason(reasons, "comparison_manifest_source_fingerprint_invalid")
    elif manifest.get("harness_source") != _source_fingerprint():
        _add_reason(reasons, "comparison_manifest_source_checkout_mismatch")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or not runtime.get("python"):
        _add_reason(reasons, "comparison_manifest_runtime_invalid")
    for field in (
        "stage_turn_caps",
        "forced_tool_prefix_routing",
        "stage_allowed_tools",
        "allowed_observed_terminals",
    ):
        value = manifest.get(field)
        if not isinstance(value, dict) or set(value) != set(manifest.get("arms") or []):
            _add_reason(reasons, f"comparison_manifest_{field}_invalid")
    arms = tuple(str(arm) for arm in (manifest.get("arms") or []))
    max_turns = configuration.get("max_turns_per_arm") if isinstance(configuration, dict) else None
    try:
        expected_caps = comparison_stage_turn_caps(max_turns, arms)
    except (TypeError, ValueError):
        _add_reason(reasons, "comparison_manifest_turn_caps_invalid")
    else:
        if manifest.get("stage_turn_caps") != expected_caps:
            _add_reason(reasons, "comparison_manifest_turn_caps_mismatch")
        expected_prefixes = {
            arm: {
                stage: list(prefix)
                for stage, prefix in COMPARISON_FORCED_TOOL_PREFIX_POLICY[arm].items()
            }
            for arm in arms
        }
        if manifest.get("forced_tool_prefix_routing") != expected_prefixes:
            _add_reason(reasons, "comparison_manifest_forced_routing_mismatch")
        if manifest.get("stage_allowed_tools") != _stage_allowed_tools_policy(arms):
            _add_reason(reasons, "comparison_manifest_stage_tools_mismatch")
        if manifest.get("allowed_observed_terminals") != (
            _allowed_observed_terminals_policy(expected_caps)
        ):
            _add_reason(reasons, "comparison_manifest_terminal_policy_mismatch")


def _audit_row_contract(
    record: dict[str, Any],
    row: dict[str, Any],
    task: SpreadsheetTask,
    arm: str,
    manifest: dict[str, Any],
    manifest_sha256: str | None,
) -> None:
    reasons: list[str] = record["reasons"]
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        _add_reason(reasons, "manifest_configuration_unavailable")
        return
    if row.get("comparison_manifest_sha256") != manifest_sha256:
        _add_reason(reasons, "manifest_sha256_binding_mismatch")
    expected_fields = {
        "model": configuration.get("model"),
        "api_protocol": configuration.get("api_protocol"),
        "requested_reasoning_effort": configuration.get("requested_reasoning_effort"),
        "reasoning_effort": configuration.get("reasoning_effort"),
        "request_interval_seconds": configuration.get("request_interval_seconds"),
        "litellm_timeout_seconds": configuration.get("litellm_timeout_seconds"),
        "generation": configuration.get("generation"),
        "max_model_calls": configuration.get("max_model_calls"),
        "max_turns_per_arm": configuration.get("max_turns_per_arm"),
        "stage_turn_caps": (manifest.get("stage_turn_caps") or {}).get(arm),
        "calculation_backend": (
            "libreoffice" if configuration.get("recalculate") is True else "not_recalculated"
        ),
        "instruction_type": task.instruction_type,
    }
    for field, expected in expected_fields.items():
        if row.get(field) != expected:
            _add_reason(reasons, f"row_manifest_mismatch:{field}")
    budget = row.get("budget")
    limit = budget.get("limit") if isinstance(budget, dict) else None
    used = budget.get("used") if isinstance(budget, dict) else None
    expected_limit = {
        "model_calls": configuration.get("max_model_calls"),
        "total_tokens": configuration.get("max_total_tokens"),
        "elapsed_seconds": configuration.get("task_timeout_seconds"),
    }
    if limit != expected_limit:
        _add_reason(reasons, "budget_limit_mismatch")
    if not isinstance(used, dict):
        _add_reason(reasons, "budget_used_invalid")
    else:
        for field in ("model_calls", "total_tokens"):
            value = used.get(field)
            ceiling = expected_limit[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or not isinstance(ceiling, int)
                or value > ceiling
            ):
                _add_reason(reasons, f"budget_used_invalid:{field}")


def _audit_completed_agent(
    record: dict[str, Any],
    row: dict[str, Any],
    arm: str,
    manifest: dict[str, Any],
) -> None:
    reasons: list[str] = record["reasons"]
    agent = row.get("agent")
    if not isinstance(agent, dict):
        _add_reason(reasons, "agent_evidence_missing")
        return
    if agent.get("arm") != arm:
        _add_reason(reasons, "agent_arm_mismatch")
    stages = agent.get("stages")
    expected_caps = (manifest.get("stage_turn_caps") or {}).get(arm)
    expected_prefixes = (manifest.get("forced_tool_prefix_routing") or {}).get(arm)
    expected_tools = (manifest.get("stage_allowed_tools") or {}).get(arm)
    allowed_terminals = (manifest.get("allowed_observed_terminals") or {}).get(arm)
    if not isinstance(stages, list) or not isinstance(expected_caps, dict):
        _add_reason(reasons, "agent_stages_invalid")
        return
    expected_names = list(expected_caps)
    if [stage.get("name") if isinstance(stage, dict) else None for stage in stages] != expected_names:
        _add_reason(reasons, "agent_stage_order_mismatch")
        return
    timing_count = 0
    for stage in stages:
        assert isinstance(stage, dict)
        name = str(stage["name"])
        if stage.get("max_turns") != expected_caps.get(name):
            _add_reason(reasons, f"agent_stage_turn_cap_mismatch:{name}")
        if stage.get("allowed_tools") != (expected_tools or {}).get(name):
            _add_reason(reasons, f"agent_stage_tools_mismatch:{name}")
        prefix = (expected_prefixes or {}).get(name)
        if stage.get("forced_tool_prefix") != prefix:
            _add_reason(reasons, f"agent_forced_prefix_mismatch:{name}")
        if stage.get("observed_forced_tool_prefix") != prefix:
            _add_reason(reasons, f"agent_observed_prefix_mismatch:{name}")
        if prefix:
            if stage.get("first_tool_choice") != prefix[0] or stage.get(
                "observed_first_tool"
            ) != prefix[0]:
                _add_reason(reasons, f"agent_first_tool_mismatch:{name}")
        expected_terminal = (
            "assistant_text"
            if arm == "paper" and name == "reconcile"
            else (manifest.get("post_prefix_routing") or {}).get("terminal_tool")
        )
        if stage.get("terminal_tool") != expected_terminal:
            _add_reason(reasons, f"agent_terminal_tool_mismatch:{name}")
        if stage.get("observed_terminal_tool") not in (allowed_terminals or {}).get(name, []):
            _add_reason(reasons, f"agent_observed_terminal_invalid:{name}")
        stage_agent = stage.get("agent")
        if not isinstance(stage_agent, dict):
            _add_reason(reasons, f"agent_stage_evidence_missing:{name}")
            continue
        turns = stage_agent.get("turns")
        if (
            isinstance(turns, bool)
            or not isinstance(turns, int)
            or turns < 1
            or turns > expected_caps[name]
        ):
            _add_reason(reasons, f"agent_stage_turns_invalid:{name}")
        stage_timings = stage_agent.get("request_timings")
        if not isinstance(stage_timings, list) or len(stage_timings) != turns:
            _add_reason(reasons, f"agent_stage_request_count_mismatch:{name}")
        else:
            timing_count += len(stage_timings)
    budget_calls = ((row.get("budget") or {}).get("used") or {}).get("model_calls")
    aggregate_timings = agent.get("request_timings")
    if (
        not isinstance(aggregate_timings, list)
        or len(aggregate_timings) != timing_count
        or len(aggregate_timings) != budget_calls
    ):
        _add_reason(reasons, "agent_request_count_mismatch")
    if not bool(_request_attempt_audit(row)["exact"]):
        _add_reason(reasons, "request_attempt_audit_inexact")
    expected_endpoint = (
        "/responses" if row.get("api_protocol") == "responses" else "/chat/completions"
    )
    if isinstance(aggregate_timings, list):
        for timing in aggregate_timings:
            if not isinstance(timing, dict):
                continue
            attempts = timing.get("attempts")
            history = timing.get("attempt_history")
            if isinstance(history, list) and isinstance(attempts, int):
                for attempt in history:
                    if not isinstance(attempt, dict):
                        _add_reason(reasons, "request_attempt_history_invalid")
                        break
                    if attempt.get("api_protocol") != row.get("api_protocol"):
                        _add_reason(reasons, "request_attempt_api_protocol_mismatch")
                    if attempt.get("endpoint") != expected_endpoint:
                        _add_reason(reasons, "request_attempt_endpoint_mismatch")
    usage = agent.get("usage")
    used = (row.get("budget") or {}).get("used") or {}
    if not isinstance(usage, dict) or usage.get("total_tokens") != used.get("total_tokens"):
        _add_reason(reasons, "agent_budget_token_mismatch")
    agent_budget = agent.get("budget")
    agent_limit = agent_budget.get("limit") if isinstance(agent_budget, dict) else None
    agent_used = agent_budget.get("used") if isinstance(agent_budget, dict) else None
    if agent_limit != (row.get("budget") or {}).get("limit"):
        _add_reason(reasons, "agent_budget_limit_mismatch")
    if not isinstance(agent_used, dict) or any(
        agent_used.get(field) != used.get(field)
        for field in ("model_calls", "total_tokens")
    ):
        _add_reason(reasons, "agent_budget_usage_mismatch")


def _audit_completed_row(
    record: dict[str, Any],
    row: dict[str, Any],
    task: SpreadsheetTask,
    arm: str,
    root: Path,
    manifest: dict[str, Any],
    manifest_sha256: str | None,
) -> None:
    reasons: list[str] = record["reasons"]
    _audit_row_contract(record, row, task, arm, manifest, manifest_sha256)
    if row.get("status") != "completed":
        _add_reason(reasons, "status_not_completed")
        return
    _audit_completed_agent(record, row, arm, manifest)

    run_dir = _absolute_path(row.get("run_dir"), base=root)
    output = _absolute_path(row.get("output_workbook"), base=root)
    record["run_dir"] = str(run_dir) if run_dir is not None else None
    record["output_workbook"] = str(output) if output is not None else None
    if run_dir is None:
        _add_reason(reasons, "run_dir_missing")
    if output is None:
        _add_reason(reasons, "output_path_missing")
    if run_dir is None or output is None:
        return

    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        _add_reason(reasons, "results_dir_unreadable")
        return
    expected_parent = resolved_root / "runs" / task.task_id
    resolved_run = run_dir.resolve(strict=False)
    resolved_output = output.resolve(strict=False)
    if resolved_run.parent != expected_parent or not (
        resolved_run.name == arm or resolved_run.name.startswith(f"{arm}-")
    ):
        _add_reason(reasons, "run_dir_outside_expected_arm")
    expected_output = resolved_run / "artifacts" / "output.xlsx"
    if resolved_output != expected_output:
        _add_reason(reasons, "output_path_not_managed_artifact")
    if _has_symlink(root, run_dir) or _has_symlink(root, output):
        _add_reason(reasons, "artifact_path_contains_symlink")
    if reasons:
        return

    try:
        metadata = output.lstat()
    except OSError:
        _add_reason(reasons, "artifact_missing")
        return
    if not stat.S_ISREG(metadata.st_mode):
        _add_reason(reasons, "artifact_not_regular_file")
        return

    try:
        output_sha256_before = _file_sha256(output)
    except OSError:
        _add_reason(reasons, "artifact_unreadable")
        return
    record["output_sha256"] = output_sha256_before
    expected_sha256, conflicting_hashes = _expected_artifact_hash(row)
    record["expected_output_sha256"] = expected_sha256
    if conflicting_hashes:
        _add_reason(reasons, "stored_artifact_hashes_conflict")
    elif expected_sha256 is None:
        _add_reason(reasons, "stored_artifact_hash_missing")
    elif not all(character in "0123456789abcdef" for character in expected_sha256) or len(
        expected_sha256
    ) != 64:
        _add_reason(reasons, "stored_artifact_hash_invalid")
    elif output_sha256_before != expected_sha256:
        _add_reason(reasons, "artifact_hash_mismatch")

    try:
        workbook = load_workbook(output, read_only=True, data_only=False)
        try:
            record["sheet_names"] = list(workbook.sheetnames)
        finally:
            workbook.close()
    except Exception as exc:
        record["reopen_error_type"] = type(exc).__name__
        _add_reason(reasons, "artifact_reopen_failed")
        return

    try:
        fresh = compare_workbooks(
            task.golden_path,
            output,
            task.answer_position,
            answer_sheet=task.answer_sheet,
        )
    except Exception as exc:
        record["fresh_score_error_type"] = type(exc).__name__
        _add_reason(reasons, "fresh_score_failed")
        return
    fresh_dict = fresh.to_dict()
    record["fresh_comparison"] = fresh_dict
    stored_passed = row.get("passed")
    if not isinstance(stored_passed, bool):
        _add_reason(reasons, "stored_passed_not_boolean")
    elif stored_passed != fresh.passed:
        _add_reason(reasons, "stored_passed_mismatch")
    if row.get("comparison") != fresh_dict:
        _add_reason(reasons, "stored_comparison_mismatch")

    try:
        output_sha256_after = _file_sha256(output)
    except OSError:
        _add_reason(reasons, "artifact_unreadable_after_scoring")
        return
    if output_sha256_after != output_sha256_before:
        _add_reason(reasons, "artifact_changed_during_audit")


def audit_comparison(
    results_dir: str | Path,
    tasks: Iterable[SpreadsheetTask],
    arms: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Audit a comparison directory without modifying its journal or artifacts.

    The returned result is deliberately fail-closed: every selected task/arm must
    have exactly one completed row, a contained immutable artifact, a matching
    recorded hash, and a fresh score identical to the journaled score.
    """

    root = Path(os.path.abspath(Path(results_dir).expanduser()))
    manifest_path = root / "comparison-manifest.json"
    results_path = root / "results.jsonl"
    reasons: list[str] = []
    manifest = _load_manifest(manifest_path, reasons)
    if manifest.get("schema_version") != COMPARISON_MANIFEST_SCHEMA_VERSION:
        _add_reason(reasons, "comparison_manifest_schema_mismatch")
    if manifest.get("comparison_protocol_version") != COMPARISON_PROTOCOL_VERSION:
        _add_reason(reasons, "comparison_manifest_protocol_mismatch")
    _audit_manifest_contract(manifest, reasons)
    try:
        manifest_sha256 = _file_sha256(manifest_path)
    except OSError:
        manifest_sha256 = None

    task_list = list(tasks)
    task_counts = Counter(task.task_id for task in task_list)
    duplicate_task_ids = sorted(task_id for task_id, count in task_counts.items() if count > 1)
    for task_id in duplicate_task_ids:
        _add_reason(reasons, f"duplicate_input_task:{task_id}")
    unique_tasks: list[SpreadsheetTask] = []
    seen_tasks: set[str] = set()
    for task in task_list:
        if task.task_id not in seen_tasks:
            seen_tasks.add(task.task_id)
            unique_tasks.append(task)

    raw_arms = list(arms) if arms is not None else manifest.get("arms")
    if not isinstance(raw_arms, list | tuple) or not raw_arms:
        selected_arms: tuple[str, ...] = ()
        _add_reason(reasons, "audit_arms_invalid")
    else:
        selected_arms = tuple(str(arm) for arm in raw_arms)
        if len(selected_arms) != len(set(selected_arms)):
            _add_reason(reasons, "audit_arms_duplicate")
            selected_arms = tuple(dict.fromkeys(selected_arms))
    if manifest and manifest.get("arms") != list(selected_arms):
        _add_reason(reasons, "manifest_arms_mismatch")

    if not root.is_dir():
        _add_reason(reasons, "results_dir_missing")
    if root.is_symlink():
        _add_reason(reasons, "results_dir_is_symlink")

    task_manifest_reasons = _manifest_task_reasons(manifest, unique_tasks, reasons)
    raw_rows = _load_result_rows(results_path, reasons)
    rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    expected_keys = {
        (task.task_id, arm) for task in unique_tasks for arm in selected_arms
    }
    for row_number, row in enumerate(raw_rows, start=1):
        if row.get("comparison_protocol_version") != COMPARISON_PROTOCOL_VERSION:
            _add_reason(reasons, f"result_protocol_mismatch:{row_number}")
        if row.get("task_id") is None or row.get("arm") is None:
            _add_reason(reasons, f"result_identity_missing:{row_number}")
            continue
        key = (str(row["task_id"]), str(row["arm"]))
        rows_by_key.setdefault(key, []).append(row)
        if key not in expected_keys:
            _add_reason(reasons, f"unexpected_result_row:{key[0]}::{key[1]}")

    audited_rows: list[dict[str, Any]] = []
    for task in unique_tasks:
        for arm in selected_arms:
            key = (task.task_id, arm)
            candidates = rows_by_key.get(key, [])
            record: dict[str, Any] = {
                "task_id": task.task_id,
                "arm": arm,
                "audit_valid": False,
                "reasons": list(task_manifest_reasons.get(task.task_id, [])),
            }
            if not candidates:
                _add_reason(record["reasons"], "missing_result_row")
            else:
                if len(candidates) > 1:
                    _add_reason(record["reasons"], "duplicate_result_rows")
                _audit_completed_row(
                    record,
                    candidates[0],
                    task,
                    arm,
                    root,
                    manifest,
                    manifest_sha256,
                )
            record["audit_valid"] = not record["reasons"]
            for reason in record["reasons"]:
                _add_reason(reasons, f"{task.task_id}::{arm}:{reason}")
            audited_rows.append(record)

    results_sha256 = None
    try:
        results_sha256 = _file_sha256(results_path)
    except OSError:
        pass
    valid_rows = sum(bool(row["audit_valid"]) for row in audited_rows)
    return {
        "schema_version": 2,
        "audit_valid": not reasons and valid_rows == len(audited_rows),
        "reasons": reasons,
        "results_dir": str(root),
        "manifest_sha256": manifest_sha256,
        "results_sha256": results_sha256,
        "task_count": len(unique_tasks),
        "arms": list(selected_arms),
        "expected_rows": len(expected_keys),
        "observed_rows": len(raw_rows),
        "valid_rows": valid_rows,
        "rows": audited_rows,
    }
