"""Append-only, secret-redacted trajectory recording."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import redact_sensitive_text

_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token)", re.I)
_DATA_URL = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)")


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _pread_exact(descriptor: int, size: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    while consumed < size:
        chunk = os.pread(descriptor, size - consumed, offset + consumed)
        if not chunk:
            raise OSError("Trajectory became shorter during a descriptor read")
        chunks.append(chunk)
        consumed += len(chunk)
    return b"".join(chunks)


def _sanitize(
    value: Any,
    key: str | None = None,
    *,
    secrets: tuple[str, ...] = (),
) -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            string_key = str(raw_key)
            safe_key = redact_sensitive_text(string_key, secrets=secrets)
            result[safe_key] = _sanitize(item, string_key, secrets=secrets)
        return result
    if isinstance(value, list | tuple):
        return [_sanitize(item, secrets=secrets) for item in value]
    if isinstance(value, Path):
        return _sanitize(str(value), secrets=secrets)
    if isinstance(value, str):
        value = redact_sensitive_text(value, secrets=secrets)

        def replace_image(match: re.Match[str]) -> str:
            encoded = match.group(1)
            digest = hashlib.sha256(encoded.encode("ascii", "ignore")).hexdigest()[:12]
            return f"[IMAGE_DATA_URL sha256={digest} encoded_chars={len(encoded)}]"

        return _DATA_URL.sub(replace_image, value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _sanitize(repr(value), secrets=secrets)


class TrajectoryRecorder:
    """Write one JSON object per line so interrupted runs remain inspectable."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.path = path
        self.run_id = run_id
        self._secrets = tuple(secret for secret in secrets if secret)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
        with self.transaction() as transaction:
            transaction.record(event, payload)
            transaction.commit()

    def _encoded_record(self, event: str, payload: dict[str, Any] | None = None) -> str:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            "payload": _sanitize(payload or {}, secrets=self._secrets),
        }
        return json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"

    def transaction(self) -> _TrajectoryTransaction:
        """Hold the recorder lock and roll back an uncommitted append."""

        return _TrajectoryTransaction(self)


