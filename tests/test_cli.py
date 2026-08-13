from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from spreadsheet_harness import cli
from spreadsheet_harness.agent import AgentResult, ResponsesClient
from spreadsheet_harness.config import ProviderConfig


def test_cmd_run_passes_provider_key_to_registry_redaction(
    sample_workbook: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    secret = "key://cli+tenant?signature=" + "C" * 128
    config = ProviderConfig("https://example.test/v1", secret, "test-model")
    captured: dict[str, Any] = {}

    class CapturingRegistry:
        def __init__(self, session: Any, **kwargs: Any) -> None:
            captured["session"] = session
            captured.update(kwargs)

    class OfflineAgent:
        def __init__(self, provider: ProviderConfig, tools: Any, **_: Any) -> None:
            assert provider is config
            assert tools is not None

        def run(self, _: str, **__: Any) -> AgentResult:
            return AgentResult("done", 1, 0, {}, "response")

    monkeypatch.setattr(cli, "_provider", lambda _: config)
    monkeypatch.setattr(cli, "SpreadsheetToolRegistry", CapturingRegistry)
    monkeypatch.setattr(cli, "SpreadsheetAgent", OfflineAgent)
    monkeypatch.setattr(cli, "find_libreoffice", lambda: None)
    monkeypatch.setattr(cli, "libreoffice_version", lambda _: None)
    args = cli.build_parser().parse_args(
        [
            "run",
            str(sample_workbook),
            "--instruction",
            "Make no changes",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--quiet",
        ]
    )

    assert cli.cmd_run(args) == 0
    assert captured["enable_code"] is True
    assert captured["redaction_secrets"] == (secret,)


def test_main_does_not_print_provider_echoed_configured_secret(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    secret = "key://cli+tenant?signature=" + "S" * 256 + "&scope=%2Fall"
    leaked_prefix = secret[:96]
    diagnostic = "x" * 3_872 + secret + " tail"

    def provider_client(config: ProviderConfig) -> ResponsesClient:
        client = ResponsesClient(config)
        client._client.close()
        event = {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "server_error",
                    "message": diagnostic,
                }
            },
        }
        body = f"data: {json.dumps(event)}\n\n"
        client._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text=body,
                    headers={"content-type": "text/event-stream"},
                )
            )
        )
        return client

    monkeypatch.setattr(cli, "_provider_client", provider_client)
    monkeypatch.setattr(cli, "find_libreoffice", lambda: None)

    exit_code = cli.main(
        [
            "doctor",
            "--online",
            "--base-url",
            "https://example.test/v1",
            "--api-key",
            secret,
            "--model",
            "test-model",
            "--request-retries",
            "0",
        ]
    )

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert exit_code == 1
    assert secret not in rendered
    assert leaked_prefix not in rendered
    assert "[REDACTED]" in rendered
    assert len(rendered) <= 5_000
