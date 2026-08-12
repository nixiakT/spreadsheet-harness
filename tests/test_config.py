from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from spreadsheet_harness.cli import _provider, build_parser
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import HarnessError

_GENERATION_ENVIRONMENT = (
    "SHEET_AGENT_TEMPERATURE",
    "SHEET_AGENT_TOP_P",
    "SHEET_AGENT_SEED",
    "SHEET_AGENT_PRESENCE_PENALTY",
    "SHEET_AGENT_TOP_K",
    "SHEET_AGENT_MIN_P",
    "SHEET_AGENT_REPETITION_PENALTY",
    "SHEET_AGENT_ENABLE_THINKING",
)


def _secure_key_file(path: Path, key: str, *, trailing_newline: bool = True) -> Path:
    path.write_text(key + ("\n" if trailing_newline else ""), encoding="utf-8")
    path.chmod(0o600)
    return path


def _isolate_provider_environment(
    monkeypatch: Any, codex_home: Path
) -> None:
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("SHEET_AGENT_API_KEY_FILE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in _GENERATION_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def test_ultra_alias_is_recorded_and_sent_as_max() -> None:
    config = ProviderConfig.discover(
        base_url="https://example.test/v1",
        api_key="not-a-real-key",
        model="gpt-5.6-sol",
        reasoning_effort="ultra",
    )

    assert config.requested_reasoning_effort == "ultra"
    assert config.reasoning_effort == "max"
    assert config.public_dict()["requested_reasoning_effort"] == "ultra"
    assert config.request_interval_seconds == 0.0


def test_direct_config_construction_also_normalizes_ultra() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "gpt-5.6-sol",
        reasoning_effort="ultra",
    )

    assert config.requested_reasoning_effort == "ultra"
    assert config.reasoning_effort == "max"
    assert config.store_responses is False


def test_request_interval_is_public_and_validated() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        request_interval_seconds=20,
    )

    assert config.request_interval_seconds == 20.0
    assert config.public_dict()["request_interval_seconds"] == 20.0
    with pytest.raises(HarnessError, match="Request interval"):
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            request_interval_seconds=-1,
        )


def test_benchmark_cli_exposes_turn_and_pacing_controls() -> None:
    parser = build_parser()
    comparison = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--max-model-calls",
            "100",
            "--max-turns-per-arm",
            "100",
            "--request-interval-seconds",
            "0",
        ]
    )
    single = parser.parse_args(
        [
            "benchmark",
            "run",
            "--max-turns",
            "100",
            "--request-interval-seconds",
            "0",
        ]
    )

    assert comparison.max_model_calls == 100
    assert comparison.max_turns_per_arm == 100
    assert comparison.request_interval_seconds == 0.0
    assert single.max_turns == 100
    assert single.request_interval_seconds == 0.0


def test_generation_controls_are_explicit_public_and_partitioned_for_wire() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "Qwen/Qwen3.5-35B-A3B",
        temperature=1.0,
        top_p=1.0,
        seed=42,
        presence_penalty=2.0,
        top_k=40,
        min_p=0.0,
        repetition_penalty=1.0,
        enable_thinking=False,
    )

    generation = {
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 42,
        "presence_penalty": 2.0,
        "top_k": 40,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "enable_thinking": False,
    }
    assert config.generation_dict() == generation
    assert config.public_dict()["generation"] == generation
    applied = config.apply_generation({"model": config.model})
    assert applied == {
        "model": config.model,
        "temperature": 1.0,
        "top_p": 1.0,
        "presence_penalty": 2.0,
        "extra_body": {
            "seed": 42,
            "top_k": 40,
            "min_p": 0.0,
            "repetition_penalty": 1.0,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }
    assert config.apply_generation(applied) == applied
    assert ProviderConfig(
        "https://example.test/v1", "key", "model"
    ).generation_dict() == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", float("nan")),
        ("temperature", -0.1),
        ("top_p", 0.0),
        ("top_p", 1.1),
        ("presence_penalty", 2.1),
        ("top_k", True),
        ("top_k", -2),
        ("min_p", -0.1),
        ("repetition_penalty", 0.0),
        ("seed", True),
        ("seed", 2**63),
        ("enable_thinking", "false"),
    ],
)
def test_generation_controls_reject_invalid_values(field: str, value: Any) -> None:
    with pytest.raises(HarnessError, match="Generation"):
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            **{field: value},
        )


