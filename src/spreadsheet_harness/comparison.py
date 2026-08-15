"""Resource-matched, resumable three-arm SpreadsheetBench comparison."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import stat
import subprocess
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any

from .agent import (
    ASSISTANT_TEXT_TERMINAL,
    BUDGET_EXHAUSTED_TERMINAL,
    CONNECT_RETRY_MIN_SECONDS,
    OVERLOAD_RETRY_MIN_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
    SAFE_AUTOMATIC_RETRY_REASONS,
    SAFE_RETRY_HTTP_STATUSES,
    TERMINAL_TOOL_NAME,
)
from .arms import (
    BARE_TOOLS,
    COMPARISON_EDIT_RECOVERY_POLICY_VERSION,
    COMPARISON_FORCED_TOOL_PREFIX_POLICY,
    COMPARISON_TURN_CAP_POLICY_VERSION,
    OURS_TOOLS,
    PAPER_EXTRACTION_TOOLS,
    PAPER_LATEX_TOOLS,
    PAPER_RECONCILIATION_TOOLS,
    PAPER_SOLVER_TOOLS,
    PAPER_TURN_CAP_SCALING_VERSION,
    PAPER_VISION_TOOLS,
    PaperStageValidationError,
    comparison_stage_turn_caps,
    run_arm,
)
from .benchmark import (
    VERIFIED_REVISION,
    VERIFIED_SHA256,
    SpreadsheetTask,
    _atomic_write_json,
    _reject_duplicate_json_keys,
    _run_spec_source_fingerprint,
    _runtime_fingerprint,
    _sha256,
    _source_fingerprint,
    _text_sha256,
    compare_workbooks_chartsheet_safe,
    comparison_evidence,
    require_evaluation_task_authorization,
    verify_trace2skill_split_provenance,
)
from .budget import RunBudget
from .code_interpreter import STRICT_ISOLATION_POLICY, ensure_strict_code_isolation
from .config import ProviderConfig
from .errors import (
    AGENT_EXECUTION_FAILURE_REASONS,
    AGENT_TOOL_RECALCULATION_FAILURE_STAGE,
    LEGACY_AGENT_EXECUTION_FAILURE_REASONS,
    POSTPROCESS_RECALCULATION_FAILURE_STAGE,
    RECALCULATION_VALIDATION_TOOL,
    AgentBudgetError,
    AgentExecutionFailure,
    AgentRoutingError,
    AgentTimeoutError,
    CodeIsolationError,
    HarnessError,
    ProviderError,
    RecalculationIntegrityError,
    ScoringInfrastructureError,
)
from .pacing import PACING_POLICY, RelayPacer
from .preprocess import (
    DETERMINISTIC_PROFILE_BOUNDS,
    DETERMINISTIC_PROFILE_SCHEMA_VERSION,
    build_deterministic_profile,
)
from .render import RECALCULATION_SHEET_INTEGRITY_POLICY
from .session import WorkbookSession
from .skills import SkillRegistry

DEFAULT_COMPARISON_ARMS = ("bare", "paper", "ours")
AVAILABLE_COMPARISON_ARMS = ("bare", "profile", "native", "paper", "ours")
# Backwards-compatible name for the historical default three-arm protocol.
COMPARISON_ARMS = DEFAULT_COMPARISON_ARMS
COMPARISON_ARM_DISPLAY_NAMES = {
    "bare": "bare",
    "profile": "bare + deterministic profile",
    "native": "native harness without skills",
    "paper": "paper-inspired",
    "ours": "ours",
}
TERMINAL_SUBMISSION_TRUNCATED_OBSERVED = "submit_result_length"
HISTORICAL_FINAL_RECOVERY_TERMINAL = "final_recovery_code_interpreter"
V24_COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v24"
V24_COMPARISON_MANIFEST_SCHEMA_VERSION = 13
V25_COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v25"
V25_COMPARISON_MANIFEST_SCHEMA_VERSION = 14
V25_RUN_SPEC_SOURCE_CONTRACT = {
    "schema_version": 1,
    "policy": "python-package-pyproject-normalized-run-spec-anchor-sha-v1",
    "sha256": "3ce79390a288a039fd411e0f77f81c879a83f653a242f85ed305da64c159ad0b",
    "file_count": 21,
}
V26_COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v26"
V26_COMPARISON_MANIFEST_SCHEMA_VERSION = 15
V26_RUN_SPEC_SOURCE_CONTRACT = {
    "schema_version": 1,
    "policy": "python-package-pyproject-normalized-run-spec-anchor-sha-v1",
    "sha256": "10ead91dc5e40b5f065b09e2c0b132342350cc7afa6edd3d8d38d2edc6f4a1d3",
    "file_count": 21,
}
V27_COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v27"
V27_COMPARISON_MANIFEST_SCHEMA_VERSION = 16
V27_RUN_SPEC_SOURCE_CONTRACT = {
    "schema_version": 1,
    "policy": "python-package-pyproject-normalized-run-spec-anchor-sha-v1",
    "sha256": "ab359f5c45ab797ec1b88ae1cfa54e50c9aba7fd44d6fddeb28e0a5df1448328",
    "file_count": 21,
}
COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v28"
COMPARISON_MANIFEST_SCHEMA_VERSION = 17
_V26_RUNTIME_PROTOCOL_VERSIONS = frozenset(
    {
        V26_COMPARISON_PROTOCOL_VERSION,
        V27_COMPARISON_PROTOCOL_VERSION,
        COMPARISON_PROTOCOL_VERSION,
    }
)
PILOT_RUN_SPEC_SCHEMA_VERSION = "spreadsheet-harness-comparison-run-spec-v1"
PILOT_RUN_SPEC_ID = "qwen36-local-pilot16-v2-bare-ours-v23-seed41"
PILOT_RUN_SPEC_FILENAME = "qwen35-trace2skill-local-pilot16-run-spec-v1.json"
PILOT_RUN_SPEC_SHA256 = (
    "8dc1583b96a76209023586c8ebafde9dfa1a55cb31778e88247da595bdd60086"
)
RUN_SPEC_COPY_FILENAME = "run-spec.json"
INFLIGHT_FILENAME = ".inflight-arm-task.json"
INTERRUPTED_SEALS_FILENAME = "interrupted-arm-tasks.json"
CONTINUATION_SOURCE_FILENAME = "continuation-source.json"
PILOT_SPLIT_MANIFEST_ID = "qwen35-trace2skill-local-unattempted-pilot16-v2"
LEGACY_PILOT_MANIFEST_SHA256 = (
    "7eee847bc9880ec112ac78eefaac0c97d350da31c6e045c1d5c924e95b0b04c1"
)
LEGACY_COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v23"
LEGACY_COMPARISON_MANIFEST_SCHEMA_VERSION = 12
LEGACY_COMPARISON_CONFIGURATION_POLICIES = {
    "code_workbook_formula_gate": (
        "rollback-new-invalid-a1-or-high-confidence-unprefixed-formula-text-v2"
    ),
    "failed_edit_recovery_policy": "force-successful-code-edit-before-terminal-v1",
    "spreadsheet_skill_policy": "pre-evaluation-baseline-frozen-v1",
    "edit_recovery_prompt_policy": "self-contained-request-scoped-verification-v1",
    "result_manifest_binding_policy": "exact-manifest-sha256-v1",
    "resume_journal_policy": "durable-inflight-fail-closed-no-replay-v3",
    "request_attempt_audit_policy": "exact-attempt-history-per-response-v1",
}
V24_COMPARISON_CONFIGURATION_POLICIES = {
    "code_workbook_formula_gate": (
        "rollback-new-invalid-a1-or-high-confidence-unprefixed-formula-text-v2"
    ),
    "failed_edit_recovery_policy": COMPARISON_EDIT_RECOVERY_POLICY_VERSION,
    "spreadsheet_skill_policy": "pre-evaluation-baseline-frozen-v1",
    "edit_recovery_prompt_policy": "self-contained-request-scoped-verification-v1",
    "result_manifest_binding_policy": "exact-manifest-sha256-v1",
    "resume_journal_policy": "durable-inflight-fail-closed-no-replay-v3",
    "request_attempt_audit_policy": "exact-attempt-history-per-response-v1",
    "model_execution_failure_policy": (
        "known-false-score-artifact-and-request-audited-nonbreaker-v1"
    ),
    "model_execution_failure_reasons": sorted(
        LEGACY_AGENT_EXECUTION_FAILURE_REASONS
    ),
    "circuit_breaker_nonbreaker_categories": ["model_execution_failure"],
}
V25_COMPARISON_CONFIGURATION_POLICIES = {
    **V24_COMPARISON_CONFIGURATION_POLICIES,
    "model_execution_failure_reasons": [
        "budget_exhausted",
        "edit_recovery_exhausted",
        "terminal_submission_invalid",
        "workbook_unchanged",
    ],
}
V26_COMPARISON_CONFIGURATION_POLICIES = {
    **V25_COMPARISON_CONFIGURATION_POLICIES,
    "model_execution_failure_reasons": sorted(AGENT_EXECUTION_FAILURE_REASONS),
    "terminal_submission_policy": "empty-ack-harness-final-text-v1",
    "edit_recovery_terminal_policy": "penultimate-recovery-final-submit-v1",
    "ours_tool_policy": "fixed-six-code-first-v1",
    "deterministic_profile_policy": "representative-evidence-12k-v1",
    "formula_verification_skill_policy": "trajectory-local-transfer-gate-v1",
}
V27_COMPARISON_CONFIGURATION_POLICIES = dict(V26_COMPARISON_CONFIGURATION_POLICIES)
# v28 also fails closed if the recalculation engine changes sheet identity.
COMPARISON_CONFIGURATION_POLICIES = {
    **V27_COMPARISON_CONFIGURATION_POLICIES,
    "recalculation_integrity_policy": RECALCULATION_SHEET_INTEGRITY_POLICY,
    "recalculation_failure_policy": "audited-infrastructure-error-no-score-v1",
    "recalculation_failure_stage_policy": (
        "postprocess-or-agent-tool-recalculation-v1"
    ),
    "artifact_reopen_policy": (
        "ooxml-inventory-plus-worksheet-only-openpyxl-view-v1"
    ),
    "scoring_compatibility_policy": (
        "worksheet-only-ooxml-view-scorer-infrastructure-no-score-v1"
    ),
    "formula_runtime_gate": (
        "raw-ooxml-dirty-formula-scope-complete-clean-calc-v1"
    ),
    "formula_runtime_gate_arms": ["ours"],
    "formula_runtime_validation_scope": (
        "range-or-single-recalc-sparse-pending-formulas-v1"
    ),
}


@dataclass(frozen=True)
class RunSpecAnchor:
    """Code-owned identity and execution policy for one immutable run spec."""

    run_spec_id: str
    filename: str
    sha256: str
    schema_version: str
    phase: str
    split_manifest_id: str
    comparison_protocol_version: str
    comparison_manifest_schema_version: int
    launchable: bool
    resumable: bool = False

    def provenance(self) -> dict[str, str]:
        return {
            "run_spec_id": self.run_spec_id,
            "schema_version": self.schema_version,
            "run_spec_sha256": self.sha256,
        }


RUN_SPEC_ANCHORS = (
    RunSpecAnchor(
        run_spec_id=PILOT_RUN_SPEC_ID,
        filename=PILOT_RUN_SPEC_FILENAME,
        sha256=PILOT_RUN_SPEC_SHA256,
        schema_version=PILOT_RUN_SPEC_SCHEMA_VERSION,
        phase="exploratory_development_pilot",
        split_manifest_id=PILOT_SPLIT_MANIFEST_ID,
        comparison_protocol_version=LEGACY_COMPARISON_PROTOCOL_VERSION,
        comparison_manifest_schema_version=LEGACY_COMPARISON_MANIFEST_SCHEMA_VERSION,
        launchable=False,
    ),
    RunSpecAnchor(
        run_spec_id="qwen36-local-postopt-eval16-v3-bare-ours-v24-seed41",
        filename="qwen35-trace2skill-local-postopt16-run-spec-v1.json",
        sha256="a7e335c81cd86ec1edb81f223b103bafabec19e983a669d56f3ded6965151644",
        schema_version=PILOT_RUN_SPEC_SCHEMA_VERSION,
        phase="post_optimization_evaluation",
        split_manifest_id="qwen35-trace2skill-local-postopt16-v1",
        comparison_protocol_version=V24_COMPARISON_PROTOCOL_VERSION,
        comparison_manifest_schema_version=V24_COMPARISON_MANIFEST_SCHEMA_VERSION,
        launchable=False,
    ),
    RunSpecAnchor(
        run_spec_id="qwen36-local-confirm-eval16-v1-bare-ours-v25-seed41",
        filename="qwen35-trace2skill-local-confirm16-run-spec-v1.json",
        sha256="61ec4d37d0548e1be63ebf8619feb591d98ca78d7dce4d9d573886498ca74984",
        schema_version=PILOT_RUN_SPEC_SCHEMA_VERSION,
        phase="post_optimization_confirmation",
        split_manifest_id="qwen35-trace2skill-local-confirm16-v1",
        comparison_protocol_version=V25_COMPARISON_PROTOCOL_VERSION,
        comparison_manifest_schema_version=V25_COMPARISON_MANIFEST_SCHEMA_VERSION,
        launchable=False,
    ),
    RunSpecAnchor(
        run_spec_id="qwen36-local-v26-confirm-eval16-v1-bare-ours-seed41",
        filename="qwen35-trace2skill-local-v26-confirm16-run-spec-v1.json",
        sha256="4bca7fe452c9ba2dadc31c374f29abcda575cb243e5f960789f2f50b4191884a",
        schema_version=PILOT_RUN_SPEC_SCHEMA_VERSION,
        phase="v26_post_optimization_confirmation",
        split_manifest_id="qwen35-trace2skill-local-v26-confirm16-v1",
        comparison_protocol_version=V26_COMPARISON_PROTOCOL_VERSION,
        comparison_manifest_schema_version=V26_COMPARISON_MANIFEST_SCHEMA_VERSION,
        launchable=False,
    ),
    RunSpecAnchor(
        run_spec_id="qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41",
        filename="qwen35-trace2skill-local-v27-reserve79-run-spec-v1.json",
        # Normalized out of the executable-source hash to avoid a hash cycle.
        sha256="748fd0458e9b2c20adf5161fc9471e4f29421faecd5b4e02bdfa6b32b9342371",
        schema_version=PILOT_RUN_SPEC_SCHEMA_VERSION,
        phase="v27_reserve79_evaluation",
        split_manifest_id="qwen35-trace2skill-local-v27-reserve79-v1",
        comparison_protocol_version=V27_COMPARISON_PROTOCOL_VERSION,
        comparison_manifest_schema_version=V27_COMPARISON_MANIFEST_SCHEMA_VERSION,
        launchable=False,
    ),
)


def _run_spec_anchor_by_sha256(digest: str) -> RunSpecAnchor | None:
    return next((anchor for anchor in RUN_SPEC_ANCHORS if anchor.sha256 == digest), None)


def resolve_run_spec_anchor(value: Any) -> RunSpecAnchor:
    """Resolve a registered anchor from a document or provenance object."""

    if not isinstance(value, dict):
        raise HarnessError("Run spec identity must be a JSON object")
    digest = value.get("run_spec_sha256")
    if isinstance(digest, str):
        anchor = _run_spec_anchor_by_sha256(digest)
    else:
        run_spec_id = value.get("run_spec_id")
        anchor = next(
            (
                candidate
                for candidate in RUN_SPEC_ANCHORS
                if candidate.run_spec_id == run_spec_id
            ),
            None,
        )
    if anchor is None:
        raise HarnessError("Run spec checksum is not registered")
    if "execution" in value:
        expected = {
            "run_spec_id": anchor.run_spec_id,
            "schema_version": anchor.schema_version,
            "phase": anchor.phase,
        }
        if any(value.get(field) != expected_value for field, expected_value in expected.items()):
            raise HarnessError("Run spec identity does not match its registered anchor")
    elif value != anchor.provenance():
        raise HarnessError("Run spec provenance does not match its registered anchor")
    return anchor


def require_launchable_run_spec(
    value: Any,
    *,
    resume: bool = False,
    operation: str = "launch",
) -> RunSpecAnchor:
    anchor = resolve_run_spec_anchor(value)
    if not anchor.launchable:
        raise HarnessError(
            f"Run spec {anchor.run_spec_id} is read-only and cannot {operation}"
        )
    if resume and not anchor.resumable:
        raise HarnessError(f"Run spec {anchor.run_spec_id} is fresh-only and cannot resume")
    if (
        anchor.comparison_protocol_version != COMPARISON_PROTOCOL_VERSION
        or anchor.comparison_manifest_schema_version
        != COMPARISON_MANIFEST_SCHEMA_VERSION
    ):
        raise HarnessError("Run spec execution version is not supported by this runner")
    return anchor


def protected_run_spec_split_ids() -> frozenset[str]:
    return frozenset(anchor.split_manifest_id for anchor in RUN_SPEC_ANCHORS)


def _strict_json_document(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError(f"{label} must be valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HarnessError(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"{label} must be a JSON object")
    return value


def _strict_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.is_file():
        return [], 0
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return [], 1
    rows: list[dict[str, Any]] = []
    invalid = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError):
            invalid += 1
            continue
        if not isinstance(row, dict):
            invalid += 1
            continue
        rows.append(row)
    return rows, invalid


def _regular_file_bytes(path: Path, *, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HarnessError(f"Unable to read {label}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise HarnessError(f"{label} must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(f"Unable to read {label}: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise HarnessError(f"{label} must be a regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            offset = 0
            while offset < len(value):
                offset += os.write(descriptor, value[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def parse_pilot_run_spec_bytes(
    raw: bytes,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse exact bytes for a registered immutable execution contract."""

    digest = hashlib.sha256(raw).hexdigest()
    anchor = _run_spec_anchor_by_sha256(digest)
    if anchor is None:
        raise HarnessError("Run spec checksum does not match a registered code anchor")
    document = _strict_json_document(raw, label="run spec")
    if set(document) != {
        "schema_version",
        "run_spec_id",
        "phase",
        "repository_relative_paths",
        "execution",
    }:
        raise HarnessError("Run spec top-level fields do not match its schema")
    if (
        document.get("schema_version") != anchor.schema_version
        or document.get("run_spec_id") != anchor.run_spec_id
        or document.get("phase") != anchor.phase
        or not isinstance(document.get("repository_relative_paths"), dict)
        or not isinstance(document.get("execution"), dict)
    ):
        raise HarnessError("Run spec identity does not match its registered anchor")
    execution = document["execution"]
    split_provenance = execution.get("split_provenance")
    if (
        execution.get("comparison_protocol_version")
        != anchor.comparison_protocol_version
        or execution.get("comparison_manifest_schema_version")
        != anchor.comparison_manifest_schema_version
        or not isinstance(split_provenance, dict)
        or split_provenance.get("manifest_id") != anchor.split_manifest_id
    ):
        raise HarnessError("Run spec execution identity does not match its registered anchor")
    provenance = anchor.provenance()
    return document, provenance


