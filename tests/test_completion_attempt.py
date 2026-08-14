from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from spreadsheet_harness.completion_attempt import (
    COMPLETION_ATTEMPT_SCHEMA_VERSION,
    CompletionAttemptAudit,
    CompletionAttemptError,
    CompletionAttemptLedger,
    CompletionAttemptRecord,
    audit_completion_attempt,
)
from spreadsheet_harness.evidence_contract import ArtifactRef


def _workbook(tmp_path: Path, content: bytes = b"exact-xlsx-package") -> tuple[Path, ArtifactRef]:
    path = tmp_path / "output.xlsx"
    path.write_bytes(content)
    return path, ArtifactRef(0, hashlib.sha256(content).hexdigest())


def test_capture_creates_read_only_regular_snapshot_and_canonical_record(
    tmp_path: Path,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    ledger = CompletionAttemptLedger(tmp_path)

    record = ledger.capture(
        workbook,
        artifact,
        stage="edit",
        turn=3,
        response_id="response-3",
        call_id="call-3",
    )
    snapshot = tmp_path / record.snapshot_path

    assert record.attempt_id == 1
    assert record.artifact == artifact
    assert record.snapshot_sha256 == artifact.sha256
    assert record.response_id == "response-3"
    assert record.call_id == "call-3"
    assert snapshot.read_bytes() == workbook.read_bytes()
    assert snapshot.is_file() and not snapshot.is_symlink()
    assert snapshot.stat().st_mode & 0o222 == 0
    assert ledger.records == (record,)
    assert ledger.next_attempt_id == 2
    document = json.loads(record.canonical_json())
    assert document == record.to_dict()
    assert document["schema_version"] == COMPLETION_ATTEMPT_SCHEMA_VERSION
    assert document["assurance"] == {
        "scope": "snapshot-consistency-only",
        "contains_evaluator_outcome": False,
        "is_digital_signature": False,
        "proves_task_correctness": False,
    }
    assert "outcome" not in document
    assert audit_completion_attempt(tmp_path, record).valid is True


def test_attempt_ids_are_monotonic_across_hash_cycle_revisions(tmp_path: Path) -> None:
    workbook, revision_zero = _workbook(tmp_path)
    ledger = CompletionAttemptLedger(tmp_path)
    first = ledger.capture(
        workbook,
        revision_zero,
        stage="edit",
        turn=1,
        response_id="response-1",
        call_id="call-1",
    )
    workbook.write_bytes(b"intermediate-revision")
    workbook.write_bytes(b"exact-xlsx-package")
    revision_two = ArtifactRef(2, revision_zero.sha256)

    second = ledger.capture(
        workbook,
        revision_two,
        stage="edit",
        turn=5,
        response_id="response-5",
        call_id="call-5",
    )

    assert (first.attempt_id, second.attempt_id) == (1, 2)
    assert first.artifact.revision == 0
    assert second.artifact.revision == 2
    assert first.artifact.sha256 == second.artifact.sha256
    assert first.snapshot_path != second.snapshot_path
    assert first.record_sha256 != second.record_sha256
    assert all(audit_completion_attempt(tmp_path, item).valid for item in ledger.records)


def test_capture_preserves_macro_enabled_workbook_suffix(tmp_path: Path) -> None:
    workbook = tmp_path / "output.xlsm"
    workbook_bytes = b"exact-macro-enabled-package"
    workbook.write_bytes(workbook_bytes)
    artifact = ArtifactRef(7, hashlib.sha256(workbook_bytes).hexdigest())

    record = CompletionAttemptLedger(tmp_path).capture(
        workbook,
        artifact,
        stage="solve",
        turn=2,
        response_id="response-xlsm",
        call_id="call-xlsm",
    )

    assert record.snapshot_path == "completion-attempts/attempt-000001.xlsm"
    assert (tmp_path / record.snapshot_path).read_bytes() == workbook_bytes
    assert audit_completion_attempt(tmp_path, record.to_dict()).valid is True


def test_capture_rejects_artifact_sha_mismatch_without_advancing_ledger(
    tmp_path: Path,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    mismatched = ArtifactRef(artifact.revision, "f" * 64)
    ledger = CompletionAttemptLedger(tmp_path)

    with pytest.raises(CompletionAttemptError, match="supplied ArtifactRef"):
        ledger.capture(
            workbook,
            mismatched,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )

    assert ledger.records == ()
    assert ledger.next_attempt_id == 1
    assert not (tmp_path / "completion-attempts" / "attempt-000001.xlsx").exists()


def test_failed_postwrite_audit_removes_partial_attempt_without_advancing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    ledger = CompletionAttemptLedger(tmp_path)

    monkeypatch.setattr(
        "spreadsheet_harness.completion_attempt.audit_completion_attempt",
        lambda *_: CompletionAttemptAudit(False, ("injected-audit-failure",)),
    )

    with pytest.raises(CompletionAttemptError, match="failed fresh audit"):
        ledger.capture(
            workbook,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )

    assert ledger.records == ()
    assert ledger.next_attempt_id == 1
    assert not (tmp_path / "completion-attempts" / "attempt-000001.xlsx").exists()


def test_capture_rejects_source_path_escape_and_symlink(tmp_path: Path) -> None:
    workbook, artifact = _workbook(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.xlsx"
    outside.write_bytes(workbook.read_bytes())
    ledger = CompletionAttemptLedger(tmp_path)

    with pytest.raises(CompletionAttemptError, match="escapes"):
        ledger.capture(
            outside,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )

    link = tmp_path / "linked-output.xlsx"
    try:
        link.symlink_to(workbook.name)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(CompletionAttemptError, match="symbolic link"):
        ledger.capture(
            link,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )


def test_capture_rejects_internal_snapshot_directory_symlink(tmp_path: Path) -> None:
    workbook, artifact = _workbook(tmp_path)
    ledger = CompletionAttemptLedger(tmp_path)
    attempt_dir = tmp_path / "completion-attempts"
    attempt_dir.rmdir()
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    try:
        attempt_dir.symlink_to(alternate.name, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(CompletionAttemptError, match="symbolic link"):
        ledger.capture(
            workbook,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )

    assert list(alternate.iterdir()) == []
    assert ledger.records == ()


def test_capture_never_overwrites_existing_attempt_path(tmp_path: Path) -> None:
    workbook, artifact = _workbook(tmp_path)
    ledger = CompletionAttemptLedger(tmp_path)
    occupied = tmp_path / "completion-attempts" / "attempt-000001.xlsx"
    occupied.write_bytes(b"preexisting")

    with pytest.raises(CompletionAttemptError, match="cannot be overwritten"):
        ledger.capture(
            workbook,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )

    assert occupied.read_bytes() == b"preexisting"
    assert ledger.records == ()
    assert ledger.next_attempt_id == 1


def test_fresh_audit_is_recorder_and_ledger_independent(tmp_path: Path) -> None:
    workbook, artifact = _workbook(tmp_path)
    record_document = (
        CompletionAttemptLedger(tmp_path)
        .capture(
            workbook,
            artifact,
            stage="verification",
            turn=4,
            response_id="response-4",
            call_id="call-4",
        )
        .to_dict()
    )
    serialized = json.dumps(record_document, sort_keys=True)

    # A fresh process only needs the task workspace and serialized record.
    audit = audit_completion_attempt(tmp_path, json.loads(serialized))

    assert audit.valid is True
    assert audit.record == CompletionAttemptRecord.from_dict(record_document)


@pytest.mark.parametrize(
    "field,replacement",
    [("turn", 9), ("stage", "final"), ("call_id", "different-call")],
)
def test_fresh_audit_rejects_canonical_field_tampering(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    document = (
        CompletionAttemptLedger(tmp_path)
        .capture(
            workbook,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )
        .to_dict()
    )
    document[field] = replacement

    audit = audit_completion_attempt(tmp_path, document)

    assert audit.valid is False
    assert "digest mismatch" in audit.reasons[0]


def test_fresh_audit_rejects_writable_snapshot_even_when_bytes_match(
    tmp_path: Path,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    record = CompletionAttemptLedger(tmp_path).capture(
        workbook,
        artifact,
        stage="edit",
        turn=1,
        response_id="response-1",
        call_id="call-1",
    )
    snapshot = tmp_path / record.snapshot_path
    snapshot.chmod(0o600)

    audit = audit_completion_attempt(tmp_path, record.to_dict())

    assert audit.valid is False
    assert "writable mode bits" in audit.reasons[0]


def test_fresh_audit_rejects_changed_snapshot_bytes(tmp_path: Path) -> None:
    workbook, artifact = _workbook(tmp_path)
    record = CompletionAttemptLedger(tmp_path).capture(
        workbook,
        artifact,
        stage="edit",
        turn=1,
        response_id="response-1",
        call_id="call-1",
    )
    snapshot = tmp_path / record.snapshot_path
    snapshot.chmod(0o600)
    snapshot.write_bytes(b"tampered")
    snapshot.chmod(0o444)

    audit = audit_completion_attempt(tmp_path, record)

    assert audit.valid is False
    assert "snapshot bytes" in audit.reasons[0]


def test_fresh_audit_rejects_snapshot_replaced_by_internal_symlink(
    tmp_path: Path,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    record = CompletionAttemptLedger(tmp_path).capture(
        workbook,
        artifact,
        stage="edit",
        turn=1,
        response_id="response-1",
        call_id="call-1",
    )
    snapshot = tmp_path / record.snapshot_path
    relocated = snapshot.with_name("relocated.xlsx")
    snapshot.replace(relocated)
    try:
        snapshot.symlink_to(relocated.name)
    except OSError:
        relocated.replace(snapshot)
        pytest.skip("symlinks are unavailable")

    audit = audit_completion_attempt(tmp_path, record.to_dict())

    assert audit.valid is False
    assert "symbolic link" in audit.reasons[0]


def test_direct_record_rejects_non_numeric_revision_and_noncanonical_path() -> None:
    with pytest.raises(ValueError, match="revision"):
        ArtifactRef(True, "a" * 64)
    with pytest.raises(CompletionAttemptError, match="normalized relative POSIX path"):
        CompletionAttemptRecord(
            attempt_id=1,
            artifact=ArtifactRef(0, "a" * 64),
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
            snapshot_path="completion-attempts/../escape.xlsx",
            snapshot_sha256="a" * 64,
        )


def test_snapshot_has_single_link_and_mode_is_not_affected_by_umask(
    tmp_path: Path,
) -> None:
    workbook, artifact = _workbook(tmp_path)
    previous_umask = os.umask(0o777)
    try:
        record = CompletionAttemptLedger(tmp_path).capture(
            workbook,
            artifact,
            stage="edit",
            turn=1,
            response_id="response-1",
            call_id="call-1",
        )
    finally:
        os.umask(previous_umask)

    metadata = (tmp_path / record.snapshot_path).stat()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o777 == 0o444
    assert audit_completion_attempt(tmp_path, record).valid is True
