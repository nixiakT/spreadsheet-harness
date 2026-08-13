"""Thread-safe model-call budgets shared across agent stages."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from typing import Any

from .errors import AgentBudgetError


def _optional_non_negative_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")
    return value


def _optional_non_negative_float(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a non-negative finite number or None")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError(f"{name} must be a non-negative finite number or None")
    return converted


def _usage_total_tokens(usage: Mapping[str, Any]) -> int:
    raw_total = usage.get("total_tokens")
    if raw_total is None:
        raw_total = int(usage.get("input_tokens", 0) or 0) + int(
            usage.get("output_tokens", 0) or 0
        )
    try:
        tokens = int(raw_total or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider usage total_tokens must be an integer") from exc
    if tokens < 0:
        raise ValueError("provider usage total_tokens must not be negative")
    return tokens


class RunBudget:
    """A process-local budget that can be shared by multiple agent stages.

    Calls are reserved before a request so concurrent stages cannot race past the
    call limit. A reservation is committed only after a provider response, at
    which point both the completed call and its provider-reported token usage are
    recorded atomically.
    """

    def __init__(
        self,
        max_model_calls: int | None = None,
        max_total_tokens: int | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        self.max_model_calls = _optional_non_negative_int(
            max_model_calls, "max_model_calls"
        )
        self.max_total_tokens = _optional_non_negative_int(
            max_total_tokens, "max_total_tokens"
        )
        self.max_elapsed_seconds = _optional_non_negative_float(
            max_elapsed_seconds, "max_elapsed_seconds"
        )
        self._started_at = time.monotonic()
        self._model_calls = 0
        self._total_tokens = 0
        self._next_reservation = 1
        self._reservations: dict[int, str | None] = {}
        self._termination: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @property
    def deadline(self) -> float | None:
        if self.max_elapsed_seconds is None:
            return None
        return self._started_at + self.max_elapsed_seconds

    def _used_locked(self, now: float) -> dict[str, int | float]:
        return {
            "model_calls": self._model_calls,
            "total_tokens": self._total_tokens,
            "elapsed_seconds": max(now - self._started_at, 0.0),
        }

    def _to_dict_locked(self, now: float) -> dict[str, Any]:
        return {
            "limit": {
                "model_calls": self.max_model_calls,
                "total_tokens": self.max_total_tokens,
                "elapsed_seconds": self.max_elapsed_seconds,
            },
            "used": self._used_locked(now),
            "termination": dict(self._termination) if self._termination is not None else None,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a consistent snapshot suitable for result JSON."""

        with self._lock:
            return self._to_dict_locked(time.monotonic())

    def remaining_model_calls(self) -> int | None:
        """Return unreserved response slots, or ``None`` for an unlimited budget."""

        with self._lock:
            if self.max_model_calls is None:
                return None
            return max(
                self.max_model_calls - self._model_calls - len(self._reservations),
                0,
            )

    def _terminate_locked(
        self,
        reason: str,
        message: str,
        *,
        stage: str | None,
        now: float,
    ) -> None:
        if self._termination is None:
            self._termination = {
                "reason": reason,
                "message": message,
                "stage": stage,
                "elapsed_seconds": max(now - self._started_at, 0.0),
            }
        raise AgentBudgetError(
            message,
            reason=str(self._termination["reason"]),
            budget=self._to_dict_locked(now),
        )

    def _raise_if_terminated_locked(self, now: float) -> None:
        if self._termination is None:
            return
        raise AgentBudgetError(
            str(self._termination["message"]),
            reason=str(self._termination["reason"]),
            budget=self._to_dict_locked(now),
        )

    def _check_wall_locked(self, now: float, *, stage: str | None) -> None:
        self._raise_if_terminated_locked(now)
        if (
            self.max_elapsed_seconds is not None
            and now - self._started_at >= self.max_elapsed_seconds
        ):
            self._terminate_locked(
                "max_elapsed_seconds",
                f"Run budget exhausted its {self.max_elapsed_seconds:g}-second elapsed-time limit",
                stage=stage,
                now=now,
            )

    def ensure_within_time(self, *, stage: str | None = None) -> None:
        """Raise if the shared wall-clock limit has expired."""

        with self._lock:
            self._check_wall_locked(time.monotonic(), stage=stage)

    def begin_model_call(self, *, stage: str | None = None) -> int:
        """Check wall/call limits and reserve one provider response slot."""

        with self._lock:
            now = time.monotonic()
            self._check_wall_locked(now, stage=stage)
            if (
                self.max_total_tokens is not None
                and self._total_tokens >= self.max_total_tokens
            ):
                self._terminate_locked(
                    "max_total_tokens",
                    (
                        "Run budget exhausted its "
                        f"{self.max_total_tokens} total-token limit"
                    ),
                    stage=stage,
                    now=now,
                )
            if self.max_model_calls is not None and (
                self._model_calls + len(self._reservations) >= self.max_model_calls
            ):
                self._terminate_locked(
                    "max_model_calls",
                    f"Run budget exhausted its {self.max_model_calls} model-call limit",
                    stage=stage,
                    now=now,
                )
            reservation = self._next_reservation
            self._next_reservation += 1
            self._reservations[reservation] = stage
            return reservation

    def cancel_model_call(self, reservation: int) -> None:
        """Release a reservation when no provider response was received."""

        with self._lock:
            self._reservations.pop(reservation, None)

    def record_response(
        self,
        reservation: int,
        usage: Mapping[str, Any],
        *,
        stage: str | None = None,
    ) -> dict[str, Any]:
        """Atomically commit a call and provider token usage.

        The response is always accounted before an over-token or elapsed-time
        error is raised, so diagnostics reflect the actual cost incurred.
        """

        tokens = _usage_total_tokens(usage)
        with self._lock:
            if reservation not in self._reservations:
                raise RuntimeError("unknown or already completed model-call reservation")
            reserved_stage = self._reservations.pop(reservation)
            effective_stage = stage if stage is not None else reserved_stage
            self._model_calls += 1
            self._total_tokens += tokens
            now = time.monotonic()
            if self._termination is not None:
                self._raise_if_terminated_locked(now)
            if (
                self.max_total_tokens is not None
                and self._total_tokens > self.max_total_tokens
            ):
                self._terminate_locked(
                    "max_total_tokens",
                    (
                        "Run budget exceeded its "
                        f"{self.max_total_tokens} total-token limit "
                        f"with {self._total_tokens} provider-reported tokens"
                    ),
                    stage=effective_stage,
                    now=now,
                )
            if (
                self.max_elapsed_seconds is not None
                and now - self._started_at >= self.max_elapsed_seconds
            ):
                self._terminate_locked(
                    "max_elapsed_seconds",
                    (
                        "Run budget exhausted its "
                        f"{self.max_elapsed_seconds:g}-second elapsed-time limit"
                    ),
                    stage=effective_stage,
                    now=now,
                )
            return self._to_dict_locked(now)