def load_pilot_run_spec(path: str | Path) -> tuple[dict[str, Any], dict[str, str], bytes]:
    """Load a registered run spec without accepting a filename alias."""

    candidate = Path(path).expanduser()
    raw = _regular_file_bytes(candidate, label="run spec")
    document, provenance = parse_pilot_run_spec_bytes(raw)
    anchor = resolve_run_spec_anchor(provenance)
    if candidate.name != anchor.filename:
        raise HarnessError(f"Run spec must be named {anchor.filename}")
    return document, provenance, raw


def verify_pilot_run_spec_provenance(value: Any) -> bool:
    try:
        resolve_run_spec_anchor(value)
    except HarnessError:
        return False
    return True


def verify_repository_source_state(
    repository_root: str | Path | None = None,
) -> dict[str, Any]:
    """Prove a clean committed checkout matches local ``origin/main`` state."""

    root = (
        Path(repository_root).expanduser().resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )

    def git(*arguments: str, timeout: int = 15) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError("Unable to verify the repository source state") from exc
        if completed.returncode != 0:
            raise HarnessError("Unable to verify the repository source state")
        return completed.stdout.strip()

    top_level = Path(git("rev-parse", "--show-toplevel")).resolve()
    if top_level != root:
        raise HarnessError("Harness source is not running from the expected repository root")
    commit = git("rev-parse", "--verify", "HEAD^{commit}")
    tree = git("rev-parse", "--verify", "HEAD^{tree}")
    upstream_ref = "refs/remotes/origin/main"
    upstream_commit = git("rev-parse", "--verify", f"{upstream_ref}^{{commit}}")
    if commit != upstream_commit:
        raise HarnessError("Harness HEAD does not match the local origin/main tracking ref")
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        raise HarnessError("Harness repository must be clean before a frozen pilot launch")
    remote_name = "origin"
    remote_ref = "refs/heads/main"
    remote_lines = git(
        "ls-remote",
        "--exit-code",
        remote_name,
        remote_ref,
        timeout=30,
    ).splitlines()
    expected_remote_line = f"{commit}\t{remote_ref}"
    if remote_lines != [expected_remote_line] or any(
        character not in "0123456789abcdef" for character in commit
    ) or len(commit) != 40:
        raise HarnessError("Harness HEAD does not match the observed origin/main remote head")
    source = _source_fingerprint()
    return {
        "schema_version": 1,
        "git_commit": commit,
        "git_tree": tree,
        "remote_tracking_ref": upstream_ref,
        "remote_tracking_commit": upstream_commit,
        "remote_name": remote_name,
        "remote_ref": remote_ref,
        "remote_observed_commit": commit,
        "source_fingerprint": source,
    }


def comparison_execution_contract(
    config: ProviderConfig,
    *,
    arms: tuple[str, ...],
    max_model_calls: int,
    max_turns_per_arm: int,
    max_total_tokens: int,
    max_output_tokens: int,
    task_timeout_seconds: float,
    recalculate: bool,
    arm_order_seed: int,
    circuit_breaker_threshold: int,
    split_provenance: dict[str, Any] | None,
    skills: SkillRegistry,
) -> dict[str, Any]:
    return {
        "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
        "comparison_manifest_schema_version": COMPARISON_MANIFEST_SCHEMA_VERSION,
        "source_contract": _run_spec_source_fingerprint(),
        "split_provenance": split_provenance,
        "arms": list(arms),
        "provider": {
            "base_url": config.base_url,
            "model": config.model,
            "api_protocol": config.api_protocol,
            "requested_reasoning_effort": (
                config.requested_reasoning_effort or config.reasoning_effort
            ),
            "reasoning_effort": config.reasoning_effort,
            "request_timeout_seconds": config.timeout_seconds,
            "request_retries": config.max_retries,
            "request_interval_seconds": config.request_interval_seconds,
            "litellm_timeout_seconds": config.litellm_timeout_seconds,
            "store_responses": config.store_responses,
            "generation": config.generation_dict(),
        },
        "resources": {
            "max_model_calls": max_model_calls,
            "max_turns_per_arm": max_turns_per_arm,
            "max_total_tokens": max_total_tokens,
            "max_output_tokens_per_call": max_output_tokens,
            "task_timeout_seconds": float(task_timeout_seconds),
            "recalculate": recalculate,
            "task_retries": 0,
            "circuit_breaker_threshold": circuit_breaker_threshold,
            "arm_order_seed": arm_order_seed,
        },
        "skills_for_ours_only": [
            {"name": skill.name, "sha256": skill.sha256}
            for skill in skills.discover()
        ],
    }


def verify_pilot_run_spec_contract(
    document: dict[str, Any], actual: dict[str, Any]
) -> None:
    if document.get("execution") != actual:
        raise HarnessError("Pilot run spec does not match the resolved execution contract")


def manifest_execution_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        return {}
    contract = {
        "comparison_protocol_version": manifest.get("comparison_protocol_version"),
        "comparison_manifest_schema_version": manifest.get("schema_version"),
        "split_provenance": manifest.get("split_provenance"),
        "arms": manifest.get("arms"),
        "provider": {
            "base_url": configuration.get("provider_base_url"),
            "model": configuration.get("model"),
            "api_protocol": configuration.get("api_protocol"),
            "requested_reasoning_effort": configuration.get(
                "requested_reasoning_effort"
            ),
            "reasoning_effort": configuration.get("reasoning_effort"),
            "request_timeout_seconds": configuration.get("request_timeout_seconds"),
            "request_retries": configuration.get("request_retries"),
            "request_interval_seconds": configuration.get("request_interval_seconds"),
            "litellm_timeout_seconds": configuration.get("litellm_timeout_seconds"),
            "store_responses": configuration.get("store_responses"),
            "generation": configuration.get("generation"),
        },
        "resources": {
            "max_model_calls": configuration.get("max_model_calls"),
            "max_turns_per_arm": configuration.get("max_turns_per_arm"),
            "max_total_tokens": configuration.get("max_total_tokens"),
            "max_output_tokens_per_call": configuration.get(
                "max_output_tokens_per_call"
            ),
            "task_timeout_seconds": configuration.get("task_timeout_seconds"),
            "recalculate": configuration.get("recalculate"),
            "task_retries": configuration.get("task_retries"),
            "circuit_breaker_threshold": configuration.get(
                "circuit_breaker_threshold"
            ),
            "arm_order_seed": manifest.get("arm_order_seed"),
        },
        "skills_for_ours_only": configuration.get("skills_for_ours_only"),
    }
    protocol_version = manifest.get("comparison_protocol_version")
    if protocol_version == V25_COMPARISON_PROTOCOL_VERSION:
        contract["source_contract"] = dict(V25_RUN_SPEC_SOURCE_CONTRACT)
    elif protocol_version == V26_COMPARISON_PROTOCOL_VERSION:
        contract["source_contract"] = dict(V26_RUN_SPEC_SOURCE_CONTRACT)
    elif protocol_version == V27_COMPARISON_PROTOCOL_VERSION:
        contract["source_contract"] = dict(V27_RUN_SPEC_SOURCE_CONTRACT)
    elif protocol_version == COMPARISON_PROTOCOL_VERSION:
        contract["source_contract"] = _run_spec_source_fingerprint()
    return contract


def _run_key(task_id: str, arm: str) -> str:
    return f"{task_id}::{arm}"


