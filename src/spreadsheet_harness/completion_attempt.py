"""Immutable workbook snapshots for model completion attempts.

Each record binds one logical artifact revision to the exact bytes present when
the model called ``submit_result``.  The canonical digest and fresh audit prove
snapshot consistency under a trusted workspace; they are not signatures and do
not contain or imply a benchmark evaluator outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence_contract import ArtifactRef

COMPLETION_ATTEMPT_SCHEMA_VERSION = "spreadsheet-completion-attempt-v1"
COMPLETION_ATTEMPT_RELATIVE_DIR = "completion-attempts"

_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
_SUPPORTED_WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xlsm"})
_ASSURANCE = {
    "scope": "snapshot-consistency-only",
    "contains_evaluator_outcome": False,
    "is_digital_signature": False,
    "proves_task_correctness": False,
}
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "artifact",
        "stage",
        "turn",
        "response_id",
        "call_id",
        "snapshot_path",
        "snapshot_sha256",
        "digest_algorithm",
        "assurance",
        "record_sha256",
    }
)
_STAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CompletionAttemptError(ValueError):
    """Raised when an attempt cannot be captured or verified fail-closed."""


@dataclass(frozen=True)
class CompletionAttemptRecord:
    """Canonical metadata for one exact, revision-bound workbook snapshot."""

    attempt_id: int
    artifact: ArtifactRef
    stage: str
    turn: int
    response_id: str | None
    call_id: str
    snapshot_path: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        _validate_positive_integer(self.attempt_id, label="attempt_id")
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact must be an ArtifactRef")
        _validate_stage(self.stage)
        _validate_positive_integer(self.turn, label="turn")
        _validate_provider_id(self.response_id, label="response_id", allow_none=True)
        _validate_provider_id(self.call_id, label="call_id", allow_none=False)
        relative = _validate_relative_posix_path(
            self.snapshot_path,
            label="snapshot_path",
        )
        if self.snapshot_path not in {
            _snapshot_relative_path(self.attempt_id, suffix)
            for suffix in _SUPPORTED_WORKBOOK_SUFFIXES
        }:
            raise CompletionAttemptError("snapshot_path does not match the canonical attempt path")
        if relative.parts[0] != COMPLETION_ATTEMPT_RELATIVE_DIR:
            raise CompletionAttemptError(
                "snapshot_path is outside the completion-attempt directory"
            )
        _validate_sha256(self.snapshot_sha256, label="snapshot_sha256")
        if self.snapshot_sha256 != self.artifact.sha256:
            raise CompletionAttemptError("snapshot_sha256 must equal the artifact byte hash")

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPLETION_ATTEMPT_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "artifact": self.artifact.to_dict(),
            "stage": self.stage,
            "turn": self.turn,
            "response_id": self.response_id,
            "call_id": self.call_id,
            "snapshot_path": self.snapshot_path,
            "snapshot_sha256": self.snapshot_sha256,
            "digest_algorithm": _DIGEST_ALGORITHM,
            "assurance": dict(_ASSURANCE),
        }

    @property
    def record_sha256(self) -> str:
        return _canonical_sha256(self.payload_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "record_sha256": self.record_sha256}

    def canonical_json(self) -> str:
        return _canonical_json_bytes(self.to_dict()).decode("ascii")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompletionAttemptRecord:
        """Parse an exact record schema and verify its canonical digest."""

        if not isinstance(value, Mapping) or set(value) != _RECORD_KEYS:
            raise CompletionAttemptError("completion-attempt record fields are invalid")
        if value.get("schema_version") != COMPLETION_ATTEMPT_SCHEMA_VERSION:
            raise CompletionAttemptError("completion-attempt schema_version is invalid")
        if value.get("digest_algorithm") != _DIGEST_ALGORITHM:
            raise CompletionAttemptError("completion-attempt digest_algorithm is invalid")
        if value.get("assurance") != _ASSURANCE:
            raise CompletionAttemptError("completion-attempt assurance is invalid")

        artifact_value = value.get("artifact")
        if not isinstance(artifact_value, Mapping) or set(artifact_value) != {
            "revision",
            "sha256",
        }:
            raise CompletionAttemptError("artifact must be an exact artifact reference")
        try:
            artifact = ArtifactRef(
                artifact_value.get("revision"),  # type: ignore[arg-type]
                artifact_value.get("sha256"),  # type: ignore[arg-type]
            )
            record = cls(
                attempt_id=value.get("attempt_id"),  # type: ignore[arg-type]
                artifact=artifact,
                stage=value.get("stage"),  # type: ignore[arg-type]
                turn=value.get("turn"),  # type: ignore[arg-type]
                response_id=value.get("response_id"),  # type: ignore[arg-type]
                call_id=value.get("call_id"),  # type: ignore[arg-type]
                snapshot_path=value.get("snapshot_path"),  # type: ignore[arg-type]
                snapshot_sha256=value.get("snapshot_sha256"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise CompletionAttemptError(f"completion-attempt record is invalid: {exc}") from exc

        supplied_digest = value.get("record_sha256")
        _validate_sha256(supplied_digest, label="record_sha256")
        if supplied_digest != record.record_sha256:
            raise CompletionAttemptError("completion-attempt record digest mismatch")
        return record


@dataclass(frozen=True)
class CompletionAttemptAudit:
    """Result of a fresh record-and-filesystem consistency audit."""

    valid: bool
    reasons: tuple[str, ...]
    record: CompletionAttemptRecord | None = None


class CompletionAttemptLedger:
    """Session-owned monotonic ledger of model completion snapshots."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = _validate_workspace(workspace)
        self._records: list[CompletionAttemptRecord] = []
        self._next_attempt_id = 1
        _ensure_directory(self._workspace, PurePosixPath(COMPLETION_ATTEMPT_RELATIVE_DIR))

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def records(self) -> tuple[CompletionAttemptRecord, ...]:
        return tuple(self._records)

    @property
    def next_attempt_id(self) -> int:
        return self._next_attempt_id

    def capture(
        self,
        workbook_path: Path,
        artifact: ArtifactRef,
        *,
        stage: str,
        turn: int,
        response_id: str | None,
        call_id: str,
    ) -> CompletionAttemptRecord:
        """Capture exact workbook bytes without consulting an event recorder."""

        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact must be an ArtifactRef")
        _validate_stage(stage)
        _validate_positive_integer(turn, label="turn")
        _validate_provider_id(response_id, label="response_id", allow_none=True)
        _validate_provider_id(call_id, label="call_id", allow_none=False)

        source = _existing_path_inside_workspace(
            self._workspace,
            workbook_path,
            label="workbook_path",
        )
        workbook_bytes = _read_regular_non_symlink(source, label="workbook_path")
        snapshot_sha256 = hashlib.sha256(workbook_bytes).hexdigest()
        if snapshot_sha256 != artifact.sha256:
            raise CompletionAttemptError(
                "workbook bytes do not match the supplied ArtifactRef sha256"
            )

        attempt_id = self._next_attempt_id
        workbook_suffix = source.suffix.lower()
        if workbook_suffix not in _SUPPORTED_WORKBOOK_SUFFIXES:
            raise CompletionAttemptError("workbook_path must use a supported .xlsx or .xlsm suffix")
        relative_path = _snapshot_relative_path(attempt_id, workbook_suffix)
        destination = _new_path_inside_workspace(
            self._workspace,
            relative_path,
            label="snapshot_path",
        )
        try:
            _write_read_only_exclusive(destination, workbook_bytes)

            record = CompletionAttemptRecord(
                attempt_id=attempt_id,
                artifact=artifact,
                stage=stage,
                turn=turn,
                response_id=response_id,
                call_id=call_id,
                snapshot_path=relative_path,
                snapshot_sha256=snapshot_sha256,
            )
            audit = audit_completion_attempt(self._workspace, record)
            if not audit.valid:
                raise CompletionAttemptError(
                    "new completion-attempt snapshot failed fresh audit: "
                    + ", ".join(audit.reasons)
                )
        except BaseException:
            # A failed capture must not occupy the next monotonic attempt path.
            destination.unlink(missing_ok=True)
            raise

        self._records.append(record)
        self._next_attempt_id += 1
        return record


