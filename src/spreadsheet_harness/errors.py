"""Domain-specific exceptions."""

import re
from typing import Any

LEGACY_AGENT_EXECUTION_FAILURE_REASONS = frozenset(
    {"edit_recovery_exhausted", "workbook_unchanged"}
)
V28_AGENT_EXECUTION_FAILURE_REASONS = LEGACY_AGENT_EXECUTION_FAILURE_REASONS | {
    "budget_exhausted",
    "terminal_submission_invalid",
    "terminal_submission_truncated",
}
AGENT_EXECUTION_FAILURE_REASONS = V28_AGENT_EXECUTION_FAILURE_REASONS | {
    "model_response_truncated",
}
MODEL_EXECUTION_BUDGET_TERMINATIONS = frozenset(
    {"max_model_calls", "max_total_tokens"}
)
AGENT_TOOL_RECALCULATION_FAILURE_STAGE = "agent_tool_recalculation"
POSTPROCESS_RECALCULATION_FAILURE_STAGE = "recalculation"
RECALCULATION_VALIDATION_TOOL = "recalculate_and_read"

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


class RecalculationIntegrityError(RenderError):
    """Raised when recalculation changes the workbook's sheet identity."""

    def __init__(self, message: str, *, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence
        # The agent boundary fills these fields when the failing recalculation
        # originated from a model tool call rather than runner postprocessing.
        self.agent_result: Any | None = None
        self.agent_stage: str | None = None
        self.failed_tool: str | None = None


class ScoringInfrastructureError(HarnessError):
    """Raised when a valid workbook shape cannot be consumed by the scorer."""


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


class ProviderOutputLimitError(ProviderError):
    """Parsed HTTP-200 response that ended at the provider output limit.

    The discarded assistant message is represented only by a digest and size
    counters. Keeping its partial text or tool arguments would risk treating a
    truncated function call as valid evidence later in the pipeline.
    """

    def __init__(
        self,
        message: str,
        *,
        response_id: str | None,
        usage: dict[str, int],
        timing: dict[str, object],
        discarded_message: dict[str, object],
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.response_id = response_id
        self.usage = dict(usage)
        self.timing = dict(timing)
        self.discarded_message = dict(discarded_message)

    def public_dict(self, *, secrets: tuple[str, ...] = ()) -> dict[str, object]:
        def redact(value: object) -> object:
            if isinstance(value, str):
                return redact_sensitive_text(value, secrets=secrets)
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {str(key): redact(item) for key, item in value.items()}
            return value

        result = super().public_dict(secrets=secrets)
        result["output_limit"] = {
            "response_id": redact(self.response_id),
            "usage": dict(self.usage),
            "timing": redact(self.timing),
            "discarded_message": dict(self.discarded_message),
        }
        return result


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


class AgentExecutionFailure(HarnessError):
    """Known model-execution failure with auditable partial agent evidence."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        agent_result: Any | None = None,
    ) -> None:
        super().__init__(message)
        if reason not in AGENT_EXECUTION_FAILURE_REASONS:
            raise ValueError(f"Unknown agent execution failure reason: {reason!r}")
        self.reason = reason
        self.agent_result = agent_result
