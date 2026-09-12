#!/usr/bin/env python3
"""Tiny local proxy for vLLM's named-tool finish_reason convention.

vLLM intentionally returns ``finish_reason=stop`` for named tool_choice even
when ``message.tool_calls`` is present.  The spreadsheet harness uses the
OpenAI convention ``tool_calls``.  This proxy only normalizes that one field;
all request/response content is otherwise passed through unchanged.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


class Handler(BaseHTTPRequestHandler):
    upstream = "http://127.0.0.1:8625"

    def _forward(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        req = Request(self.upstream + self.path, data=body, method=self.command)
        for key, value in self.headers.items():
            if key.lower() not in {"host", "content-length", "connection"}:
                req.add_header(key, value)
        try:
            with urlopen(req, timeout=900) as resp:
                payload = resp.read()
                status = resp.status
                headers = dict(resp.headers.items())
        except Exception as exc:
            self.send_error(502, str(exc))
            return
        if self.path.endswith("/chat/completions"):
            try:
                data = json.loads(payload)
                for choice in data.get("choices", []):
                    msg = choice.get("message", {})
                    if msg.get("tool_calls") and choice.get("finish_reason") == "stop":
                        choice["finish_reason"] = "tool_calls"
                payload = json.dumps(data, ensure_ascii=False).encode()
                headers["Content-Length"] = str(len(payload))
            except Exception:
                pass
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def log_message(self, *_args):
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default="http://127.0.0.1:8625")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8626)
    args = ap.parse_args()
    Handler.upstream = args.upstream.rstrip("/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
