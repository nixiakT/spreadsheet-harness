from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from spreadsheet_harness.agent import (
    CONTEXT_POLICY,
    ResponseTurn,
    SpreadsheetAgent,
    _history_summary_item,
    _render_history_summary,
)
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import ProviderError
from spreadsheet_harness.session import WorkbookSession
from spreadsheet_harness.tools import ToolOutcome
from spreadsheet_harness.trajectory import read_trajectory


class ScriptedResponsesClient:
    requests: list[dict[str, Any]] = []
    turns: list[ResponseTurn] = []

    def __init__(self, _: ProviderConfig) -> None:
        self.turn_index = 0

    def __enter__(self) -> ScriptedResponsesClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
        self.requests.append(payload)
        turn = self.turns[self.turn_index]
        self.turn_index += 1
        return turn


def _final_turn(number: int = 2) -> ResponseTurn:
    return ResponseTurn(
        f"response-{number}",
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


def _configure_scripted_client(monkeypatch: Any, turns: list[ResponseTurn]) -> None:
    ScriptedResponsesClient.requests = []
    ScriptedResponsesClient.turns = turns
    monkeypatch.setattr(
        "spreadsheet_harness.agent.ResponsesClient",
        ScriptedResponsesClient,
    )


def _config() -> ProviderConfig:
    return ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model")


def _independent_json_size(value: Any) -> tuple[int, int]:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return len(encoded), len(encoded.encode("utf-8"))


def test_agent_preserves_reasoning_and_pairs_multiple_tool_outputs_by_call_id(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    first_output = [
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "summary": [{"type": "summary_text", "text": "Inspect both ranges."}],
        },
        {
            "type": "function_call",
            "id": "function-item-1",
            "call_id": "call-alpha",
            "name": "echo",
            "arguments": json.dumps({"label": "alpha"}),
        },
        {
            "type": "function_call",
            "id": "function-item-2",
            "call_id": "call-beta",
            "name": "echo",
            "arguments": json.dumps({"label": "beta"}),
        },
    ]
    _configure_scripted_client(
        monkeypatch,
        [ResponseTurn("response-1", first_output, "", {}), _final_turn()],
    )

    class EchoTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.invocations: list[str] = []
            self.schemas = [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "Echo a label for context tests.",
                    "parameters": {"type": "object"},
                }
            ]

        def invoke(self, _: str, arguments: dict[str, Any]) -> ToolOutcome:
            label = str(arguments["label"])
            self.invocations.append(label)
            return ToolOutcome({"ok": True, "label": label})

    session = WorkbookSession.create(sample_workbook, tmp_path / "pairing-run")
    tools = EchoTools(session)

    SpreadsheetAgent(_config(), tools).run("exercise tool pairing")

    replayed = ScriptedResponsesClient.requests[1]["input"][1:]
    assert [item["type"] for item in replayed] == [
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert replayed[:3] == first_output
    tool_outputs = replayed[3:]
    assert [item["call_id"] for item in tool_outputs] == ["call-alpha", "call-beta"]
    assert len({item["call_id"] for item in tool_outputs}) == len(tool_outputs)
    assert [json.loads(item["output"])["label"] for item in tool_outputs] == [
        "alpha",
        "beta",
    ]
    assert tools.invocations == ["alpha", "beta"]


@pytest.mark.parametrize(
    ("call_ids", "error_match"),
    [
        pytest.param(["call-valid", None], "missing", id="missing-call-id"),
        pytest.param(["call-duplicate", "call-duplicate"], "duplicate", id="duplicate-call-id"),
    ],
)
def test_agent_rejects_invalid_call_ids_before_invoking_any_tool(
    sample_workbook: Path,
    tmp_path: Path,
    monkeypatch: Any,
    call_ids: list[str | None],
    error_match: str,
) -> None:
    function_calls: list[dict[str, Any]] = []
    for index, call_id in enumerate(call_ids):
        function_call = {
            "type": "function_call",
            "id": f"function-item-{index}",
            "name": "record_invocation",
            "arguments": json.dumps({"index": index}),
        }
        if call_id is not None:
            function_call["call_id"] = call_id
        function_calls.append(function_call)
    _configure_scripted_client(
        monkeypatch,
        [ResponseTurn("response-invalid", function_calls, "", {})],
    )

    class InvocationRecordingTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.invocations: list[dict[str, Any]] = []
            self.schemas = [
                {
                    "type": "function",
                    "name": "record_invocation",
                    "description": "Record whether a tool was invoked.",
                    "parameters": {"type": "object"},
                }
            ]

        def invoke(self, _: str, arguments: dict[str, Any]) -> ToolOutcome:
            self.invocations.append(arguments)
            return ToolOutcome({"ok": True})

    session = WorkbookSession.create(sample_workbook, tmp_path / "invalid-call-id-run")
    tools = InvocationRecordingTools(session)

    with pytest.raises(ProviderError, match=error_match) as caught:
        SpreadsheetAgent(_config(), tools).run("reject invalid call identifiers")

    assert caught.value.phase == "response_protocol"
    assert tools.invocations == []
    assert len(ScriptedResponsesClient.requests) == 1


def test_agent_caps_raw_tool_output_in_the_next_model_request(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    _configure_scripted_client(
        monkeypatch,
        [
            ResponseTurn(
                "response-1",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-large",
                        "name": "large_result",
                        "arguments": "{}",
                    }
                ],
                "",
                {},
            ),
            _final_turn(),
        ],
    )

    class LargeResultTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.schemas = [
                {
                    "type": "function",
                    "name": "large_result",
                    "description": "Return an intentionally oversized result.",
                    "parameters": {"type": "object"},
                }
            ]

        def invoke(self, _: str, __: dict[str, Any]) -> ToolOutcome:
            return ToolOutcome({"ok": True, "blob": "x" * 200_000})

    session = WorkbookSession.create(sample_workbook, tmp_path / "raw-cap-run")
    SpreadsheetAgent(_config(), LargeResultTools(session)).run("exercise raw output cap")

    second_input = ScriptedResponsesClient.requests[1]["input"]
    raw_output = next(
        item["output"] for item in second_input if item.get("type") == "function_call_output"
    )
    assert len(raw_output) <= CONTEXT_POLICY["raw_tool_output_max_chars"]
    envelope = json.loads(raw_output)
    assert envelope["ok"] is True
    assert envelope["truncated"] is True
    assert envelope["original_chars"] > CONTEXT_POLICY["raw_tool_output_max_chars"]
    assert len(envelope["sha256"]) == 64
    assert "narrower" in envelope["message"].lower()
    assert len(raw_output) >= CONTEXT_POLICY["raw_tool_output_max_chars"] - 100


