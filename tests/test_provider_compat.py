from __future__ import annotations

import json
from typing import Any

import pytest

from spreadsheet_harness.agent import ResponseTurn
from spreadsheet_harness.cli import build_parser, cmd_doctor
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import HarnessError, ProviderError
from spreadsheet_harness.provider_compat import (
    check_chat_completions_tool_compatibility,
    check_responses_tool_compatibility,
    check_tool_compatibility,
)


class ScriptedResponsesClient:
    requests: list[dict[str, Any]] = []

    def __init__(self, _: ProviderConfig) -> None:
        self.turn = 0

    def __enter__(self) -> ScriptedResponsesClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
        self.requests.append(payload)
        self.turn += 1
        if self.turn == 1:
            return ResponseTurn(
                "compat-first",
                [
                    {
                        "type": "function_call",
                        "id": "fc-compat",
                        "call_id": "call-compat",
                        "name": "harness_compat_echo",
                        "arguments": '{"value":"SPREADSHEET_HARNESS_CANARY_7B19"}',
                    }
                ],
                "",
                {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            )
        return ResponseTurn(
            "compat-second",
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "SPREADSHEET_HARNESS_TOOLS_OK",
                        }
                    ],
                }
            ],
            "SPREADSHEET_HARNESS_TOOLS_OK",
            {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
        )


class ScriptedChatClient(ScriptedResponsesClient):
    requests: list[dict[str, Any]] = []

    def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
        self.requests.append(payload)
        self.turn += 1
        if self.turn == 1:
            return ResponseTurn(
                "compat-chat-first",
                [
                    {
                        "type": "function_call",
                        "id": "fc-chat-echo",
                        "call_id": "call-chat-echo",
                        "name": "harness_compat_echo",
                        "arguments": '{"value":"SPREADSHEET_HARNESS_CANARY_7B19"}',
                    }
                ],
                "",
                {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            )
        return ResponseTurn(
            "compat-chat-second",
            [
                {
                    "type": "function_call",
                    "id": "fc-chat-submit",
                    "call_id": "call-chat-submit",
                    "name": "harness_compat_submit",
                    "arguments": '{"result":"SPREADSHEET_HARNESS_TOOLS_OK"}',
                }
            ],
            "",
            {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
        )


def _config(api_key: str = "not-a-real-key") -> ProviderConfig:
    return ProviderConfig(
        "https://example.test/v1",
        api_key,
        "Qwen/Qwen3.5-35B-A3B",
        reasoning_effort="none",
        max_retries=0,
    )


def test_tool_compatibility_canary_forces_and_replays_function_call(
    monkeypatch: Any,
) -> None:
    ScriptedResponsesClient.requests = []
    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.ResponsesClient", ScriptedResponsesClient
    )

    report = check_responses_tool_compatibility(_config())

    assert report == {
        "ok": True,
        "protocol": "responses_function_call_v1",
        "endpoint": "/responses",
        "forced_function_call": True,
        "call_id_replayed": True,
        "function_call_output_consumed": True,
        "terminal_text": True,
        "requests": 2,
        "generation": {},
        "usage": {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37},
    }
    assert len(ScriptedResponsesClient.requests) == 2
    first, second = ScriptedResponsesClient.requests
    assert first["tool_choice"] == {
        "type": "function",
        "name": "harness_compat_echo",
    }
    assert first["parallel_tool_calls"] is False
    assert first["reasoning"] == {"effort": "none"}
    replay = next(
        item for item in second["input"] if item.get("type") == "function_call_output"
    )
    assert replay["call_id"] == "call-compat"
    assert json.loads(replay["output"]) == {
        "ok": True,
        "value": "SPREADSHEET_HARNESS_CANARY_7B19",
    }
    assert second["tool_choice"] == "auto"


def test_chat_tool_compatibility_canary_uses_chat_client(monkeypatch: Any) -> None:
    ScriptedChatClient.requests = []
    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.ChatCompletionsClient",
        ScriptedChatClient,
    )
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "qwen36-35b-a3b",
        api_protocol="chat-completions",
        reasoning_effort="none",
        max_retries=0,
    )

    report = check_chat_completions_tool_compatibility(config)

    assert report == {
        "ok": True,
        "protocol": "chat_completions_tool_calls_v2",
        "endpoint": "/chat/completions",
        "forced_function_call": True,
        "call_id_replayed": True,
        "function_call_output_consumed": True,
        "terminal_function_call": True,
        "requests": 2,
        "generation": {},
        "usage": {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37},
    }
    first, second = ScriptedChatClient.requests
    assert first["tool_choice"] == {
        "type": "function",
        "name": "harness_compat_echo",
    }
    assert second["tools"] == [
        {
            "type": "function",
            "name": "harness_compat_submit",
            "description": "Submit the result of the compatibility check.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "enum": ["SPREADSHEET_HARNESS_TOOLS_OK"],
                    }
                },
                "required": ["result"],
                "additionalProperties": False,
            },
            "strict": False,
        }
    ]
    assert second["tool_choice"] == {
        "type": "function",
        "name": "harness_compat_submit",
    }
    replay = next(
        item for item in second["input"] if item.get("type") == "function_call_output"
    )
    assert replay["call_id"] == "call-chat-echo"