def test_generation_discovery_reads_environment_and_cli_overrides(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    monkeypatch.setenv("SHEET_AGENT_TEMPERATURE", "0.6")
    monkeypatch.setenv("SHEET_AGENT_TOP_P", "0.95")
    monkeypatch.setenv("SHEET_AGENT_SEED", "73")
    monkeypatch.setenv("SHEET_AGENT_PRESENCE_PENALTY", "1.5")
    monkeypatch.setenv("SHEET_AGENT_TOP_K", "20")
    monkeypatch.setenv("SHEET_AGENT_MIN_P", "0")
    monkeypatch.setenv("SHEET_AGENT_REPETITION_PENALTY", "1")
    monkeypatch.setenv("SHEET_AGENT_ENABLE_THINKING", "off")

    config = ProviderConfig.discover(
        base_url="https://example.test/v1",
        api_key="not-a-real-key",
        model="test-model",
        temperature=1.0,
    )

    assert config.generation_dict() == {
        "temperature": 1.0,
        "top_p": 0.95,
        "seed": 73,
        "presence_penalty": 1.5,
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "enable_thinking": False,
    }


def test_generation_discovery_rejects_invalid_environment_boolean(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    monkeypatch.setenv("SHEET_AGENT_ENABLE_THINKING", "sometimes")

    with pytest.raises(HarnessError, match="SHEET_AGENT_ENABLE_THINKING"):
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key="not-a-real-key",
            model="test-model",
        )


def test_generation_payload_conflicts_fail_closed() -> None:
    config = ProviderConfig(
        "https://example.test/v1",
        "not-a-real-key",
        "test-model",
        temperature=1.0,
        top_k=40,
        enable_thinking=False,
    )

    with pytest.raises(HarnessError, match="temperature"):
        config.apply_generation({"temperature": 0.5})
    with pytest.raises(HarnessError, match="top_k"):
        config.apply_generation({"extra_body": {"top_k": 20}})
    with pytest.raises(HarnessError, match="enable_thinking"):
        config.apply_generation(
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
        )


def test_generation_cli_flags_preserve_explicit_false(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    args = build_parser().parse_args(
        [
            "doctor",
            "--base-url",
            "https://example.test/v1",
            "--api-key",
            "not-a-real-key",
            "--model",
            "Qwen/Qwen3.5-35B-A3B",
            "--temperature",
            "1",
            "--top-p",
            "1",
            "--seed",
            "42",
            "--presence-penalty",
            "2",
            "--top-k",
            "40",
            "--min-p",
            "0",
            "--repetition-penalty",
            "1",
            "--disable-thinking",
        ]
    )

    assert _provider(args).generation_dict() == {
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 42,
        "presence_penalty": 2.0,
        "top_k": 40,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "enable_thinking": False,
    }


def test_api_key_discovery_precedence(tmp_path: Path, monkeypatch: Any) -> None:
    codex_home = tmp_path / "codex-home"
    _isolate_provider_environment(monkeypatch, codex_home)
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "codex-auth-key"}), encoding="utf-8"
    )
    environment_file = _secure_key_file(
        tmp_path / "environment-key", "environment-file-key"
    )
    explicit_file = _secure_key_file(tmp_path / "explicit-key", "explicit-file-key")
    monkeypatch.setenv("SHEET_AGENT_API_KEY_FILE", str(environment_file))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-environment-key")
    common = {
        "base_url": "https://example.test/v1",
        "model": "test-model",
    }

    explicit = ProviderConfig.discover(
        **common,
        api_key="explicit-api-key",
        api_key_file=tmp_path / "does-not-exist",
    )
    explicit_file_config = ProviderConfig.discover(
        **common,
        api_key_file=explicit_file,
    )
    environment_file_config = ProviderConfig.discover(**common)
    monkeypatch.delenv("SHEET_AGENT_API_KEY_FILE")
    openai_environment_config = ProviderConfig.discover(**common)
    monkeypatch.delenv("OPENAI_API_KEY")
    codex_auth_config = ProviderConfig.discover(**common)

    assert explicit.api_key == "explicit-api-key"
    assert explicit_file_config.api_key == "explicit-file-key"
    assert environment_file_config.api_key == "environment-file-key"
    assert openai_environment_config.api_key == "openai-environment-key"
    assert codex_auth_config.api_key == "codex-auth-key"


