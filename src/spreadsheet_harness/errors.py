"""Domain-specific exceptions."""

import re

_SECRET_VALUE = re.compile(
    r"\b(?:cr|sk)[-_][A-Za-z0-9_-]{12,}\b"
    r"|\b(?:ghp|github_pat|xoxb|xoxp|xoxa|xoxr)-?[A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)


def redact_sensitive_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Remove configured and common token-shaped secrets from persisted diagnostics."""

    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return _SECRET_VALUE.sub("[REDACTED]", value)


class HarnessError(RuntimeError):
    """Base exception for expected harness failures."""


class WorkbookValidationError(HarnessError):
    """Raised when a workbook mutation leaves an unreadable workbook."""


class ToolInputError(HarnessError):
    """Raised when a model supplies invalid tool arguments."""


class CodeIsolationError(HarnessError):
    """Raised when a required code-execution sandbox is unavailable or did not start."""


class RenderError(HarnessError):
    """Raised when LibreOffice or PDF rasterization fails."""


class ProviderError(HarnessError):
    """Provider failure with separate transience and safe-replay decisions.

    ``retryable`` is retained as the legacy transient/provider-health signal.
    Callers must use ``safe_to_retry`` plus ``safe_retry_reason`` before
    replaying a request whose delivery state may be ambiguous.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
        phase: str | None = None,
        attempts: int | None = None,
        elapsed_seconds: float | None = None,
        global_fatal: bool = False,
        safe_to_retry: bool = False,
        safe_retry_reason: str | None = None,
        delivery_state: str | None = None,
        attempt_detail: dict[str, object] | None = None,
        attempt_history: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.phase = phase
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.global_fatal = global_fatal
        self.safe_to_retry = safe_to_retry
        self.safe_retry_reason = safe_retry_reason
        self.delivery_state = delivery_state
        self.attempt_detail = attempt_detail
        self.attempt_history = attempt_history or []

    def public_dict(self, *, secrets: tuple[str, ...] = ()) -> dict[str, object]:
        """Return safe diagnostics suitable for trajectories and result JSON."""

        def redact(value: object) -> object:
            if isinstance(value, str):
                return redact_sensitive_text(value, secrets=secrets)
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {str(key): redact(item) for key, item in value.items()}
            return value

        return {
            "message": redact(str(self)),
            "retryable": self.retryable,
            "status_code": self.status_code,
            "retry_after_seconds": self.retry_after,
            "phase": self.phase,
            "attempts": self.attempts,
            "elapsed_seconds": self.elapsed_seconds,
            "global_fatal": self.global_fatal,
            "safe_to_retry": self.safe_to_retry,
            "safe_retry_reason": self.safe_retry_reason,
            "delivery_state": self.delivery_state,
            "attempt_history": redact(self.attempt_history),
        }


class AgentTimeoutError(HarnessError):
    """Raised when a workbook task exceeds its configured wall-clock budget."""


class AgentBudgetError(HarnessError):
    """Raised when a shared, multi-stage run budget is exhausted."""

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        budget: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.budget = budget


class AgentTurnLimitError(HarnessError):
    """Raised when one task exhausts its allowed model/tool turns."""


class AgentRoutingError(HarnessError):
    """Raised when a required deterministic first-tool route is not honored."""
