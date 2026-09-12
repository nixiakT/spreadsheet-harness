#!/usr/bin/env python3
"""Small local Responses proxy used by the Codex SpreadsheetBench runner.

Codex owns the agent loop.  This proxy only forwards the Responses request to
LiteLLM and adds the sampling fields that the Codex CLI does not expose as
flags.  Request bodies are never logged; only non-sensitive request metadata
and a payload digest are written to the optional JSONL audit file.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from spreadsheet_harness.agent import _chat_wire_payload


def _to_chat_payload(
    payload: dict[str, object], *, max_output_tokens: int = 32768
) -> dict[str, object]:
    source = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "chat_template_kwargs",
            "client_metadata",
            "prompt_cache_key",
            "store",
            "stream",
        }
    }
    source["extra_body"] = {
        "chat_template_kwargs": payload.get(
            "chat_template_kwargs", {"enable_thinking": True}
        )
    }
    chat = _chat_wire_payload(source)  # type: ignore[arg-type]
    chat["stream"] = False
    chat["max_tokens"] = max_output_tokens
    return chat


def _response_metadata(chat_response: dict[str, object]) -> dict[str, object]:
    choices = chat_response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}

    content = message.get("content")
    if isinstance(content, list):
        content_chars = sum(
            len(str(part.get("text", "")))
            for part in content
            if isinstance(part, dict)
        )
    else:
        content_chars = len(str(content or ""))
    reasoning = message.get("reasoning_content", message.get("reasoning", ""))
    reasoning_chars = len(
        reasoning
        if isinstance(reasoning, str)
        else json.dumps(reasoning, ensure_ascii=False, separators=(",", ":"))
    ) if reasoning else 0
    tool_calls = message.get("tool_calls")
    tool_call_count = len(tool_calls) if isinstance(tool_calls, list) else 0

    usage = chat_response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    return {
        "finish_reason": choice.get("finish_reason"),
        "response_content_chars": content_chars,
        "response_reasoning_chars": reasoning_chars,
        "response_tool_call_count": tool_call_count,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "reasoning_tokens": int(completion_details.get("reasoning_tokens", 0) or 0),
    }


def _responses_sse(chat_response: dict[str, object], requested_model: str) -> bytes:
    choices = chat_response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("chat response has no choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("chat response has no assistant message")
    response_id = "resp_" + uuid.uuid4().hex
    created_at = int(time.time())
    output: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    base_response: dict[str, object] = {
        "id": response_id,
        "created_at": created_at,
        "model": requested_model,
        "object": "response",
        "output": [],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "status": "in_progress",
        "store": False,
    }
    events.append({"type": "response.created", "response": base_response, "model": requested_model})
    events.append({"type": "response.in_progress", "response": base_response, "model": requested_model})

    content = message.get("content")
    if isinstance(content, list):
        text = "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    else:
        text = str(content or "")
    raw_tool_calls = message.get("tool_calls")
    tool_calls = [
        call
        for call in raw_tool_calls
        if isinstance(call, dict) and isinstance(call.get("function"), dict)
    ] if isinstance(raw_tool_calls, list) else []
    output_index = 0
    # Emit assistant prose before function calls. Codex preserves Responses
    # item order, and the chat replay must keep the tool-call assistant message
    # immediately adjacent to its tool result.
    if text or not tool_calls:
        item_id = "msg_" + uuid.uuid4().hex
        pending_message = {
            "id": item_id,
            "status": "in_progress",
            "type": "message",
            "role": "assistant",
            "content": [],
        }
        content_part = {"type": "output_text", "text": text, "annotations": []}
        done_message = dict(pending_message, status="completed", content=[content_part])
        events.append({"type": "response.output_item.added", "output_index": output_index, "item": pending_message, "model": requested_model})
        events.append({"type": "response.content_part.added", "item_id": item_id, "output_index": output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}, "model": requested_model})
        if text:
            events.append({"type": "response.output_text.delta", "item_id": item_id, "output_index": output_index, "content_index": 0, "delta": text, "model": requested_model})
        events.extend(
            [
                {"type": "response.output_text.done", "item_id": item_id, "output_index": output_index, "content_index": 0, "text": text, "model": requested_model},
                {"type": "response.content_part.done", "item_id": item_id, "output_index": output_index, "content_index": 0, "part": content_part, "model": requested_model},
                {"type": "response.output_item.done", "output_index": output_index, "item": done_message, "model": requested_model},
            ]
        )
        output.append(done_message)
        output_index += 1

    for call in tool_calls:
        function = call["function"]
        call_id = str(call.get("id") or ("call_" + uuid.uuid4().hex))
        name = str(function.get("name", ""))
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        pending = {
            "type": "function_call",
            "id": call_id,
            "call_id": call_id,
            "name": name,
            "status": "in_progress",
            "arguments": "",
        }
        done = dict(pending, status="completed", arguments=arguments)
        events.extend(
            [
                {"type": "response.output_item.added", "output_index": output_index, "item": pending, "model": requested_model},
                {"type": "response.function_call_arguments.delta", "item_id": call_id, "output_index": output_index, "delta": arguments, "model": requested_model},
                {"type": "response.function_call_arguments.done", "item_id": call_id, "output_index": output_index, "arguments": arguments, "model": requested_model},
                {"type": "response.output_item.done", "output_index": output_index, "item": done, "model": requested_model},
            ]
        )
        output.append(done)
        output_index += 1

    raw_usage = chat_response.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    input_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
    usage = {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": int(
                (raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                if isinstance(raw_usage.get("completion_tokens_details"), dict)
                else 0
            )
        },
        "total_tokens": int(raw_usage.get("total_tokens", input_tokens + output_tokens) or 0),
    }
    completed = dict(base_response, output=output, status="completed", usage=usage)
    events.append({"type": "response.completed", "response": completed, "model": requested_model})
    return ("".join("data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-responses-proxy/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def settings(self) -> argparse.Namespace:
        return self.server.settings  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health", "/v1/health"}:
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            payload["temperature"] = 0.0
            payload["top_p"] = 1.0
            template_kwargs = payload.get("chat_template_kwargs", {})
            if not isinstance(template_kwargs, dict):
                template_kwargs = {}
            template_kwargs = dict(template_kwargs)
            template_kwargs["enable_thinking"] = True
            payload["chat_template_kwargs"] = template_kwargs
            # Codex's fallback metadata uses OpenAI-specific function-tool and
            # item variants that this LiteLLM deployment does not normalize
            # reliably for DeepSeek. Keep the request contract in the common
            # Responses subset without changing the agent loop itself.
            payload.pop("reasoning", None)
            payload.pop("include", None)
            tools = payload.get("tools")
            if isinstance(tools, list):
                normalized_tools = []
                for tool in tools:
                    if not isinstance(tool, dict) or tool.get("type") != "function":
                        continue
                    tool = dict(tool)
                    tool.pop("strict", None)
                    normalized_tools.append(tool)
                payload["tools"] = normalized_tools
            responses_body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
            chat_payload = _to_chat_payload(
                payload, max_output_tokens=self.settings.max_output_tokens
            )
            body = json.dumps(
                chat_payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
        except Exception as exc:  # pragma: no cover - exercised by remote clients
            self._json_error(400, f"invalid JSON request: {exc}")
            return

        request_id = hashlib.sha256(body).hexdigest()
        parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
        task_key = (
            unquote(parts[1])
            if len(parts) >= 2 and parts[0] == "task"
            else "unscoped"
        )
        with self.settings.counter_lock:
            request_index = self.settings.task_counts.get(task_key, 0) + 1
            self.settings.task_counts[task_key] = request_index
        if request_index > self.settings.max_requests:
            self._audit(
                {
                    "timestamp": time.time(),
                    "path": self.path,
                    "task_key": task_key,
                    "request_index": request_index,
                    "max_requests": self.settings.max_requests,
                    "limit_exceeded": True,
                }
            )
            self._json_error(400, f"model request limit exceeded for {task_key}")
            return
        self._audit(
            {
                "timestamp": time.time(),
                "path": self.path,
                "model": payload.get("model"),
                "temperature": payload.get("temperature"),
                "top_p": payload.get("top_p"),
                "enable_thinking": template_kwargs.get("enable_thinking"),
                "stream": payload.get("stream"),
                "payload_sha256": request_id,
                "responses_payload_sha256": hashlib.sha256(responses_body).hexdigest(),
                "body_bytes": len(body),
                "task_key": task_key,
                "request_index": request_index,
                "max_requests": self.settings.max_requests,
                "max_output_tokens": chat_payload.get("max_tokens"),
                "top_level_keys": sorted(payload),
                "input_item_types": [
                    item.get("type")
                    for item in payload.get("input", [])
                    if isinstance(item, dict)
                ],
                "tool_types": [
                    item.get("type")
                    for item in payload.get("tools", [])
                    if isinstance(item, dict)
                ],
                "tool_count": len(payload.get("tools", [])) if isinstance(payload.get("tools"), list) else None,
                "tool_names": [
                    item.get("name")
                    for item in payload.get("tools", [])
                    if isinstance(item, dict)
                ],
            }
        )

        target = urlsplit(self.settings.upstream)
        path = target.path.rstrip("/") + "/chat/completions"
        if target.query:
            path += "?" + target.query
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": self.headers.get("Authorization", ""),
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
        if self.headers.get("x-litellm-timeout"):
            headers["x-litellm-timeout"] = self.headers["x-litellm-timeout"]
        connection: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            for attempt in range(1, self.settings.retries + 2):
                with self.settings.pace_lock:
                    wait = self.settings.next_upstream_at - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    self.settings.next_upstream_at = time.monotonic() + self.settings.interval
                connection = http.client.HTTPConnection(
                    target.hostname, target.port, timeout=self.settings.timeout
                )
                connection.request("POST", path, body=body, headers=headers)
                response = connection.getresponse()
                self._audit(
                    {
                        "timestamp": time.time(),
                        "task_key": task_key,
                        "request_index": request_index,
                        "payload_sha256": request_id,
                        "upstream_path": path,
                        "upstream_status": response.status,
                        "upstream_attempt": attempt,
                    }
                )
                retryable = response.status in {404, 425, 429, 503} or response.status >= 500
                if not retryable or attempt > self.settings.retries:
                    break
                response.read()
                connection.close()
                response = None
                connection = None
                time.sleep(min(16.0, float(2 ** (attempt - 1))))
            assert response is not None
            upstream_body = response.read()
            if response.status == 200:
                chat_response = json.loads(upstream_body)
                self._audit(
                    {
                        "timestamp": time.time(),
                        "task_key": task_key,
                        "request_index": request_index,
                        "payload_sha256": request_id,
                        "response_bytes": len(upstream_body),
                        **_response_metadata(chat_response),
                    }
                )
                bridged = _responses_sse(
                    chat_response, str(payload.get("model", ""))
                )
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(bridged)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(bridged)
                self.wfile.flush()
                return
            self.send_response(response.status, response.reason)
            hop_by_hop = {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date"}
            for name, value in response.getheaders():
                if name.lower() not in hop_by_hop:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(upstream_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(upstream_body)
            self.wfile.flush()
        except Exception as exc:  # pragma: no cover - network dependent
            self._audit({"timestamp": time.time(), "payload_sha256": request_id, "error": type(exc).__name__})
            try:
                self._json_error(502, f"upstream error: {type(exc).__name__}")
            except Exception:
                pass
        finally:
            if connection is not None:
                connection.close()
            self.close_connection = True

    def _audit(self, entry: dict[str, object]) -> None:
        path = self.settings.audit
        if not path:
            return
        line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self.settings.audit_lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message}}, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--audit")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--max-requests", type=int, default=50)
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.1)
    settings = parser.parse_args()
    settings.audit_lock = threading.Lock()
    settings.counter_lock = threading.Lock()
    settings.pace_lock = threading.Lock()
    settings.task_counts = {}
    if settings.audit:
        try:
            with open(settings.audit, encoding="utf-8") as handle:
                for line in handle:
                    event = json.loads(line)
                    task_key = event.get("task_key")
                    request_index = event.get("request_index")
                    if isinstance(task_key, str) and isinstance(request_index, int):
                        task_key = unquote(task_key)
                        settings.task_counts[task_key] = max(
                            settings.task_counts.get(task_key, 0), request_index
                        )
        except FileNotFoundError:
            pass
    settings.next_upstream_at = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", settings.port), ProxyHandler)
    server.settings = settings  # type: ignore[attr-defined]
    print(json.dumps({"listening": settings.port, "upstream": settings.upstream}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
