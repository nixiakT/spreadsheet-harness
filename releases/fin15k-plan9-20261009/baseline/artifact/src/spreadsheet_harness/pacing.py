"""Deterministic, process-local pacing for Relay HTTP attempts."""

from __future__ import annotations

import math
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

from .errors import AgentTimeoutError

PACING_POLICY = "process_local_min_attempt_start_interval_v1"


class RelayPacer:
    """Enforce a minimum start-to-start interval across shared clients.

    The pacer is thread-safe but intentionally process-local. Comparison runners
    share one instance across every client, stage, arm, and task in that process.
    """

    def __init__(
        self,
        interval_seconds: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if isinstance(interval_seconds, bool) or not isinstance(
            interval_seconds, int | float
        ):
            raise ValueError("request interval must be a non-negative finite number")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("request interval must be a non-negative finite number")
        self.interval_seconds = interval
        self._clock = clock
        self._sleep = sleep
        self._utcnow = utcnow
        self._last_started_at: float | None = None
        self._sequence = 0
        self._lock = threading.Lock()

    def acquire(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Wait for and atomically admit one physical HTTP attempt."""

        with self._lock:
            now = self._clock()
            previous = self._last_started_at
            eligible_at = (
                now
                if previous is None
                else max(now, previous + self.interval_seconds)
            )
            requested_wait = max(eligible_at - now, 0.0)
            if deadline is not None and eligible_at >= deadline:
                raise AgentTimeoutError(
                    "Task deadline would expire before the next paced Relay request"
                )
            wait_started = self._clock()
            if requested_wait > 0:
                self._sleep(requested_wait)
            admitted_at = self._clock()
            if deadline is not None and admitted_at >= deadline:
                raise AgentTimeoutError(
                    "Task deadline expired while pacing the next Relay request"
                )
            self._sequence += 1
            self._last_started_at = admitted_at
            return {
                "policy": PACING_POLICY,
                "scope": "process",
                "sequence": self._sequence,
                "interval_seconds": self.interval_seconds,
                "wait_requested_seconds": round(requested_wait, 3),
                "wait_seconds": round(max(admitted_at - wait_started, 0.0), 3),
                "previous_start_delta_seconds": (
                    round(admitted_at - previous, 3)
                    if previous is not None
                    else None
                ),
                "admitted_at": self._utcnow().isoformat(),
                "deadline_remaining_seconds": (
                    round(max(deadline - admitted_at, 0.0), 3)
                    if deadline is not None
                    else None
                ),
            }


class GlobalRelayLimiter:
    """Cross-process rolling-window request/token limiter backed by flock."""

    def __init__(self, path: str, requests_per_minute: int, tokens_per_minute: int) -> None:
        self.path = Path(path)
        self.requests_per_minute = int(requests_per_minute)
        self.tokens_per_minute = int(tokens_per_minute)
        self.interval_seconds = 1.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)

    def acquire(self, *, deadline: float | None = None, estimated_tokens: int = 1) -> dict[str, Any]:
        estimate = max(1, min(int(estimated_tokens), self.tokens_per_minute))
        started = time.monotonic()
        while True:
            now = time.time()
            with self.path.open("r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    try:
                        state = json.load(handle)
                    except Exception:
                        state = {"events": []}
                    events = [e for e in state.get("events", []) if now - float(e.get("at", 0)) < 60]
                    request_count = len(events)
                    token_count = sum(int(e.get("tokens", 0)) for e in events)
                    if request_count < self.requests_per_minute and token_count + estimate <= self.tokens_per_minute:
                        admission_id = uuid.uuid4().hex
                        events.append({"id": admission_id, "at": now, "tokens": estimate})
                        handle.seek(0); handle.truncate(); json.dump({"events": events}, handle); handle.flush(); os.fsync(handle.fileno())
                        return {
                            "policy": "cross_process_rolling_window_v1",
                            "scope": "global",
                            "admission_id": admission_id,
                            "estimated_tokens": estimate,
                            "requests_per_minute": self.requests_per_minute,
                            "tokens_per_minute": self.tokens_per_minute,
                            "wait_seconds": round(time.monotonic() - started, 3),
                        }
                    waits = [60 - (now - float(e["at"])) for e in events]
                    wait = max(0.25, min(waits) if waits else 0.25)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise AgentTimeoutError("Task deadline would expire before global provider quota is available")
            time.sleep(min(wait, 5.0))

    def record_usage(self, pacing: dict[str, Any], total_tokens: int) -> None:
        admission_id = pacing.get("admission_id")
        if not admission_id:
            return
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                try: state = json.load(handle)
                except Exception: state = {"events": []}
                for event in state.get("events", []):
                    if event.get("id") == admission_id:
                        event["tokens"] = max(0, int(total_tokens)); break
                handle.seek(0); handle.truncate(); json.dump(state, handle); handle.flush(); os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def relay_pacer(interval_seconds: float) -> RelayPacer | GlobalRelayLimiter:
    path = os.environ.get("SHEET_AGENT_GLOBAL_LIMITER_FILE")
    if not path:
        return RelayPacer(interval_seconds)
    return GlobalRelayLimiter(
        path,
        int(os.environ.get("SHEET_AGENT_GLOBAL_REQUESTS_PER_MINUTE", "15")),
        int(os.environ.get("SHEET_AGENT_GLOBAL_TOKENS_PER_MINUTE", "60000")),
    )
