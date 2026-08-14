from __future__ import annotations

import errno
import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from spreadsheet_harness import cli
from spreadsheet_harness.agent import AgentResult, ResponsesClient
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import HarnessError


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


def test_pilot_compare_rejects_cli_key_before_loading_spec(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    parser = cli.build_parser()
    key_file = tmp_path / "does-not-need-to-exist"
    called = False

    def load_spec(_: Path) -> Any:
        nonlocal called
        called = True
        raise AssertionError("run spec must not be loaded")

    monkeypatch.setattr(cli, "_load_pilot_run_spec_from_repository", load_spec)
    args = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--run-spec",
            "run-spec.json",
            "--dataset",
            "dataset",
            "--split-manifest",
            "split.json",
            "--output",
            "output",
            "--api-key",
            "argv-secret",
            "--api-key-file",
            str(key_file),
        ]
    )

    with pytest.raises(HarnessError, match="only --api-key-file"):
        cli.cmd_benchmark_compare(args)

    assert called is False


def test_pilot_provider_reads_explicit_owner_only_key_file(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    key_file = tmp_path / "pilot.key"
    key_file.write_text("file-only-secret\n", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.setenv("OPENAI_API_KEY", "different-environment-secret")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--api-key-file",
            str(key_file),
            "--base-url",
            "https://example.test/v1",
            "--model",
            "test-model",
        ]
    )

    config = cli._provider(args)

    assert config.api_key == "file-only-secret"


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_pilot_provider_rejects_non_owner_only_key_file(
    tmp_path: Path,
    mode: int,
) -> None:
    key_file = tmp_path / "pilot.key"
    key_file.write_text("secret\n", encoding="utf-8")
    key_file.chmod(mode)
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--api-key-file",
            str(key_file),
            "--base-url",
            "https://example.test/v1",
            "--model",
            "test-model",
        ]
    )

    with pytest.raises(HarnessError, match="group or other permissions"):
        cli._provider(args)


def test_pilot_provider_rejects_symlink_key_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real.key"
    target.write_text("secret\n", encoding="utf-8")
    target.chmod(0o600)
    key_file = tmp_path / "pilot.key"
    try:
        key_file.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--api-key-file",
            str(key_file),
            "--base-url",
            "https://example.test/v1",
            "--model",
            "test-model",
        ]
    )

    with pytest.raises(HarnessError, match="regular file"):
        cli._provider(args)


def test_pilot_repository_paths_reject_output_ancestor_symlink(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "benchmarks" / "data" / "dataset").mkdir(parents=True)
    (repository / "benchmarks" / "protocols").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repository / "benchmarks" / "results").symlink_to(
            outside, target_is_directory=True
        )
    except OSError:
        pytest.skip("symlinks are unavailable")
    document = {
        "repository_relative_paths": {
            "dataset": "benchmarks/data/dataset",
            "split_manifest": "benchmarks/protocols/split.json",
            "output": "benchmarks/results/pilot",
        }
    }
    args = type(
        "Args",
        (),
        {
            "dataset": repository / "benchmarks/data/dataset",
            "split_manifest": repository / "benchmarks/protocols/split.json",
        },
    )()
    monkeypatch.setattr(cli, "_repository_root", lambda: repository)

    with pytest.raises(HarnessError, match="must not contain symlinks"):
        cli._pilot_repository_paths(
            args,
            document,
            output_argument=repository / "benchmarks/results/pilot",
        )


