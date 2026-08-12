"""Provider compatibility probes that never open or mutate a workbook."""

from __future__ import annotations

import json
from typing import Any

from .agent import ChatCompletionsClient, ResponsesClient, _validated_function_calls
from .config import ProviderConfig
from .errors import HarnessError, ProviderError

_CANARY_TOOL_NAME = "harness_compat_echo"
_CANARY_VALUE = "SPREADSHEET_HARNESS_CANARY_7B19"
_CANARY_RESULT = "SPREADSHEET_HARNESS_TOOLS_OK"
_CANARY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": _CANARY_TOOL_NAME,
    "description": "Return the supplied compatibility token unchanged.",
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "string", "enum": [_CANARY_VALUE]}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "strict": False,
}


def _usage(value: dict[str, Any]) -> dict[str, int]:
    return {
        name: int(value.get(name, 0) or 0)
        for name in ("input_tokens", "output_tokens", "total_tokens")
    }


def check_responses_tool_compatibility(config: ProviderConfig) -> dict[str, Any]:
    """Exercise the exact Responses tool-call round trip used by the harness.

    The only "tool result" is an in-memory constant. No local tool is invoked and no
    workbook or working directory is created.
    """

    first_payload: dict[str, Any] = config.apply_generation({
        "model": config.model,
        "instructions": (
            "This is a protocol compatibility check. Call the supplied function exactly "
            "once with the only permitted value. Do not answer with prose."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Call harness_compat_echo now.",
                    }
                ],
            }
        ],
        "tools": [_CANARY_TOOL_SCHEMA],
        "tool_choice": {"type": "function", "name": _CANARY_TOOL_NAME},
        "parallel_tool_calls": False,
        "reasoning": {"effort": config.reasoning_effort},
        "max_output_tokens": 128,
    })
    try:
        with ResponsesClient(config) as client:
            first = client.create(first_payload)
            calls = _validated_function_calls(first.output)
            if len(calls) != 1:
                raise HarnessError(
                    "Responses compatibility check required exactly one forced function call; "
                    f"provider returned {len(calls)}"
                )
            function_call, call_id = calls[0]
            if function_call.get("name") != _CANARY_TOOL_NAME:
                raise HarnessError(
                    "Responses compatibility check forced harness_compat_echo but provider "
                    f"returned {function_call.get('name')!r}"
                )
            raw_arguments = function_call.get("arguments", "{}")
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
            except json.JSONDecodeError as exc:
                raise HarnessError(
                    "Responses compatibility function arguments were not valid JSON"
                ) from exc
            if arguments != {"value": _CANARY_VALUE}:
                raise HarnessError(
                    "Responses compatibility function arguments did not match the forced schema"
                )

            second_payload: dict[str, Any] = config.apply_generation({
                "model": config.model,
                "instructions": (
                    "This is a protocol compatibility check. After receiving the function "
                    f"output, reply exactly {_CANARY_RESULT}."
                ),
                "input": [
                    *first_payload["input"],
                    *first.output,
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            {"ok": True, "value": _CANARY_VALUE},
                            separators=(",", ":"),
                        ),
                    },
                ],
                "tools": [_CANARY_TOOL_SCHEMA],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "reasoning": {"effort": config.reasoning_effort},
                "max_output_tokens": 64,
            })
            second = client.create(second_payload)
            follow_up_calls = _validated_function_calls(second.output)
    except ProviderError as exc:
        detail = exc.public_dict(secrets=(config.api_key,))
        raise HarnessError(
            "Responses tool compatibility check failed. The harness requires POST "
            f"/responses with function_call/call_id replay support: {detail['message']}"
        ) from exc

    if follow_up_calls:
        raise HarnessError(
            "Responses compatibility follow-up returned another function call instead of "
            "terminal assistant text"
        )
    if second.text.strip() != _CANARY_RESULT:
        raise HarnessError(
            "Responses compatibility follow-up did not consume function_call_output and return "
            f"the required terminal text; received {second.text[:200]!r}"
        )

    return {
        "ok": True,
        "protocol": "responses_function_call_v1",
        "endpoint": "/responses",
        "forced_function_call": True,
        "call_id_replayed": True,
        "function_call_output_consumed": True,
        "terminal_text": True,
        "requests": 2,
        "generation": config.generation_dict(),
        "usage": {
            name: _usage(first.usage)[name] + _usage(second.usage)[name]
            for name in ("input_tokens", "output_tokens", "total_tokens")
        },
    }


