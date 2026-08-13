from __future__ import annotations

import hashlib
import json
import signal
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from spreadsheet_harness.agent import (
    ChatCompletionsClient,
    ResponsesClient,
    ResponseTurn,
    SpreadsheetAgent,
)
from spreadsheet_harness.budget import RunBudget
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import AgentRoutingError, HarnessError, ProviderError
from spreadsheet_harness.session import WorkbookSession
from spreadsheet_harness.tools import SpreadsheetToolRegistry, ToolOutcome


class _LateStallingStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        time.sleep(1.0)
        yield b'data: {"type":"response.completed","response":{"output":[]}}\n\n'


class FakeResponsesClient:
    requests: list[dict[str, Any]] = []

    def __init__(self, _: ProviderConfig) -> None:
        self.turn = 0

    def __enter__(self) -> FakeResponsesClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def create(
        self, payload: dict[str, Any], on_text: Any = None, **_: Any
    ) -> ResponseTurn:
        self.requests.append(payload)
        self.turn += 1
        if self.turn == 1:
            return ResponseTurn(
                "resp-1",
                [
                    {
                        "type": "function_call",
                        "id": "fc-1",
                        "call_id": "call-1",
                        "name": "list_sheets",
                        "arguments": "{}",
                    }
                ],
                "",
                {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
        return ResponseTurn(
            "resp-2",
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done"}],
                }
            ],
            "Done",
            {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
        )


def test_agent_executes_and_replays_tool_call(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    FakeResponsesClient.requests = []
    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", FakeResponsesClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(
        config, tools, first_tool_choice="list_sheets"
    ).run("Inspect the workbook")

    assert result.final_text == "Done"
    assert result.turns == 2
    assert result.tool_calls == 1
    assert result.tool_trace == [{"name": "list_sheets", "ok": True}]
    assert result.first_tool_choice == "list_sheets"
    assert result.observed_first_tool == "list_sheets"
    assert FakeResponsesClient.requests[0]["tool_choice"] == {
        "type": "function",
        "name": "list_sheets",
    }
    assert FakeResponsesClient.requests[1]["tool_choice"] == "auto"
    assert result.usage["total_tokens"] == 35
    second_input = FakeResponsesClient.requests[1]["input"]
    output = next(item for item in second_input if item.get("type") == "function_call_output")
    assert output["call_id"] == "call-1"
    assert json.loads(output["output"])["sheets"][0]["name"] == "Sales"
    trajectory = session.paths.trajectory.read_text(encoding="utf-8")
    assert "agent.completed" in trajectory
    assert "not-a-real-key" not in trajectory


def test_agent_rejects_unchanged_workbook_submit(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class UnchangedSubmitClient:
        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> UnchangedSubmitClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(
            self, payload: dict[str, Any], on_text: Any = None, **_: Any
        ) -> ResponseTurn:
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "resp-1",
                    [
                        {
                            "type": "function_call",
                            "id": "fc-1",
                            "call_id": "call-1",
                            "name": "list_sheets",
                            "arguments": "{}",
                        }
                    ],
                    "",
                    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            return ResponseTurn(
                "resp-2",
                [
                    {
                        "type": "function_call",
                        "id": "fc-2",
                        "call_id": "call-2",
                        "name": "submit_result",
                        "arguments": json.dumps({"result": "done"}),
                    }
                ],
                "",
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", UnchangedSubmitClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    with pytest.raises(AgentRoutingError, match="before changing"):
        SpreadsheetAgent(
            config,
            tools,
            forced_tool_prefix=("list_sheets",),
            required_tool_termination=True,
            require_workbook_change=True,
            max_turns=2,
        ).run("Edit the workbook")


def test_agent_sanitizes_no_arg_tool_arguments_before_replay(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class NoArgReplayClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> NoArgReplayClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "response-tool",
                    [
                        {
                            "type": "function_call",
                            "id": "fc-1",
                            "call_id": "call-1",
                            "name": "list_sheets",
                            "arguments": json.dumps({"irrelevant": "x" * 100_000}),
                        }
                    ],
                    "",
                    {},
                )
            return ResponseTurn(
                "response-final",
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done"}],
                    }
                ],
                "Done",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", NoArgReplayClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "no-arg-replay")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    SpreadsheetAgent(config, tools, first_tool_choice="list_sheets").run("Inspect")

    second_input = NoArgReplayClient.requests[1]["input"]
    replayed = next(item for item in second_input if item.get("type") == "function_call")
    assert replayed["name"] == "list_sheets"
    assert replayed["arguments"] == "{}"


def test_agent_records_and_applies_explicit_generation_controls(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    FakeResponsesClient.requests = []
    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", FakeResponsesClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "generation-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "Qwen/Qwen3.5-35B-A3B",
        temperature=1.0,
        top_k=40,
        min_p=0.0,
        enable_thinking=False,
    )

    SpreadsheetAgent(config, tools, first_tool_choice="list_sheets").run("Inspect")

    for request in FakeResponsesClient.requests:
        assert request["temperature"] == 1.0
        assert request["extra_body"] == {
            "top_k": 40,
            "min_p": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    events = [
        json.loads(line)
        for line in session.paths.trajectory.read_text(encoding="utf-8").splitlines()
    ]
    started = next(event for event in events if event["event"] == "agent.started")
    requested = [event for event in events if event["event"] == "model.requested"]
    expected = {
        "temperature": 1.0,
        "top_k": 40,
        "min_p": 0.0,
        "enable_thinking": False,
    }
    assert started["payload"]["provider"]["generation"] == expected
    assert all(event["payload"]["generation"] == expected for event in requested)


def test_agent_required_first_tool_fails_closed_on_missing_or_wrong_route(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "routing-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    with pytest.raises(AgentRoutingError, match="not available"):
        SpreadsheetAgent(config, tools, first_tool_choice="missing_tool").run("inspect")

    class WrongRouteClient:
        def __init__(self, _: ProviderConfig) -> None:
            pass

        def __enter__(self) -> WrongRouteClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, _: dict[str, Any], **__: Any) -> ResponseTurn:
            return ResponseTurn(
                "wrong-route",
                [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "skipped"}],
                    }
                ],
                "skipped",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", WrongRouteClient)
    with pytest.raises(AgentRoutingError, match="required exactly one"):
        SpreadsheetAgent(config, tools, first_tool_choice="list_sheets").run("inspect")
    assert "agent.routing_failed" in session.paths.trajectory.read_text(encoding="utf-8")


def test_agent_enforces_and_audits_multi_turn_tool_prefix(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class PrefixClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> PrefixClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn <= 2:
                return ResponseTurn(
                    f"response-{self.turn}",
                    [
                        {
                            "type": "function_call",
                            "call_id": f"call-{self.turn}",
                            "name": "list_sheets",
                            "arguments": "{}",
                        }
                    ],
                    "",
                    {},
                )
            return ResponseTurn(
                "response-3",
                [{"type": "message", "content": []}],
                "done",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", PrefixClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "prefix-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(
        config,
        tools,
        forced_tool_prefix=("list_sheets", "list_sheets"),
    ).run("inspect")

    assert [request["tool_choice"] for request in PrefixClient.requests] == [
        {"type": "function", "name": "list_sheets"},
        {"type": "function", "name": "list_sheets"},
        "auto",
    ]
    assert result.first_tool_choice == "list_sheets"
    assert result.observed_first_tool == "list_sheets"
    assert result.forced_tool_prefix == ["list_sheets", "list_sheets"]
    assert result.observed_forced_tool_prefix == ["list_sheets", "list_sheets"]

    with pytest.raises(ValueError, match="leave at least one turn"):
        SpreadsheetAgent(
            config,
            tools,
            max_turns=2,
            forced_tool_prefix=("list_sheets", "list_sheets"),
        )


def test_agent_forced_tool_prefix_reprompts_empty_response_without_advancing(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class EmptyThenForcedClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> EmptyThenForcedClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "response-empty",
                    [{"type": "message", "role": "assistant", "content": []}],
                    "",
                    {},
                )
            if self.turn == 2:
                return ResponseTurn(
                    "response-tool",
                    [
                        {
                            "type": "function_call",
                            "call_id": "call-tool",
                            "name": "list_sheets",
                            "arguments": "{}",
                        }
                    ],
                    "",
                    {},
                )
            return ResponseTurn(
                "response-final",
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "done",
                {},
            )

    monkeypatch.setattr(
        "spreadsheet_harness.agent.ResponsesClient", EmptyThenForcedClient
    )
    session = WorkbookSession.create(sample_workbook, tmp_path / "forced-empty-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(
        config,
        tools,
        forced_tool_prefix=("list_sheets",),
        max_turns=3,
    ).run("inspect")

    assert result.final_text == "done"
    assert [request["tool_choice"] for request in EmptyThenForcedClient.requests] == [
        {"type": "function", "name": "list_sheets"},
        {"type": "function", "name": "list_sheets"},
        "auto",
    ]
    second_input = EmptyThenForcedClient.requests[1]["input"]
    assert any(
        "previous response did not call the required function"
        in content.get("text", "")
        for item in second_input
        for content in item.get("content", [])
    )
    assert result.observed_forced_tool_prefix == ["list_sheets"]
    events = [
        json.loads(line)
        for line in session.paths.trajectory.read_text(encoding="utf-8").splitlines()
    ]
    reprompted = [
        event
        for event in events
        if event["event"] == "agent.empty_forced_tool_response_reprompted"
    ]
    assert reprompted[0]["payload"]["forced_prefix_index"] == 0


def test_agent_forced_tool_prefix_fails_closed_on_later_turn(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class WrongSecondRouteClient:
        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> WrongSecondRouteClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, _: dict[str, Any], **__: Any) -> ResponseTurn:
            self.turn += 1
            name = "list_sheets" if self.turn == 1 else "inspect_range"
            return ResponseTurn(
                f"response-{self.turn}",
                [
                    {
                        "type": "function_call",
                        "call_id": f"call-{self.turn}",
                        "name": name,
                        "arguments": "{}",
                    }
                ],
                "",
                {},
            )

    monkeypatch.setattr(
        "spreadsheet_harness.agent.ResponsesClient", WrongSecondRouteClient
    )
    session = WorkbookSession.create(sample_workbook, tmp_path / "wrong-prefix-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    with pytest.raises(AgentRoutingError, match="Forced turn 2"):
        SpreadsheetAgent(
            config,
            tools,
            forced_tool_prefix=("list_sheets", "list_sheets"),
        ).run("inspect")


def test_agent_forced_turn_rejects_extra_calls_before_tool_execution(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class ExtraCallClient:
        def __init__(self, _: ProviderConfig) -> None:
            pass

        def __enter__(self) -> ExtraCallClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, _: dict[str, Any], **__: Any) -> ResponseTurn:
            return ResponseTurn(
                "response-extra",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-expected",
                        "name": "list_sheets",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-extra",
                        "name": "inspect_range",
                        "arguments": '{"sheet":"Sales","range":"A1"}',
                    },
                ],
                "",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", ExtraCallClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "extra-call-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    invocations = 0
    original_invoke = tools.invoke

    def counted_invoke(name: str, arguments: dict[str, Any]) -> ToolOutcome:
        nonlocal invocations
        invocations += 1
        return original_invoke(name, arguments)

    monkeypatch.setattr(tools, "invoke", counted_invoke)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    with pytest.raises(AgentRoutingError, match="required exactly one"):
        SpreadsheetAgent(
            config,
            tools,
            forced_tool_prefix=("list_sheets",),
        ).run("inspect")
    assert invocations == 0


def test_agent_required_tool_termination_uses_required_and_submit_result(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class RequiredClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> RequiredClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "response-tool",
                    [
                        {
                            "type": "function_call",
                            "call_id": "call-tool",
                            "name": "list_sheets",
                            "arguments": "{}",
                        }
                    ],
                    "",
                    {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                )
            return ResponseTurn(
                "response-submit",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-submit",
                        "name": "submit_result",
                        "arguments": json.dumps({"result": "verified result"}),
                    }
                ],
                "",
                {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", RequiredClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "required-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")
    budget = RunBudget(max_model_calls=3, max_total_tokens=100, max_elapsed_seconds=60)

    result = SpreadsheetAgent(
        config,
        tools,
        forced_tool_prefix=("list_sheets",),
        required_tool_termination=True,
        budget=budget,
    ).run("inspect")

    assert [request["tool_choice"] for request in RequiredClient.requests] == [
        {"type": "function", "name": "list_sheets"},
        "auto",
    ]
    assert RequiredClient.requests[0]["max_output_tokens"] == 512
    assert RequiredClient.requests[1]["max_output_tokens"] == 16000
    assert [tool["name"] for tool in RequiredClient.requests[0]["tools"]] == [
        "list_sheets"
    ]
    assert any(
        tool["name"] == "submit_result" for tool in RequiredClient.requests[1]["tools"]
    )
    assert result.final_text == "verified result"
    assert result.tool_calls == 1
    assert result.terminal_submissions == 1
    assert result.to_dict()["function_calls_total"] == 2
    assert result.usage["total_tokens"] == 15
    assert result.budget is not None
    assert result.budget["used"]["model_calls"] == 2
    assert result.post_prefix_tool_choice == "auto"
    assert result.terminal_tool == "submit_result"
    assert result.observed_terminal_tool == "submit_result"
    events = [
        json.loads(line)
        for line in session.paths.trajectory.read_text(encoding="utf-8").splitlines()
    ]
    requested = [event for event in events if event["event"] == "model.requested"]
    assert requested[0]["payload"]["available_tool_names"] == ["list_sheets"]
    assert "submit_result" in requested[1]["payload"]["available_tool_names"]


def test_forced_code_interpreter_keeps_full_output_token_budget(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class CodePrefixClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> CodePrefixClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "response-code",
                    [
                        {
                            "type": "function_call",
                            "call_id": "call-code",
                            "name": "code_interpreter",
                            "arguments": json.dumps({"code": "print('ok')"}),
                        }
                    ],
                    "",
                    {},
                )
            return ResponseTurn(
                "response-submit",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-submit",
                        "name": "submit_result",
                        "arguments": json.dumps({"result": "done"}),
                    }
                ],
                "",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", CodePrefixClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "code-prefix-run")
    tools = SpreadsheetToolRegistry(
        session, enable_code=True, allowed_tools={"code_interpreter"}
    )
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    SpreadsheetAgent(
        config,
        tools,
        forced_tool_prefix=("code_interpreter",),
        required_tool_termination=True,
        max_output_tokens=4096,
    ).run("inspect")

    assert CodePrefixClient.requests[0]["max_output_tokens"] == 4096
    assert CodePrefixClient.requests[1]["max_output_tokens"] == 4096


def test_required_tool_termination_forces_submit_only_on_final_turn(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class FinalTurnClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> FinalTurnClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "response-tool",
                    [
                        {
                            "type": "function_call",
                            "call_id": "call-tool",
                            "name": "list_sheets",
                            "arguments": "{}",
                        }
                    ],
                    "",
                    {},
                )
            assert [tool["name"] for tool in payload["tools"]] == ["submit_result"]
            return ResponseTurn(
                "response-submit",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-submit",
                        "name": "submit_result",
                        "arguments": json.dumps({"result": "final turn result"}),
                    }
                ],
                "",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", FinalTurnClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "final-required-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(
        config,
        tools,
        max_turns=2,
        required_tool_termination=True,
    ).run("inspect")

    assert result.final_text == "final turn result"
    assert [tool["name"] for tool in FinalTurnClient.requests[0]["tools"]] == [
        "list_sheets",
        "inspect_range",
        "range_to_latex",
        "find_cells",
        "write_range",
        "fill_formula",
        "format_range",
        "clear_range",
        "delete_rows",
        "delete_columns",
        "manage_sheet",
        "recalculate_and_read",
        "render_workbook",
        "view_image",
        "undo_last",
        "submit_result",
    ]
    assert [tool["name"] for tool in FinalTurnClient.requests[1]["tools"]] == [
        "submit_result"
    ]
    assert FinalTurnClient.requests[1]["tool_choice"] == {
        "type": "function",
        "name": "submit_result",
    }
    assert FinalTurnClient.requests[1]["max_output_tokens"] == 1024


def test_required_tool_termination_accepts_nonempty_text_as_terminal_fallback(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class TextFallbackClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            pass

        def __enter__(self) -> TextFallbackClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            return ResponseTurn(
                "response-text",
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The workbook has been updated and verified.",
                            }
                        ],
                    }
                ],
                "The workbook has been updated and verified.",
                {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
            )

    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", TextFallbackClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "text-fallback-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(
        config,
        tools,
        required_tool_termination=True,
    ).run("inspect")

    assert result.final_text == "The workbook has been updated and verified."
    assert result.terminal_tool == "submit_result"
    assert result.observed_terminal_tool == "assistant_text"
    assert result.terminal_submissions == 0
    assert result.to_dict()["function_calls_total"] == 0
    events = [
        json.loads(line)
        for line in session.paths.trajectory.read_text(encoding="utf-8").splitlines()
    ]
    submitted = [
        event for event in events if event["event"] == "agent.terminal_submitted"
    ]
    assert submitted[0]["payload"]["observed_terminal_tool"] == "assistant_text"