def test_agent_caps_the_combined_raw_output_of_multiple_tool_calls(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    call_ids = ["call-large-alpha", "call-large-beta"]
    _configure_scripted_client(
        monkeypatch,
        [
            ResponseTurn(
                "response-1",
                [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "large_result",
                        "arguments": json.dumps({"label": label}),
                    }
                    for call_id, label in zip(call_ids, ("alpha", "beta"), strict=True)
                ],
                "",
                {},
            ),
            _final_turn(),
        ],
    )

    class MultipleLargeResultTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.schemas = [
                {
                    "type": "function",
                    "name": "large_result",
                    "description": "Return one of multiple oversized results.",
                    "parameters": {"type": "object"},
                }
            ]

        def invoke(self, _: str, arguments: dict[str, Any]) -> ToolOutcome:
            label = str(arguments["label"])
            return ToolOutcome({"ok": True, "label": label, "blob": label * 16_000})

    session = WorkbookSession.create(sample_workbook, tmp_path / "raw-turn-cap-run")
    SpreadsheetAgent(_config(), MultipleLargeResultTools(session)).run(
        "exercise combined raw output cap"
    )

    second_input = ScriptedResponsesClient.requests[1]["input"]
    tool_outputs = [item for item in second_input if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in tool_outputs] == call_ids
    assert len({item["call_id"] for item in tool_outputs}) == 2
    assert (
        sum(len(item["output"]) for item in tool_outputs)
        <= CONTEXT_POLICY["raw_tool_turn_max_chars"]
    )
    assert all(json.loads(item["output"])["truncated"] is True for item in tool_outputs)