def test_pilot_compare_manifest_preflight_precedes_output_creation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    parser = cli.build_parser()
    output = tmp_path / "pilot-output"
    dataset = tmp_path / "dataset"
    split = tmp_path / "split.json"
    spec = tmp_path / "run-spec.json"
    key_file = tmp_path / "key"
    document = {
        "execution": {},
        "repository_relative_paths": {
            "dataset": "ignored",
            "split_manifest": "ignored",
            "output": "ignored",
        },
    }
    provenance = {"run_spec_id": "test"}
    raw = b"fixed run spec"
    task = type("Task", (), {"task_id": "task-1"})()

    monkeypatch.setattr(
        cli,
        "_load_pilot_run_spec_from_repository",
        lambda _: (document, provenance, raw),
    )
    monkeypatch.setattr(
        cli,
        "_pilot_repository_paths",
        lambda *_args, **_kwargs: (dataset, split, output),
    )
    monkeypatch.setattr(cli, "load_verified_tasks", lambda _: [task])
    monkeypatch.setattr(
        cli,
        "load_and_verify_trace2skill_split_manifest",
        lambda *_: {"task_ids": ["task-1"]},
    )
    monkeypatch.setattr(cli, "trace2skill_split_provenance", lambda _: {"split": True})
    monkeypatch.setattr(
        cli,
        "_provider",
        lambda _: ProviderConfig("https://example.test/v1", "secret", "model"),
    )
    monkeypatch.setattr(cli, "verify_pilot_run_spec_contract", lambda *_: None)
    monkeypatch.setattr(cli, "comparison_execution_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "require_launchable_run_spec", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli, "require_evaluation_task_authorization", lambda *_args, **_kwargs: None
    )

    class FrozenSkills:
        def freeze(self) -> FrozenSkills:
            return self

    monkeypatch.setattr(cli, "_skills", lambda _: FrozenSkills())

    class RejectingRunner:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def preflight(self, _: list[Any]) -> dict[str, Any]:
            raise HarnessError("preflight rejected")

    monkeypatch.setattr(cli, "ComparisonBenchmarkRunner", RejectingRunner)
    args = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--run-spec",
            str(spec),
            "--dataset",
            str(dataset),
            "--split-manifest",
            str(split),
            "--output",
            str(output),
            "--api-key-file",
            str(key_file),
        ]
    )

    with pytest.raises(HarnessError, match="preflight rejected"):
        cli.cmd_benchmark_compare(args)

    assert not output.exists()


def test_fresh_pilot_output_claim_publishes_complete_private_copy(
    tmp_path: Path,
) -> None:
    output = tmp_path / "pilot"
    raw = b"fixed run spec\n"

    cli._claim_fresh_pilot_output(output, raw)

    copy = output / "run-spec.json"
    assert copy.read_bytes() == raw
    assert copy.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert not list(tmp_path.glob(".pilot.claim-*"))


def test_fresh_pilot_output_claim_failure_leaves_no_canonical_directory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    output = tmp_path / "pilot"

    def fail_write(_: Path, __: bytes) -> None:
        raise OSError("injected write failure")

    monkeypatch.setattr(cli, "_write_private_bytes", fail_write)

    with pytest.raises(OSError, match="injected write failure"):
        cli._claim_fresh_pilot_output(output, b"fixed run spec")

    assert not output.exists()
    assert not list(tmp_path.glob(".pilot.claim-*"))


def test_fresh_pilot_output_claim_never_replaces_existing_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "pilot"
    output.mkdir()
    marker = output / "belongs-to-another-run"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(HarnessError, match="must not already exist"):
        cli._claim_fresh_pilot_output(output, b"fixed run spec")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (output / "run-spec.json").exists()
    assert not list(tmp_path.glob(".pilot.claim-*"))


def test_fresh_pilot_output_claim_falls_back_when_renameat2_is_unsupported(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    output = tmp_path / "pilot"
    raw = b"fixed run spec"

    def unsupported(*_: Any, **__: Any) -> None:
        raise OSError(errno.EINVAL, "renameat2 unsupported")

    monkeypatch.setattr(cli, "_rename_directory_noreplace", unsupported)

    cli._claim_fresh_pilot_output(output, raw)

    assert output.is_dir()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert (output / "run-spec.json").read_bytes() == raw
    assert stat.S_IMODE((output / "run-spec.json").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".pilot.claim-*"))


def test_fresh_pilot_output_mkdir_fallback_never_replaces_existing_directory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    output = tmp_path / "pilot"
    output.mkdir()
    marker = output / "belongs-to-another-run"
    marker.write_text("keep", encoding="utf-8")

    def unsupported(*_: Any, **__: Any) -> None:
        raise OSError(errno.EINVAL, "renameat2 unsupported")

    monkeypatch.setattr(cli, "_rename_directory_noreplace", unsupported)

    with pytest.raises(HarnessError, match="must not already exist"):
        cli._claim_fresh_pilot_output(output, b"fixed run spec")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (output / "run-spec.json").exists()
    assert not list(tmp_path.glob(".pilot.claim-*"))


def test_fresh_pilot_output_claim_rolls_back_after_publish_sync_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    output = tmp_path / "pilot"
    calls = 0
    original = cli._fsync_directory

    def fail_parent_sync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected parent sync failure")
        original(path)

    monkeypatch.setattr(cli, "_fsync_directory", fail_parent_sync)

    with pytest.raises(OSError, match="injected parent sync failure"):
        cli._claim_fresh_pilot_output(output, b"fixed run spec")

    assert not output.exists()
    assert not list(tmp_path.glob(".pilot.claim-*"))