def audit_completion_attempt(
    workspace: Path,
    value: CompletionAttemptRecord | Mapping[str, Any],
) -> CompletionAttemptAudit:
    """Freshly verify record fields, digest, path, permissions, and bytes."""

    try:
        record = (
            value
            if isinstance(value, CompletionAttemptRecord)
            else CompletionAttemptRecord.from_dict(value)
        )
        if isinstance(value, CompletionAttemptRecord):
            # Reparse the serialization so direct instances follow the same audit path.
            record = CompletionAttemptRecord.from_dict(value.to_dict())
        root = _validate_workspace(workspace)
        snapshot = _existing_path_inside_workspace(
            root,
            Path(*PurePosixPath(record.snapshot_path).parts),
            label="snapshot_path",
        )
        metadata = snapshot.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CompletionAttemptError("snapshot_path must be a regular non-symbolic file")
        if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise CompletionAttemptError("snapshot_path must have no writable mode bits")
        if metadata.st_nlink != 1:
            raise CompletionAttemptError("snapshot_path must have exactly one hard link")
        observed_sha256 = hashlib.sha256(
            _read_regular_non_symlink(snapshot, label="snapshot_path")
        ).hexdigest()
        if observed_sha256 != record.snapshot_sha256:
            raise CompletionAttemptError("snapshot bytes do not match snapshot_sha256")
        if observed_sha256 != record.artifact.sha256:
            raise CompletionAttemptError("snapshot bytes do not match artifact sha256")
    except (OSError, TypeError, ValueError) as exc:
        return CompletionAttemptAudit(
            valid=False,
            reasons=(f"completion_attempt_invalid:{type(exc).__name__}:{exc}",),
        )
    return CompletionAttemptAudit(valid=True, reasons=(), record=record)


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
        raise CompletionAttemptError(f"record is not canonical JSON data: {exc}") from exc
    return rendered.encode("ascii")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _validate_positive_integer(value: Any, *, label: str) -> None:
    if type(value) is not int or value < 1:
        raise CompletionAttemptError(f"{label} must be a positive integer")