def test_required_tool_termination_reprompts_empty_response_before_final_turn(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class EmptyThenSubmitClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> EmptyThenSubmitClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **__: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn == 1:
                return ResponseTurn(
                    "response-empty",
                    [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": ""}],
                        }
                    ],
                    "",
                    {},
                )
            return ResponseTurn(
                "response-submit",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-submit",
                        "name": "submit_result",
                        "arguments": json.dumps({"result": "submitted after reprompt"}),
                    }
                ],
                "",
                {},
            )

    monkeypatch.setattr(
        "spreadsheet_harness.agent.ResponsesClient", EmptyThenSubmitClient
    )
    session = WorkbookSession.create(sample_workbook, tmp_path / "empty-reprompt-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(
        config,
        tools,
        max_turns=2,
        required_tool_termination=True,
    ).run("inspect")

    assert result.final_text == "submitted after reprompt"
    assert len(EmptyThenSubmitClient.requests) == 2
    second_input = EmptyThenSubmitClient.requests[1]["input"]
    assert any(
        "previous response was empty"
        in content.get("text", "")
        for item in second_input
        for content in item.get("content", [])
    )
    events = [
        json.loads(line)
        for line in session.paths.trajectory.read_text(encoding="utf-8").splitlines()
    ]
    reprompted = [
        event
        for event in events
        if event["event"] == "agent.empty_required_response_reprompted"
    ]
    assert reprompted[0]["payload"]["turn"] == 1


