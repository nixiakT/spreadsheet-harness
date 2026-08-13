"""Read-only integrity audit for completed SpreadsheetBench comparisons."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .arms import COMPARISON_FORCED_TOOL_PREFIX_POLICY, comparison_stage_turn_caps
from .benchmark import (
    SpreadsheetTask,
    _source_fingerprint,
    compare_workbooks,
    verify_trace2skill_split_provenance,
)
from .comparison import (
    COMPARISON_CONFIGURATION_POLICIES,
    COMPARISON_MANIFEST_SCHEMA_VERSION,
    COMPARISON_PROTOCOL_VERSION,
    CONTINUATION_SOURCE_FILENAME,
    INFLIGHT_FILENAME,
    INTERRUPTED_SEALS_FILENAME,
    LEGACY_COMPARISON_CONFIGURATION_POLICIES,
    LEGACY_COMPARISON_MANIFEST_SCHEMA_VERSION,
    LEGACY_COMPARISON_PROTOCOL_VERSION,
    LEGACY_PILOT_MANIFEST_SHA256,
    RUN_SPEC_COPY_FILENAME,
    _allowed_observed_terminals_policy,
    _request_attempt_audit,
    _stage_allowed_tools_policy,
    manifest_execution_contract,
    parse_pilot_run_spec_bytes,
    protected_run_spec_split_ids,
    resolve_run_spec_anchor,
    verify_pilot_run_spec_contract,
    verify_pilot_run_spec_provenance,
)
from .errors import AGENT_EXECUTION_FAILURE_REASONS, HarnessError


@dataclass(frozen=True)
class _AuditProtocolContract:
    protocol_version: str
    manifest_schema_version: int
    configuration_policies: dict[str, Any]
    require_v24_outcome_fields: bool
    strict_current_source: bool


_V23_AUDIT_CONTRACT = _AuditProtocolContract(
    protocol_version=LEGACY_COMPARISON_PROTOCOL_VERSION,
    manifest_schema_version=LEGACY_COMPARISON_MANIFEST_SCHEMA_VERSION,
    configuration_policies=LEGACY_COMPARISON_CONFIGURATION_POLICIES,
    require_v24_outcome_fields=False,
    strict_current_source=False,
)
_V24_AUDIT_CONTRACT = _AuditProtocolContract(
    protocol_version=COMPARISON_PROTOCOL_VERSION,
    manifest_schema_version=COMPARISON_MANIFEST_SCHEMA_VERSION,
    configuration_policies=COMPARISON_CONFIGURATION_POLICIES,
    require_v24_outcome_fields=True,
    strict_current_source=True,
)


def _select_audit_contract(
    manifest: dict[str, Any],
    manifest_sha256: str | None,
    reasons: list[str],
) -> _AuditProtocolContract | None:
    identity = (
        manifest.get("comparison_protocol_version"),
        manifest.get("schema_version"),
    )
    if identity == (
        _V24_AUDIT_CONTRACT.protocol_version,
        _V24_AUDIT_CONTRACT.manifest_schema_version,
    ):
        return _V24_AUDIT_CONTRACT
    if identity == (
        _V23_AUDIT_CONTRACT.protocol_version,
        _V23_AUDIT_CONTRACT.manifest_schema_version,
    ):
        if manifest_sha256 != LEGACY_PILOT_MANIFEST_SHA256:
            _add_reason(reasons, "legacy_comparison_manifest_sha256_mismatch")
        return _V23_AUDIT_CONTRACT
    _add_reason(reasons, "comparison_manifest_protocol_schema_unsupported")
    if manifest.get("schema_version") not in {
        _V23_AUDIT_CONTRACT.manifest_schema_version,
        _V24_AUDIT_CONTRACT.manifest_schema_version,
    }:
        _add_reason(reasons, "comparison_manifest_schema_mismatch")
    if manifest.get("comparison_protocol_version") not in {
        _V23_AUDIT_CONTRACT.protocol_version,
        _V24_AUDIT_CONTRACT.protocol_version,
    }:
        _add_reason(reasons, "comparison_manifest_protocol_mismatch")
    return None


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


def _regular_file_bytes_for_audit(path: Path) -> bytes:
    if path.is_symlink():
        raise OSError(f"symlink is not allowed: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"not a regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _reject_duplicate_json_keys_for_audit(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_interrupted_seals(
    path: Path,
    *,
    manifest: dict[str, Any],
    manifest_sha256: str | None,
    expected_keys: set[tuple[str, str]],
    reasons: list[str],
    protocol_version: str | None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """Load exact non-replay seals without treating unknown outcomes as failures."""

    if not path.exists():
        return {}, None
    seals_sha256: str | None = None
    try:
        raw = _regular_file_bytes_for_audit(path)
        seals_sha256 = hashlib.sha256(raw).hexdigest()
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys_for_audit,
        )
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "seals",
        }:
            raise ValueError("invalid interrupted seals document fields")
        seals = document.get("seals")
        if document.get("schema_version") != 1 or not isinstance(seals, list):
            raise ValueError("invalid interrupted seals document")
        required_fields = {
            "schema_version",
            "task_id",
            "arm",
            "comparison_protocol_version",
            "comparison_manifest_sha256",
            "split_provenance",
            "run_spec_provenance",
            "status",
            "passed",
            "outcome_observed",
            "score_available",
            "usage_observed",
            "replay_permitted",
            "error_retryable",
            "error_category",
            "sealed_at",
            "sealed_from_inflight_marker_sha256",
        }
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for seal in seals:
            if not isinstance(seal, dict) or set(seal) != required_fields:
                raise ValueError("invalid interrupted seal fields")
            task_id = seal.get("task_id")
            arm = seal.get("arm")
            key = (task_id, arm)
            marker_sha256 = seal.get("sealed_from_inflight_marker_sha256")
            sealed_at = seal.get("sealed_at")
            try:
                parsed_sealed_at = datetime.fromisoformat(sealed_at)
            except (TypeError, ValueError):
                raise ValueError("invalid interrupted seal timestamp") from None
            if (
                not isinstance(task_id, str)
                or not isinstance(arm, str)
                or key not in expected_keys
                or key in by_key
                or seal.get("schema_version") != 1
                or seal.get("comparison_protocol_version")
                != protocol_version
                or seal.get("comparison_manifest_sha256") != manifest_sha256
                or seal.get("split_provenance") != manifest.get("split_provenance")
                or seal.get("run_spec_provenance")
                != manifest.get("run_spec_provenance")
                or seal.get("status") != "interrupted"
                or seal.get("passed") is not None
                or seal.get("outcome_observed") is not False
                or seal.get("score_available") is not False
                or seal.get("usage_observed") is not False
                or seal.get("replay_permitted") is not False
                or seal.get("error_retryable") is not False
                or seal.get("error_category") != "interrupted_unknown_outcome"
                or parsed_sealed_at.tzinfo is None
                or parsed_sealed_at.utcoffset() is None
                or not isinstance(marker_sha256, str)
                or len(marker_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in marker_sha256
                )
            ):
                raise ValueError("invalid interrupted seal")
            by_key[key] = seal
        return by_key, seals_sha256
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _add_reason(reasons, "interrupted_arm_task_seals_invalid")
        return {}, seals_sha256


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
        raw = _regular_file_bytes_for_audit(path)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys_for_audit,
        )
    except FileNotFoundError:
        _add_reason(reasons, "comparison_manifest_missing")
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _add_reason(reasons, "comparison_manifest_invalid")
        return {}
    if not isinstance(value, dict):
        _add_reason(reasons, "comparison_manifest_invalid")
        return {}
    return value


def _load_result_rows(path: Path, reasons: list[str]) -> list[dict[str, Any]]:
    try:
        raw = _regular_file_bytes_for_audit(path)
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
            row = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys_for_audit,
            )
        except (json.JSONDecodeError, ValueError):
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


def _valid_git_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _current_repository_git_identity() -> dict[str, str] | None:
    root = Path(__file__).resolve().parents[2]

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if completed.returncode != 0:
            raise OSError("git identity lookup failed")
        return completed.stdout.strip()

    try:
        return {
            "git_commit": git("rev-parse", "--verify", "HEAD^{commit}"),
            "git_tree": git("rev-parse", "--verify", "HEAD^{tree}"),
            "remote_tracking_commit": git(
                "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"
            ),
        }
    except (OSError, subprocess.SubprocessError):
        return None


def _load_continuation_source(
    path: Path,
    *,
    manifest_sha256: str | None,
    required: bool,
    reasons: list[str],
    strict_current_source: bool = True,
    expected_repository_source: dict[str, Any] | None = None,
    bind_repository_source: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        if required:
            _add_reason(reasons, "continuation_source_missing")
        return None, None
    record_sha256: str | None = None
    try:
        raw = _regular_file_bytes_for_audit(path)
        file_sha256 = hashlib.sha256(raw).hexdigest()
        record = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys_for_audit,
        )
        if not isinstance(record, dict) or set(record) != {
            "schema_version",
            "comparison_manifest_sha256",
            "repository_source",
            "record_sha256",
        }:
            raise ValueError("invalid continuation source fields")
        repository_source = record.get("repository_source")
        if not isinstance(repository_source, dict) or set(repository_source) != {
            "schema_version",
            "git_commit",
            "git_tree",
            "remote_tracking_ref",
            "remote_tracking_commit",
            "remote_name",
            "remote_ref",
            "remote_observed_commit",
            "source_fingerprint",
        }:
            raise ValueError("invalid repository source fields")
        unsigned = {
            "schema_version": record.get("schema_version"),
            "comparison_manifest_sha256": record.get(
                "comparison_manifest_sha256"
            ),
            "repository_source": repository_source,
        }
        record_sha256 = _text_sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if (
            record.get("schema_version") != 1
            or record.get("comparison_manifest_sha256") != manifest_sha256
            or record.get("record_sha256") != record_sha256
            or repository_source.get("schema_version") != 1
            or not _valid_git_object_id(repository_source.get("git_commit"))
            or not _valid_git_object_id(repository_source.get("git_tree"))
            or repository_source.get("remote_tracking_ref")
            != "refs/remotes/origin/main"
            or repository_source.get("remote_tracking_commit")
            != repository_source.get("git_commit")
            or repository_source.get("remote_name") != "origin"
            or repository_source.get("remote_ref") != "refs/heads/main"
            or repository_source.get("remote_observed_commit")
            != repository_source.get("git_commit")
            or not _valid_source_fingerprint(
                repository_source.get("source_fingerprint")
            )
            or (
                strict_current_source
                and repository_source.get("source_fingerprint")
                != _source_fingerprint()
            )
            or (bind_repository_source and repository_source != expected_repository_source)
        ):
            raise ValueError("invalid continuation source")
        return record, file_sha256
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _add_reason(reasons, "continuation_source_invalid")
        return None, record_sha256


def _audit_manifest_contract(
    manifest: dict[str, Any],
    reasons: list[str],
    *,
    results_root: Path,
    manifest_sha256: str | None,
    contract: _AuditProtocolContract | None,
) -> None:
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
    elif contract is not None and any(
        configuration.get(field) != expected
        for field, expected in contract.configuration_policies.items()
    ):
        _add_reason(reasons, "comparison_manifest_policy_mismatch")
    if not _valid_source_fingerprint(manifest.get("harness_source")):
        _add_reason(reasons, "comparison_manifest_source_fingerprint_invalid")
    elif (
        contract is not None
        and contract.strict_current_source
        and manifest.get("harness_source") != _source_fingerprint()
    ):
        _add_reason(reasons, "comparison_manifest_source_checkout_mismatch")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or not runtime.get("python"):
        _add_reason(reasons, "comparison_manifest_runtime_invalid")
    split_provenance = manifest.get("split_provenance")
    if split_provenance is not None:
        task_ids = manifest.get("task_ids")
        required_split_fields = {
            "manifest_id",
            "schema_version",
            "manifest_sha256",
            "task_count",
            "task_ids_sha256",
            "dataset_json_sha256",
        }
        if (
            not isinstance(split_provenance, dict)
            or set(split_provenance) != required_split_fields
            or not isinstance(task_ids, list)
            or split_provenance.get("task_count") != len(task_ids)
            or split_provenance.get("task_ids_sha256")
            != _text_sha256("".join(f"{task_id}\n" for task_id in task_ids))
            or split_provenance.get("dataset_json_sha256")
            != manifest.get("dataset_manifest_sha256")
            or any(
                not isinstance(split_provenance.get(field), str)
                or not split_provenance[field]
                for field in ("schema_version", "manifest_sha256", "task_ids_sha256")
            )
            or any(
                len(str(split_provenance.get(field))) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in str(split_provenance.get(field))
                )
                for field in (
                    "manifest_sha256",
                    "task_ids_sha256",
                    "dataset_json_sha256",
                )
            )
            or not verify_trace2skill_split_provenance(split_provenance)
        ):
            _add_reason(reasons, "comparison_manifest_split_provenance_invalid")
    run_spec_provenance = manifest.get("run_spec_provenance")
    split_manifest_id = (
        split_provenance.get("manifest_id")
        if isinstance(split_provenance, dict)
        and isinstance(split_provenance.get("manifest_id"), str)
        else None
    )
    if (
        isinstance(split_provenance, dict)
        and split_manifest_id in protected_run_spec_split_ids()
        and run_spec_provenance is None
    ):
        _add_reason(reasons, "comparison_manifest_run_spec_missing_for_pilot")
    if run_spec_provenance is not None:
        if not verify_pilot_run_spec_provenance(run_spec_provenance):
            _add_reason(reasons, "comparison_manifest_run_spec_provenance_invalid")
        try:
            raw_spec = _regular_file_bytes_for_audit(
                results_root / RUN_SPEC_COPY_FILENAME
            )
            document, provenance = parse_pilot_run_spec_bytes(raw_spec)
            if provenance != run_spec_provenance:
                _add_reason(reasons, "comparison_manifest_run_spec_copy_mismatch")
            anchor = resolve_run_spec_anchor(provenance)
            if contract is None or (
                anchor.comparison_protocol_version != contract.protocol_version
                or anchor.comparison_manifest_schema_version
                != contract.manifest_schema_version
            ):
                _add_reason(reasons, "comparison_manifest_run_spec_version_mismatch")
            verify_pilot_run_spec_contract(
                document, manifest_execution_contract(manifest)
            )
        except (HarnessError, OSError):
            _add_reason(reasons, "comparison_manifest_run_spec_contract_invalid")
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
            _allowed_observed_terminals_policy(
                expected_caps,
                protocol_version=(
                    contract.protocol_version
                    if contract is not None
                    else COMPARISON_PROTOCOL_VERSION
                ),
            )
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
    if row.get("split_provenance") != manifest.get("split_provenance"):
        _add_reason(reasons, "row_manifest_mismatch:split_provenance")
    if row.get("run_spec_provenance") != manifest.get("run_spec_provenance"):
        _add_reason(reasons, "row_manifest_mismatch:run_spec_provenance")
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
    contract: _AuditProtocolContract | None,
) -> None:
    reasons: list[str] = record["reasons"]
    _audit_row_contract(record, row, task, arm, manifest, manifest_sha256)
    if row.get("status") != "completed":
        _add_reason(reasons, "status_not_completed")
        return
    require_v24_fields = bool(contract and contract.require_v24_outcome_fields)
    outcome_kind = row.get("outcome_kind") if require_v24_fields else "scored"
    model_execution_failure = outcome_kind == "model_execution_failure"
    record["outcome_kind"] = outcome_kind
    record["error_category"] = row.get("error_category")
    record["model_failure_reason"] = row.get("model_failure_reason")
    record["error_type"] = row.get("error_type")
    record["error"] = row.get("error")
    if require_v24_fields and outcome_kind not in {"scored", "model_execution_failure"}:
        _add_reason(reasons, "outcome_kind_invalid")
    if model_execution_failure:
        if row.get("passed") is not False:
            _add_reason(reasons, "model_execution_failure_not_failed")
        if row.get("error_category") != "model_execution_failure":
            _add_reason(reasons, "model_execution_failure_category_invalid")
        if row.get("model_failure_reason") not in AGENT_EXECUTION_FAILURE_REASONS:
            _add_reason(reasons, "model_execution_failure_reason_invalid")
        if row.get("error_type") != "AgentExecutionFailure":
            _add_reason(reasons, "model_execution_failure_type_invalid")
        if not isinstance(row.get("error"), str) or not row["error"]:
            _add_reason(reasons, "model_execution_failure_error_missing")
        if row.get("error_retryable") is not False:
            _add_reason(reasons, "model_execution_failure_retryable_invalid")
    elif require_v24_fields and any(
        field in row
        for field in (
            "model_failure_reason",
            "error_category",
            "error_type",
            "error_retryable",
        )
    ):
        _add_reason(reasons, "scored_outcome_has_failure_metadata")
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
    if require_v24_fields and row.get("artifact_score_passed") is not fresh.passed:
        _add_reason(reasons, "stored_artifact_score_passed_mismatch")
    stored_passed = row.get("passed")
    if not isinstance(stored_passed, bool):
        _add_reason(reasons, "stored_passed_not_boolean")
    elif model_execution_failure:
        record["outcome_passed"] = False
    elif stored_passed != fresh.passed:
        _add_reason(reasons, "stored_passed_mismatch")
    else:
        record["outcome_passed"] = stored_passed
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

    Integrity is deliberately fail-closed. A selected task/arm must have either
    one freshly verified completed row or one exact non-replay interruption seal.
    A valid seal can preserve journal integrity, but never study completeness or
    inferential validity because its outcome is unknown.
    """

    root = Path(os.path.abspath(Path(results_dir).expanduser()))
    manifest_path = root / "comparison-manifest.json"
    results_path = root / "results.jsonl"
    reasons: list[str] = []
    manifest = _load_manifest(manifest_path, reasons)
    try:
        manifest_sha256 = _file_sha256(manifest_path)
    except OSError:
        manifest_sha256 = None
    contract = _select_audit_contract(manifest, manifest_sha256, reasons)
    _audit_manifest_contract(
        manifest,
        reasons,
        results_root=root,
        manifest_sha256=manifest_sha256,
        contract=contract,
    )
    if (root / INFLIGHT_FILENAME).exists():
        _add_reason(reasons, "ambiguous_inflight_arm_task")
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

    split_provenance = manifest.get("split_provenance")
    raw_split_manifest_id = (
        split_provenance.get("manifest_id")
        if isinstance(split_provenance, dict)
        else None
    )
    registered_run = isinstance(raw_split_manifest_id, str) and (
        raw_split_manifest_id in protected_run_spec_split_ids()
    )
    strict_registered_source = bool(
        registered_run and contract is not None and contract.strict_current_source
    )
    manifest_repository_source = manifest.get("repository_source")
    if strict_registered_source:
        current_identity = _current_repository_git_identity()
        if not isinstance(manifest_repository_source, dict):
            _add_reason(reasons, "comparison_manifest_repository_source_invalid")
        elif current_identity is None:
            _add_reason(reasons, "current_repository_git_identity_unavailable")
        elif any(
            manifest_repository_source.get(field) != value
            for field, value in current_identity.items()
        ):
            _add_reason(reasons, "comparison_manifest_repository_checkout_mismatch")
    continuation_source, continuation_source_file_sha256 = _load_continuation_source(
        root / CONTINUATION_SOURCE_FILENAME,
        manifest_sha256=manifest_sha256,
        required=registered_run,
        reasons=reasons,
        strict_current_source=bool(contract is None or contract.strict_current_source),
        expected_repository_source=manifest_repository_source,
        bind_repository_source=bool(
            contract is not None
            and contract.strict_current_source
            and (registered_run or manifest_repository_source is not None)
        ),
    )

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
    interrupted_seals, interrupted_seals_sha256 = _load_interrupted_seals(
        root / INTERRUPTED_SEALS_FILENAME,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        expected_keys=expected_keys,
        reasons=reasons,
        protocol_version=contract.protocol_version if contract is not None else None,
    )
    if (
        not results_path.exists()
        and set(interrupted_seals) == expected_keys
        and "results_file_missing" in reasons
    ):
        reasons.remove("results_file_missing")
    interrupted_keys = sorted(f"{task_id}::{arm}" for task_id, arm in interrupted_seals)
    for key in interrupted_keys:
        _add_reason(reasons, f"interrupted_unknown_outcome:{key}")
    for row_number, row in enumerate(raw_rows, start=1):
        if (
            contract is None
            or row.get("comparison_protocol_version") != contract.protocol_version
        ):
            _add_reason(reasons, f"result_protocol_mismatch:{row_number}")
        if row.get("task_id") is None or row.get("arm") is None:
            _add_reason(reasons, f"result_identity_missing:{row_number}")
            continue
        key = (str(row["task_id"]), str(row["arm"]))
        rows_by_key.setdefault(key, []).append(row)
        if key not in expected_keys:
            _add_reason(reasons, f"unexpected_result_row:{key[0]}::{key[1]}")
        if key in interrupted_seals:
            _add_reason(reasons, f"result_row_conflicts_with_interrupted_seal:{key[0]}::{key[1]}")
        row_continuation_source = row.get("continuation_source")
        if continuation_source is not None:
            legacy_row_without_continuation = (
                manifest_sha256 == LEGACY_PILOT_MANIFEST_SHA256
                and row_continuation_source is None
            )
            if (
                row_continuation_source != continuation_source
                and not legacy_row_without_continuation
            ):
                _add_reason(reasons, f"result_continuation_source_mismatch:{row_number}")
        elif row_continuation_source is not None:
            _add_reason(reasons, f"result_continuation_source_without_record:{row_number}")

    audited_rows: list[dict[str, Any]] = []
    for task in unique_tasks:
        for arm in selected_arms:
            key = (task.task_id, arm)
            candidates = rows_by_key.get(key, [])
            record: dict[str, Any] = {
                "task_id": task.task_id,
                "arm": arm,
                "audit_valid": False,
                "journal_integrity_valid": False,
                "outcome_observed": key not in interrupted_seals,
                "reasons": list(task_manifest_reasons.get(task.task_id, [])),
            }
            if key in interrupted_seals and not candidates:
                record["status"] = "interrupted"
                record["error_category"] = "interrupted_unknown_outcome"
                record["seal"] = interrupted_seals[key]
                record["journal_integrity_valid"] = not record["reasons"]
                _add_reason(record["reasons"], "interrupted_unknown_outcome")
            elif not candidates:
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
                    contract,
                )
            record["audit_valid"] = not record["reasons"]
            if key not in interrupted_seals:
                record["journal_integrity_valid"] = record["audit_valid"]
                for reason in record["reasons"]:
                    _add_reason(reasons, f"{task.task_id}::{arm}:{reason}")
            audited_rows.append(record)

    results_sha256 = None
    try:
        results_sha256 = _file_sha256(results_path)
    except OSError:
        pass
    valid_rows = sum(bool(row["audit_valid"]) for row in audited_rows)
    journal_integrity_valid = all(
        row["journal_integrity_valid"] for row in audited_rows
    ) and not any(
        not reason.startswith("interrupted_unknown_outcome:") for reason in reasons
    )
    study_complete = journal_integrity_valid and not interrupted_seals
    known_passed_rows = sum(
        row.get("outcome_passed") is True
        for row in audited_rows
        if row.get("outcome_observed") is True
    )
    known_failed_rows = sum(
        row.get("outcome_passed") is False
        for row in audited_rows
        if row.get("outcome_observed") is True
    )
    report = {
        "schema_version": 2,
        "audit_valid": journal_integrity_valid,
        "journal_integrity_valid": journal_integrity_valid,
        "study_complete": study_complete,
        "inference_valid": study_complete,
        "inference_invalid_reasons": (
            [] if study_complete else ["interrupted_unknown_outcome"]
            if journal_integrity_valid and interrupted_seals
            else ["comparison_audit_failed"]
        ),
        "reasons": reasons,
        "results_dir": str(root),
        "manifest_sha256": manifest_sha256,
        "results_sha256": results_sha256,
        "interrupted_seals_sha256": interrupted_seals_sha256,
        "continuation_source": continuation_source,
        "continuation_source_file_sha256": continuation_source_file_sha256,
        "split_provenance": manifest.get("split_provenance"),
        "run_spec_provenance": manifest.get("run_spec_provenance"),
        "task_count": len(unique_tasks),
        "arms": list(selected_arms),
        "expected_rows": len(expected_keys),
        "observed_rows": len(raw_rows),
        "valid_rows": valid_rows,
        "interrupted_arm_tasks": len(interrupted_keys),
        "interrupted_arm_task_keys": interrupted_keys,
        "known_passed_rows": known_passed_rows,
        "known_failed_rows": known_failed_rows,
        "known_model_execution_failure_rows": sum(
            row.get("outcome_kind") == "model_execution_failure"
            for row in audited_rows
            if row.get("outcome_observed") is True
        ),
        "rows": audited_rows,
    }
    if not study_complete:
        report.update(
            {
                "mcnemar_exact_p": None,
                "stratified_bootstrap_95": None,
                "holm_adjusted_p": None,
            }
        )
    return report
