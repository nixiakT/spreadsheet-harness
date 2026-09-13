#!/usr/bin/env python3
"""Transparent Chat Completions proxy for official DeepSeek Harness runs.

The official ``dsh`` process owns the agent loop, tools, sessions, and skill
loading.  This proxy only pins sampling fields that the headless CLI does not
expose, enforces a per-SpreadsheetBench-task model-request ceiling, forwards
the streaming response unchanged, and records non-secret request metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _task_key(path: str) -> str:
    parts = [part for part in urlsplit(path).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "task":
        return unquote(parts[1])
    return "unscoped"


class DshProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dsh-chat-completions-proxy/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def settings(self) -> argparse.Namespace:
        return self.server.settings  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path.rstrip("/") in {"", "/health", "/v1/health"}:
            self._send_json(200, {"status": "ok"})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            payload["temperature"] = 0.0
            payload["top_p"] = 1.0
            template_kwargs = payload.get("chat_template_kwargs")
            if not isinstance(template_kwargs, dict):
                template_kwargs = {}
            template_kwargs = dict(template_kwargs)
            template_kwargs["enable_thinking"] = True
            payload["chat_template_kwargs"] = template_kwargs
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
        except Exception as exc:
            self._send_json(400, {"error": {"message": f"invalid request: {exc}"}})
            return

        task_key = _task_key(self.path)
        payload_sha256 = hashlib.sha256(body).hexdigest()
        with self.settings.counter_lock:
            completed_count = self.settings.task_counts.get(task_key, 0)
            request_index = self.settings.request_sequences.get(task_key, 0) + 1
            self.settings.request_sequences[task_key] = request_index
        base_audit: dict[str, object] = {
            "event": "request",
            "timestamp": time.time(),
            "task_key": task_key,
            "request_index": request_index,
            "max_requests": self.settings.max_requests,
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "top_p": payload.get("top_p"),
            "enable_thinking": template_kwargs.get("enable_thinking"),
            "stream": payload.get("stream"),
            "payload_sha256": payload_sha256,
            "body_bytes": len(body),
            "message_count": (
                len(payload.get("messages", []))
                if isinstance(payload.get("messages"), list)
                else None
            ),
            "tool_count": (
                len(payload.get("tools", []))
                if isinstance(payload.get("tools"), list)
                else None
            ),
            "tool_names": [
                tool.get("function", {}).get("name")
                for tool in payload.get("tools", [])
                if isinstance(tool, dict)
                and isinstance(tool.get("function"), dict)
                and tool.get("function", {}).get("name")
            ],
        }
        if completed_count >= self.settings.max_requests:
            self._audit({**base_audit, "accepted": False, "limit_exceeded": True})
            self._send_json(
                429,
                {
                    "error": {
                        "message": f"model request limit exceeded for {task_key}"
                    }
                },
            )
            return
        self._audit({**base_audit, "accepted": True})

        # Pace each task independently.  Parallel benchmark workers remain
        # parallel, while a single conversation does not hammer the gateway.
        with self.settings.pace_lock:
            now = time.monotonic()
            wait = max(0.0, self.settings.next_upstream_at.get(task_key, 0.0) - now)
            self.settings.next_upstream_at[task_key] = max(
                now, self.settings.next_upstream_at.get(task_key, 0.0)
            ) + self.settings.interval
        if wait:
            time.sleep(wait)

        target = urlsplit(self.settings.upstream)
        upstream_path = target.path.rstrip("/") + "/chat/completions"
        if target.query:
            upstream_path += "?" + target.query
        headers = {
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "text/event-stream"),
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
        for name in (
            "user-agent",
            "x-app-name",
            "x-app-version",
            "x-client-request-id",
            "x-session-affinity",
            "x-litellm-timeout",
        ):
            value = self.headers.get(name)
            if value:
                headers[name] = value

        connection: http.client.HTTPConnection | None = None
        try:
            # LiteLLM may return a transient 429 while its deployment is in
            # token-per-minute cooldown.  Keep the DSH request open and retry
            # only that response with bounded backoff, so DSH's own agent loop
            # does not mistake gateway throttling for a failed task.  A retry
            # is one logical DSH model request and does not consume another
            # per-task turn in the audit counter above.
            connection_cls = (
                http.client.HTTPSConnection
                if target.scheme == "https"
                else http.client.HTTPConnection
            )
            retry_delays = (5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 60.0, 60.0)
            response = None
            upstream_body = b""
            for attempt in range(len(retry_delays) + 1):
                connection = connection_cls(
                    target.hostname, target.port, timeout=self.settings.timeout
                )
                connection.request("POST", upstream_path, body=body, headers=headers)
                response = connection.getresponse()
                upstream_body = response.read()
                if response.status != 429 or attempt >= len(retry_delays):
                    break
                self._audit(
                    {
                        "event": "upstream_retry",
                        "timestamp": time.time(),
                        "task_key": task_key,
                        "request_index": request_index,
                        "attempt": attempt + 1,
                        "upstream_status": response.status,
                        "retry_seconds": retry_delays[attempt],
                        "payload_sha256": payload_sha256,
                    }
                )
                connection.close()
                connection = None
                time.sleep(retry_delays[attempt])
            assert response is not None
            self.send_response(response.status, response.reason)
            hop_by_hop = {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
                "content-length",
                "server",
                "date",
            }
            for name, value in response.getheaders():
                if name.lower() not in hop_by_hop:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(upstream_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(upstream_body)
            self.wfile.flush()
            self._audit(
                {
                    "event": "response",
                    "timestamp": time.time(),
                    "task_key": task_key,
                    "request_index": request_index,
                    "payload_sha256": payload_sha256,
                    "upstream_status": response.status,
                    "response_bytes": len(upstream_body),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                }
            )
            if 200 <= response.status < 300:
                with self.settings.counter_lock:
                    self.settings.task_counts[task_key] = (
                        self.settings.task_counts.get(task_key, 0) + 1
                    )
        except Exception as exc:  # pragma: no cover - network dependent
            self._audit(
                {
                    "event": "response",
                    "timestamp": time.time(),
                    "task_key": task_key,
                    "request_index": request_index,
                    "payload_sha256": payload_sha256,
                    "error": type(exc).__name__,
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                }
            )
            try:
                self._send_json(
                    502,
                    {"error": {"message": f"upstream error: {type(exc).__name__}"}},
                )
            except Exception:
                pass
        finally:
            if connection is not None:
                connection.close()
            self.close_connection = True

    def _audit(self, entry: dict[str, object]) -> None:
        if self.settings.audit is None:
            return
        line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self.settings.audit_lock:
            with self.settings.audit.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode()
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
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--max-requests", type=int, default=50)
    parser.add_argument("--interval", type=float, default=1.1)
    settings = parser.parse_args()
    settings.api_key = settings.api_key_file.read_text(encoding="utf-8").strip()
    if not settings.api_key:
        raise SystemExit("API key file is empty")
    if settings.max_requests < 1 or settings.interval < 0 or settings.timeout <= 0:
        raise SystemExit("invalid request limit, interval, or timeout")
    settings.audit_lock = threading.Lock()
    settings.counter_lock = threading.Lock()
    settings.pace_lock = threading.Lock()
    settings.task_counts: dict[str, int] = {}
    settings.request_sequences: dict[str, int] = {}
    settings.next_upstream_at: dict[str, float] = {}
    # Restore only requests that actually received a successful upstream
    # response.  Historical 429s are gateway failures, not consumed DSH turns.
    successful: dict[str, int] = {}
    if settings.audit is not None and settings.audit.is_file():
        for line in settings.audit.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            task_key = event.get("task_key")
            if not isinstance(task_key, str):
                continue
            index = event.get("request_index")
            if isinstance(index, int):
                settings.request_sequences[task_key] = max(
                    settings.request_sequences.get(task_key, 0), index
                )
            if (
                event.get("event") == "response"
                and isinstance(event.get("upstream_status"), int)
                and 200 <= event["upstream_status"] < 300
            ):
                successful[task_key] = successful.get(task_key, 0) + 1
        settings.task_counts.update(successful)
    server = ThreadingHTTPServer(("127.0.0.1", settings.port), DshProxyHandler)
    server.settings = settings  # type: ignore[attr-defined]
    print(
        json.dumps({"listening": settings.port, "upstream": settings.upstream}),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