def test_agent_required_tool_termination_rejects_multiple_calls_before_execution(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class MultipleRequiredCallsClient:
        def __init__(self, _: ProviderConfig) -> None:
            pass

        def __enter__(self) -> MultipleRequiredCallsClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, _: dict[str, Any], **__: Any) -> ResponseTurn:
            return ResponseTurn(
                "response-multiple",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-tool",
                        "name": "list_sheets",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-submit",
                        "name": "submit_result",
                        "arguments": json.dumps({"result": "premature"}),
                    },
                ],
                "",
                {},
            )

    monkeypatch.setattr(
        "spreadsheet_harness.agent.ResponsesClient", MultipleRequiredCallsClient
    )
    session = WorkbookSession.create(sample_workbook, tmp_path / "required-multiple-run")
    tools = SpreadsheetToolRegistry(session, enable_code=False)
    invocations = 0
    original_invoke = tools.invoke

    def counted_invoke(name: str, arguments: dict[str, Any]) -> ToolOutcome:
        nonlocal invocations
        invocations += 1
        return original_invoke(name, arguments)

    monkeypatch.setattr(tools, "invoke", counted_invoke)
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    with pytest.raises(AgentRoutingError, match="exactly one function call"):
        SpreadsheetAgent(
            config,
            tools,
            required_tool_termination=True,
        ).run("inspect")
    assert invocations == 0