def test_api_key_file_is_not_exposed_by_cli_or_public_config(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    secret = "cr_benchmark_file_secret_123456789"
    key_path = _secure_key_file(tmp_path / "benchmark-api-key", secret)
    argv = [
        "benchmark",
        "run",
        "--api-key-file",
        str(key_path),
        "--base-url",
        "https://example.test/v1",
        "--model",
        "test-model",
    ]

    args = build_parser().parse_args(argv)
    config = _provider(args)
    public = json.dumps(config.public_dict())

    assert args.api_key_file == key_path
    assert secret not in " ".join(argv)
    assert secret not in repr(vars(args))
    assert secret not in public
    assert str(key_path) not in public
    assert config.api_key == secret


@pytest.mark.parametrize("collision_source", ["environment", "codex-auth"])
def test_api_key_file_rejects_interactive_credential_collision(
    collision_source: str, tmp_path: Path, monkeypatch: Any
) -> None:
    codex_home = tmp_path / "codex-home"
    _isolate_provider_environment(monkeypatch, codex_home)
    secret = "never-report-colliding-secret"
    key_path = _secure_key_file(tmp_path / "benchmark-api-key", secret)
    if collision_source == "environment":
        monkeypatch.setenv("OPENAI_API_KEY", secret)
    else:
        (codex_home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": secret}), encoding="utf-8"
        )

    with pytest.raises(HarnessError, match="must differ") as caught:
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key_file=key_path,
            model="test-model",
        )

    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "\n",
        "first-secret\nsecond-secret\n",
        " leading-secret\n",
        "trailing-secret \n",
        "tabbed\tsecret\n",
        "two-newlines-secret\n\n",
    ],
)
def test_api_key_file_rejects_empty_or_non_single_key_text(
    contents: str, tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    path = tmp_path / "invalid-key"
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(HarnessError) as caught:
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key_file=path,
            model="test-model",
        )

    message = str(caught.value)
    assert "API key file" in message
    for secret_fragment in ("first-secret", "second-secret", "leading-secret"):
        assert secret_fragment not in message


@pytest.mark.parametrize("mode", [0o640, 0o620, 0o610, 0o604, 0o602, 0o601])
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
def test_api_key_file_rejects_every_group_or_other_permission(
    mode: int, tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    path = _secure_key_file(tmp_path / "insecure-key", "never-report-this-key")
    path.chmod(mode)

    with pytest.raises(HarnessError, match="group or other permissions") as caught:
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key_file=path,
            model="test-model",
        )

    assert "never-report-this-key" not in str(caught.value)


def test_api_key_file_rejects_non_regular_files(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    directory = tmp_path / "key-directory"
    directory.mkdir()

    with pytest.raises(HarnessError, match="regular file"):
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key_file=directory,
            model="test-model",
        )


def test_api_key_file_rejects_symlinks(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    target = _secure_key_file(tmp_path / "real-key", "symlink-target-secret")
    link = tmp_path / "linked-key"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(HarnessError, match="regular file") as caught:
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key_file=link,
            model="test-model",
        )

    assert "symlink-target-secret" not in str(caught.value)


def test_api_key_file_rejects_invalid_utf8_without_echoing_bytes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate_provider_environment(monkeypatch, tmp_path / "codex-home")
    path = tmp_path / "binary-key"
    path.write_bytes(b"secret-prefix-\xff-secret-suffix")
    path.chmod(0o600)

    with pytest.raises(HarnessError, match="UTF-8") as caught:
        ProviderConfig.discover(
            base_url="https://example.test/v1",
            api_key_file=path,
            model="test-model",
        )

    assert "secret-prefix" not in str(caught.value)