def test_historical_v24_contract_is_parseable_but_current_runner_differs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    key_file = tmp_path / "pilot.key"
    key_file.write_text("file-only-secret\n", encoding="utf-8")
    key_file.chmod(0o600)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    document, _, _ = cli._load_pilot_run_spec_from_repository(
        "benchmarks/protocols/qwen35-trace2skill-local-postopt16-run-spec-v1.json"
    )
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "compare",
            "--dataset",
            "benchmarks/data/spreadsheetbench_verified_400",
            "--split-manifest",
            "benchmarks/protocols/qwen35-trace2skill-local-postopt16-v1.json",
            "--run-spec",
            "benchmarks/protocols/qwen35-trace2skill-local-postopt16-run-spec-v1.json",
            "--output",
            "benchmarks/results/qwen36-local-postopt-eval16-v3-bare-ours-v24-seed41",
            "--api-key-file",
            str(key_file),
            "--arm",
            "bare",
            "--arm",
            "ours",
            "--base-url",
            "http://101.37.174.109:8010/v1",
            "--model",
            "qwen36-35b-a3b",
            "--api-protocol",
            "chat-completions",
            "--reasoning-effort",
            "none",
            "--request-timeout",
            "700",
            "--request-retries",
            "0",
            "--request-interval-seconds",
            "0",
            "--litellm-timeout",
            "600",
            "--temperature",
            "1",
            "--top-p",
            "1",
            "--seed",
            "41",
            "--presence-penalty",
            "2",
            "--top-k",
            "40",
            "--min-p",
            "0",
            "--repetition-penalty",
            "1",
            "--disable-thinking",
            "--max-model-calls",
            "8",
            "--max-turns-per-arm",
            "8",
            "--max-total-tokens",
            "120000",
            "--max-output-tokens",
            "4096",
            "--task-timeout",
            "1200",
            "--arm-order-seed",
            "20260812",
            "--circuit-breaker",
            "3",
        ]
    )
    config = cli._provider(args)
    skills = cli._skills(args).freeze()
    actual = cli.comparison_execution_contract(
        config,
        arms=tuple(args.arm),
        max_model_calls=args.max_model_calls,
        max_turns_per_arm=args.max_turns_per_arm,
        max_total_tokens=args.max_total_tokens,
        max_output_tokens=args.max_output_tokens,
        task_timeout_seconds=args.task_timeout,
        recalculate=not args.no_recalculate,
        arm_order_seed=args.arm_order_seed,
        circuit_breaker_threshold=args.circuit_breaker,
        split_provenance=document["execution"]["split_provenance"],
        skills=skills,
    )

    assert actual != document["execution"]
    assert actual["comparison_protocol_version"] == "resource_matched_multi_arm_v26"
    assert actual["comparison_manifest_schema_version"] == 15