def test_responses_client_does_not_infer_safe_retry_from_message(
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    client = ResponsesClient(config)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        raise ProviderError(
            "connection timed out; HTTP 503",
            retryable=True,
            phase="transport",
            delivery_state="ambiguous_post_send",
        )

    monkeypatch.setattr(client, "_create_once", create_once)
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test"})
    finally:
        client.close()
    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempt_history[0]["automatic_retry_scheduled"] is False
    assert (
        caught.value.attempt_history[0]["automatic_retry_suppressed_reason"]
        == "delivery_not_known_safe"
    )


def test_responses_client_flattens_generation_extensions_on_wire_and_hashes_wire() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "Qwen/Qwen3.5-35B-A3B",
        max_retries=0,
        temperature=1.0,
        top_p=0.95,
        seed=42,
        presence_penalty=1.5,
        top_k=20,
        min_p=0.0,
        repetition_penalty=1.0,
        enable_thinking=False,
        litellm_timeout_seconds=600,
    )
    seen: list[dict[str, Any]] = []
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        seen_headers.append(dict(request.headers))
        event = {
            "type": "response.completed",
            "response": {
                "id": "response-generation",
                "output": [{"type": "message", "content": []}],
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    client = ResponsesClient(config)
    headers = dict(client._client.headers)
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=headers,
    )
    try:
        turn = client.create({"model": config.model, "input": "test"})
    finally:
        client.close()

    assert len(seen) == 1
    assert seen_headers[0]["x-litellm-timeout"] == "600"
    wire = seen[0]
    assert "extra_body" not in wire
    assert wire == {
        "model": config.model,
        "input": "test",
        "temperature": 1.0,
        "top_p": 0.95,
        "presence_penalty": 1.5,
        "seed": 42,
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
        "store": False,
    }
    expected_hash = hashlib.sha256(
        json.dumps(
            wire,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert turn.request_payload_sha256 == expected_hash


def test_responses_client_rejects_extra_body_top_level_collision_before_http() -> None:
    config = ProviderConfig(
        "https://example.test/v1", "not-a-real-key", "test-model", top_k=40
    )
    client = ResponsesClient(config)
    try:
        with pytest.raises(HarnessError, match="collides"):
            client.create({"model": config.model, "top_k": 20})
    finally:
        client.close()


def test_chat_completions_client_maps_tools_and_replays_outputs() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        api_protocol="chat-completions",
        max_retries=0,
        temperature=1.0,
        top_p=0.95,
        seed=42,
        presence_penalty=1.5,
        top_k=20,
        min_p=0.0,
        repetition_penalty=1.0,
        enable_thinking=False,
        litellm_timeout_seconds=600,
    )
    seen: list[dict[str, Any]] = []
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        seen_headers.append(dict(request.headers))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chat-first",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "tool-call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "list_sheets",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chat-second",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Done",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 3,
                    "total_tokens": 23,
                },
            },
        )

    client = ChatCompletionsClient(config)
    headers = dict(client._client.headers)
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=headers,
    )
    try:
        first = client.create(
            {
                "model": config.model,
                "instructions": "Use tools.",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Inspect."}],
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "list_sheets",
                        "description": "List sheets.",
                        "parameters": {"type": "object", "properties": {}},
                        "strict": False,
                    }
                ],
                "tool_choice": {"type": "function", "name": "list_sheets"},
                "parallel_tool_calls": False,
                "max_output_tokens": 128,
            }
        )
        second = client.create(
            {
                "model": config.model,
                "input": [
                    *first.output,
                    {
                        "type": "function_call_output",
                        "call_id": "tool-call-1",
                        "output": '{"ok":true}',
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "list_sheets",
                        "description": "List sheets.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                "tool_choice": "auto",
                "max_output_tokens": 64,
            }
        )
    finally:
        client.close()

    assert first.output == [
        {
            "type": "function_call",
            "id": "tool-call-1",
            "call_id": "tool-call-1",
            "name": "list_sheets",
            "arguments": "{}",
        }
    ]
    assert first.usage == {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    assert second.text == "Done"
    assert second.usage == {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23}
    assert [headers["x-litellm-timeout"] for headers in seen_headers] == ["600", "600"]
    first_wire, second_wire = seen
    assert first_wire["messages"] == [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "Inspect."},
    ]
    assert first_wire["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "list_sheets",
                "description": "List sheets.",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            },
        }
    ]
    assert first_wire["tool_choice"] == {
        "type": "function",
        "function": {"name": "list_sheets"},
    }
    assert first_wire["max_tokens"] == 128
    assert first_wire["temperature"] == 1.0
    assert first_wire["top_p"] == 0.95
    assert first_wire["presence_penalty"] == 1.5
    assert first_wire["seed"] == 42
    assert first_wire["top_k"] == 20
    assert first_wire["min_p"] == 0.0
    assert first_wire["repetition_penalty"] == 1.0
    assert first_wire["chat_template_kwargs"] == {"enable_thinking": False}
    assert second_wire["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tool-call-1",
                    "type": "function",
                    "function": {"name": "list_sheets", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool-call-1", "content": '{"ok":true}'},
    ]
    assert first.attempt_history[0]["endpoint"] == "/chat/completions"
    assert first.terminal_event == "chat.completion"
    assert first.request_payload_sha256 is not None


def test_responses_client_rejects_unknown_safe_retry_reason(monkeypatch: Any) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    client = ResponsesClient(config)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        raise ProviderError(
            "claimed safe retry",
            retryable=True,
            safe_to_retry=True,
            safe_retry_reason="free_form_claim",
            delivery_state="pre_send",
        )

    monkeypatch.setattr(client, "_create_once", create_once)
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.safe_to_retry is False
    assert (
        caught.value.attempt_history[0]["automatic_retry_suppressed_reason"]
        == "unrecognized_safe_retry_reason"
    )


@pytest.mark.parametrize(
    ("exception_type", "expected_reason"),
    [
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ConnectError, "connect_error"),
        (httpx.PoolTimeout, "pool_timeout"),
    ],
)
def test_responses_client_retries_only_known_pre_send_failures(
    exception_type: type[httpx.RequestError],
    expected_reason: str,
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    calls = 0
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request_ids.append(request.headers["x-client-request-id"])
        if calls == 1:
            raise exception_type("pre-send failure", request=request)
        event = {
            "type": "response.completed",
            "response": {
                "id": "response-safe-retry",
                "output": [{"type": "message", "content": []}],
            },
        }
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-2"},
            text=f"data: {json.dumps(event)}\n\n",
        )

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    monkeypatch.setattr("spreadsheet_harness.agent.time.sleep", sleeps.append)
    try:
        turn = client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 2
    assert sleeps == [30.0]
    assert request_ids[0] != request_ids[1]
    assert request_ids[0].rsplit("-", 1)[0] == request_ids[1].rsplit("-", 1)[0]
    first = turn.attempt_history[0]
    assert first["safe_to_retry"] is True
    assert first["safe_retry_reason"] == expected_reason
    assert first["delivery_state"] == "pre_send"
    assert first["automatic_retry_scheduled"] is True
    assert first["backoff_requested_seconds"] == 30.0
    assert first["request_payload_sha256"] == turn.attempt_history[1][
        "request_payload_sha256"
    ]
    assert turn.attempt_history[1]["response_headers"] == {
        "x-request-id": "provider-request-2"
    }