def check_chat_completions_tool_compatibility(config: ProviderConfig) -> dict[str, Any]:
    """Exercise the Chat Completions tool-call round trip used by LiteLLM."""

    first_payload: dict[str, Any] = config.apply_generation({
        "model": config.model,
        "instructions": (
            "This is a protocol compatibility check. Call the supplied function exactly "
            "once with the only permitted value. Do not answer with prose."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Call harness_compat_echo now.",
                    }
                ],
            }
        ],
        "tools": [_CANARY_TOOL_SCHEMA],
        "tool_choice": {"type": "function", "name": _CANARY_TOOL_NAME},
        "parallel_tool_calls": False,
        "reasoning": {"effort": config.reasoning_effort},
        "max_output_tokens": 128,
    })
    try:
        with ChatCompletionsClient(config) as client:
            first = client.create(first_payload)
            calls = _validated_function_calls(first.output)
            if len(calls) != 1:
                raise HarnessError(
                    "Chat Completions compatibility check required exactly one forced "
                    f"function call; provider returned {len(calls)}"
                )
            function_call, call_id = calls[0]
            if function_call.get("name") != _CANARY_TOOL_NAME:
                raise HarnessError(
                    "Chat Completions compatibility check forced harness_compat_echo but "
                    f"provider returned {function_call.get('name')!r}"
                )
            raw_arguments = function_call.get("arguments", "{}")
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
            except json.JSONDecodeError as exc:
                raise HarnessError(
                    "Chat Completions compatibility function arguments were not valid JSON"
                ) from exc
            if arguments != {"value": _CANARY_VALUE}:
                raise HarnessError(
                    "Chat Completions compatibility function arguments did not match "
                    "the forced schema"
                )

            second_payload: dict[str, Any] = config.apply_generation({
                "model": config.model,
                "instructions": (
                    "This is a protocol compatibility check. After receiving the function "
                    f"output, reply exactly {_CANARY_RESULT}."
                ),
                "input": [
                    *first_payload["input"],
                    *first.output,
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            {"ok": True, "value": _CANARY_VALUE},
                            separators=(",", ":"),
                        ),
                    },
                ],
                "tools": [_CANARY_TOOL_SCHEMA],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "reasoning": {"effort": config.reasoning_effort},
                "max_output_tokens": 64,
            })
            second = client.create(second_payload)
            follow_up_calls = _validated_function_calls(second.output)
    except ProviderError as exc:
        detail = exc.public_dict(secrets=(config.api_key,))
        raise HarnessError(
            "Chat Completions tool compatibility check failed. The harness requires "
            "POST /chat/completions with tool_calls/tool result replay support: "
            f"{detail['message']}"
        ) from exc

    if follow_up_calls:
        raise HarnessError(
            "Chat Completions compatibility follow-up returned another function call "
            "instead of terminal assistant text"
        )
    if second.text.strip() != _CANARY_RESULT:
        raise HarnessError(
            "Chat Completions compatibility follow-up did not consume tool output and "
            f"return the required terminal text; received {second.text[:200]!r}"
        )

    return {
        "ok": True,
        "protocol": "chat_completions_tool_calls_v1",
        "endpoint": "/chat/completions",
        "forced_function_call": True,
        "call_id_replayed": True,
        "function_call_output_consumed": True,
        "terminal_text": True,
        "requests": 2,
        "generation": config.generation_dict(),
        "usage": {
            name: _usage(first.usage)[name] + _usage(second.usage)[name]
            for name in ("input_tokens", "output_tokens", "total_tokens")
        },
    }


def check_tool_compatibility(config: ProviderConfig) -> dict[str, Any]:
    if config.api_protocol == "responses":
        return check_responses_tool_compatibility(config)
    if config.api_protocol == "chat-completions":
        return check_chat_completions_tool_compatibility(config)
    raise HarnessError(f"Unsupported API protocol {config.api_protocol!r}")
