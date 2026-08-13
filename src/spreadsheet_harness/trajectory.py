"""Append-only, secret-redacted trajectory recording."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import redact_sensitive_text

_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token)", re.I)
_DATA_URL = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)")


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
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            "payload": _sanitize(payload or {}, secrets=self._secrets),
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
