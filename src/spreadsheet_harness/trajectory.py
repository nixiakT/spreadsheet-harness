"""Append-only, secret-redacted trajectory recording."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token)", re.I)
_SECRET_VALUE = re.compile(r"\b(?:cr|sk)-[A-Za-z0-9_-]{12,}\b|\bcr_[A-Za-z0-9_-]{12,}\b")
_DATA_URL = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)")


def _sanitize(value: Any, key: str | None = None) -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        value = _SECRET_VALUE.sub("[REDACTED]", value)

        def replace_image(match: re.Match[str]) -> str:
            encoded = match.group(1)
            digest = hashlib.sha256(encoded.encode("ascii", "ignore")).hexdigest()[:12]
            return f"[IMAGE_DATA_URL sha256={digest} encoded_chars={len(encoded)}]"

        return _DATA_URL.sub(replace_image, value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return repr(value)


class TrajectoryRecorder:
    """Write one JSON object per line so interrupted runs remain inspectable."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            "payload": _sanitize(payload or {}),
        }
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()


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