def _stage_allowed_tools_policy(
    arms: tuple[str, ...],
    *,
    protocol_version: str = COMPARISON_PROTOCOL_VERSION,
) -> dict[str, dict[str, Any]]:
    return {
        arm: (
            {"solve": sorted(BARE_TOOLS)}
            if arm in {"bare", "profile"}
            else {"solve": "all"}
            if arm == "native"
            else {
                "solve": (
                    sorted(OURS_TOOLS)
                    if protocol_version in _V26_RUNTIME_PROTOCOL_VERSIONS
                    else "all"
                )
            }
            if arm == "ours"
            else {
                "extract": sorted(PAPER_EXTRACTION_TOOLS),
                "vision_verify": sorted(PAPER_VISION_TOOLS),
                "latex_verify": sorted(PAPER_LATEX_TOOLS),
                "reconcile": sorted(PAPER_RECONCILIATION_TOOLS),
                "solve": sorted(PAPER_SOLVER_TOOLS),
            }
        )
        for arm in arms
    }


def _allowed_observed_terminals_policy(
    stage_turn_caps: dict[str, dict[str, int]],
    *,
    protocol_version: str = COMPARISON_PROTOCOL_VERSION,
) -> dict[str, dict[str, list[str]]]:
    return {
        arm: {
            stage: (
                [
                    ASSISTANT_TEXT_TERMINAL,
                    *(
                        [BUDGET_EXHAUSTED_TERMINAL]
                        if protocol_version
                        in {
                            V25_COMPARISON_PROTOCOL_VERSION,
                            *_V26_RUNTIME_PROTOCOL_VERSIONS,
                        }
                        else []
                    ),
                ]
                if arm == "paper" and stage == "reconcile"
                else [
                    TERMINAL_TOOL_NAME,
                    *(
                        [ASSISTANT_TEXT_TERMINAL]
                        if protocol_version not in _V26_RUNTIME_PROTOCOL_VERSIONS
                        else []
                    ),
                    *(
                        [HISTORICAL_FINAL_RECOVERY_TERMINAL]
                        if stage == "solve"
                        and protocol_version not in _V26_RUNTIME_PROTOCOL_VERSIONS
                        and (
                            protocol_version != LEGACY_COMPARISON_PROTOCOL_VERSION
                            or arm == "ours"
                        )
                        else []
                    ),
                    *(
                        [TERMINAL_SUBMISSION_TRUNCATED_OBSERVED]
                        if protocol_version in _V26_RUNTIME_PROTOCOL_VERSIONS
                        else []
                    ),
                    *(
                        [BUDGET_EXHAUSTED_TERMINAL]
                        if protocol_version
                        in {
                            V25_COMPARISON_PROTOCOL_VERSION,
                            *_V26_RUNTIME_PROTOCOL_VERSIONS,
                        }
                        else []
                    ),
                ]
            )
            for stage in stage_turn_caps[arm]
        }
        for arm in stage_turn_caps
    }


def _manifest_file_sha256(path: Path) -> str:
    return _sha256(path)


def _require_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _request_attempt_audit(row: dict[str, Any]) -> dict[str, int | bool]:
    budget_calls = _require_int(
        ((row.get("budget") or {}).get("used") or {}).get("model_calls"),
    )
    timings = (row.get("agent") or {}).get("request_timings")
    attempts: list[int] = []
    valid_timings = isinstance(timings, list) and bool(timings)
    if valid_timings:
        for timing in timings:
            if not isinstance(timing, dict):
                valid_timings = False
                break
            raw_attempts = _require_int(timing.get("attempts"), minimum=1)
            history = timing.get("attempt_history")
            if raw_attempts is None or not isinstance(history, list) or len(history) != raw_attempts:
                valid_timings = False
                break
            attempts.append(raw_attempts)
    failed_attempts = _require_int(
        (row.get("provider_error") or {}).get("attempts", 0),
    )
    if failed_attempts is None:
        failed_attempts = 0
    request_history_complete = bool(
        row.get("status") == "completed"
        or (
            row.get("status") == "error"
            and row.get("outcome_kind") == "infrastructure_failure"
            and row.get("error_category")
            in {"recalculation_infrastructure", "scoring_infrastructure"}
        )
    )
    exact = bool(
        request_history_complete
        and budget_calls is not None
        and valid_timings
        and len(attempts) == budget_calls
    )
    known_success_attempts = sum(attempts) if valid_timings else (budget_calls or 0)
    return {
        "known_http_attempts": known_success_attempts + failed_attempts,
        "known_successful_retries": sum(attempts) - len(attempts) if valid_timings else 0,
        "known_failed_attempts": failed_attempts,
        "has_audit": bool(valid_timings or budget_calls or failed_attempts),
        "exact": exact,
    }


def _arm_order(task_id: str, seed: int, arms: tuple[str, ...]) -> list[str]:
    digest = hashlib.sha256(f"{seed}:{task_id}".encode()).digest()
    offset = int.from_bytes(digest[:4], "big") % len(arms)
    return [*arms[offset:], *arms[:offset]]


def _balanced_arm_orders(
    task_ids: list[str], seed: int, arms: tuple[str, ...]
) -> dict[str, list[str]]:
    """Assign cyclic arm orders with exact or near-exact position balance.

    Hash-ranking makes the assignment independent of dataset row order while the
    cyclic offsets ensure every arm appears in every position equally often (the
    unavoidable difference is at most one when the task count is not divisible
    by the arm count).
    """

    if not arms:
        raise ValueError("arms must not be empty")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task IDs must be unique")
    ranked = sorted(
        task_ids,
        key=lambda task_id: hashlib.sha256(f"{seed}:{task_id}".encode()).digest(),
    )
    orders: dict[str, list[str]] = {}
    for index, task_id in enumerate(ranked):
        offset = index % len(arms)
        orders[task_id] = [*arms[offset:], *arms[:offset]]
    return orders


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return round(float(ordered[index]), 3)


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "median": round(float(median(values)), 3) if values else None,
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3) if values else None,
    }


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return [max(center - margin, 0.0), min(center + margin, 1.0)]


def _mcnemar_exact(left_only: int, right_only: int) -> float:
    discordant = left_only + right_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, index) for index in range(min(left_only, right_only) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def _holm_adjust(raw: dict[str, float]) -> dict[str, float]:
    ordered = sorted(raw.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, value * (total - index)))
        adjusted[name] = running
    return adjusted


def _stratified_bootstrap_delta(
    tasks: list[SpreadsheetTask],
    left: dict[str, bool],
    right: dict[str, bool],
    *,
    seed: int,
    samples: int = 5_000,
) -> list[float]:
    if not tasks:
        return [0.0, 0.0]
    strata: dict[str, list[SpreadsheetTask]] = {}
    for task in tasks:
        strata.setdefault(task.instruction_type, []).append(task)
    generator = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        difference = 0.0
        count = 0
        for members in strata.values():
            for _ in members:
                task = generator.choice(members)
                difference += int(right.get(task.task_id, False)) - int(
                    left.get(task.task_id, False)
                )
                count += 1
        deltas.append(difference / count if count else 0.0)
    deltas.sort()
    lower = deltas[max(math.floor(0.025 * len(deltas)), 0)]
    upper = deltas[min(math.ceil(0.975 * len(deltas)) - 1, len(deltas) - 1)]
    return [lower, upper]


def _usage_from_row(row: dict[str, Any]) -> dict[str, Any]:
    usage = (row.get("agent") or {}).get("usage") or {}
    budget_used = (row.get("budget") or {}).get("used") or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    agent_total = int(usage.get("total_tokens", 0) or 0)
    budget_total = int(budget_used.get("total_tokens", 0) or 0)
    total_tokens = budget_total if budget_total else agent_total
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "source": "budget" if budget_total else "agent" if usage else "none",
        "agent_budget_total_mismatch": bool(
            agent_total and budget_total and agent_total != budget_total
        ),
        "input_output_complete": bool(usage),
    }


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


def _dataset_manifest_sha256(tasks: list[SpreadsheetTask]) -> str | None:
    candidates: set[Path] = set()
    for task in tasks:
        resolved = task.input_path.resolve()
        if len(resolved.parents) >= 3:
            candidate = resolved.parents[2] / "dataset.json"
            if candidate.is_file():
                candidates.add(candidate)
    if len(candidates) != 1:
        return None
    return _sha256(next(iter(candidates)))


def _task_stratum(task: SpreadsheetTask) -> str:
    if "Cell" in task.instruction_type:
        return "cell"
    if "Sheet" in task.instruction_type:
        return "sheet"
    return "other"


def _row_has_available_score(row: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(row, dict)
        and row.get("status") == "completed"
        and isinstance(row.get("passed"), bool)
        and row.get("score_available") is not False
        and row.get("outcome_kind") != "infrastructure_failure"
    )


def _arm_subset_summary(
    tasks: list[SpreadsheetTask],
    arm: str,
    latest: dict[str, dict[str, Any]],
    passes: dict[str, bool],
) -> dict[str, Any]:
    arm_rows = [
        latest[_run_key(task.task_id, arm)]
        for task in tasks
        if _run_key(task.task_id, arm) in latest
    ]
    completed = [row for row in arm_rows if row.get("status") == "completed"]
    passed = sum(passes[task.task_id] for task in tasks)
    usage = [_usage_from_row(row) for row in arm_rows]
    elapsed = [float(row.get("elapsed_seconds", 0) or 0) for row in arm_rows]
    calls = [
        float(((row.get("budget") or {}).get("used") or {}).get("model_calls", 0) or 0)
        for row in arm_rows
    ]
    request_attempt_audits = [_request_attempt_audit(row) for row in arm_rows]
    errors = Counter(
        str(row.get("error_category") or "unspecified")
        for row in arm_rows
        if row.get("status") != "completed"
    )
    model_execution_failures = Counter(
        str(row.get("model_failure_reason") or "unspecified")
        for row in arm_rows
        if row.get("outcome_kind") == "model_execution_failure"
    )
    infrastructure_failures = Counter(
        str(
            row.get("recalculation_failure_reason")
            or row.get("scoring_failure_reason")
            or "unspecified"
        )
        for row in arm_rows
        if row.get("outcome_kind") == "infrastructure_failure"
    )
    score_unavailable = bool(infrastructure_failures)
    terminations = Counter(
        str(termination.get("reason") or "unspecified")
        for row in arm_rows
        if isinstance((row.get("budget") or {}).get("termination"), dict)
        for termination in [(row.get("budget") or {})["termination"]]
    )
    total_tokens = sum(item["total_tokens"] for item in usage)
    # Complete means every expected arm-task row is present and has an exact audit.
    request_attempt_audit_complete = len(request_attempt_audits) == len(tasks) and all(
        bool(item["exact"]) for item in request_attempt_audits
    )
    return {
        "expected": len(tasks),
        "attempted": len(arm_rows),
        "completed": len(completed),
        "errors": len(arm_rows) - len(completed),
        "missing": len(tasks) - len(arm_rows),
        "passed": passed,
        "end_to_end_accuracy": (
            None if score_unavailable else passed / len(tasks) if tasks else 0.0
        ),
        "wilson_95": None if score_unavailable else _wilson(passed, len(tasks)),
        "completed_only_accuracy": passed / len(completed) if completed else None,
        "completion_rate": len(completed) / len(tasks) if tasks else 0.0,
        "error_categories": dict(sorted(errors.items())),
        "known_model_execution_failures": sum(model_execution_failures.values()),
        "model_execution_failure_reasons": dict(
            sorted(model_execution_failures.items())
        ),
        "known_infrastructure_failures": sum(infrastructure_failures.values()),
        "infrastructure_failure_reasons": dict(sorted(infrastructure_failures.items())),
        "score_unavailable": score_unavailable,
        "budget_termination_reasons": dict(sorted(terminations.items())),
        "input_tokens": _distribution([item["input_tokens"] for item in usage]),
        "output_tokens": _distribution([item["output_tokens"] for item in usage]),
        "total_tokens": _distribution([item["total_tokens"] for item in usage]),
        "total_tokens_sum": total_tokens,
        "usage_rows_with_input_output": sum(
            item["input_output_complete"] for item in usage
        ),
        "usage_total_mismatches": sum(
            item["agent_budget_total_mismatch"] for item in usage
        ),
        "model_calls": _distribution(calls),
        "known_http_attempts": _distribution(
            [float(item["known_http_attempts"]) for item in request_attempt_audits]
        ),
        "known_http_attempts_sum": sum(
            int(item["known_http_attempts"]) for item in request_attempt_audits
        ),
        "known_successful_request_retries_sum": sum(
            int(item["known_successful_retries"]) for item in request_attempt_audits
        ),
        "known_failed_request_attempts_sum": sum(
            int(item["known_failed_attempts"]) for item in request_attempt_audits
        ),
        "request_attempt_audit_rows": sum(
            bool(item["has_audit"]) for item in request_attempt_audits
        ),
        "request_attempt_audit_complete": request_attempt_audit_complete,
        "elapsed_seconds": _distribution(elapsed),
        "tokens_per_pass": total_tokens / passed if passed else None,
    }


