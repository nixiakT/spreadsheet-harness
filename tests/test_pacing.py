from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from spreadsheet_harness.agent import ResponsesClient, ResponseTurn
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import AgentTimeoutError, ProviderError
from spreadsheet_harness.pacing import PACING_POLICY, RelayPacer


def _fake_pacer(interval: float = 20.0) -> tuple[RelayPacer, list[float], list[float]]:
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    pacer = RelayPacer(
        interval,
        clock=lambda: clock[0],
        sleep=sleep,
        utcnow=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    return pacer, clock, sleeps


def test_relay_pacer_is_deterministic_start_to_start() -> None:
    pacer, clock, sleeps = _fake_pacer()

    first = pacer.acquire()
    clock[0] += 5.0
    second = pacer.acquire()
    clock[0] += 25.0
    third = pacer.acquire()

    assert sleeps == [15.0]
    assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]
    assert first["previous_start_delta_seconds"] is None
    assert second["previous_start_delta_seconds"] == 20.0
    assert second["wait_requested_seconds"] == 15.0
    assert third["previous_start_delta_seconds"] == 25.0
    assert third["wait_seconds"] == 0.0


def test_relay_pacer_fails_before_deadline_without_consuming_slot() -> None:
    pacer, clock, sleeps = _fake_pacer()
    assert pacer.acquire()["sequence"] == 1
    clock[0] = 1.0

    with pytest.raises(AgentTimeoutError, match="deadline"):
        pacer.acquire(deadline=10.0)

    assert sleeps == []
    clock[0] = 20.0
    assert pacer.acquire()["sequence"] == 2


def test_shared_pacer_spans_responses_clients_and_records_attempt_telemetry(
    monkeypatch: Any,
) -> None:
    pacer, _, sleeps = _fake_pacer()
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        request_interval_seconds=20.0,
    )
    clients = [ResponsesClient(config, pacer=pacer), ResponsesClient(config, pacer=pacer)]

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        return ResponseTurn("response", [{"type": "message"}], "OK", {})

    for client in clients:
        monkeypatch.setattr(client, "_create_once", create_once)
    try:
        first = clients[0].create({"model": "test-model"})
        second = clients[1].create({"model": "test-model"})
    finally:
        for client in clients:
            client.close()

    first_pacing = first.attempt_history[0]["pacing"]
    second_pacing = second.attempt_history[0]["pacing"]
    assert first_pacing["policy"] == PACING_POLICY
    assert first_pacing["sequence"] == 1
    assert second_pacing["sequence"] == 2
    assert second_pacing["wait_seconds"] == 20.0
    assert second.timing_dict()["pacing_wait_seconds_total"] == 20.0
    assert sleeps == [20.0]


def test_zero_interval_never_sleeps() -> None:
    pacer, _, sleeps = _fake_pacer(0.0)

    assert pacer.acquire()["sequence"] == 1
    assert pacer.acquire()["sequence"] == 2
    assert sleeps == []


def test_safe_retry_backoff_and_shared_pacer_use_effective_maximum(
    monkeypatch: Any,
) -> None:
    pacer, clock, sleeps = _fake_pacer()
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        max_retries=1,
        request_interval_seconds=20.0,
    )
    client = ResponsesClient(config, pacer=pacer)
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "HTTP 429",
                retryable=True,
                status_code=429,
                safe_to_retry=True,
                safe_retry_reason="http_429",
                delivery_state="headers_seen",
            )
        return ResponseTurn("response", [{"type": "message"}], "OK", {})

    monkeypatch.setattr(client, "_create_once", create_once)
    monkeypatch.setattr("spreadsheet_harness.agent.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "spreadsheet_harness.agent.time.sleep",
        lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    try:
        turn = client.create({"model": "test-model"})
    finally:
        client.close()

    assert calls == 2
    assert sleeps == [15.0, 5.0]
    assert [item["pacing"]["sequence"] for item in turn.attempt_history] == [1, 2]
    assert turn.attempt_history[0]["backoff_seconds"] == 15.0
    assert turn.attempt_history[1]["pacing"]["wait_seconds"] == 5.0


def test_pacing_deadline_prevents_phantom_http_attempt(monkeypatch: Any) -> None:
    pacer, clock, _ = _fake_pacer()
    pacer.acquire()
    clock[0] = 1.0
    client = ResponsesClient(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        pacer=pacer,
    )
    calls = 0

    def create_once(_: dict[str, Any], **__: Any) -> ResponseTurn:
        nonlocal calls
        calls += 1
        return ResponseTurn("response", [{"type": "message"}], "OK", {})

    monkeypatch.setattr(client, "_create_once", create_once)
    monkeypatch.setattr("spreadsheet_harness.agent.time.monotonic", lambda: clock[0])
    try:
        with pytest.raises(AgentTimeoutError, match="deadline"):
            client.create({"model": "test-model"}, deadline=10.0)
    finally:
        client.close()

    assert calls == 0