def test_responses_client_uses_longer_backoff_for_overload(monkeypatch: Any) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    client = ResponsesClient(config)
    calls = 0
    sleeps: list[float] = []

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "server_is_overloaded",
                retryable=True,
                phase="response_stream",
                safe_to_retry=True,
                safe_retry_reason="explicit_overload",
                delivery_state="terminal_seen",
            )
        return ResponseTurn("response", [{"type": "message"}], "OK", {})

    monkeypatch.setattr(client, "_create_once", create_once)
    monkeypatch.setattr("spreadsheet_harness.agent.time.sleep", sleeps.append)
    try:
        turn = client.create({"model": "test"})
    finally:
        client.close()

    assert turn.text == "OK"
    assert calls == 2
    assert sleeps == [15.0]
    assert turn.attempt_history[0]["backoff_requested_seconds"] == 15.0
    assert turn.attempt_history[0]["backoff_seconds"] >= 0.0
    assert turn.attempt_history[0]["phase"] == "response_stream"
    assert turn.attempt_history[0]["overload_detected"] is True
    assert turn.attempt_history[0]["retry_backoff_reason"] == "capacity_rejection"


def test_responses_client_fails_closed_after_no_header_read_timeout(
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    client = ResponsesClient(config)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        raise ProviderError(
            "Responses request timed out during read",
            retryable=True,
            phase="read",
            delivery_state="ambiguous_post_send",
            attempt_detail={"headers_seconds": None},
        )

    monkeypatch.setattr(client, "_create_once", create_once)
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempts == 1
    history = caught.value.attempt_history
    assert history[0]["no_header_read_timeout"] is True
    assert history[0]["delivery_state"] == "ambiguous_post_send"
    assert history[0]["backoff_requested_seconds"] is None
    assert history[0]["automatic_retry_scheduled"] is False


def test_responses_client_fails_closed_after_read_timeout_with_headers(
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    client = ResponsesClient(config)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        raise ProviderError(
            "Responses request timed out during read",
            retryable=True,
            phase="read",
            delivery_state="ambiguous_post_send",
            attempt_detail={"headers_seconds": 1.25},
        )

    monkeypatch.setattr(client, "_create_once", create_once)
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.safe_to_retry is False
    assert caught.value.attempt_history[0]["no_header_read_timeout"] is False
    assert caught.value.attempt_history[0]["backoff_requested_seconds"] is None


def test_responses_client_does_not_count_request_when_deadline_already_expired(
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    client = ResponsesClient(config)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP request must not start")

    monkeypatch.setattr(client, "_create_once", create_once)
    monkeypatch.setattr("spreadsheet_harness.agent.time.monotonic", lambda: 10.0)
    try:
        with pytest.raises(ProviderError, match="before the task deadline") as caught:
            client.create({"model": "test"}, deadline=9.0)
    finally:
        client.close()

    assert calls == 0
    assert caught.value.attempts == 0
    assert caught.value.attempt_history == []


def test_responses_client_backoff_deadline_keeps_only_real_attempt(
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    client = ResponsesClient(config)
    calls = 0
    clock = [0.0]

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        raise ProviderError(
            "temporary connect timeout",
            retryable=True,
            phase="connect",
            safe_to_retry=True,
            safe_retry_reason="connect_timeout",
            delivery_state="pre_send",
        )

    monkeypatch.setattr(client, "_create_once", create_once)
    monkeypatch.setattr("spreadsheet_harness.agent.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "spreadsheet_harness.agent.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    try:
        with pytest.raises(ProviderError, match="before the task deadline") as caught:
            client.create({"model": "test"}, deadline=0.5)
    finally:
        client.close()

    assert calls == 1
    assert caught.value.attempts == 1
    assert len(caught.value.attempt_history) == 1
    assert caught.value.attempt_history[0]["backoff_requested_seconds"] == 0.5
    assert caught.value.attempt_history[0]["backoff_seconds"] == 0.5


def _streaming_client(
    events: list[dict[str, Any]], *, max_retries: int = 0
) -> ResponsesClient:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=max_retries,
    )
    client = ResponsesClient(config)
    client._client.close()
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    )
    return client


def test_responses_client_accepts_completed_without_done_marker() -> None:
    client = _streaming_client(
        [
            {
                "type": "response.completed",
                "response": {
                    "id": "response-1",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "OK"}],
                        }
                    ],
                    "usage": {"total_tokens": 2},
                },
            }
        ]
    )
    try:
        turn = client.create({"model": "test-model"})
    finally:
        client.close()
    assert turn.text == "OK"
    assert turn.response_id == "response-1"
    assert turn.status_code == 200
    assert turn.terminal_event == "response.completed"
    assert turn.sse_events == 1
    assert turn.headers_seconds is not None
    assert turn.first_event_seconds is not None
    assert turn.attempt_history[0]["outcome"] == "success"
    assert turn.attempt_history[0]["terminal_event"] == "response.completed"