def _pairwise_result(
    tasks: list[SpreadsheetTask],
    left_arm: str,
    right_arm: str,
    latest: dict[str, dict[str, Any]],
    passes: dict[str, dict[str, bool]],
    *,
    seed: int,
) -> dict[str, Any]:
    left_only = sum(
        passes[left_arm][task.task_id] and not passes[right_arm][task.task_id]
        for task in tasks
    )
    right_only = sum(
        passes[right_arm][task.task_id] and not passes[left_arm][task.task_id]
        for task in tasks
    )
    complete_pairs = sum(
        latest.get(_run_key(task.task_id, left_arm), {}).get("status") == "completed"
        and latest.get(_run_key(task.task_id, right_arm), {}).get("status") == "completed"
        for task in tasks
    )
    inference_valid = bool(tasks) and complete_pairs == len(tasks)
    invalid_reasons = (
        []
        if inference_valid
        else ["no_tasks" if not tasks else "incomplete_or_error_arm_task_pairs"]
    )
    return {
        "inference_valid": inference_valid,
        "inference_invalid_reasons": invalid_reasons,
        "expected_pairs": len(tasks),
        "completed_pairs": complete_pairs,
        "accuracy_delta_right_minus_left": (
            sum(passes[right_arm][task.task_id] for task in tasks)
            - sum(passes[left_arm][task.task_id] for task in tasks)
        )
        / len(tasks)
        if inference_valid
        else None,
        "stratified_bootstrap_95": (
            _stratified_bootstrap_delta(
                tasks,
                passes[left_arm],
                passes[right_arm],
                seed=seed,
            )
            if inference_valid
            else None
        ),
        "left_only_passes": left_only,
        "right_only_passes": right_only,
        "mcnemar_exact_p": (
            _mcnemar_exact(left_only, right_only) if inference_valid else None
        ),
    }


def _invalidate_pairwise_inference(
    result: dict[str, Any], reasons: list[str]
) -> None:
    """Remove inferential fields when collection integrity is not established."""

    merged = list(result.get("inference_invalid_reasons") or [])
    for reason in reasons:
        qualified = f"collection_integrity:{reason}"
        if qualified not in merged:
            merged.append(qualified)
    result["inference_valid"] = False
    result["inference_invalid_reasons"] = merged
    result["stratified_bootstrap_95"] = None
    result["mcnemar_exact_p"] = None
    result["holm_adjusted_p"] = None
    for stratum in (result.get("strata") or {}).values():
        if isinstance(stratum, dict):
            _invalidate_pairwise_inference(stratum, reasons)


