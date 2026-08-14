"""Posthoc evaluator records for immutable completion-attempt snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .benchmark import Comparison
from .completion_attempt import (
    CompletionAttemptRecord,
    audit_completion_attempt,
)
from .evidence_contract import ArtifactRef

COMPLETION_EVALUATION_SCHEMA_VERSION = "spreadsheet-completion-evaluation-v1"

_ASSURANCE = {
    "timing": "posthoc-after-agent-termination",
    "fed_back_to_model": False,
    "contains_evaluator_outcome": True,
    "is_digital_signature": False,
    "proves_task_correctness_beyond_named_evaluator": False,
}
_EVALUATOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "completion_record_sha256",
        "artifact",
        "snapshot_path",
        "snapshot_sha256",
        "evaluator",
        "comparison",
        "assurance",
        "record_sha256",
    }
)


class CompletionEvaluationError(ValueError):
    """Raised when posthoc attempt scoring cannot be verified."""


@dataclass(frozen=True)
class CompletionEvaluationRecord:
    """Canonical evaluator outcome bound to one immutable attempt record."""

    attempt_id: int
    completion_record_sha256: str
    artifact: ArtifactRef
    snapshot_path: str
    snapshot_sha256: str
    evaluator: str
    comparison: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not int or self.attempt_id < 1:
            raise CompletionEvaluationError("attempt_id must be a positive integer")
        _validate_sha256(
            self.completion_record_sha256,
            label="completion_record_sha256",
        )
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact must be an ArtifactRef")
        _validate_snapshot_path(self.snapshot_path)
        _validate_sha256(self.snapshot_sha256, label="snapshot_sha256")
        if self.snapshot_sha256 != self.artifact.sha256:
            raise CompletionEvaluationError("snapshot_sha256 must equal the artifact byte hash")
        if not isinstance(self.evaluator, str) or not _EVALUATOR_PATTERN.fullmatch(self.evaluator):
            raise CompletionEvaluationError("evaluator must be a portable non-empty label")
        normalized = _normalize_comparison(self.comparison)
        object.__setattr__(self, "comparison", normalized)

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPLETION_EVALUATION_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "completion_record_sha256": self.completion_record_sha256,
            "artifact": self.artifact.to_dict(),
            "snapshot_path": self.snapshot_path,
            "snapshot_sha256": self.snapshot_sha256,
            "evaluator": self.evaluator,
            "comparison": dict(self.comparison),
            "assurance": dict(_ASSURANCE),
        }

    @property
    def record_sha256(self) -> str:
        return _canonical_sha256(self.payload_dict())

    @property
    def passed(self) -> bool:
        return bool(self.comparison["passed"])

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "record_sha256": self.record_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompletionEvaluationRecord:
        if not isinstance(value, Mapping) or set(value) != _RECORD_KEYS:
            raise CompletionEvaluationError("completion-evaluation fields are invalid")
        if value.get("schema_version") != COMPLETION_EVALUATION_SCHEMA_VERSION:
            raise CompletionEvaluationError("completion-evaluation schema is invalid")
        if value.get("assurance") != _ASSURANCE:
            raise CompletionEvaluationError("completion-evaluation assurance is invalid")
        artifact_value = value.get("artifact")
        if not isinstance(artifact_value, Mapping) or set(artifact_value) != {
            "revision",
            "sha256",
        }:
            raise CompletionEvaluationError("artifact must be an exact artifact reference")
        try:
            record = cls(
                attempt_id=value.get("attempt_id"),  # type: ignore[arg-type]
                completion_record_sha256=value.get(  # type: ignore[arg-type]
                    "completion_record_sha256"
                ),
                artifact=ArtifactRef(
                    artifact_value.get("revision"),  # type: ignore[arg-type]
                    artifact_value.get("sha256"),  # type: ignore[arg-type]
                ),
                snapshot_path=value.get("snapshot_path"),  # type: ignore[arg-type]
                snapshot_sha256=value.get("snapshot_sha256"),  # type: ignore[arg-type]
                evaluator=value.get("evaluator"),  # type: ignore[arg-type]
                comparison=value.get("comparison"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise CompletionEvaluationError(
                f"completion-evaluation record is invalid: {exc}"
            ) from exc
        supplied_digest = value.get("record_sha256")
        _validate_sha256(supplied_digest, label="record_sha256")
        if supplied_digest != record.record_sha256:
            raise CompletionEvaluationError("completion-evaluation digest mismatch")
        return record


@dataclass(frozen=True)
class CompletionEvaluationAudit:
    valid: bool
    reasons: tuple[str, ...]
    record: CompletionEvaluationRecord | None = None


def evaluate_completion_attempt(
    workspace: Path,
    attempt_value: CompletionAttemptRecord | Mapping[str, Any],
    evaluator: Callable[[Path], Comparison],
    *,
    evaluator_id: str,
) -> CompletionEvaluationRecord:
    """Score one snapshot after the agent has terminated, with mutation guards."""

    audit = audit_completion_attempt(workspace, attempt_value)
    if not audit.valid or audit.record is None:
        raise CompletionEvaluationError(
            "completion attempt failed fresh audit before scoring: " + ", ".join(audit.reasons)
        )
    attempt = audit.record
    snapshot = _snapshot_path(workspace, attempt.snapshot_path)
    before_sha256 = _sha256(snapshot)
    evaluator_error: Exception | None = None
    comparison: Any = None
    try:
        comparison = evaluator(snapshot)
    except Exception as exc:
        evaluator_error = exc

    # Always run the mutation guard, including when the evaluator itself fails.
    after_audit = audit_completion_attempt(workspace, attempt)
    try:
        after_sha256 = _sha256(snapshot)
    except OSError:
        after_sha256 = None
    if not after_audit.valid or after_sha256 != before_sha256:
        mutation_error = CompletionEvaluationError(
            "completion evaluator mutated its immutable snapshot"
        )
        if evaluator_error is not None:
            raise mutation_error from evaluator_error
        raise mutation_error
    if evaluator_error is not None:
        raise CompletionEvaluationError(
            f"completion evaluator failed: {type(evaluator_error).__name__}"
        ) from evaluator_error
    if not isinstance(comparison, Comparison):
        raise CompletionEvaluationError("completion evaluator must return Comparison")
    return CompletionEvaluationRecord(
        attempt_id=attempt.attempt_id,
        completion_record_sha256=attempt.record_sha256,
        artifact=attempt.artifact,
        snapshot_path=attempt.snapshot_path,
        snapshot_sha256=attempt.snapshot_sha256,
        evaluator=evaluator_id,
        comparison=comparison.to_dict(),
    )


def evaluate_completion_attempts(
    workspace: Path,
    attempt_values: Sequence[CompletionAttemptRecord | Mapping[str, Any]],
    evaluator: Callable[[Path], Comparison],
    *,
    evaluator_id: str,
) -> tuple[CompletionEvaluationRecord, ...]:
    """Score a canonical attempt sequence without treating attempts as samples."""

    if isinstance(attempt_values, (str, bytes)) or not isinstance(attempt_values, Sequence):
        raise TypeError("attempt_values must be a sequence")
    evaluations = tuple(
        evaluate_completion_attempt(
            workspace,
            value,
            evaluator,
            evaluator_id=evaluator_id,
        )
        for value in attempt_values
    )
    attempt_ids = [item.attempt_id for item in evaluations]
    if attempt_ids != list(range(1, len(attempt_ids) + 1)):
        raise CompletionEvaluationError(
            "completion attempts must be ordered, unique, and contiguous from one"
        )
    return evaluations


def audit_completion_evaluation(
    workspace: Path,
    attempt_value: CompletionAttemptRecord | Mapping[str, Any],
    evaluation_value: CompletionEvaluationRecord | Mapping[str, Any],
    evaluator: Callable[[Path], Comparison],
) -> CompletionEvaluationAudit:
    """Re-run the named evaluator and compare the complete canonical record."""

    try:
        stored = (
            CompletionEvaluationRecord.from_dict(evaluation_value.to_dict())
            if isinstance(evaluation_value, CompletionEvaluationRecord)
            else CompletionEvaluationRecord.from_dict(evaluation_value)
        )
        fresh = evaluate_completion_attempt(
            workspace,
            attempt_value,
            evaluator,
            evaluator_id=stored.evaluator,
        )
        if fresh.to_dict() != stored.to_dict():
            raise CompletionEvaluationError(
                "stored completion evaluation does not match fresh scoring"
            )
    except (OSError, TypeError, ValueError) as exc:
        return CompletionEvaluationAudit(
            valid=False,
            reasons=(f"completion_evaluation_invalid:{type(exc).__name__}:{exc}",),
        )
    return CompletionEvaluationAudit(valid=True, reasons=(), record=stored)


def _normalize_comparison(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "passed",
        "checked_cells",
        "differences",
    }:
        raise CompletionEvaluationError("comparison has invalid fields")
    if type(value.get("passed")) is not bool:
        raise CompletionEvaluationError("comparison.passed must be boolean")
    checked = value.get("checked_cells")
    if type(checked) is not int or checked < 0:
        raise CompletionEvaluationError("comparison.checked_cells must be a non-negative integer")
    differences = value.get("differences")
    if not isinstance(differences, list) or not all(
        isinstance(item, Mapping) for item in differences
    ):
        raise CompletionEvaluationError("comparison.differences must be an object list")
    normalized = {
        "passed": value["passed"],
        "checked_cells": checked,
        "differences": [dict(item) for item in differences],
    }
    _canonical_json_bytes(normalized)
    return normalized


def _snapshot_path(workspace: Path, relative: str) -> Path:
    root = workspace.resolve(strict=True)
    return root.joinpath(*PurePosixPath(relative).parts)


def _validate_snapshot_path(value: Any) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CompletionEvaluationError("snapshot_path must be a relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "/".join(path.parts) != value
    ):
        raise CompletionEvaluationError("snapshot_path must be a relative POSIX path")


def _validate_sha256(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CompletionEvaluationError(f"{label} must be 64 lowercase hexadecimal characters")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompletionEvaluationError(f"record is not canonical JSON data: {exc}") from exc
    return rendered.encode("ascii")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