def test_responses_client_rejects_incomplete_stream() -> None:
    client = _streaming_client(
        [
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            }
        ]
    )
    try:
        with pytest.raises(ProviderError, match="incomplete") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()
    assert caught.value.retryable is False
    public = caught.value.public_dict()
    assert public["attempt_history"][0]["terminal_event"] == "response.incomplete"  # type: ignore[index]
    assert public["attempt_history"][0]["status_code"] == 200  # type: ignore[index]


def test_responses_client_rejects_missing_terminal_event() -> None:
    client = _streaming_client(
        [
            {
                "type": "response.output_item.done",
                "item": {"type": "message", "content": []},
            }
        ],
        max_retries=3,
    )
    try:
        with pytest.raises(ProviderError, match="terminal event") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempts == 1


def test_responses_client_invalid_completed_call_keeps_http_telemetry() -> None:
    client = _streaming_client(
        [
            {
                "type": "response.completed",
                "response": {
                    "id": "response-invalid-call",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "list_sheets",
                            "arguments": "{}",
                        }
                    ],
                },
            }
        ]
    )
    try:
        with pytest.raises(ProviderError, match="call_id") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    history = caught.value.attempt_history
    assert len(history) == 1
    assert history[0]["status_code"] == 200
    assert history[0]["headers_seconds"] is not None
    assert history[0]["first_event_seconds"] is not None
    assert history[0]["terminal_event"] == "response.completed"
    assert history[0]["sse_events"] == 1