def test_context_telemetry_matches_independent_full_wire_serialization_per_turn(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    usage_by_turn = [
        {"input_tokens": 101, "output_tokens": 7, "total_tokens": 108},
        {"input_tokens": 211, "output_tokens": 13, "total_tokens": 224},
    ]
    _configure_scripted_client(
        monkeypatch,
        [
            ResponseTurn(
                "response-1",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-telemetry",
                        "name": "unicode_result",
                        "arguments": json.dumps({"label": "财务📈"}, ensure_ascii=False),
                    }
                ],
                "",
                usage_by_turn[0],
                attempts=2,
                elapsed_seconds=1.25,
                first_event_seconds=0.5,
            ),
            ResponseTurn(
                "response-2",
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "完成"}],
                    }
                ],
                "完成",
                usage_by_turn[1],
                elapsed_seconds=2.5,
                first_event_seconds=0.75,
            ),
        ],
    )

    class UnicodeResultTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.schemas = [
                {
                    "type": "function",
                    "name": "unicode_result",
                    "description": "Return non-ASCII 工具数据.",
                    "parameters": {"type": "object"},
                }
            ]

        def invoke(self, _: str, arguments: dict[str, Any]) -> ToolOutcome:
            return ToolOutcome({"ok": True, "label": arguments["label"], "note": "检查📊"})

    session = WorkbookSession.create(sample_workbook, tmp_path / "telemetry-run")
    config = _config()
    result = SpreadsheetAgent(config, UnicodeResultTools(session)).run("检查 Résumé 📊")

    requested_events = [
        row["payload"]
        for row in read_trajectory(session.paths.trajectory)
        if row["event"] == "model.requested"
    ]
    assert len(requested_events) == len(ScriptedResponsesClient.requests) == 2
    assert len(result.request_timings) == 2

    metric_names = (
        "input_serialized_chars",
        "input_serialized_bytes",
        "request_body_chars",
        "request_body_bytes",
    )
    for turn_index, (request, requested, timing) in enumerate(
        zip(
            ScriptedResponsesClient.requests,
            requested_events,
            result.request_timings,
            strict=True,
        ),
        start=1,
    ):
        input_chars, input_bytes = _independent_json_size(request["input"])
        wire_body = {
            **request,
            "stream": True,
            "store": request.get("store", config.store_responses),
        }
        request_body_chars, request_body_bytes = _independent_json_size(wire_body)
        expected_metrics = {
            "input_serialized_chars": input_chars,
            "input_serialized_bytes": input_bytes,
            "request_body_chars": request_body_chars,
            "request_body_bytes": request_body_bytes,
        }

        assert wire_body["stream"] is True
        assert wire_body["store"] is False
        assert input_bytes > input_chars
        assert request_body_bytes > request_body_chars
        assert {name: requested[name] for name in metric_names} == expected_metrics
        assert {name: timing[name] for name in metric_names} == expected_metrics
        assert requested["turn"] == timing["turn"] == turn_index
        assert {
            "input_tokens": timing["input_tokens"],
            "output_tokens": timing["output_tokens"],
            "total_tokens": timing["total_tokens"],
        } == usage_by_turn[turn_index - 1]

    assert result.usage == {
        "input_tokens": 312,
        "output_tokens": 20,
        "total_tokens": 332,
    }


