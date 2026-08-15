from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import spreadsheet_harness.trajectory as trajectory_module
from spreadsheet_harness.trajectory import TrajectoryRecorder, read_trajectory


def test_recorder_redacts_secrets_and_image_data(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    recorder.record(
        "event",
        {
            "api_key": "secret-value",
            "text": "Bearer cr_abcdefghijklmnopqrstuvwxyz123456",
            "image": "data:image/png;base64,aGVsbG8=",
        },
    )
    raw = path.read_text(encoding="utf-8")
    assert "secret-value" not in raw
    assert "cr_abcdefghijklmnopqrstuvwxyz123456" not in raw
    assert "aGVsbG8=" not in raw
    row = read_trajectory(path)[0]
    assert row["payload"]["api_key"] == "[REDACTED]"
    assert "IMAGE_DATA_URL" in row["payload"]["image"]


def test_recorder_redacts_configured_secret_recursively(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.jsonl"
    unusual_secret = "credential-with-an-unusual-shape"
    recorder = TrajectoryRecorder(
        path,
        "test-run",
        secrets=(unusual_secret,),
    )

    recorder.record(
        "tool.returned",
        {
            "result": {
                "stdout": f"visible {unusual_secret}",
                "nested": [{"message": unusual_secret}],
                f"field-{unusual_secret}": "key name",
            }
        },
    )

    raw = path.read_text(encoding="utf-8")
    assert unusual_secret not in raw
    assert raw.count("[REDACTED]") == 3


def test_recorder_redacts_non_string_leaves_and_keys(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.jsonl"
    unusual_secret = "credential-with-an-unusual-shape"

    class LeakingValue:
        def __repr__(self) -> str:
            return f"LeakingValue({unusual_secret})"

    recorder = TrajectoryRecorder(path, "test-run", secrets=(unusual_secret,))
    recorder.record(
        "event",
        {
            Path(f"field-{unusual_secret}"): [
                Path(f"/tmp/{unusual_secret}/artifact"),
                LeakingValue(),
            ]
        },
    )
    raw = path.read_text(encoding="utf-8")

    assert unusual_secret not in raw
    assert raw.count("[REDACTED]") == 3


def test_transaction_commits_exactly_one_record(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")

    with recorder.transaction() as transaction:
        transaction.record("observer.finalization_recorded", {"ok": True})
        transaction.commit()

    rows = read_trajectory(path)
    assert len(rows) == 1
    assert rows[0]["event"] == "observer.finalization_recorded"


def test_separate_recorders_cannot_overlap_transactions_for_one_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    first = TrajectoryRecorder(path, "test-run")
    second = TrajectoryRecorder(path, "test-run")

    with first.transaction() as transaction:
        transaction.record("observer.finalization_recorded", {"ok": True})
        transaction.commit()
        with pytest.raises(OSError, match="already active"):
            with second.transaction():
                pass

    assert [row["event"] for row in read_trajectory(path)] == ["observer.finalization_recorded"]
    with second.transaction() as transaction:
        transaction.commit_read_only()


def test_transaction_rolls_back_when_record_raises_after_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    recorder.record("existing", {"value": 1})
    before = path.read_bytes()
    original_record = trajectory_module._TrajectoryTransaction.record

    def append_then_fail(
        transaction: object,
        event: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        original_record(transaction, event, payload)
        raise OSError("injected post-append failure")

    monkeypatch.setattr(
        trajectory_module._TrajectoryTransaction,
        "record",
        append_then_fail,
    )

    with pytest.raises(OSError, match="post-append"):
        with recorder.transaction() as transaction:
            transaction.record("observer.finalization_recorded", {"ok": True})
            transaction.commit()

    assert path.read_bytes() == before


def test_transaction_rollback_retains_a_new_empty_trajectory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")

    with pytest.raises(RuntimeError, match="injected failure"):
        with recorder.transaction():
            raise RuntimeError("injected failure")

    assert path.read_bytes() == b""
    assert list(tmp_path.glob(".trajectory.jsonl.rollback-*")) == []


def test_transaction_does_not_claim_or_remove_a_competing_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    original_open = os.open
    competitor = b'{"competitor":true}\n'
    injected = False

    def create_before_exclusive_open(
        name: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if (
            name == path.name
            and flags & os.O_CREAT
            and flags & os.O_EXCL
            and dir_fd is not None
            and not injected
        ):
            injected = True
            competitor_descriptor = original_open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.write(competitor_descriptor, competitor)
            finally:
                os.close(competitor_descriptor)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(trajectory_module.os, "open", create_before_exclusive_open)

    with pytest.raises(RuntimeError, match="injected failure"):
        with recorder.transaction():
            raise RuntimeError("injected failure")

    assert injected
    assert path.read_bytes() == competitor


def test_transaction_empty_rollback_never_renames_or_unlinks_a_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")

    def reject_namespace_mutation(*_: object, **__: object) -> None:
        raise AssertionError("empty rollback must not mutate a pathname")

    monkeypatch.setattr(trajectory_module.os, "rename", reject_namespace_mutation)
    monkeypatch.setattr(trajectory_module.os, "replace", reject_namespace_mutation)
    monkeypatch.setattr(trajectory_module.os, "unlink", reject_namespace_mutation)

    with pytest.raises(RuntimeError, match="injected failure"):
        with recorder.transaction():
            raise RuntimeError("injected failure")

    assert path.read_bytes() == b""


def test_transaction_empty_rollback_creates_no_quarantine(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")

    with pytest.raises(RuntimeError, match="injected failure"):
        with recorder.transaction():
            raise RuntimeError("injected failure")

    assert path.read_bytes() == b""
    assert list(tmp_path.glob(".trajectory.jsonl.rollback-*")) == []


def test_transaction_enter_close_failure_releases_every_descriptor_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    path.write_bytes(b'{"incomplete":true}')
    recorder = TrajectoryRecorder(path, "test-run")
    transaction = trajectory_module._TrajectoryTransaction(recorder)
    original_close = os.close
    failed = False

    def close_then_fail_once(descriptor: int) -> None:
        nonlocal failed
        original_close(descriptor)
        if not failed:
            failed = True
            raise OSError("injected close failure")

    monkeypatch.setattr(trajectory_module.os, "close", close_then_fail_once)

    with pytest.raises(OSError, match="injected close failure"):
        transaction.__enter__()

    assert failed
    assert transaction._descriptor is None
    assert transaction._parent_descriptor is None
    acquired: list[bool] = []

    def acquire_from_other_thread() -> None:
        locked = recorder._lock.acquire(timeout=0.2)
        acquired.append(locked)
        if locked:
            recorder._lock.release()

    thread = threading.Thread(target=acquire_from_other_thread)
    thread.start()
    thread.join(timeout=1.0)
    assert acquired == [True]


def test_transaction_preserves_same_inode_append_after_durability_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    recorder.record("existing", {"value": 1})
    before = path.read_bytes()
    original_fsync = os.fsync
    injected = False

    def append_before_first_fsync(descriptor: int) -> None:
        nonlocal injected
        if not injected:
            injected = True
            with path.open("ab") as handle:
                handle.write(b'{"racing":true}\n')
        original_fsync(descriptor)

    monkeypatch.setattr(trajectory_module.os, "fsync", append_before_first_fsync)

    with pytest.raises(OSError, match="contents changed during a transaction"):
        with recorder.transaction() as transaction:
            transaction.record("observer.finalization_recorded", {"ok": True})
            transaction.commit()

    assert injected
    after = path.read_bytes()
    assert after.startswith(before)
    assert b'"event":"observer.finalization_recorded"' in after
    assert after.endswith(b'{"racing":true}\n')


def test_partial_write_failure_never_truncates_a_racing_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    recorder.record("existing", {"value": 1})
    before = path.read_bytes()
    competitor = b'{"racing":"must-survive"}\n'
    original_write = os.write
    calls = 0

    def partial_then_compete(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            written = original_write(descriptor, data[:8])
            with path.open("ab") as handle:
                handle.write(competitor)
            return written
        raise OSError("injected second trajectory write failure")

    def reject_truncate(*_: object, **__: object) -> None:
        raise AssertionError("failed trajectory appends must never truncate shared data")

    monkeypatch.setattr(trajectory_module.os, "write", partial_then_compete)
    monkeypatch.setattr(trajectory_module.os, "ftruncate", reject_truncate)

    with pytest.raises(OSError, match="second trajectory write failure"):
        with recorder.transaction() as transaction:
            transaction.record("observer.finalization_recorded", {"ok": True})
            transaction.commit()

    after = path.read_bytes()
    assert after.startswith(before)
    assert after[len(before) : len(before) + 8]
    assert after.endswith(competitor)


def test_transaction_rejects_same_length_append_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    recorder.record("existing", {"value": 1})
    before = path.read_bytes()
    original_write = os.write
    corrupted = False

    def corrupt_first_write(descriptor: int, data: bytes) -> int:
        nonlocal corrupted
        if not corrupted and data:
            corrupted = True
            data = bytes([data[0] ^ 1]) + data[1:]
        return original_write(descriptor, data)

    monkeypatch.setattr(trajectory_module.os, "write", corrupt_first_write)

    with pytest.raises(OSError, match="not byte-identical"):
        with recorder.transaction() as transaction:
            transaction.record("observer.finalization_recorded", {"ok": True})
            transaction.commit()

    assert corrupted
    assert path.read_bytes().startswith(before)
    assert len(path.read_bytes()) > len(before)


def test_event_counts_reassembles_a_short_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(path, "test-run")
    payload = {"ok": True}
    recorder.record("observer.finalization_recorded", payload)
    original_pread = os.pread
    shortened = False

    def short_once(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal shortened
        if not shortened and offset == 0 and size > 1:
            shortened = True
            return original_pread(descriptor, max(1, size // 2), offset)
        return original_pread(descriptor, size, offset)

    monkeypatch.setattr(trajectory_module.os, "pread", short_once)

    with recorder.transaction() as transaction:
        assert transaction.event_counts("observer.finalization_recorded", payload) == (1, 1)
        transaction.commit_read_only()

    assert shortened