def test_documented_v25_confirmation_command_is_historical_and_read_only(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    key_file = tmp_path / "confirmation.key"
    key_file.write_text("file-only-secret\n", encoding="utf-8")
    key_file.chmod(0o600)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    document, provenance, _ = cli._load_pilot_run_spec_from_repository(
        "benchmarks/protocols/qwen35-trace2skill-local-confirm16-run-spec-v1.json"
    )
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "compare",
            "--dataset",
            "benchmarks/data/spreadsheetbench_verified_400",
            "--split-manifest",
            "benchmarks/protocols/qwen35-trace2skill-local-confirm16-v1.json",
            "--run-spec",
            "benchmarks/protocols/qwen35-trace2skill-local-confirm16-run-spec-v1.json",
            "--output",
            "benchmarks/results/qwen36-local-confirm-eval16-v1-bare-ours-v25-seed41",
            "--api-key-file",
            str(key_file),
            "--arm",
            "bare",
            "--arm",
            "ours",
            "--base-url",
            "http://101.37.174.109:8010/v1",
            "--model",
            "qwen36-35b-a3b",
            "--api-protocol",
            "chat-completions",
            "--reasoning-effort",
            "none",
            "--request-timeout",
            "700",
            "--request-retries",
            "0",
            "--request-interval-seconds",
            "0",
            "--litellm-timeout",
            "600",
            "--temperature",
            "1",
            "--top-p",
            "1",
            "--seed",
            "41",
            "--presence-penalty",
            "2",
            "--top-k",
            "40",
            "--min-p",
            "0",
            "--repetition-penalty",
            "1",
            "--disable-thinking",
            "--max-model-calls",
            "8",
            "--max-turns-per-arm",
            "8",
            "--max-total-tokens",
            "120000",
            "--max-output-tokens",
            "4096",
            "--task-timeout",
            "1200",
            "--arm-order-seed",
            "20260812",
            "--circuit-breaker",
            "3",
        ]
    )
    config = cli._provider(args)
    skills = cli._skills(args).freeze()
    actual = cli.comparison_execution_contract(
        config,
        arms=tuple(args.arm),
        max_model_calls=args.max_model_calls,
        max_turns_per_arm=args.max_turns_per_arm,
        max_total_tokens=args.max_total_tokens,
        max_output_tokens=args.max_output_tokens,
        task_timeout_seconds=args.task_timeout,
        recalculate=not args.no_recalculate,
        arm_order_seed=args.arm_order_seed,
        circuit_breaker_threshold=args.circuit_breaker,
        split_provenance=document["execution"]["split_provenance"],
        skills=skills,
    )

    with pytest.raises(HarnessError, match="read-only"):
        cli.require_launchable_run_spec(provenance)
    assert actual != document["execution"]
    assert document["execution"]["comparison_protocol_version"] == (
        "resource_matched_multi_arm_v25"
    )
    assert document["execution"]["comparison_manifest_schema_version"] == 14


def test_pilot_seal_calls_public_locked_runner_entrypoint(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    parser = cli.build_parser()
    output = tmp_path / "pilot-output"
    output.mkdir()
    dataset = tmp_path / "dataset"
    split = tmp_path / "split.json"
    spec = tmp_path / "run-spec.json"
    key_file = tmp_path / "key"
    document = {
        "execution": {
            "arms": ["bare", "ours"],
            "resources": {
                "max_model_calls": 8,
                "max_turns_per_arm": 8,
                "max_total_tokens": 120000,
                "max_output_tokens_per_call": 4096,
                "task_timeout_seconds": 1200.0,
                "recalculate": True,
                "arm_order_seed": 20260812,
                "circuit_breaker_threshold": 3,
            },
        },
        "repository_relative_paths": {},
    }
    task = type("Task", (), {"task_id": "task-1"})()
    monkeypatch.setattr(
        cli,
        "_load_pilot_run_spec_from_repository",
        lambda _: (document, {"run_spec_id": "test"}, b"spec"),
    )
    monkeypatch.setattr(
        cli,
        "_pilot_repository_paths",
        lambda *_args, **_kwargs: (dataset, split, output),
    )
    monkeypatch.setattr(
        cli,
        "load_and_verify_trace2skill_split_manifest",
        lambda *_: {"task_ids": ["task-1"]},
    )
    monkeypatch.setattr(cli, "trace2skill_split_provenance", lambda _: {"split": True})
    monkeypatch.setattr(cli, "load_verified_tasks", lambda _: [task])
    monkeypatch.setattr(
        cli,
        "_provider",
        lambda _: ProviderConfig("https://example.test/v1", "secret", "model"),
    )
    monkeypatch.setattr(cli, "verify_pilot_run_spec_contract", lambda *_: None)
    monkeypatch.setattr(cli, "comparison_execution_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "require_launchable_run_spec", lambda *_args, **_kwargs: None)

    class FrozenSkills:
        def freeze(self) -> FrozenSkills:
            return self

    monkeypatch.setattr(cli, "_skills", lambda _: FrozenSkills())
    captured: dict[str, Any] = {}

    class Runner:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        def seal_interrupted_inflight(self, tasks: list[Any]) -> dict[str, Any]:
            captured["tasks"] = tasks
            return {"status": "interrupted"}

        def _seal_interrupted_inflight(self) -> None:
            raise AssertionError("CLI must not call the unlocked private method")

    monkeypatch.setattr(cli, "ComparisonBenchmarkRunner", Runner)
    args = parser.parse_args(
        [
            "benchmark",
            "seal-interrupted",
            str(output),
            "--dataset",
            str(dataset),
            "--split-manifest",
            str(split),
            "--run-spec",
            str(spec),
            "--api-key-file",
            str(key_file),
        ]
    )

    assert cli.cmd_benchmark_seal_interrupted(args) == 0
    assert captured["tasks"] == [task]


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
