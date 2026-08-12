"""Deterministic, process-local pacing for Relay HTTP attempts."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

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