def _validate_sha256(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CompletionAttemptError(f"{label} must be 64 lowercase hexadecimal characters")


def _validate_stage(value: Any) -> None:
    if not isinstance(value, str) or not _STAGE_PATTERN.fullmatch(value):
        raise CompletionAttemptError(
            "stage must be a portable non-empty label of at most 128 characters"
        )


def _validate_provider_id(value: Any, *, label: str, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        nullable = " or null" if allow_none else ""
        raise CompletionAttemptError(f"{label} must be a non-empty control-free string{nullable}")


def _validate_relative_posix_path(value: Any, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CompletionAttemptError(f"{label} must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "/".join(path.parts) != value
    ):
        raise CompletionAttemptError(f"{label} must be a normalized relative POSIX path")
    return path


def _snapshot_relative_path(attempt_id: int, suffix: str) -> str:
    if suffix not in _SUPPORTED_WORKBOOK_SUFFIXES:
        raise CompletionAttemptError("snapshot suffix must be .xlsx or .xlsm")
    return f"{COMPLETION_ATTEMPT_RELATIVE_DIR}/attempt-{attempt_id:06d}{suffix}"


def _validate_workspace(workspace: Path) -> Path:
    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    try:
        metadata = workspace.lstat()
    except OSError as exc:
        raise CompletionAttemptError("workspace cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CompletionAttemptError("workspace must be a non-symbolic directory")
    try:
        return workspace.resolve(strict=True)
    except OSError as exc:
        raise CompletionAttemptError("workspace cannot be resolved") from exc


def _path_from_workspace(root: Path, path: Path, *, label: str) -> tuple[Path, Path]:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a pathlib.Path")
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise CompletionAttemptError(f"{label} escapes the task workspace") from exc
    if not relative.parts:
        raise CompletionAttemptError(f"{label} must identify a file inside the workspace")
    current = root
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CompletionAttemptError(f"{label} path cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CompletionAttemptError(f"{label} path contains a symbolic link")
    return candidate, relative


def _existing_path_inside_workspace(root: Path, path: Path, *, label: str) -> Path:
    candidate, _ = _path_from_workspace(root, path, label=label)
    try:
        candidate.lstat()
    except OSError as exc:
        raise CompletionAttemptError(f"{label} does not exist or cannot be inspected") from exc
    return candidate


def _new_path_inside_workspace(root: Path, relative_value: str, *, label: str) -> Path:
    relative = _validate_relative_posix_path(relative_value, label=label)
    destination, _ = _path_from_workspace(
        root,
        Path(*relative.parts),
        label=label,
    )
    _ensure_directory(root, PurePosixPath(*relative.parts[:-1]))
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise CompletionAttemptError(f"{label} cannot be inspected") from exc
    raise CompletionAttemptError(f"{label} already exists; snapshots cannot be overwritten")


def _ensure_directory(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for component in relative.parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                current.chmod(0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise CompletionAttemptError(
                    "completion-attempt directory cannot be created"
                ) from exc
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise CompletionAttemptError(
                    "completion-attempt directory cannot be inspected"
                ) from exc
        except OSError as exc:
            raise CompletionAttemptError(
                "completion-attempt directory cannot be inspected"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CompletionAttemptError(
                "completion-attempt path contains a non-directory or symbolic link"
            )
    return current


def _read_regular_non_symlink(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CompletionAttemptError(f"{label} cannot be opened without following links") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CompletionAttemptError(f"{label} must be a regular non-symbolic file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_read_only_exclusive(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o400)
    except FileExistsError as exc:
        raise CompletionAttemptError(
            "snapshot_path already exists; snapshots cannot be overwritten"
        ) from exc
    except OSError as exc:
        raise CompletionAttemptError("snapshot_path cannot be created exclusively") from exc
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CompletionAttemptError("snapshot write did not make progress")
            view = view[written:]
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
