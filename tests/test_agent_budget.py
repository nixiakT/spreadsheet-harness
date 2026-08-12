from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spreadsheet_harness.agent import AgentResult, ResponseTurn, SpreadsheetAgent
from spreadsheet_harness.budget import RunBudget
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import AgentBudgetError
from spreadsheet_harness.session import WorkbookSession


class EmptyTools:
    def __init__(self, session: WorkbookSession) -> None:
        self.session = session
        self.schemas: list[dict[str, Any]] = []


class FinalResponsesClient:
    requests: list[dict[str, Any]] = []
    usages: list[dict[str, int]] = []

    def __init__(self, _: ProviderConfig) -> None:
        pass

    def __enter__(self) -> FinalResponsesClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
        self.requests.append(payload)
        usage = self.usages.pop(0)
        return ResponseTurn(
            f"response-{len(self.requests)}",
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ],
            "done",
            usage,
        )


def _config() -> ProviderConfig:
    return ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")


def test_run_budget_accepts_exact_token_boundary_then_blocks_another_call() -> None:
    budget = RunBudget(max_model_calls=3, max_total_tokens=10)

    first = budget.begin_model_call(stage="inspect")
    snapshot = budget.record_response(
        first,
        {"input_tokens": 7, "output_tokens": 3},
        stage="inspect",
    )

    assert snapshot["used"]["model_calls"] == 1
    assert snapshot["used"]["total_tokens"] == 10
    assert snapshot["termination"] is None

    with pytest.raises(AgentBudgetError, match="total-token") as caught:
        budget.begin_model_call(stage="solve")

    assert caught.value.reason == "max_total_tokens"
    assert caught.value.budget is not None
    assert caught.value.budget["used"]["model_calls"] == 1
    assert caught.value.budget["used"]["total_tokens"] == 10
    assert caught.value.budget["termination"]["stage"] == "solve"


def test_run_budget_records_single_response_token_overage() -> None:
    budget = RunBudget(max_model_calls=3, max_total_tokens=10)
    reservation = budget.begin_model_call(stage="solve")

    with pytest.raises(AgentBudgetError, match="total-token") as caught:
        budget.record_response(reservation, {"total_tokens": 11}, stage="solve")

    assert caught.value.reason == "max_total_tokens"
    assert caught.value.budget["used"]["model_calls"] == 1
    assert caught.value.budget["used"]["total_tokens"] == 11


def test_run_budget_checks_call_and_wall_boundaries(monkeypatch: Any) -> None:
    now = [100.0]
    monkeypatch.setattr("spreadsheet_harness.budget.time.monotonic", lambda: now[0])
    timed = RunBudget(max_elapsed_seconds=5)
    now[0] = 105.0

    with pytest.raises(AgentBudgetError) as timed_out:
        timed.begin_model_call(stage="render")

    assert timed_out.value.reason == "max_elapsed_seconds"
    assert timed.to_dict()["termination"]["stage"] == "render"

    calls = RunBudget(max_model_calls=0)
    with pytest.raises(AgentBudgetError) as calls_out:
        calls.begin_model_call(stage="baseline")
    assert calls_out.value.reason == "max_model_calls"
    assert calls.to_dict()["used"]["model_calls"] == 0


def test_shared_budget_spans_agents_and_empty_tools_are_omitted(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    FinalResponsesClient.requests = []
    FinalResponsesClient.usages = [
        {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
    ]
    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", FinalResponsesClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "shared-budget")
    tools = EmptyTools(session)
    budget = RunBudget(max_model_calls=2, max_total_tokens=20, max_elapsed_seconds=60)

    inspect = SpreadsheetAgent(
        _config(),
        tools,  # type: ignore[arg-type]
        base_instructions="inspect-only",
        budget=budget,
        stage="inspect",
    ).run("inspect")
    solve = SpreadsheetAgent(
        _config(),
        tools,  # type: ignore[arg-type]
        base_instructions="solve-only",
        budget=budget,
        stage="solve",
    ).run("solve")

    assert inspect.budget is not None
    assert inspect.budget["used"]["model_calls"] == 1
    assert solve.budget is not None
    assert solve.budget["used"]["model_calls"] == 2
    assert solve.budget["used"]["total_tokens"] == 12
    assert solve.stage == "solve"
    assert solve.request_timings[0]["stage"] == "solve"
    assert [request["instructions"] for request in FinalResponsesClient.requests] == [
        "inspect-only",
        "solve-only",
    ]
    for request in FinalResponsesClient.requests:
        assert "tools" not in request
        assert "tool_choice" not in request
        assert "parallel_tool_calls" not in request

    with pytest.raises(AgentBudgetError) as exhausted:
        SpreadsheetAgent(
            _config(),
            tools,  # type: ignore[arg-type]
            budget=budget,
            stage="verify",
        ).run("verify")

    assert exhausted.value.reason == "max_model_calls"
    assert len(FinalResponsesClient.requests) == 2
    assert budget.to_dict()["termination"]["stage"] == "verify"


def test_agent_records_response_before_raising_token_budget(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    FinalResponsesClient.requests = []
    FinalResponsesClient.usages = [
        {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    ]
    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", FinalResponsesClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "token-budget")
    budget = RunBudget(max_model_calls=2, max_total_tokens=10)

    with pytest.raises(AgentBudgetError) as caught:
        SpreadsheetAgent(
            _config(),
            EmptyTools(session),  # type: ignore[arg-type]
            budget=budget,
            stage="solve",
        ).run("solve")

    assert caught.value.reason == "max_total_tokens"
    assert budget.to_dict()["used"]["model_calls"] == 1
    assert budget.to_dict()["used"]["total_tokens"] == 11
    trajectory = session.paths.trajectory.read_text(encoding="utf-8")
    assert "model.responded" in trajectory
    assert "agent.budget_exceeded" in trajectory


def test_agent_result_old_construction_remains_compatible() -> None:
    result = AgentResult("done", 1, 0, {}, "response")

    assert result.budget is None
    assert result.stage is None
    assert "budget" not in result.to_dict()
    assert "stage" not in result.to_dict()