def test_chat_tool_compatibility_canary_rejects_wrong_terminal_arguments(
    monkeypatch: Any,
) -> None:
    class WrongTerminalArgumentsClient(ScriptedChatClient):
        def create(self, payload: dict[str, Any], **kwargs: Any) -> ResponseTurn:
            response = super().create(payload, **kwargs)
            if self.turn == 2:
                response.output[0]["arguments"] = '{"result":"wrong"}'
            return response

    WrongTerminalArgumentsClient.requests = []
    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.ChatCompletionsClient",
        WrongTerminalArgumentsClient,
    )

    with pytest.raises(HarnessError, match="terminal function arguments"):
        check_chat_completions_tool_compatibility(
            ProviderConfig(
                "https://example.test/v1",
                "not-a-real-key",
                "qwen36-35b-a3b",
                api_protocol="chat-completions",
            )
        )


@pytest.mark.parametrize(
    ("terminal_output", "error_match"),
    [
        (
            [{"type": "message", "role": "assistant", "content": []}],
            "exactly one forced terminal function call; provider returned 0",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call-submit-one",
                    "name": "harness_compat_submit",
                    "arguments": '{"result":"SPREADSHEET_HARNESS_TOOLS_OK"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call-submit-two",
                    "name": "harness_compat_submit",
                    "arguments": '{"result":"SPREADSHEET_HARNESS_TOOLS_OK"}',
                },
            ],
            "exactly one forced terminal function call; provider returned 2",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call-wrong-tool",
                    "name": "harness_compat_echo",
                    "arguments": '{"result":"SPREADSHEET_HARNESS_TOOLS_OK"}',
                }
            ],
            "forced harness_compat_submit",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call-malformed-arguments",
                    "name": "harness_compat_submit",
                    "arguments": "{not-json",
                }
            ],
            "terminal function arguments were not valid JSON",
        ),
    ],
)
def test_chat_tool_compatibility_canary_rejects_invalid_terminal_calls(
    monkeypatch: Any,
    terminal_output: list[dict[str, Any]],
    error_match: str,
) -> None:
    class InvalidTerminalCallClient(ScriptedChatClient):
        def create(self, payload: dict[str, Any], **kwargs: Any) -> ResponseTurn:
            response = super().create(payload, **kwargs)
            if self.turn == 2:
                response.output = terminal_output
            return response

    InvalidTerminalCallClient.requests = []
    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.ChatCompletionsClient",
        InvalidTerminalCallClient,
    )

    with pytest.raises(HarnessError, match=error_match):
        check_chat_completions_tool_compatibility(
            ProviderConfig(
                "https://example.test/v1",
                "not-a-real-key",
                "qwen36-35b-a3b",
                api_protocol="chat-completions",
            )
        )


def test_tool_compatibility_dispatches_by_protocol(monkeypatch: Any) -> None:
    calls: list[str] = []

    def responses(_: ProviderConfig) -> dict[str, Any]:
        calls.append("responses")
        return {"ok": True}

    def chat(_: ProviderConfig) -> dict[str, Any]:
        calls.append("chat")
        return {"ok": True}

    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.check_responses_tool_compatibility",
        responses,
    )
    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.check_chat_completions_tool_compatibility",
        chat,
    )

    check_tool_compatibility(_config())
    check_tool_compatibility(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "qwen36-35b-a3b",
            api_protocol="chat-completions",
        )
    )

    assert calls == ["responses", "chat"]


def test_tool_compatibility_canary_applies_generation_to_both_requests(
    monkeypatch: Any,
) -> None:
    ScriptedResponsesClient.requests = []
    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.ResponsesClient", ScriptedResponsesClient
    )
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "Qwen/Qwen3.5-35B-A3B",
        reasoning_effort="none",
        max_retries=0,
        temperature=1.0,
        top_k=40,
        min_p=0.0,
        enable_thinking=False,
    )

    report = check_responses_tool_compatibility(config)

    assert report["generation"] == {
        "temperature": 1.0,
        "top_k": 40,
        "min_p": 0.0,
        "enable_thinking": False,
    }
    for request in ScriptedResponsesClient.requests:
        assert request["temperature"] == 1.0
        assert request["extra_body"] == {
            "top_k": 40,
            "min_p": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }


def test_tool_compatibility_canary_rejects_text_instead_of_forced_call(
    monkeypatch: Any,
) -> None:
    class TextOnlyClient(ScriptedResponsesClient):
        def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
            return ResponseTurn(
                "text-only",
                [{"type": "message", "content": []}],
                "I cannot call tools",
                {},
            )

    monkeypatch.setattr("spreadsheet_harness.provider_compat.ResponsesClient", TextOnlyClient)

    with pytest.raises(HarnessError, match="exactly one forced function call"):
        check_responses_tool_compatibility(_config())


def test_tool_compatibility_canary_explains_missing_responses_endpoint_and_redacts_key(
    monkeypatch: Any,
) -> None:
    secret = "sk-providercompat123456789"

    class UnsupportedResponsesClient(ScriptedResponsesClient):
        def create(self, payload: dict[str, Any], **_: Any) -> ResponseTurn:
            raise ProviderError(
                f"Responses API returned HTTP 404 for {secret}",
                status_code=404,
                global_fatal=True,
            )

    monkeypatch.setattr(
        "spreadsheet_harness.provider_compat.ResponsesClient", UnsupportedResponsesClient
    )

    with pytest.raises(HarnessError, match=r"POST /responses") as caught:
        check_responses_tool_compatibility(_config(secret))

    assert secret not in str(caught.value)


def test_doctor_tools_requires_explicit_online_flag() -> None:
    args = build_parser().parse_args(["doctor", "--tools"])

    with pytest.raises(HarnessError, match="requires --online"):
        cmd_doctor(args)