class _TrajectoryTransaction:
    def __init__(self, recorder: TrajectoryRecorder) -> None:
        self._recorder = recorder
        self._descriptor: int | None = None
        self._parent_descriptor: int | None = None
        self._parent_identity: tuple[int, int] | None = None
        self._identity: tuple[int, int] | None = None
        self._expected_state: tuple[int, int, int, int, int, int, int] | None = None
        self._start_size = 0
        self._encoded_record_bytes = b""
        self._written_size = 0
        self._recorded = False
        self._committed = False
        self._durable = False
        self._file_lock_acquired = False

    @property
    def durable(self) -> bool:
        return self._durable

    def __enter__(self) -> _TrajectoryTransaction:
        self._recorder._lock.acquire()
        created = False
        try:
            path = self._recorder.path
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            parent_descriptor = os.open(path.parent, directory_flags)
            self._parent_descriptor = parent_descriptor
            parent_metadata = os.fstat(parent_descriptor)
            path_parent_metadata = path.parent.lstat()
            if not stat.S_ISDIR(parent_metadata.st_mode) or (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            ) != (path_parent_metadata.st_dev, path_parent_metadata.st_ino):
                raise OSError("Trajectory parent changed identity")
            self._parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
            common_flags = (
                os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor: int | None = None
            before: os.stat_result | None = None
            for _ in range(16):
                try:
                    before = os.stat(
                        path.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    try:
                        descriptor = os.open(
                            path.name,
                            common_flags | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=parent_descriptor,
                        )
                    except FileExistsError:
                        continue
                    created = True
                    break
                if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                    raise OSError("Trajectory path must be a regular non-symbolic file")
                try:
                    descriptor = os.open(
                        path.name,
                        common_flags,
                        dir_fd=parent_descriptor,
                    )
                except FileNotFoundError:
                    continue
                break
            if descriptor is None:
                raise OSError("Trajectory path did not stabilize while opening")
            self._descriptor = descriptor
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("Trajectory path must be a regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OSError("Trajectory transaction is already active for this file") from exc
            self._file_lock_acquired = True
            metadata = os.fstat(descriptor)
            locked_path_metadata = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                (metadata.st_dev, metadata.st_ino)
                != (locked_path_metadata.st_dev, locked_path_metadata.st_ino)
                or metadata.st_nlink != 1
                or locked_path_metadata.st_nlink != 1
                or (
                    not created
                    and before is not None
                    and (before.st_dev != metadata.st_dev or before.st_ino != metadata.st_ino)
                )
            ):
                raise OSError("Trajectory identity changed while opening a transaction")
            self._identity = (metadata.st_dev, metadata.st_ino)
            self._expected_state = _file_state(metadata)
            self._start_size = metadata.st_size
            if self._start_size and os.pread(descriptor, 1, self._start_size - 1) != b"\n":
                raise OSError("Trajectory ends with an incomplete record")
            return self
        except BaseException as exc:
            close_error: OSError | None = None
            for attribute in ("_descriptor", "_parent_descriptor"):
                owned_descriptor = getattr(self, attribute)
                setattr(self, attribute, None)
                if owned_descriptor is None:
                    continue
                try:
                    os.close(owned_descriptor)
                except OSError as close_exc:
                    close_error = close_error or close_exc
            self._recorder._lock.release()
            if close_error is not None:
                raise close_error from exc
            raise

    def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
        if self._descriptor is None or self._recorded:
            raise RuntimeError("Trajectory transaction accepts exactly one record")
        self._recorded = True
        encoded = self._recorder._encoded_record(event, payload).encode("utf-8")
        self._encoded_record_bytes = encoded

    def _append_record(self) -> None:
        assert self._descriptor is not None
        encoded = self._encoded_record_bytes
        offset = 0
        while offset < len(encoded):
            written = os.write(self._descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("Trajectory append made no progress")
            offset += written
            self._written_size += written
        if _pread_exact(self._descriptor, len(encoded), self._start_size) != encoded:
            raise OSError("Trajectory append is not byte-identical to the encoded record")
        metadata = os.fstat(self._descriptor)
        if (
            self._identity != (metadata.st_dev, metadata.st_ino)
            or metadata.st_nlink != 1
            or metadata.st_size != self._start_size + len(encoded)
        ):
            raise OSError("Trajectory changed while appending a transaction record")
        self._expected_state = _file_state(metadata)

    def commit(self) -> None:
        if self._descriptor is None or not self._recorded:
            raise RuntimeError("Trajectory transaction cannot commit without a record")
        self._committed = True

    def commit_read_only(self) -> None:
        if self._descriptor is None or self._recorded:
            raise RuntimeError("Read-only trajectory commit cannot follow a record")
        self._committed = True

    def event_counts(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        if self._descriptor is None or self._recorded:
            raise RuntimeError("Trajectory events can only be inspected before recording")
        raw = _pread_exact(self._descriptor, self._start_size, 0)
        expected_payload = _sanitize(payload or {}, secrets=self._recorder._secrets)
        total = 0
        matching = 0
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OSError("Trajectory contains an invalid record") from exc
            if not isinstance(row, dict) or row.get("event") != event:
                continue
            total += 1
            if (
                row.get("run_id") == self._recorder.run_id
                and row.get("payload") == expected_payload
            ):
                matching += 1
        return total, matching

    def _validate_identity(self, *, require_expected_state: bool = False) -> None:
        assert self._descriptor is not None
        assert self._parent_descriptor is not None
        assert self._parent_identity is not None
        assert self._identity is not None
        descriptor_metadata = os.fstat(self._descriptor)
        path_metadata = os.stat(
            self._recorder.path.name,
            dir_fd=self._parent_descriptor,
            follow_symlinks=False,
        )
        path_parent_metadata = self._recorder.path.parent.lstat()
        if (
            (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != self._identity
            or (path_metadata.st_dev, path_metadata.st_ino) != self._identity
            or (path_parent_metadata.st_dev, path_parent_metadata.st_ino) != self._parent_identity
            or not stat.S_ISREG(path_metadata.st_mode)
            or descriptor_metadata.st_nlink != 1
            or path_metadata.st_nlink != 1
        ):
            raise OSError("Trajectory identity changed during a transaction")
        if require_expected_state and (
            self._expected_state is None
            or _file_state(descriptor_metadata) != self._expected_state
            or _file_state(path_metadata) != self._expected_state
        ):
            raise OSError("Trajectory contents changed during a transaction")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        descriptor = self._descriptor
        try:
            if descriptor is None:
                return False
            if exc_type is None and self._committed:
                self._validate_identity(require_expected_state=True)
                if self._recorded:
                    self._append_record()
                os.fsync(descriptor)
                assert self._parent_descriptor is not None
                os.fsync(self._parent_descriptor)
                self._validate_identity(require_expected_state=True)
                self._durable = True
            return False
        finally:
            close_error: OSError | None = None
            for attribute in ("_descriptor", "_parent_descriptor"):
                owned_descriptor = getattr(self, attribute)
                setattr(self, attribute, None)
                if owned_descriptor is None:
                    continue
                try:
                    os.close(owned_descriptor)
                except OSError as close_exc:
                    close_error = close_error or close_exc
            self._recorder._lock.release()
            if close_error is not None:
                raise close_error


def read_trajectory(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid trajectory JSON on line {number}: {path}") from exc
    return rows