def test_agent_limits_images_per_turn_and_drops_them_after_the_immediate_request(
    sample_workbook: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    image_call_output = [
        {
            "type": "function_call",
            "call_id": "call-image-a",
            "name": "image_result",
            "arguments": json.dumps({"image": "a"}),
        },
        {
            "type": "function_call",
            "call_id": "call-image-b",
            "name": "image_result",
            "arguments": json.dumps({"image": "b"}),
        },
    ]
    _configure_scripted_client(
        monkeypatch,
        [
            ResponseTurn("response-1", image_call_output, "", {}),
            ResponseTurn(
                "response-2",
                [
                    {
                        "type": "function_call",
                        "call_id": "call-no-image",
                        "name": "no_image_result",
                        "arguments": "{}",
                    }
                ],
                "",
                {},
            ),
            _final_turn(3),
        ],
    )

    image_size = 11 * 1024 * 1024
    image_a = tmp_path / "context-image-a.png"
    image_b = tmp_path / "context-image-b.png"
    image_a.write_bytes(b"a" * image_size)
    image_b.write_bytes(b"b" * image_size)

    class ImageTools:
        def __init__(self, session: WorkbookSession) -> None:
            self.session = session
            self.schemas = [
                {
                    "type": "function",
                    "name": name,
                    "description": "Return a test result.",
                    "parameters": {"type": "object"},
                }
                for name in ("image_result", "no_image_result")
            ]

        def invoke(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            if name == "no_image_result":
                return ToolOutcome({"ok": True})
            image_path = image_a if arguments["image"] == "a" else image_b
            return ToolOutcome({"ok": True, "name": image_path.name}, image_path=image_path)

    session = WorkbookSession.create(sample_workbook, tmp_path / "image-budget-run")
    SpreadsheetAgent(_config(), ImageTools(session)).run("exercise image budget")

    immediate_input = ScriptedResponsesClient.requests[1]["input"]
    image_content = [
        content
        for item in immediate_input
        for content in item.get("content", [])
        if content.get("type") == "input_image"
    ]
    assert len(image_content) == 1
    attached_bytes = sum(
        len(base64.b64decode(content["image_url"].partition(",")[2])) for content in image_content
    )
    assert attached_bytes <= CONTEXT_POLICY["image_turn_max_bytes"]

    immediate_text = json.dumps(immediate_input, ensure_ascii=False).lower()
    assert image_a.name in immediate_text
    assert image_b.name not in " ".join(
        content.get("text", "") for item in immediate_input for content in item.get("content", [])
    )
    outputs_by_call_id = {
        item["call_id"]: item["output"]
        for item in immediate_input
        if item.get("type") == "function_call_output"
    }
    skipped_output = outputs_by_call_id["call-image-b"].lower()
    assert "image" in skipped_output
    assert any(marker in skipped_output for marker in ("budget", "limit", "omitted", "skipped"))

    following_input = ScriptedResponsesClient.requests[2]["input"]
    assert not any(
        content.get("type") == "input_image"
        for item in following_input
        for content in item.get("content", [])
    )


def test_history_summary_limit_includes_the_complete_envelope() -> None:
    entries = [
        _history_summary_item(
            turn=turn,
            name=f"summary-tool-{turn:03d}",
            arguments={"argument": f"arg-{turn}-" + "a" * 2_000},
            result={"result": f"result-{turn}-" + "r" * 5_000},
        )
        for turn in range(100)
    ]

    summary = _render_history_summary(entries)

    assert len(entries[-1]["arguments"]) <= CONTEXT_POLICY["argument_max_chars"]
    assert len(entries[-1]["result"]) <= CONTEXT_POLICY["result_max_chars"]
    assert len(summary) <= CONTEXT_POLICY["summary_max_chars"]
    assert summary.startswith("<tool_history_summary>\n")
    assert summary.endswith("\n</tool_history_summary>")
    assert "older tool calls were omitted" in summary
    assert "summary-tool-099" in summary
    assert "summary-tool-000" not in summary