def comparison_summary(
    results_path: str | Path,
    tasks: list[SpreadsheetTask],
    *,
    arms: tuple[str, ...] = COMPARISON_ARMS,
    bootstrap_seed: int = 20260811,
    collection_tasks: list[SpreadsheetTask] | None = None,
    interrupted_keys: set[str] | None = None,
    expected_protocol_version: str = COMPARISON_PROTOCOL_VERSION,
) -> dict[str, Any]:
    interrupted_keys = set() if interrupted_keys is None else set(interrupted_keys)
    collection_tasks = tasks if collection_tasks is None else collection_tasks
    task_ids = [task.task_id for task in tasks]
    collection_task_ids = [task.task_id for task in collection_tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("analysis task IDs must be unique")
    if len(set(collection_task_ids)) != len(collection_task_ids):
        raise ValueError("collection task IDs must be unique")
    if not set(task_ids).issubset(collection_task_ids):
        raise ValueError("analysis tasks must be a subset of collection tasks")
    rows, invalid = _strict_jsonl_rows(Path(results_path))
    expected_task_ids = set(collection_task_ids)
    expected_arms = set(arms)
    identity_counts: Counter[str] = Counter()
    unknown_task_ids: set[str] = set()
    unknown_arms: set[str] = set()
    unexpected_arms: set[str] = set()
    protocol_mismatch_rows = 0
    observed_protocols: set[str] = set()
    unknown_task_rows = 0
    unknown_arm_rows = 0
    unexpected_arm_rows = 0
    for row in rows:
        raw_task_id = row.get("task_id")
        raw_arm = row.get("arm")
        task_id = str(raw_task_id) if raw_task_id is not None else "<missing>"
        arm = str(raw_arm) if raw_arm is not None else "<missing>"
        raw_protocol = row.get("comparison_protocol_version")
        observed_protocol = (
            str(raw_protocol) if raw_protocol is not None else "<missing>"
        )
        observed_protocols.add(observed_protocol)
        if raw_protocol != expected_protocol_version:
            protocol_mismatch_rows += 1
        if raw_task_id is not None and raw_arm is not None:
            identity_counts[_run_key(task_id, arm)] += 1
        if raw_task_id is None or task_id not in expected_task_ids:
            unknown_task_rows += 1
            unknown_task_ids.add(task_id)
        if raw_arm is None or arm not in AVAILABLE_COMPARISON_ARMS:
            unknown_arm_rows += 1
            unknown_arms.add(arm)
        elif arm not in expected_arms:
            unexpected_arm_rows += 1
            unexpected_arms.add(arm)
    duplicate_arm_task_keys = sorted(
        key for key, count in identity_counts.items() if count > 1
    )
    duplicate_arm_task_rows = sum(
        count - 1 for count in identity_counts.values() if count > 1
    )
    latest = {
        _run_key(str(row.get("task_id")), str(row.get("arm"))): row
        for row in rows
        if row.get("task_id") is not None and row.get("arm") in arms
    }
    infrastructure_failure_keys = {
        key
        for key, row in latest.items()
        if row.get("outcome_kind") == "infrastructure_failure"
        and row.get("score_available") is False
    }
    recalculation_infrastructure_keys = {
        key
        for key in infrastructure_failure_keys
        if latest[key].get("error_category") == "recalculation_infrastructure"
    }
    scoring_infrastructure_keys = {
        key
        for key in infrastructure_failure_keys
        if latest[key].get("error_category") == "scoring_infrastructure"
    }
    score_unavailable_keys = interrupted_keys | infrastructure_failure_keys
    expected_keys = [_run_key(task.task_id, arm) for task in tasks for arm in arms]
    unknown_interrupted_keys = sorted(interrupted_keys - set(expected_keys))
    if unknown_interrupted_keys:
        raise ValueError("interrupted arm-task keys must belong to the analysis set")
    by_arm: dict[str, dict[str, Any]] = {}
    passes: dict[str, dict[str, bool]] = {}
    for arm in arms:
        passes[arm] = {
            task.task_id: bool(
                latest.get(_run_key(task.task_id, arm), {}).get("status") == "completed"
                and latest.get(_run_key(task.task_id, arm), {}).get("passed") is True
            )
            for task in tasks
        }
        arm_summary = _arm_subset_summary(tasks, arm, latest, passes[arm])
        arm_interrupted = sorted(
            key for key in interrupted_keys if key.endswith(f"::{arm}")
        )
        arm_recalculation_infrastructure_failures = sorted(
            key
            for key in recalculation_infrastructure_keys
            if key.endswith(f"::{arm}")
        )
        arm_scoring_infrastructure_failures = sorted(
            key
            for key in scoring_infrastructure_keys
            if key.endswith(f"::{arm}")
        )
        known_tasks = [
            task
            for task in tasks
            if _row_has_available_score(latest.get(_run_key(task.task_id, arm)))
        ]
        known_passed = sum(passes[arm][task.task_id] for task in known_tasks)
        if score_unavailable_keys:
            arm_summary["end_to_end_accuracy"] = None
            arm_summary["wilson_95"] = None
        arm_summary["interrupted_unknown_outcomes"] = len(arm_interrupted)
        arm_summary["interrupted_unknown_keys"] = arm_interrupted
        arm_summary["recalculation_infrastructure_failures"] = len(
            arm_recalculation_infrastructure_failures
        )
        arm_summary["recalculation_infrastructure_failure_keys"] = (
            arm_recalculation_infrastructure_failures
        )
        arm_summary["scoring_infrastructure_failures"] = len(
            arm_scoring_infrastructure_failures
        )
        arm_summary["scoring_infrastructure_failure_keys"] = (
            arm_scoring_infrastructure_failures
        )
        arm_summary["known_outcome_descriptive"] = {
            "tasks": len(known_tasks),
            "passed": known_passed,
            "accuracy": known_passed / len(known_tasks) if known_tasks else None,
            "wilson_95": _wilson(known_passed, len(known_tasks)) if known_tasks else None,
            "primary": False,
        }
        arm_summary["attempted"] += len(arm_interrupted)
        arm_summary["missing"] -= len(arm_interrupted)
        strata = {
            stratum: _arm_subset_summary(
                [task for task in tasks if _task_stratum(task) == stratum],
                arm,
                latest,
                passes[arm],
            )
            for stratum in ("cell", "sheet", "other")
        }
        for stratum, stratum_summary in strata.items():
            stratum_tasks = [task for task in tasks if _task_stratum(task) == stratum]
            stratum_interrupted = [
                task
                for task in stratum_tasks
                if _run_key(task.task_id, arm) in interrupted_keys
            ]
            stratum_recalculation_infrastructure_failures = [
                task
                for task in stratum_tasks
                if _run_key(task.task_id, arm)
                in recalculation_infrastructure_keys
            ]
            stratum_scoring_infrastructure_failures = [
                task
                for task in stratum_tasks
                if _run_key(task.task_id, arm) in scoring_infrastructure_keys
            ]
            stratum_unknown = {
                task.task_id
                for task in [
                    *stratum_interrupted,
                    *stratum_recalculation_infrastructure_failures,
                    *stratum_scoring_infrastructure_failures,
                ]
            }
            stratum_known = [
                task
                for task in stratum_tasks
                if task.task_id not in stratum_unknown
                and _row_has_available_score(
                    latest.get(_run_key(task.task_id, arm))
                )
            ]
            stratum_passed = sum(passes[arm][task.task_id] for task in stratum_known)
            if stratum_unknown:
                stratum_summary["end_to_end_accuracy"] = None
                stratum_summary["wilson_95"] = None
            stratum_summary["interrupted_unknown_outcomes"] = len(stratum_interrupted)
            stratum_summary["recalculation_infrastructure_failures"] = len(
                stratum_recalculation_infrastructure_failures
            )
            stratum_summary["scoring_infrastructure_failures"] = len(
                stratum_scoring_infrastructure_failures
            )
            stratum_summary["known_outcome_descriptive"] = {
                "tasks": len(stratum_known),
                "passed": stratum_passed,
                "accuracy": (
                    stratum_passed / len(stratum_known) if stratum_known else None
                ),
                "wilson_95": (
                    _wilson(stratum_passed, len(stratum_known))
                    if stratum_known
                    else None
                ),
                "primary": False,
            }
        arm_summary["strata"] = strata
        # Retain the original field names while making their contents complete.
        arm_summary["cell_level"] = strata["cell"]
        arm_summary["sheet_level"] = strata["sheet"]
        by_arm[arm] = arm_summary

    pairwise: dict[str, dict[str, Any]] = {}
    raw_p: dict[str, float] = {}
    for left_index, left_arm in enumerate(arms):
        for right_arm in arms[left_index + 1 :]:
            name = f"{left_arm}_vs_{right_arm}"
            result = _pairwise_result(
                tasks,
                left_arm,
                right_arm,
                latest,
                passes,
                seed=bootstrap_seed + len(pairwise),
            )
            result["strata"] = {
                stratum: _pairwise_result(
                    [task for task in tasks if _task_stratum(task) == stratum],
                    left_arm,
                    right_arm,
                    latest,
                    passes,
                    seed=bootstrap_seed + len(pairwise) * 10 + stratum_index + 1,
                )
                for stratum_index, stratum in enumerate(("cell", "sheet", "other"))
            }
            pairwise[name] = result
            p_value = result["mcnemar_exact_p"]
            if isinstance(p_value, int | float):
                raw_p[name] = float(p_value)
    adjusted = _holm_adjust(raw_p)
    for name, value in adjusted.items():
        pairwise[name]["holm_adjusted_p"] = value
    for name in pairwise:
        pairwise[name].setdefault("holm_adjusted_p", None)
        left_arm, right_arm = name.split("_vs_", 1)
        known_pairs = [
            task
            for task in tasks
            if _row_has_available_score(
                latest.get(_run_key(task.task_id, left_arm))
            )
            and _row_has_available_score(
                latest.get(_run_key(task.task_id, right_arm))
            )
        ]
        pairwise[name]["known_outcome_descriptive"] = {
            "pairs": len(known_pairs),
            "accuracy_delta_right_minus_left": (
                sum(passes[right_arm][task.task_id] for task in known_pairs)
                - sum(passes[left_arm][task.task_id] for task in known_pairs)
            )
            / len(known_pairs)
            if known_pairs
            else None,
            "primary": False,
        }
        if not pairwise[name]["inference_valid"]:
            pairwise[name]["accuracy_delta_right_minus_left"] = None
        if score_unavailable_keys:
            unavailable_reasons: list[str] = []
            if interrupted_keys:
                unavailable_reasons.append("interrupted_unknown_outcomes")
            if recalculation_infrastructure_keys:
                unavailable_reasons.append("recalculation_infrastructure_failures")
            if scoring_infrastructure_keys:
                unavailable_reasons.append("scoring_infrastructure_failures")
            _invalidate_pairwise_inference(
                pairwise[name], unavailable_reasons
            )

    attempted_keys = set(latest)
    expected_key_set = set(expected_keys)
    attempted_expected = attempted_keys & expected_key_set
    errored_arm_tasks = sum(
        latest[key].get("status") != "completed" for key in attempted_expected
    )
    missing_arm_tasks = len(expected_key_set - attempted_keys - interrupted_keys)
    inference_invalid_reasons: list[str] = []
    if invalid:
        inference_invalid_reasons.append("invalid_result_rows")
    if duplicate_arm_task_keys:
        inference_invalid_reasons.append("duplicate_arm_tasks")
    if unknown_task_rows:
        inference_invalid_reasons.append("unknown_tasks")
    if unknown_arm_rows:
        inference_invalid_reasons.append("unknown_arms")
    if unexpected_arm_rows:
        inference_invalid_reasons.append("unexpected_arms")
    if protocol_mismatch_rows:
        inference_invalid_reasons.append("comparison_protocol_mismatch")
    if any(
        not arm_summary.get("request_attempt_audit_complete")
        for arm_summary in by_arm.values()
    ):
        inference_invalid_reasons.append("request_attempt_audit_incomplete")
    if missing_arm_tasks:
        inference_invalid_reasons.append("missing_arm_tasks")
    if errored_arm_tasks:
        inference_invalid_reasons.append("errored_arm_tasks")
    if recalculation_infrastructure_keys:
        inference_invalid_reasons.append("recalculation_infrastructure_failures")
    if scoring_infrastructure_keys:
        inference_invalid_reasons.append("scoring_infrastructure_failures")
    if interrupted_keys:
        inference_invalid_reasons.append("interrupted_unknown_outcomes")
    if inference_invalid_reasons:
        for result in pairwise.values():
            _invalidate_pairwise_inference(result, inference_invalid_reasons)
    calculation_backends = Counter(
        str(latest[key].get("calculation_backend") or "unspecified")
        for key in attempted_expected
    )
    return {
        "protocol": expected_protocol_version,
        "arm_display_names": {
            arm: COMPARISON_ARM_DISPLAY_NAMES[arm] for arm in arms
        },
        "scorer": "cleanroom-corrected-value-v1",
        "style_checked": False,
        "calculation_backends": dict(sorted(calculation_backends.items())),
        "dataset_revision": f"KAKA22/SpreadsheetBench@{VERIFIED_REVISION}",
        "task_count": len(tasks),
        "expected_arm_tasks": len(expected_keys),
        "attempted_arm_tasks": len(attempted_expected) + len(interrupted_keys),
        "completed_arm_tasks": len(attempted_expected) - errored_arm_tasks,
        "errored_arm_tasks": errored_arm_tasks,
        "missing_arm_tasks": missing_arm_tasks,
        "interrupted_unknown_arm_tasks": len(interrupted_keys),
        "known_model_execution_failure_arm_tasks": sum(
            latest[key].get("outcome_kind") == "model_execution_failure"
            for key in attempted_expected
        ),
        "known_infrastructure_failure_arm_tasks": sum(
            latest[key].get("outcome_kind") == "infrastructure_failure"
            for key in attempted_expected
        ),
        "known_recalculation_infrastructure_failure_arm_tasks": len(
            recalculation_infrastructure_keys & attempted_expected
        ),
        "known_scoring_infrastructure_failure_arm_tasks": len(
            scoring_infrastructure_keys & attempted_expected
        ),
        "interrupted_unknown_keys": sorted(interrupted_keys),
        "invalid_result_rows_ignored": invalid,
        "duplicate_arm_tasks": len(duplicate_arm_task_keys),
        "duplicate_arm_task_rows": duplicate_arm_task_rows,
        "duplicate_arm_task_keys": duplicate_arm_task_keys,
        "unknown_task_rows": unknown_task_rows,
        "unknown_task_ids": sorted(unknown_task_ids),
        "unknown_arm_rows": unknown_arm_rows,
        "unknown_arms": sorted(unknown_arms),
        "unexpected_arm_rows": unexpected_arm_rows,
        "unexpected_arms": sorted(unexpected_arms),
        "protocol_mismatch_rows": protocol_mismatch_rows,
        "observed_protocols": sorted(observed_protocols),
        "inference_valid": not inference_invalid_reasons,
        "inference_invalid_reasons": inference_invalid_reasons,
        "arms": by_arm,
        "pairwise": pairwise,
    }


class ComparisonBenchmarkRunner:
    """Run task-matched arms in a deterministic rotating order with durable resume."""

    def __init__(
        self,
        config: ProviderConfig,
        output_dir: Path,
        *,
        skill_registry: SkillRegistry,
        arms: tuple[str, ...] = COMPARISON_ARMS,
        max_model_calls: int = 20,
        max_turns_per_arm: int = 20,
        max_total_tokens: int = 100_000,
        max_output_tokens: int = 4_096,
        task_timeout_seconds: float = 900,
        recalculate: bool = True,
        arm_order_seed: int = 20260811,
        circuit_breaker_threshold: int = 3,
        split_provenance: dict[str, Any] | None = None,
        run_spec_document: dict[str, Any] | None = None,
        run_spec_provenance: dict[str, str] | None = None,
        run_spec_bytes: bytes | None = None,
    ) -> None:
        if not arms or len(set(arms)) != len(arms) or any(
            arm not in AVAILABLE_COMPARISON_ARMS for arm in arms
        ):
            raise ValueError(f"arms must be unique members of {AVAILABLE_COMPARISON_ARMS}")
        if max_model_calls < 1 or max_total_tokens < 1 or max_output_tokens < 1:
            raise ValueError("comparison budgets must be positive")
        self.stage_turn_caps = comparison_stage_turn_caps(max_turns_per_arm, arms)
        if max_model_calls < max_turns_per_arm:
            raise ValueError(
                "max_model_calls must be at least max_turns_per_arm so the declared "
                "response ceiling is reachable"
            )
        if task_timeout_seconds <= 0 or circuit_breaker_threshold < 1:
            raise ValueError("timeouts and circuit breaker must be positive")
        self.config = config
        self.output_dir = output_dir.resolve()
        self.skill_registry = skill_registry.freeze()
        self.arms = arms
        self.max_model_calls = max_model_calls
        self.max_turns_per_arm = max_turns_per_arm
        self.max_total_tokens = max_total_tokens
        self.max_output_tokens = max_output_tokens
        self.task_timeout_seconds = task_timeout_seconds
        self.recalculate = recalculate
        self.arm_order_seed = arm_order_seed
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.split_provenance = (
            json.loads(json.dumps(split_provenance))
            if split_provenance is not None
            else None
        )
        self.repository_source_state: dict[str, Any] | None = None
        self.continuation_source_record: dict[str, Any] | None = None
        self.legacy_source_transition = False
        self.run_spec_document = (
            json.loads(json.dumps(run_spec_document))
            if run_spec_document is not None
            else None
        )
        self.run_spec_provenance = (
            json.loads(json.dumps(run_spec_provenance))
            if run_spec_provenance is not None
            else None
        )
        self.run_spec_bytes = bytes(run_spec_bytes) if run_spec_bytes is not None else None
        self.run_spec_anchor: RunSpecAnchor | None = None
        if any(
            value is not None
            for value in (
                self.run_spec_document,
                self.run_spec_provenance,
                self.run_spec_bytes,
            )
        ) and any(
            value is None
            for value in (
                self.run_spec_document,
                self.run_spec_provenance,
                self.run_spec_bytes,
            )
        ):
            raise ValueError("run spec document, provenance, and bytes must be provided together")
        if self.run_spec_bytes is not None:
            parsed_document, parsed_provenance = parse_pilot_run_spec_bytes(
                self.run_spec_bytes
            )
            if (
                parsed_document != self.run_spec_document
                or parsed_provenance != self.run_spec_provenance
            ):
                raise ValueError("run spec document or provenance does not match its bytes")
            self.run_spec_anchor = resolve_run_spec_anchor(parsed_provenance)
        self.relay_pacer = RelayPacer(config.request_interval_seconds)
        self.results_path = self.output_dir / "results.jsonl"
        self.manifest_path = self.output_dir / "comparison-manifest.json"
        self.summary_path = self.output_dir / "summary.json"
        self.lock_path = self.output_dir / ".comparison.lock"
        self.run_spec_copy_path = self.output_dir / RUN_SPEC_COPY_FILENAME
        self.inflight_path = self.output_dir / INFLIGHT_FILENAME
        self.interrupted_seals_path = self.output_dir / INTERRUPTED_SEALS_FILENAME
        self.continuation_source_path = self.output_dir / CONTINUATION_SOURCE_FILENAME
        if self.run_spec_document is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError(
                    f"Another comparison process is already using {self.output_dir}"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _prepare_repository_source_state(self) -> None:
        self.repository_source_state = (
            verify_repository_source_state()
            if self.run_spec_anchor is not None and self.run_spec_anchor.launchable
            else None
        )

    def _require_launchable_run_spec(
        self, *, resume: bool = False, operation: str = "launch"
    ) -> RunSpecAnchor | None:
        if self.run_spec_provenance is None:
            return None
        return require_launchable_run_spec(
            self.run_spec_provenance,
            resume=resume,
            operation=operation,
        )

    def preflight(self, tasks: list[SpreadsheetTask]) -> dict[str, Any]:
        """Perform every read-only launch gate and return the prospective manifest."""

        if not tasks or len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("comparison tasks must be non-empty with unique IDs")
        self._require_launchable_run_spec(operation="launch")
        require_evaluation_task_authorization(
            (task.task_id for task in tasks),
            authorized_manifest_id=(
                self.run_spec_anchor.split_manifest_id
                if self.run_spec_anchor is not None
                and self.run_spec_anchor.launchable
                else None
            ),
        )
        self._manifest(tasks)
        self._prepare_repository_source_state()
        ensure_strict_code_isolation((self.config.api_key,))
        return self._manifest(tasks)

    def _manifest(self, tasks: list[SpreadsheetTask]) -> dict[str, Any]:
        execution_task_ids = "".join(f"{task.task_id}\n" for task in tasks)
        dataset_manifest_sha256 = _dataset_manifest_sha256(tasks)
        if self.split_provenance is not None and (
            not verify_trace2skill_split_provenance(self.split_provenance)
            or self.split_provenance["task_count"] != len(tasks)
            or self.split_provenance["task_ids_sha256"]
            != _text_sha256(execution_task_ids)
            or self.split_provenance["dataset_json_sha256"]
            != dataset_manifest_sha256
        ):
            raise HarnessError(
                "Comparison tasks or dataset do not match frozen split provenance"
            )
        split_manifest_id = (
            self.split_provenance.get("manifest_id")
            if isinstance(self.split_provenance, dict)
            else None
        )
        if (
            split_manifest_id in protected_run_spec_split_ids()
            and self.run_spec_document is None
        ):
            raise HarnessError("The frozen split requires its code-anchored run spec")
        if self.run_spec_document is not None:
            anchor = resolve_run_spec_anchor(self.run_spec_provenance)
            if anchor.split_manifest_id != split_manifest_id:
                raise HarnessError("Run spec and split manifest identities do not match")
            actual_contract = comparison_execution_contract(
                self.config,
                arms=self.arms,
                max_model_calls=self.max_model_calls,
                max_turns_per_arm=self.max_turns_per_arm,
                max_total_tokens=self.max_total_tokens,
                max_output_tokens=self.max_output_tokens,
                task_timeout_seconds=self.task_timeout_seconds,
                recalculate=self.recalculate,
                arm_order_seed=self.arm_order_seed,
                circuit_breaker_threshold=self.circuit_breaker_threshold,
                split_provenance=self.split_provenance,
                skills=self.skill_registry,
            )
            verify_pilot_run_spec_contract(self.run_spec_document, actual_contract)
            if not verify_pilot_run_spec_provenance(self.run_spec_provenance):
                raise HarnessError("Pilot run spec provenance does not match its code anchor")
        skills = [
            {"name": skill.name, "sha256": skill.sha256}
            for skill in self.skill_registry.discover()
        ]
        arm_orders = _balanced_arm_orders(
            [task.task_id for task in tasks], self.arm_order_seed, self.arms
        )
        canonical_task_ids = "".join(f"{task_id}\n" for task_id in sorted(arm_orders))
        profile_evidence = (
            {
                task.task_id: build_deterministic_profile(task.input_path)["profile_sha256"]
                for task in tasks
            }
            if {"profile", "ours"} & set(self.arms)
            else {}
        )
        return {
            "schema_version": COMPARISON_MANIFEST_SCHEMA_VERSION,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "study": "SpreadsheetAgent-style adapted small-model comparison",
            "not_paper_reproduction": True,
            "dataset_revision": f"KAKA22/SpreadsheetBench@{VERIFIED_REVISION}",
            "dataset_archive_sha256": VERIFIED_SHA256,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "protocol": "agent_per_workbook",
            "scorer": "cleanroom-corrected-value-v1",
            "task_count": len(tasks),
            "task_ids": [task.task_id for task in tasks],
            "task_id_set_sha256": _text_sha256(canonical_task_ids),
            "task_execution_order_sha256": _text_sha256(execution_task_ids),
            "split_provenance": self.split_provenance,
            "run_spec_provenance": self.run_spec_provenance,
            "repository_source": self.repository_source_state,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "instruction_sha256": _text_sha256(task.instruction),
                    "instruction_type": task.instruction_type,
                    "input_sha256": _sha256(task.input_path),
                    "golden_sha256": _sha256(task.golden_path),
                    "scoring_metadata_sha256": _scoring_metadata_sha256(task),
                }
                for task in tasks
            ],
            "arms": list(self.arms),
            "arm_display_names": {
                arm: COMPARISON_ARM_DISPLAY_NAMES[arm] for arm in self.arms
            },
            "arm_order_seed": self.arm_order_seed,
            "arm_order_policy": "seeded_hash_rank_cyclic_counterbalance_v1",
            "arm_order": arm_orders,
            "forced_tool_prefix_routing": {
                arm: {
                    stage: list(prefix)
                    for stage, prefix in COMPARISON_FORCED_TOOL_PREFIX_POLICY[arm].items()
                }
                for arm in self.arms
            },
            "post_prefix_routing": {
                "tool_choice": "auto",
                "terminal_tool": TERMINAL_TOOL_NAME,
                "applies_to": "comparison stages with workbook tools after forced prefix",
                "direct_text_stages": ["paper.reconcile"],
            },
            "stage_allowed_tools": _stage_allowed_tools_policy(
                self.arms,
                protocol_version=COMPARISON_PROTOCOL_VERSION,
            ),
            "allowed_observed_terminals": _allowed_observed_terminals_policy(
                self.stage_turn_caps
            ),
            "forced_prefix_wire_policy": {
                "tool_choice": "explicit_function",
                "available_tools": "forced tool only",
                "terminal_tool_available": False,
            },
            "stage_turn_caps": {
                arm: dict(self.stage_turn_caps[arm]) for arm in self.arms
            },
            "turn_cap_policy": {
                "version": COMPARISON_TURN_CAP_POLICY_VERSION,
                "max_turns_per_arm": self.max_turns_per_arm,
                "paper_scaling_version": PAPER_TURN_CAP_SCALING_VERSION,
                "paper_base_stage_caps": {
                    "extract": 6,
                    "vision_verify": 3,
                    "latex_verify": 3,
                    "reconcile": 1,
                    "solve": 7,
                },
                "paper_stage_minimum": "forced_tool_prefix_length_plus_one_terminal_turn",
                "paper_allocation": (
                    "scale 6/3/3/1/7 proportions by arm ceiling, clamp each stage to its "
                    "forced-prefix-plus-terminal minimum, then adjust by largest remainder "
                    "with stage-name ties"
                ),
            },
            "deterministic_profile": {
                "enabled": bool({"profile", "ours"} & set(self.arms)),
                "consumed_by_arms": [
                    arm for arm in self.arms if arm in {"profile", "ours"}
                ],
                "schema_version": DETERMINISTIC_PROFILE_SCHEMA_VERSION,
                "bounds": dict(DETERMINISTIC_PROFILE_BOUNDS),
                "task_profile_sha256": profile_evidence,
                "task_independent": True,
                "model_calls": 0,
            },
            "hidden_from_models": [
                "instruction_type",
                "answer_position",
                "answer_sheet",
                "golden_path",
            ],
            "harness_source": _source_fingerprint(),
            "runtime": _runtime_fingerprint(),
            "configuration": {
                "model": self.config.model,
                "api_protocol": self.config.api_protocol,
                "requested_reasoning_effort": (
                    self.config.requested_reasoning_effort or self.config.reasoning_effort
                ),
                "reasoning_effort": self.config.reasoning_effort,
                "provider_base_url": self.config.base_url,
                "request_timeout_seconds": self.config.timeout_seconds,
                "litellm_timeout_seconds": self.config.litellm_timeout_seconds,
                "request_retries": self.config.max_retries,
                "request_interval_seconds": self.config.request_interval_seconds,
                "request_pacing_policy": PACING_POLICY,
                "request_pacing_scope": "comparison_runner_process",
                "request_pacing_retries_included": True,
                "request_pacing_first_attempt_immediate": True,
                "automatic_retry_policy": "delivery-aware-allowlist-v1",
                "safe_retry_http_statuses": sorted(SAFE_RETRY_HTTP_STATUSES),
                "safe_automatic_retry_reasons": sorted(
                    SAFE_AUTOMATIC_RETRY_REASONS
                ),
                "overload_retry_min_seconds": OVERLOAD_RETRY_MIN_SECONDS,
                "capacity_retry_delay_policy": (
                    "max-valid-retry-after-and-overload-min-then-global-cap"
                ),
                "connect_retry_min_seconds": CONNECT_RETRY_MIN_SECONDS,
                "retry_backoff_max_seconds": RETRY_BACKOFF_MAX_SECONDS,
                "read_timeout_policy": "fail-closed-no-replay",
                "http_408_policy": "fail-closed-no-replay",
                "stream_interruption_policy": "fail-closed-no-replay",
                "request_attempt_telemetry": (
                    "delivery-safe-retry-ids-headers-backoff-pacing-v4"
                ),
                "store_responses": self.config.store_responses,
                "generation": self.config.generation_dict(),
                "max_model_calls": self.max_model_calls,
                "max_turns_per_arm": self.max_turns_per_arm,
                "max_total_tokens": self.max_total_tokens,
                "max_output_tokens_per_call": self.max_output_tokens,
                "task_timeout_seconds": self.task_timeout_seconds,
                "recalculate": self.recalculate,
                "task_retries": 0,
                "circuit_breaker_threshold": self.circuit_breaker_threshold,
                "circuit_breaker_threshold_categories": [
                    "provider_transient",
                    "routing_protocol",
                ],
                "circuit_breaker_immediate_categories": [
                    "provider_fatal",
                    "recalculation_infrastructure",
                ],
                "skills_for_ours_only": skills,
                "code_isolation": STRICT_ISOLATION_POLICY,
                **COMPARISON_CONFIGURATION_POLICIES,
            },
        }

    def _prepare_manifest(self, tasks: list[SpreadsheetTask]) -> None:
        expected = self._manifest(tasks)
        if self.manifest_path.is_file():
            try:
                actual = _strict_json_document(
                    _regular_file_bytes(self.manifest_path, label="comparison manifest"),
                    label="comparison manifest",
                )
            except HarnessError as exc:
                raise HarnessError(f"Invalid comparison manifest: {self.manifest_path}") from exc
            actual_sha256 = _manifest_file_sha256(self.manifest_path)
            if actual != expected:
                if actual_sha256 != LEGACY_PILOT_MANIFEST_SHA256:
                    raise HarnessError(
                        "Refusing to resume comparison with a different frozen config"
                    )
                mutable_source_fields = {"harness_source", "runtime", "repository_source"}
                actual_static = {
                    key: value
                    for key, value in actual.items()
                    if key not in mutable_source_fields
                }
                expected_static = {
                    key: value
                    for key, value in expected.items()
                    if key not in mutable_source_fields
                }
                if actual_static != expected_static or actual.get("repository_source") is not None:
                    raise HarnessError(
                        "Legacy pilot manifest differs outside its immutable source fields"
                    )
                self.legacy_source_transition = True
            if self.run_spec_document is not None:
                verify_pilot_run_spec_contract(
                    self.run_spec_document, manifest_execution_contract(actual)
                )
            return
        if self.results_path.is_file() and self.results_path.stat().st_size:
            raise HarnessError("Comparison results exist without a compatibility manifest")
        _atomic_write_json(self.manifest_path, expected)

    def _prepare_continuation_source(
        self, *, comparison_manifest_sha256: str
    ) -> dict[str, Any] | None:
        if self.repository_source_state is None:
            return None
        expected = {
            "schema_version": 1,
            "comparison_manifest_sha256": comparison_manifest_sha256,
            "repository_source": self.repository_source_state,
        }
        expected["record_sha256"] = _text_sha256(
            json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        if self.continuation_source_path.exists():
            actual = _strict_json_document(
                _regular_file_bytes(
                    self.continuation_source_path, label="continuation source record"
                ),
                label="continuation source record",
            )
            if set(actual) != set(expected) or actual != expected:
                raise HarnessError("Continuation source record does not match this checkout")
        else:
            _atomic_write_json(self.continuation_source_path, expected)
        return expected

    def _prepare_run_spec_copy(self) -> None:
        if self.run_spec_bytes is None:
            return
        if self.run_spec_copy_path.is_file():
            if _regular_file_bytes(
                self.run_spec_copy_path, label="saved pilot run spec"
            ) != self.run_spec_bytes:
                raise HarnessError("Saved pilot run spec does not match the current run spec")
            return
        if self.run_spec_copy_path.exists():
            raise HarnessError("Saved pilot run spec is not a regular file")
        _atomic_write_bytes(self.run_spec_copy_path, self.run_spec_bytes)

    def _latest(self) -> dict[str, dict[str, Any]]:
        rows, _ = _strict_jsonl_rows(self.results_path)
        latest: dict[str, dict[str, Any]] = {}
        seen_keys: set[str] = set()
        duplicate_keys: set[str] = set()
        for row in rows:
            if row.get("task_id") is None or row.get("arm") is None:
                continue
            key = _run_key(str(row["task_id"]), str(row["arm"]))
            if key in seen_keys:
                duplicate_keys.add(key)
            seen_keys.add(key)
            if row.get("arm") not in self.arms:
                continue
            latest[key] = row
        if duplicate_keys:
            duplicates = ", ".join(sorted(duplicate_keys))
            raise HarnessError(
                "Refusing to resume comparison with duplicate arm-task rows: "
                f"{duplicates}"
            )
        return latest

    def _append(self, row: dict[str, Any]) -> None:
        encoded = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode()
        descriptor = os.open(self.results_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _task_directory(self, task_id: str, arm: str) -> Path:
        path = self.output_dir / "runs" / task_id / arm
        if path.exists() and any(path.iterdir()):
            path = path.with_name(f"{arm}-{uuid.uuid4().hex[:8]}")
        return path

    def _write_inflight(
        self, task_id: str, arm: str, *, comparison_manifest_sha256: str
    ) -> None:
        if self.inflight_path.exists():
            raise HarnessError(
                "Refusing to sample with an unresolved in-flight arm-task marker"
            )
        _atomic_write_json(
            self.inflight_path,
            {
                "schema_version": 1,
                "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
                "comparison_manifest_sha256": comparison_manifest_sha256,
                "run_spec_provenance": self.run_spec_provenance,
                "task_id": task_id,
                "arm": arm,
            },
        )

    def _clear_inflight(self) -> None:
        self.inflight_path.unlink()
        directory = os.open(self.output_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _read_valid_inflight_marker(
        self,
        tasks: list[SpreadsheetTask],
        *,
        comparison_manifest_sha256: str,
    ) -> tuple[dict[str, Any], bytes]:
        marker_bytes = _regular_file_bytes(self.inflight_path, label="in-flight marker")
        marker = _strict_json_document(marker_bytes, label="in-flight marker")
        required = {
            "schema_version",
            "comparison_protocol_version",
            "comparison_manifest_sha256",
            "run_spec_provenance",
            "task_id",
            "arm",
        }
        allowed_tasks = {task.task_id for task in tasks}
        if (
            set(marker) != required
            or marker.get("schema_version") != 1
            or marker.get("comparison_protocol_version") != COMPARISON_PROTOCOL_VERSION
            or marker.get("comparison_manifest_sha256")
            != comparison_manifest_sha256
            or marker.get("run_spec_provenance") != self.run_spec_provenance
            or marker.get("task_id") not in allowed_tasks
            or marker.get("arm") not in self.arms
        ):
            raise HarnessError("In-flight marker does not match the frozen comparison")
        return marker, marker_bytes

    def _clear_inflight_if_terminal_row_is_durable(
        self,
        tasks: list[SpreadsheetTask],
        rows: list[dict[str, Any]],
        *,
        comparison_manifest_sha256: str,
    ) -> bool:
        if not self.inflight_path.exists():
            return False
        marker, _ = self._read_valid_inflight_marker(
            tasks,
            comparison_manifest_sha256=comparison_manifest_sha256,
        )
        matching = [
            row
            for row in rows
            if row.get("task_id") == marker["task_id"]
            and row.get("arm") == marker["arm"]
        ]
        if not matching:
            return False
        if len(matching) != 1:
            raise HarnessError("In-flight arm-task has duplicate terminal result rows")
        row = matching[0]
        if (
            row.get("comparison_protocol_version") != COMPARISON_PROTOCOL_VERSION
            or row.get("comparison_manifest_sha256") != comparison_manifest_sha256
            or row.get("split_provenance") != self.split_provenance
            or row.get("run_spec_provenance") != self.run_spec_provenance
            or row.get("status") not in {"completed", "error"}
            or not isinstance(row.get("passed"), bool)
            or row.get("continuation_source") != self.continuation_source_record
        ):
            raise HarnessError("In-flight terminal row is not bound to the frozen comparison")
        self._clear_inflight()
        return True

    def _read_interrupted_seals(self) -> list[dict[str, Any]]:
        if not self.interrupted_seals_path.exists():
            return []
        document = _strict_json_document(
            _regular_file_bytes(
                self.interrupted_seals_path, label="interrupted arm-task seals"
            ),
            label="interrupted arm-task seals",
        )
        seals = document.get("seals")
        if (
            set(document) != {"schema_version", "seals"}
            or document.get("schema_version") != 1
            or not isinstance(seals, list)
        ):
            raise HarnessError("Interrupted arm-task seals document is invalid")
        if not all(isinstance(seal, dict) for seal in seals):
            raise HarnessError("Interrupted arm-task seal entries must be objects")
        keys = [_run_key(str(seal.get("task_id")), str(seal.get("arm"))) for seal in seals]
        if len(keys) != len(set(keys)):
            raise HarnessError("Interrupted arm-task seals contain duplicate keys")
        return seals

    def _validate_interrupted_seals(
        self,
        tasks: list[SpreadsheetTask],
        *,
        comparison_manifest_sha256: str,
    ) -> dict[str, dict[str, Any]]:
        allowed_tasks = {task.task_id for task in tasks}
        result: dict[str, dict[str, Any]] = {}
        required = {
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
        for seal in self._read_interrupted_seals():
            task_id = seal.get("task_id")
            arm = seal.get("arm")
            digest = seal.get("sealed_from_inflight_marker_sha256")
            if (
                set(seal) != required
                or seal.get("schema_version") != 1
                or task_id not in allowed_tasks
                or arm not in self.arms
                or seal.get("comparison_protocol_version")
                != COMPARISON_PROTOCOL_VERSION
                or seal.get("comparison_manifest_sha256")
                != comparison_manifest_sha256
                or seal.get("split_provenance") != self.split_provenance
                or seal.get("run_spec_provenance") != self.run_spec_provenance
                or seal.get("status") != "interrupted"
                or seal.get("passed") is not None
                or seal.get("outcome_observed") is not False
                or seal.get("score_available") is not False
                or seal.get("usage_observed") is not False
                or seal.get("replay_permitted") is not False
                or seal.get("error_retryable") is not False
                or seal.get("error_category") != "interrupted_unknown_outcome"
                or not isinstance(seal.get("sealed_at"), str)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise HarnessError("Interrupted arm-task seal does not match the run")
            try:
                datetime.fromisoformat(str(seal["sealed_at"]))
            except ValueError as exc:
                raise HarnessError("Interrupted arm-task seal timestamp is invalid") from exc
            result[_run_key(str(task_id), str(arm))] = seal
        return result

    def _seal_interrupted_inflight(
        self,
        tasks: list[SpreadsheetTask],
        *,
        comparison_manifest_sha256: str,
    ) -> dict[str, Any]:
        """Consume an ambiguous intent without ever authorizing its replay."""

        if not self.inflight_path.is_file() or self.inflight_path.is_symlink():
            raise HarnessError("No regular in-flight marker is available to seal")
        marker, marker_bytes = self._read_valid_inflight_marker(
            tasks,
            comparison_manifest_sha256=comparison_manifest_sha256,
        )
        existing, invalid = _strict_jsonl_rows(self.results_path)
        if invalid or (
            self.results_path.is_file()
            and self.results_path.stat().st_size
            and not self.results_path.read_bytes().endswith(b"\n")
        ):
            raise HarnessError("Cannot seal against a damaged results journal")
        key = _run_key(str(marker["task_id"]), str(marker["arm"]))
        if any(
            _run_key(str(row.get("task_id")), str(row.get("arm"))) == key
            for row in existing
        ):
            raise HarnessError("In-flight arm-task already has a terminal result row")
        existing_seals = self._read_interrupted_seals()
        existing_seal = next(
            (
                seal
                for seal in existing_seals
                if _run_key(str(seal.get("task_id")), str(seal.get("arm"))) == key
            ),
            None,
        )
        marker_sha256 = hashlib.sha256(marker_bytes).hexdigest()
        if existing_seal is not None:
            if existing_seal.get("sealed_from_inflight_marker_sha256") != marker_sha256:
                raise HarnessError("Existing interrupted seal does not match the marker")
            self._clear_inflight()
            return existing_seal
        now = datetime.now(timezone.utc).isoformat()
        seal = {
            "schema_version": 1,
            "task_id": marker["task_id"],
            "arm": marker["arm"],
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "comparison_manifest_sha256": comparison_manifest_sha256,
            "split_provenance": self.split_provenance,
            "run_spec_provenance": self.run_spec_provenance,
            "status": "interrupted",
            "passed": None,
            "outcome_observed": False,
            "score_available": False,
            "usage_observed": False,
            "replay_permitted": False,
            "error_retryable": False,
            "error_category": "interrupted_unknown_outcome",
            "sealed_at": now,
            "sealed_from_inflight_marker_sha256": marker_sha256,
        }
        _atomic_write_json(
            self.interrupted_seals_path,
            {"schema_version": 1, "seals": [*existing_seals, seal]},
        )
        self._clear_inflight()
        return seal

    def seal_interrupted_inflight(self, tasks: list[SpreadsheetTask]) -> dict[str, Any]:
        if not tasks or len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("comparison tasks must be non-empty with unique IDs")
        self._require_launchable_run_spec(operation="seal interrupted state for")
        # Sealing changes no model-visible state, but it must be attributed to
        # the same clean, remotely observed source identity as a continuation.
        self._prepare_repository_source_state()
        with self._exclusive_lock():
            self._prepare_run_spec_copy()
            self._prepare_manifest(tasks)
            manifest_sha256 = _manifest_file_sha256(self.manifest_path)
            self.continuation_source_record = self._prepare_continuation_source(
                comparison_manifest_sha256=manifest_sha256
            )
            existing_seals = self._validate_interrupted_seals(
                tasks, comparison_manifest_sha256=manifest_sha256
            )
            if not self.inflight_path.exists():
                if len(existing_seals) == 1:
                    return next(iter(existing_seals.values()))
                raise HarnessError("No unambiguous interrupted arm-task seal is available")
            return self._seal_interrupted_inflight(
                tasks, comparison_manifest_sha256=manifest_sha256
            )

    def _run_one(
        self,
        task: SpreadsheetTask,
        arm: str,
        *,
        comparison_manifest_sha256: str,
    ) -> dict[str, Any]:
        if len(comparison_manifest_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in comparison_manifest_sha256
        ):
            raise ValueError("comparison_manifest_sha256 must be a lowercase SHA-256")
        started_at = datetime.now(timezone.utc)
        started_clock = monotonic()
        task_dir = self._task_directory(task.task_id, arm)
        budget = RunBudget(
            max_model_calls=self.max_model_calls,
            max_total_tokens=self.max_total_tokens,
            max_elapsed_seconds=self.task_timeout_seconds,
        )
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "comparison_manifest_sha256": comparison_manifest_sha256,
            "split_provenance": self.split_provenance,
            "run_spec_provenance": self.run_spec_provenance,
            "continuation_source": self.continuation_source_record,
            "instruction_type": task.instruction_type,
            "model": self.config.model,
            "api_protocol": self.config.api_protocol,
            "requested_reasoning_effort": (
                self.config.requested_reasoning_effort or self.config.reasoning_effort
            ),
            "reasoning_effort": self.config.reasoning_effort,
            "request_interval_seconds": self.config.request_interval_seconds,
            "litellm_timeout_seconds": self.config.litellm_timeout_seconds,
            "generation": self.config.generation_dict(),
            "max_model_calls": self.max_model_calls,
            "max_turns_per_arm": self.max_turns_per_arm,
            "stage_turn_caps": dict(self.stage_turn_caps[arm]),
            "run_dir": str(task_dir),
            "started_at": started_at.isoformat(),
            "calculation_backend": "libreoffice" if self.recalculate else "not_recalculated",
        }
        session: WorkbookSession | None = None
        result: Any | None = None

        def remaining_seconds(stage: str) -> float:
            ensure_postprocess_time(stage)
            assert budget.deadline is not None
            return max(budget.deadline - monotonic(), 0.001)

        def ensure_postprocess_time(stage: str) -> None:
            termination = budget.to_dict().get("termination")
            reason = termination.get("reason") if isinstance(termination, dict) else None
            if reason in {"max_model_calls", "max_total_tokens"}:
                if budget.deadline is not None and monotonic() >= budget.deadline:
                    raise AgentTimeoutError(
                        f"Agent exceeded the task timeout during {stage}"
                    )
                return
            budget.ensure_within_time(stage=stage)

        try:
            session = WorkbookSession.create(
                task.input_path,
                task_dir,
                run_id=f"{task.task_id}-{arm}",
                recorder_secrets=(self.config.api_key,),
            )
            session.recorder.record(
                "benchmark.configured",
                {
                    "schema_version": COMPARISON_MANIFEST_SCHEMA_VERSION,
                    "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
                    "arm": arm,
                    "api_protocol": self.config.api_protocol,
                    "request_interval_seconds": self.config.request_interval_seconds,
                    "litellm_timeout_seconds": self.config.litellm_timeout_seconds,
                    "generation": self.config.generation_dict(),
                    "request_pacing_policy": PACING_POLICY,
                    "request_pacing_scope": "comparison_runner_process",
                    "max_model_calls": self.max_model_calls,
                    "max_turns_per_arm": self.max_turns_per_arm,
                    "stage_turn_caps": dict(self.stage_turn_caps[arm]),
                    "max_total_tokens": self.max_total_tokens,
                    "max_output_tokens_per_call": self.max_output_tokens,
                    "task_timeout_seconds": self.task_timeout_seconds,
                },
            )
            execution_failure: AgentExecutionFailure | None = None
            try:
                result = run_arm(
                    arm=arm,
                    config=self.config,
                    session=session,
                    skills=self.skill_registry if arm == "ours" else None,
                    instruction=task.instruction,
                    max_output_tokens=self.max_output_tokens,
                    max_elapsed_seconds=self.task_timeout_seconds,
                    budget=budget,
                    pacer=self.relay_pacer,
                    max_turns_per_arm=self.max_turns_per_arm,
                )
            except AgentExecutionFailure as exc:
                if exc.reason not in AGENT_EXECUTION_FAILURE_REASONS:
                    raise HarnessError(
                        "Agent execution failure used an unknown reason"
                    ) from exc
                result = exc.agent_result
                if result is None or not callable(getattr(result, "to_dict", None)):
                    raise HarnessError(
                        "Agent execution failure omitted auditable agent evidence"
                    ) from exc
                execution_failure = exc
            recalculation: dict[str, Any] | None = None
            if self.recalculate:
                from .render import recalculate_workbook

                recalculation = recalculate_workbook(
                    session.workbook_path,
                    session.workbook_path,
                    timeout_seconds=min(120.0, remaining_seconds("recalculate")),
                )
                ensure_postprocess_time("recalculate")
            ensure_postprocess_time("score")
            comparison = compare_workbooks_chartsheet_safe(
                task.golden_path,
                session.workbook_path,
                task.answer_position,
                answer_sheet=task.answer_sheet,
            )
            ensure_postprocess_time("score")
            agent_evidence = result.to_dict()
            if not isinstance(agent_evidence, dict):
                raise HarnessError("Agent result evidence must be a JSON object")
            row.update(
                {
                    "status": "completed",
                    "passed": comparison.passed if execution_failure is None else False,
                    "artifact_score_passed": comparison.passed,
                    "comparison": comparison.to_dict(),
                    "agent": agent_evidence,
                    "recalculation": recalculation,
                    "output_workbook": str(session.workbook_path),
                    "output_sha256": _sha256(session.workbook_path),
                    "outcome_kind": (
                        "scored"
                        if execution_failure is None
                        else "model_execution_failure"
                    ),
                }
            )
            if execution_failure is not None:
                safe_error = str(execution_failure).replace(
                    self.config.api_key, "[REDACTED]"
                )
                row.update(
                    {
                        "error": safe_error,
                        "error_type": type(execution_failure).__name__,
                        "error_retryable": False,
                        "error_category": "model_execution_failure",
                        "model_failure_reason": execution_failure.reason,
                    }
                )
            session.recorder.record(
                "benchmark.evaluated",
                {
                    "task_id": task.task_id,
                    "arm": arm,
                    "passed": row["passed"],
                    "artifact_score_passed": comparison.passed,
                    "outcome_kind": row["outcome_kind"],
                    "model_failure_reason": row.get("model_failure_reason"),
                    "status": "completed",
                    "scorer": "cleanroom-corrected-value-v1",
                    "style_checked": False,
                    "calculation_backend": row["calculation_backend"],
                    **comparison_evidence(comparison),
                    "scoring_metadata_sha256": _scoring_metadata_sha256(task),
                },
            )
        except CodeIsolationError:
            # Comparison results are invalid if code can escape its arm. Stop
            # the entire run instead of recording an error and continuing.
            raise
        except Exception as caught:
            effective_exc = caught
            if not isinstance(caught, RecalculationIntegrityError):
                try:
                    ensure_postprocess_time("postprocess")
                except (AgentBudgetError, AgentTimeoutError) as budget_exc:
                    effective_exc = budget_exc
            safe_error = str(effective_exc).replace(self.config.api_key, "[REDACTED]")
            row.update(
                {
                    "status": "error",
                    "passed": False,
                    "error": safe_error,
                    "error_type": type(effective_exc).__name__,
                    "error_retryable": False,
                    "error_category": "harness",
                }
            )
            if isinstance(effective_exc, AgentBudgetError):
                row["error_category"] = (
                    "task_timeout"
                    if effective_exc.reason == "max_elapsed_seconds"
                    else "budget_exhausted"
                )
            elif isinstance(effective_exc, AgentTimeoutError):
                row["error_category"] = "task_timeout"
            elif isinstance(effective_exc, PaperStageValidationError):
                row.update(
                    {
                        "error_category": "paper_stage_validation",
                        "paper_stage": effective_exc.stage,
                        "paper_stage_reason": effective_exc.reason,
                    }
                )
            elif isinstance(effective_exc, AgentRoutingError):
                row["error_category"] = "routing_protocol"
            elif isinstance(effective_exc, RecalculationIntegrityError):
                exception_result = effective_exc.agent_result
                evidence_result = result if result is not None else exception_result
                agent_evidence = (
                    evidence_result.to_dict()
                    if callable(getattr(evidence_result, "to_dict", None))
                    else None
                )
                if not isinstance(agent_evidence, dict) or session is None:
                    raise HarnessError(
                        "Recalculation integrity failure omitted auditable run evidence"
                    ) from effective_exc
                agent_tool_failure = bool(
                    effective_exc.failed_tool == RECALCULATION_VALIDATION_TOOL
                    and isinstance(effective_exc.agent_stage, str)
                    and effective_exc.agent_stage
                )
                row.update(
                    {
                        "outcome_kind": "infrastructure_failure",
                        "score_available": False,
                        "error_category": "recalculation_infrastructure",
                        "infrastructure_failure_stage": (
                            AGENT_TOOL_RECALCULATION_FAILURE_STAGE
                            if agent_tool_failure
                            else POSTPROCESS_RECALCULATION_FAILURE_STAGE
                        ),
                        "recalculation_failure_reason": "sheet_inventory_changed",
                        "agent": agent_evidence,
                        "recalculation": effective_exc.evidence,
                        "output_workbook": str(session.workbook_path),
                        "output_sha256": _sha256(session.workbook_path),
                    }
                )
                if agent_tool_failure:
                    row.update(
                        {
                            "agent_failure_stage": effective_exc.agent_stage,
                            "infrastructure_failure_tool": (
                                effective_exc.failed_tool
                            ),
                        }
                    )
                if execution_failure is not None:
                    row["prior_model_execution_failure"] = {
                        "error": str(execution_failure).replace(
                            self.config.api_key, "[REDACTED]"
                        ),
                        "error_type": type(execution_failure).__name__,
                        "model_failure_reason": execution_failure.reason,
                    }
            elif isinstance(effective_exc, ScoringInfrastructureError):
                agent_evidence = result.to_dict() if result is not None else None
                if not isinstance(agent_evidence, dict) or session is None:
                    raise HarnessError(
                        "Scoring infrastructure failure omitted auditable run evidence"
                    ) from effective_exc
                row.update(
                    {
                        "outcome_kind": "infrastructure_failure",
                        "score_available": False,
                        "error_category": "scoring_infrastructure",
                        "infrastructure_failure_stage": "scoring",
                        "scoring_failure_reason": "worksheet_scorer_unsupported",
                        "agent": agent_evidence,
                        "recalculation": recalculation,
                        "output_workbook": str(session.workbook_path),
                        "output_sha256": _sha256(session.workbook_path),
                    }
                )
                if execution_failure is not None:
                    row["prior_model_execution_failure"] = {
                        "error": str(execution_failure).replace(
                            self.config.api_key, "[REDACTED]"
                        ),
                        "error_type": type(execution_failure).__name__,
                        "model_failure_reason": execution_failure.reason,
                    }
            elif isinstance(effective_exc, ProviderError):
                row.update(
                    {
                        "error_retryable": bool(effective_exc.safe_to_retry),
                        "error_category": (
                            "provider_transient"
                            if effective_exc.retryable
                            else "provider_fatal"
                            if effective_exc.global_fatal
                            else "provider_task"
                        ),
                        "provider_error": effective_exc.public_dict(
                            secrets=(self.config.api_key,)
                        ),
                    }
                )
        finally:
            row["budget"] = budget.to_dict()
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            row["elapsed_seconds"] = round(monotonic() - started_clock, 3)
            if session is not None:
                if row.get("status") != "completed":
                    try:
                        session.recorder.record(
                            "benchmark.not_evaluated",
                            {
                                "task_id": task.task_id,
                                "arm": arm,
                                "status": str(row.get("status", "error")),
                                "scorer": "cleanroom-corrected-value-v1",
                                "style_checked": False,
                                "calculation_backend": row["calculation_backend"],
                                "error_category": row.get("error_category"),
                                "outcome_kind": row.get("outcome_kind"),
                                "score_available": row.get("score_available"),
                                "infrastructure_failure_stage": row.get(
                                    "infrastructure_failure_stage"
                                ),
                                "recalculation_failure_reason": row.get(
                                    "recalculation_failure_reason"
                                ),
                                "agent_failure_stage": row.get(
                                    "agent_failure_stage"
                                ),
                                "infrastructure_failure_tool": row.get(
                                    "infrastructure_failure_tool"
                                ),
                                "scoring_failure_reason": row.get(
                                    "scoring_failure_reason"
                                ),
                                "recalculation": row.get("recalculation"),
                            },
                        )
                    except Exception:
                        pass
                try:
                    session.write_manifest(
                        {
                            "task_id": task.task_id,
                            "arm": arm,
                            "instruction_sha256": _text_sha256(task.instruction),
                            "result": row,
                        }
                    )
                except Exception:
                    pass
        return row

    def run(self, tasks: list[SpreadsheetTask], *, resume: bool = True) -> dict[str, Any]:
        if not tasks or len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("comparison tasks must be non-empty with unique IDs")
        self._require_launchable_run_spec(resume=resume, operation="launch")
        # Source verification and the isolation probe run before creating a
        # manifest, spending model budget, or writing a result row.
        self.preflight(tasks)
        if not resume and self.output_dir.exists():
            allowed = {RUN_SPEC_COPY_FILENAME} if self.run_spec_document is not None else set()
            unexpected = {path.name for path in self.output_dir.iterdir()} - allowed
            if unexpected:
                raise HarnessError(
                    "Refusing to start a fresh comparison in a non-empty directory"
                )
        with self._exclusive_lock():
            self._prepare_run_spec_copy()
            self._prepare_manifest(tasks)
            raw_results = self.results_path.read_bytes() if self.results_path.is_file() else b""
            _, invalid_rows = _strict_jsonl_rows(self.results_path)
            if invalid_rows or (raw_results and not raw_results.endswith(b"\n")):
                raise HarnessError(
                    "Refusing to resume comparison with a damaged or non-terminated "
                    "results journal"
                )
            manifest_sha256 = _manifest_file_sha256(self.manifest_path)
            self.continuation_source_record = self._prepare_continuation_source(
                comparison_manifest_sha256=manifest_sha256
            )
            resume_rows, _ = _strict_jsonl_rows(self.results_path)
            interrupted_seals = self._validate_interrupted_seals(
                tasks, comparison_manifest_sha256=manifest_sha256
            )
            expected_task_ids = {task.task_id for task in tasks}
            for row_number, row in enumerate(resume_rows, start=1):
                task_id = row.get("task_id")
                arm = row.get("arm")
                if task_id is None or arm is None:
                    raise HarnessError(
                        "Refusing to resume comparison with a result row missing its "
                        f"identity: line {row_number}"
                    )
                if str(task_id) not in expected_task_ids or str(arm) not in self.arms:
                    raise HarnessError(
                        "Refusing to resume comparison with an unexpected arm-task row: "
                        f"{task_id}::{arm}"
                    )
                if row.get("comparison_protocol_version") != COMPARISON_PROTOCOL_VERSION:
                    raise HarnessError(
                        "Refusing to resume comparison with result rows from a different "
                        f"or missing protocol: {task_id}::{arm}"
                    )
                if row.get("comparison_manifest_sha256") != manifest_sha256:
                    raise HarnessError(
                        "Refusing to resume comparison with a result row not bound to the "
                        f"current manifest: {task_id}::{arm}"
                    )
                if row.get("split_provenance") != self.split_provenance:
                    raise HarnessError(
                        "Refusing to resume comparison with split provenance that differs "
                        f"from the current manifest: {task_id}::{arm}"
                    )
                if row.get("run_spec_provenance") != self.run_spec_provenance:
                    raise HarnessError(
                        "Refusing to resume comparison with run spec provenance that differs "
                        f"from the current manifest: {task_id}::{arm}"
                    )
                if row.get("continuation_source") != self.continuation_source_record and not (
                    self.legacy_source_transition
                    and row.get("continuation_source") is None
                ):
                    raise HarnessError(
                        "Refusing to resume comparison with a result row not bound to the "
                        f"continuation source: {task_id}::{arm}"
                    )
            if self.inflight_path.exists() and not self._clear_inflight_if_terminal_row_is_durable(
                tasks,
                resume_rows,
                comparison_manifest_sha256=manifest_sha256,
            ):
                raise HarnessError(
                    "Refusing to resume after an ambiguous in-flight arm-task; seal it "
                    "before continuing"
                )
            latest = self._latest()
            arm_orders = _balanced_arm_orders(
                [task.task_id for task in tasks], self.arm_order_seed, self.arms
            )
            exhausted_transient = sum(
                row.get("error_category") == "provider_transient"
                for row in latest.values()
            )
            fatal_provider_errors = sum(
                row.get("error_category") == "provider_fatal"
                for row in latest.values()
            )
            routing_protocol_errors = sum(
                row.get("error_category") == "routing_protocol"
                for row in latest.values()
            )
            recalculation_infrastructure_errors = sum(
                row.get("error_category") == "recalculation_infrastructure"
                for row in latest.values()
            )
            scoring_infrastructure_errors = sum(
                row.get("error_category") == "scoring_infrastructure"
                for row in latest.values()
            )
            circuit_breaker = bool(
                fatal_provider_errors
                or recalculation_infrastructure_errors
                or scoring_infrastructure_errors
                or exhausted_transient >= self.circuit_breaker_threshold
                or routing_protocol_errors >= self.circuit_breaker_threshold
            )
            expected = len(tasks) * len(self.arms)
            finished = sum(
                _run_key(task.task_id, arm) in latest for task in tasks for arm in self.arms
            )
            for task in tasks:
                for arm in arm_orders[task.task_id]:
                    key = _run_key(task.task_id, arm)
                    if key in latest or key in interrupted_seals:
                        continue
                    if circuit_breaker:
                        break
                    self._write_inflight(
                        task.task_id,
                        arm,
                        comparison_manifest_sha256=manifest_sha256,
                    )
                    row = self._run_one(
                        task,
                        arm,
                        comparison_manifest_sha256=manifest_sha256,
                    )
                    self._append(row)
                    self._clear_inflight()
                    latest[key] = row
                    finished += 1
                    if row.get("error_category") == "provider_fatal":
                        fatal_provider_errors += 1
                        circuit_breaker = True
                    elif row.get("error_category") == "recalculation_infrastructure":
                        recalculation_infrastructure_errors += 1
                        circuit_breaker = True
                    elif row.get("error_category") == "scoring_infrastructure":
                        scoring_infrastructure_errors += 1
                        circuit_breaker = True
                    elif row.get("error_category") == "provider_transient":
                        exhausted_transient += 1
                        if exhausted_transient >= self.circuit_breaker_threshold:
                            circuit_breaker = True
                    elif row.get("error_category") == "routing_protocol":
                        routing_protocol_errors += 1
                        if routing_protocol_errors >= self.circuit_breaker_threshold:
                            circuit_breaker = True
                    print(
                        json.dumps(
                            {
                                "event": "comparison.arm_task_finished",
                                "task_id": task.task_id,
                                "arm": arm,
                                "status": row.get("status"),
                                "passed": row.get("passed"),
                                "error_category": row.get("error_category"),
                                "elapsed_seconds": row.get("elapsed_seconds"),
                                "finished": finished,
                                "expected": expected,
                                "circuit_breaker_tripped": circuit_breaker,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                if circuit_breaker:
                    break
            summary = comparison_summary(
                self.results_path,
                tasks,
                arms=self.arms,
                bootstrap_seed=self.arm_order_seed,
                interrupted_keys=set(interrupted_seals),
                expected_protocol_version=COMPARISON_PROTOCOL_VERSION,
            )
            summary["circuit_breaker_tripped"] = circuit_breaker
            summary["exhausted_transient_arm_tasks"] = exhausted_transient
            summary["fatal_provider_arm_tasks"] = fatal_provider_errors
            summary["routing_protocol_arm_tasks"] = routing_protocol_errors
            summary["recalculation_infrastructure_arm_tasks"] = (
                recalculation_infrastructure_errors
            )
            summary["scoring_infrastructure_arm_tasks"] = (
                scoring_infrastructure_errors
            )
            summary["circuit_breaker_threshold"] = self.circuit_breaker_threshold
            # Bind the official summary to a full read-only protocol audit. A
            # score alone is not evidence that the frozen resources and routes
            # produced the artifact.
            from .audit import audit_comparison

            protocol_audit = audit_comparison(
                self.output_dir,
                tasks,
                arms=self.arms,
            )
            summary["protocol_audit_valid"] = protocol_audit["audit_valid"]
            summary["protocol_audit_reasons"] = protocol_audit["reasons"]
            summary["protocol_audit_manifest_sha256"] = protocol_audit[
                "manifest_sha256"
            ]
            summary["protocol_audit_results_sha256"] = protocol_audit[
                "results_sha256"
            ]
            if not protocol_audit["audit_valid"]:
                if "comparison_audit_failed" not in summary["inference_invalid_reasons"]:
                    summary["inference_invalid_reasons"].append(
                        "comparison_audit_failed"
                    )
                summary["inference_valid"] = False
                for pairwise in summary["pairwise"].values():
                    _invalidate_pairwise_inference(
                        pairwise,
                        ["comparison_audit_failed"],
                    )
            _atomic_write_json(self.summary_path, summary)
            return summary