def test_provider_error_public_diagnostics_redact_nested_token_shapes() -> None:
    token = "cr_exampletoken1234567890"
    error = ProviderError(
        f"provider echoed {token}",
        attempt_history=[{"message": f"nested {token}"}],
    )

    encoded = json.dumps(error.public_dict())

    assert token not in encoded
    assert encoded.count("[REDACTED]") == 2


def test_responses_client_classifies_stream_server_error_as_transient() -> None:
    client = _streaming_client(
        [
            {
                "type": "response.failed",
                "response": {"error": {"code": "server_error", "message": "retry"}},
            }
        ]
    )
    try:
        with pytest.raises(ProviderError, match="server_error") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False


def test_responses_client_retries_exact_structured_overload(monkeypatch: Any) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            failed = {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "server_is_overloaded",
                        "message": "capacity rejection",
                    }
                },
            }
            return httpx.Response(200, text=f"data: {json.dumps(failed)}\n\n")
        completed = {
            "type": "response.completed",
            "response": {
                "id": "response-after-overload",
                "output": [{"type": "message", "content": []}],
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(completed)}\n\n")

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    monkeypatch.setattr("spreadsheet_harness.agent.time.sleep", sleeps.append)
    try:
        turn = client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 2
    assert sleeps == [15.0]
    assert turn.attempt_history[0]["safe_retry_reason"] == "explicit_overload"
    assert turn.attempt_history[0]["delivery_state"] == "terminal_seen"


def test_responses_client_x_should_retry_false_vetoes_stream_overload() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    calls = 0
    failed = {
        "type": "response.failed",
        "response": {"error": {"code": "server_is_overloaded"}},
    }

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"x-should-retry": "false"},
            text=f"data: {json.dumps(failed)}\n\n",
        )

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="server_is_overloaded") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempts == 1


def test_responses_client_x_should_retry_false_vetoes_safe_http_status() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            headers={"x-should-retry": "false"},
            json={"error": {"code": "server_is_overloaded"}},
        )

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="HTTP 503") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempts == 1


def test_responses_client_does_not_treat_stream_rate_limit_as_overload() -> None:
    client = _streaming_client(
        [
            {
                "type": "response.failed",
                "response": {
                    "error": {"code": "rate_limit", "message": "retry later"}
                },
            }
        ],
        max_retries=3,
    )
    try:
        with pytest.raises(ProviderError, match="rate_limit") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempts == 1


@pytest.mark.parametrize(
    "exception_type",
    [
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_responses_client_fails_closed_for_post_send_transport_errors(
    exception_type: type[httpx.RequestError],
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise exception_type("ambiguous delivery", request=request)

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.delivery_state == "ambiguous_post_send"
    assert caught.value.attempts == 1


def test_responses_client_enforces_absolute_stream_deadline_after_sse() -> None:
    calls = 0
    previous_handler = signal.getsignal(signal.SIGALRM)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_LateStallingStream())

    client = ResponsesClient(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            timeout_seconds=0.05,
            max_retries=3,
        )
    )
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError, match="absolute") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert time.monotonic() - started < 0.5
    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.phase == "total"
    assert caught.value.delivery_state == "ambiguous_post_send"
    assert caught.value.attempts == 1
    assert caught.value.attempt_history[0]["automatic_retry_scheduled"] is False
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == previous_handler


