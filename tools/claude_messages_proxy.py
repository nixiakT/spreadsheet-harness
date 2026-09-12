#!/usr/bin/env python3
"""Small Anthropic Messages proxy for the Claude Code SpreadsheetBench runner.

Claude Code owns the agent/tool loop.  This proxy only forwards Anthropic
Messages requests to LiteLLM, pins the requested sampling settings, enforces a
per-task request ceiling, and writes non-sensitive audit metadata.
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


class MessagesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-messages-proxy/1"

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
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            # LiteLLM accepts these common sampling fields on the Anthropic
            # compatibility endpoint.  The thinking budget is intentionally
            # pinned to the requested 50-token configuration.
            payload["temperature"] = 0.0
            payload["top_p"] = 1.0
            payload["thinking"] = {"type": "enabled", "budget_tokens": 50}
            payload["max_tokens"] = self.settings.max_output_tokens
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        except Exception as exc:  # pragma: no cover - network dependent
            self._send_json(400, {"error": {"message": f"invalid JSON request: {exc}"}})
            return

        task_key = _task_key(self.path)
        request_id = hashlib.sha256(body).hexdigest()
        with self.settings.counter_lock:
            request_index = self.settings.task_counts.get(task_key, 0) + 1
            self.settings.task_counts[task_key] = request_index
        base_audit = {
            "timestamp": time.time(),
            "task_key": task_key,
            "request_index": request_index,
            "max_requests": self.settings.max_requests,
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "top_p": payload.get("top_p"),
            "thinking": payload.get("thinking"),
            "payload_sha256": request_id,
            "body_bytes": len(body),
            "message_count": len(payload.get("messages", [])) if isinstance(payload.get("messages"), list) else None,
            "tool_count": len(payload.get("tools", [])) if isinstance(payload.get("tools"), list) else None,
            "tool_names": [
                item.get("name") for item in payload.get("tools", [])
                if isinstance(item, dict) and item.get("name")
            ],
        }
        if request_index > self.settings.max_requests:
            self._audit({**base_audit, "limit_exceeded": True})
            self._send_json(429, {"error": {"message": f"model request limit exceeded for {task_key}"}})
            return
        self._audit(base_audit)

        target = urlsplit(self.settings.upstream)
        upstream_path = target.path.rstrip("/") + "/messages"
        if target.query:
            upstream_path += "?" + target.query
        headers = {
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "application/json"),
            "Authorization": f"Bearer {self.settings.api_key}",
            "x-api-key": self.settings.api_key,
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
        for name in ("anthropic-version", "anthropic-beta"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        connection: http.client.HTTPConnection | None = None
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
                connection.request("POST", upstream_path, body=body, headers=headers)
                response = connection.getresponse()
                self._audit({**base_audit, "upstream_attempt": attempt, "upstream_status": response.status})
                retryable = response.status in {404, 408, 425, 429, 500, 502, 503, 504}
                if not retryable or attempt > self.settings.retries:
                    break
                response.read()
                connection.close()
                connection = None
                time.sleep(min(16.0, float(2 ** (attempt - 1))))
            upstream_body = response.read()
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
            self._audit({
                **base_audit,
                "response_bytes": len(upstream_body),
                "upstream_status": response.status,
            })
        except Exception as exc:  # pragma: no cover - network dependent
            self._audit({**base_audit, "error": type(exc).__name__})
            self._send_json(502, {"error": {"message": f"upstream error: {type(exc).__name__}"}})
        finally:
            if connection is not None:
                connection.close()
            self.close_connection = True

    def _audit(self, entry: dict[str, object]) -> None:
        path = self.settings.audit
        if not path:
            return
        with self.settings.audit_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n")

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
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.1)
    settings = parser.parse_args()
    settings.api_key = settings.api_key_file.read_text(encoding="utf-8").strip()
    if not settings.api_key:
        raise SystemExit("API key file is empty")
    settings.audit_lock = threading.Lock()
    settings.counter_lock = threading.Lock()
    settings.pace_lock = threading.Lock()
    settings.task_counts = {}
    settings.next_upstream_at = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", settings.port), MessagesHandler)
    server.settings = settings  # type: ignore[attr-defined]
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