@pytest.mark.parametrize("status_code", [425, 429, 503])
def test_responses_client_retries_explicit_safe_http_statuses(
    status_code: int,
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert json.loads(request.content)["store"] is False
        if calls == 1:
            return httpx.Response(
                status_code,
                headers={
                    "x-request-id": "provider-rejection-1",
                },
                json={"error": {"code": "explicit_rejection"}},
            )
        event = {
            "type": "response.completed",
            "response": {
                "id": "response-2",
                "output": [{"type": "message", "content": []}],
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    monkeypatch.setattr("spreadsheet_harness.agent.time.sleep", sleeps.append)
    try:
        turn = client.create({"model": "test-model"})
    finally:
        client.close()
    assert turn.response_id == "response-2"
    assert turn.attempts == 2
    assert calls == 2
    assert sleeps == [15.0]
    first = turn.attempt_history[0]
    assert first["status_code"] == status_code
    assert first["safe_to_retry"] is True
    assert first["safe_retry_reason"] == f"http_{status_code}"
    assert first["retry_backoff_reason"] == "capacity_rejection"
    assert first["response_headers"]["x-request-id"] == "provider-rejection-1"


@pytest.mark.parametrize(
    ("retry_after", "expected_delay", "expected_reason"),
    [
        (2.5, 15.0, "provider_retry_after_capacity_floor"),
        (20.0, 20.0, "provider_retry_after"),
    ],
)
def test_responses_client_safe_retry_honors_capacity_floor_and_retry_after(
    retry_after: float,
    expected_delay: float,
    expected_reason: str,
    monkeypatch: Any,
) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    client = ResponsesClient(config)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "HTTP 429",
                retryable=True,
                status_code=429,
                retry_after=retry_after,
                safe_to_retry=True,
                safe_retry_reason="http_429",
                delivery_state="headers_seen",
            )
        return ResponseTurn("response", [{"type": "message"}], "OK", {})

    monkeypatch.setattr(client, "_create_once", create_once)
    sleeps: list[float] = []
    monkeypatch.setattr("spreadsheet_harness.agent.time.sleep", sleeps.append)
    try:
        turn = client.create({"model": "test-model"})
    finally:
        client.close()

    assert turn.text == "OK"
    assert sleeps == [expected_delay]
    assert turn.attempt_history[0]["retry_backoff_reason"] == expected_reason


def test_responses_client_fails_closed_for_exact_replay_http_408() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            408,
            headers={"retry-after": "0", "x-should-retry": "true"},
            json={"error": {"code": "server_is_overloaded"}},
        )

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="HTTP 408") as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.delivery_state == "ambiguous_post_send"
    assert caught.value.attempts == 1
    history = caught.value.attempt_history
    assert history[0]["status_code"] == 408
    assert history[0]["safe_retry_reason"] is None
    assert history[0]["automatic_retry_scheduled"] is False
    assert history[0]["backoff_requested_seconds"] is None


@pytest.mark.parametrize("status_code", [500, 502, 504, 521])
def test_responses_client_does_not_replay_other_5xx(status_code: int) -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=3,
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            headers={"x-should-retry": "true"},
            json={"error": {"code": "server_error"}},
        )

    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 1
    assert caught.value.retryable is True
    assert caught.value.safe_to_retry is False
    assert caught.value.attempts == 1


def test_responses_client_marks_quota_as_global_fatal() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
    )
    client = ResponsesClient(config)
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                402,
                json={"error": {"code": "insufficient_quota"}},
            )
        )
    )
    try:
        with pytest.raises(ProviderError) as caught:
            client.create({"model": "test-model"})
    finally:
        client.close()
    assert caught.value.global_fatal is True
    assert caught.value.retryable is False


def test_agent_keeps_only_one_raw_tool_turn_in_context(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    class ThreeTurnClient:
        requests: list[dict[str, Any]] = []

        def __init__(self, _: ProviderConfig) -> None:
            self.turn = 0

        def __enter__(self) -> ThreeTurnClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
            self.requests.append(payload)
            self.turn += 1
            if self.turn < 3:
                label = "A" if self.turn == 1 else "B"
                return ResponseTurn(
                    f"response-{self.turn}",
                    [
                        {
                            "type": "function_call",
                            "call_id": f"call-{self.turn}",
                            "name": "large_result",
                            "arguments": json.dumps({"label": label}),
                        }
                    ],
                    "",
                    {},
                )
            return ResponseTurn(
                "response-3",
                [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "done",
                {},
            )

    class LargeResultTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.schemas = [
                {
                    "type": "function",
                    "name": "large_result",
                    "description": "test",
                    "parameters": {"type": "object"},
                }
            ]

        def invoke(self, _: str, arguments: dict[str, Any]) -> ToolOutcome:
            label = str(arguments["label"])
            image_path = None
            if label == "A":
                image_path = self.session.workspace / "one-turn.png"
                image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            return ToolOutcome(
                {"ok": True, "blob": label * 10_000},
                image_path=image_path,
            )

    ThreeTurnClient.requests = []
    monkeypatch.setattr("spreadsheet_harness.agent.ResponsesClient", ThreeTurnClient)
    session = WorkbookSession.create(sample_workbook, tmp_path / "compact-run")
    config = ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")

    result = SpreadsheetAgent(config, LargeResultTools(session)).run("test compaction")

    second = json.dumps(ThreeTurnClient.requests[1]["input"])
    third = json.dumps(ThreeTurnClient.requests[2]["input"])
    assert second.count("A") >= 10_000
    assert third.count("A") < 2_000
    assert third.count("B") >= 10_000
    assert "tool_history_summary" in third
    assert "input_image" in second
    assert "input_image" not in third
    assert result.context_policy["recent_raw_turns"] == 1
