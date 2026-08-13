from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from spreadsheet_harness import cli as cli_module
from spreadsheet_harness.agent import AgentResult
from spreadsheet_harness.arms import PaperStageValidationError
from spreadsheet_harness.benchmark import SpreadsheetTask, compare_workbooks
from spreadsheet_harness.comparison import (
    AVAILABLE_COMPARISON_ARMS,
    COMPARISON_ARMS,
    COMPARISON_PROTOCOL_VERSION,
    ComparisonBenchmarkRunner,
    _arm_order,
    _balanced_arm_orders,
    comparison_summary,
    load_pilot_run_spec,
    verify_repository_source_state,
)
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.errors import (
    AgentRoutingError,
    CodeIsolationError,
    HarnessError,
    ProviderError,
)
from spreadsheet_harness.skills import SkillRegistry
from spreadsheet_harness.trajectory import read_trajectory


def _book(path: Path, value: object = 1) -> None:
    workbook = Workbook()
    workbook.active["A1"] = value
    workbook.save(path)
    workbook.close()


def _tasks(tmp_path: Path) -> list[SpreadsheetTask]:
    initial = tmp_path / "initial.xlsx"
    golden = tmp_path / "golden.xlsx"
    _book(initial)
    _book(golden)
    return [
        SpreadsheetTask(
            "cell-1",
            "cell secret instruction",
            initial,
            golden,
            "Cell-Level Manipulation",
            "TOP_SECRET_CELL_RANGE",
            "TOP_SECRET_CELL_SHEET",
        ),
        SpreadsheetTask(
            "sheet-1",
            "sheet secret instruction",
            initial,
            golden,
            "Sheet-Level Manipulation",
            "TOP_SECRET_SHEET_RANGE",
            "TOP_SECRET_SHEET_NAME",
        ),
    ]


def test_arm_order_is_deterministic_rotation() -> None:
    first = _arm_order("task-a", 7, COMPARISON_ARMS)
    second = _arm_order("task-a", 7, COMPARISON_ARMS)

    assert first == second
    assert set(first) == set(COMPARISON_ARMS)
    assert len(first) == len(COMPARISON_ARMS)


def test_balanced_arm_orders_counterbalance_every_position() -> None:
    task_ids = [f"task-{index}" for index in range(6)]
    orders = _balanced_arm_orders(task_ids, 20260811, COMPARISON_ARMS)

    assert orders == _balanced_arm_orders(task_ids, 20260811, COMPARISON_ARMS)
    for position in range(3):
        counts = {arm: 0 for arm in COMPARISON_ARMS}
        for order in orders.values():
            counts[order[position]] += 1
        assert counts == {arm: 2 for arm in COMPARISON_ARMS}


def test_comparison_manifest_hides_answer_metadata(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            max_retries=3,
            request_interval_seconds=20.0,
            litellm_timeout_seconds=600,
        ),
        tmp_path / "comparison",
        skill_registry=SkillRegistry([]),
    )

    manifest = runner._manifest(tasks)
    encoded = json.dumps(manifest)

    assert manifest["task_count"] == 2
    assert manifest["schema_version"] == 12
    assert COMPARISON_PROTOCOL_VERSION == "resource_matched_multi_arm_v23"
    assert manifest["comparison_protocol_version"] == COMPARISON_PROTOCOL_VERSION
    assert manifest["configuration"]["code_workbook_formula_gate"] == (
        "rollback-new-invalid-a1-or-high-confidence-unprefixed-formula-text-v2"
    )
    assert manifest["configuration"]["failed_edit_recovery_policy"] == (
        "force-successful-code-edit-before-terminal-v1"
    )
    assert manifest["configuration"]["spreadsheet_skill_policy"] == (
        "pre-evaluation-baseline-frozen-v1"
    )
    assert manifest["configuration"]["edit_recovery_prompt_policy"] == (
        "self-contained-request-scoped-verification-v1"
    )
    assert manifest["arms"] == list(COMPARISON_ARMS)
    assert manifest["arm_display_names"] == {
        "bare": "bare",
        "paper": "paper-inspired",
        "ours": "ours",
    }
    assert manifest["forced_tool_prefix_routing"] == {
        "bare": {"solve": ["code_interpreter", "code_interpreter"]},
        "paper": {
            "extract": ["list_sheets", "inspect_range"],
            "vision_verify": ["render_workbook", "view_image"],
            "latex_verify": ["range_to_latex"],
            "reconcile": [],
            "solve": ["code_interpreter", "code_interpreter"],
        },
        "ours": {"solve": ["code_interpreter", "code_interpreter"]},
    }
    assert manifest["post_prefix_routing"] == {
        "tool_choice": "auto",
        "terminal_tool": "submit_result",
        "applies_to": "comparison stages with workbook tools after forced prefix",
        "direct_text_stages": ["paper.reconcile"],
    }
    assert manifest["stage_allowed_tools"] == {
        "bare": {"solve": ["code_interpreter"]},
        "paper": {
            "extract": ["inspect_range", "list_sheets"],
            "vision_verify": ["render_workbook", "view_image"],
            "latex_verify": ["range_to_latex"],
            "reconcile": [],
            "solve": ["code_interpreter"],
        },
        "ours": {"solve": "all"},
    }
    assert manifest["allowed_observed_terminals"]["paper"]["reconcile"] == [
        "assistant_text"
    ]
    assert manifest["allowed_observed_terminals"]["ours"]["solve"] == [
        "submit_result",
        "assistant_text",
        "final_recovery_code_interpreter",
    ]
    assert manifest["forced_prefix_wire_policy"] == {
        "tool_choice": "explicit_function",
        "available_tools": "forced tool only",
        "terminal_tool_available": False,
    }
    assert manifest["stage_turn_caps"] == {
        "bare": {"solve": 20},
        "paper": {
            "extract": 6,
            "vision_verify": 3,
            "latex_verify": 3,
            "reconcile": 1,
            "solve": 7,
        },
        "ours": {"solve": 20},
    }
    assert manifest["turn_cap_policy"]["version"] == "per_arm_turn_cap_v2"
    assert manifest["turn_cap_policy"]["max_turns_per_arm"] == 20
    assert manifest["turn_cap_policy"]["paper_scaling_version"] == (
        "constrained_largest_remainder_v1"
    )
    assert manifest["deterministic_profile"]["enabled"] is True
    assert manifest["deterministic_profile"]["consumed_by_arms"] == ["ours"]
    assert set(manifest["deterministic_profile"]["task_profile_sha256"]) == {
        "cell-1",
        "sheet-1",
    }
    assert "TOP_SECRET" not in encoded
    assert all("scoring_metadata_sha256" in task for task in manifest["tasks"])
    assert manifest["configuration"]["circuit_breaker_threshold"] == 3
    assert manifest["configuration"]["circuit_breaker_threshold_categories"] == [
        "provider_transient",
        "routing_protocol",
    ]
    assert manifest["configuration"]["circuit_breaker_immediate_categories"] == [
        "provider_fatal"
    ]
    assert manifest["configuration"]["overload_retry_min_seconds"] == 15.0
    assert manifest["configuration"]["connect_retry_min_seconds"] == 30.0
    assert manifest["configuration"]["request_interval_seconds"] == 20.0
    assert manifest["configuration"]["litellm_timeout_seconds"] == 600.0
    assert manifest["configuration"]["max_turns_per_arm"] == 20
    assert manifest["configuration"]["request_pacing_policy"] == (
        "process_local_min_attempt_start_interval_v1"
    )
    assert manifest["configuration"]["request_pacing_scope"] == (
        "comparison_runner_process"
    )
    assert manifest["configuration"]["request_pacing_retries_included"] is True
    assert manifest["configuration"]["request_pacing_first_attempt_immediate"] is True
    assert manifest["configuration"]["automatic_retry_policy"] == (
        "delivery-aware-allowlist-v1"
    )
    assert manifest["configuration"]["safe_retry_http_statuses"] == [425, 429, 503]
    assert manifest["configuration"]["safe_automatic_retry_reasons"] == [
        "connect_error",
        "connect_timeout",
        "explicit_overload",
        "http_425",
        "http_429",
        "http_503",
        "pool_timeout",
    ]
    assert manifest["configuration"]["capacity_retry_delay_policy"] == (
        "max-valid-retry-after-and-overload-min-then-global-cap"
    )
    assert manifest["configuration"]["retry_backoff_max_seconds"] == 60.0
    assert manifest["configuration"]["read_timeout_policy"] == "fail-closed-no-replay"
    assert manifest["configuration"]["http_408_policy"] == "fail-closed-no-replay"
    assert manifest["configuration"]["stream_interruption_policy"] == (
        "fail-closed-no-replay"
    )
    assert manifest["configuration"]["request_attempt_telemetry"] == (
        "delivery-safe-retry-ids-headers-backoff-pacing-v4"
    )
    assert manifest["configuration"]["result_manifest_binding_policy"] == (
        "exact-manifest-sha256-v1"
    )
    assert manifest["configuration"]["resume_journal_policy"] == (
        "durable-inflight-fail-closed-no-replay-v3"
    )
    assert manifest["hidden_from_models"] == [
        "instruction_type",
        "answer_position",
        "answer_sheet",
        "golden_path",
    ]


def test_optional_ablation_arms_are_available_without_changing_default_manifest(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    assert COMPARISON_ARMS == ("bare", "paper", "ours")
    assert set(AVAILABLE_COMPARISON_ARMS) == {
        "bare",
        "profile",
        "native",
        "paper",
        "ours",
    }
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "key", "model"),
        tmp_path / "ablations",
        skill_registry=SkillRegistry([]),
        arms=("bare", "profile", "native", "ours"),
    )

    manifest = runner._manifest(tasks)

    assert manifest["arms"] == ["bare", "profile", "native", "ours"]
    contract = manifest["deterministic_profile"]
    assert contract["enabled"] is True
    assert contract["consumed_by_arms"] == ["profile", "ours"]
    assert contract["schema_version"] == "deterministic-workbook-profile-v1"
    assert contract["task_independent"] is True
    assert contract["model_calls"] == 0
    assert set(contract["task_profile_sha256"]) == {"cell-1", "sheet-1"}
    assert all(len(value) == 64 for value in contract["task_profile_sha256"].values())
    assert manifest["stage_turn_caps"]["profile"] == {"solve": 20}
    assert manifest["stage_turn_caps"]["native"] == {"solve": 20}


def test_comparison_manifest_records_custom_turn_caps_and_zero_pacing(
    tmp_path: Path,
) -> None:
    runner = ComparisonBenchmarkRunner(
        ProviderConfig(
            "http://localhost:8000/v1",
            "EMPTY",
            "Qwen/Qwen3.5-35B-A3B",
            request_interval_seconds=0,
        ),
        tmp_path / "qwen-100-turns",
        skill_registry=SkillRegistry([]),
        arms=("bare", "paper", "ours"),
        max_model_calls=100,
        max_turns_per_arm=100,
    )

    manifest = runner._manifest(_tasks(tmp_path))

    assert manifest["configuration"]["request_interval_seconds"] == 0.0
    assert manifest["configuration"]["max_model_calls"] == 100
    assert manifest["configuration"]["max_turns_per_arm"] == 100
    assert manifest["stage_turn_caps"] == {
        "bare": {"solve": 100},
        "paper": {
            "extract": 30,
            "vision_verify": 15,
            "latex_verify": 15,
            "reconcile": 5,
            "solve": 35,
        },
        "ours": {"solve": 100},
    }


def test_comparison_manifest_records_and_locks_generation_controls(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path)
    output = tmp_path / "generation-lock"
    first = ComparisonBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            api_protocol="chat-completions",
            seed=41,
            temperature=1.0,
            litellm_timeout_seconds=600,
        ),
        output,
        skill_registry=SkillRegistry([]),
    )
    first._prepare_manifest(tasks)

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["configuration"]["api_protocol"] == "chat-completions"
    assert manifest["configuration"]["litellm_timeout_seconds"] == 600.0
    assert manifest["configuration"]["generation"] == {
        "temperature": 1.0,
        "seed": 41,
    }

    second = ComparisonBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            api_protocol="chat-completions",
            seed=42,
            temperature=1.0,
            litellm_timeout_seconds=600,
        ),
        output,
        skill_registry=SkillRegistry([]),
    )
    with pytest.raises(HarnessError, match="different frozen config"):
        second._prepare_manifest(tasks)


def test_comparison_manifest_and_rows_bind_split_provenance(tmp_path: Path) -> None:
    provenance = {
        "manifest_id": "test-derivative-split-v2",
        "schema_version": "spreadsheetbench-trace2skill-derivative-v2",
        "manifest_sha256": (
            "f29d6e5627161b355c24acfbda6c5dcc250d12b5f4933d3c3fb0c50a8bac39b3"
        ),
        "task_count": 2,
        "task_ids_sha256": hashlib.sha256(b"cell-1\nsheet-1\n").hexdigest(),
        "dataset_json_sha256": "3" * 64,
    }
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "key", "model"),
        tmp_path / "split-binding",
        skill_registry=SkillRegistry([]),
        split_provenance=provenance,
    )

    tasks = _tasks(tmp_path)
    monkeypatch_provenance = provenance.copy()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "spreadsheet_harness.comparison.verify_trace2skill_split_provenance",
            lambda value: value == monkeypatch_provenance,
        )
        monkeypatch.setattr(
            "spreadsheet_harness.comparison._dataset_manifest_sha256",
            lambda _: "3" * 64,
        )
        manifest = runner._manifest(tasks)

    assert manifest["split_provenance"] == provenance
    provenance["manifest_id"] = "mutated-after-construction"
    assert manifest["split_provenance"]["manifest_id"] == "test-derivative-split-v2"


def test_comparison_manifest_rejects_invalid_split_provenance_before_write(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "key", "model"),
        tmp_path / "invalid-split-preflight",
        skill_registry=SkillRegistry([]),
        split_provenance={
            "manifest_id": "attacker-split",
            "schema_version": "attacker-v1",
            "manifest_sha256": "1" * 64,
            "task_count": 2,
            "task_ids_sha256": hashlib.sha256(b"cell-1\nsheet-1\n").hexdigest(),
            "dataset_json_sha256": "3" * 64,
        },
    )

    with pytest.raises(HarnessError, match="frozen split provenance"):
        runner._prepare_manifest(tasks)

    assert not runner.manifest_path.exists()


def test_split_manifest_rejects_offset_or_limit(monkeypatch: Any, tmp_path: Path) -> None:
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"task_ids": []}), encoding="utf-8")
    parser = cli_module.build_parser()
    monkeypatch.setattr(
        cli_module,
        "download_verified",
        lambda _: tmp_path / "dataset",
    )
    monkeypatch.setattr(cli_module, "load_verified_tasks", lambda _: [])
    monkeypatch.setattr(
        cli_module,
        "load_and_verify_trace2skill_split_manifest",
        lambda *_: {"valid": True, "task_ids": []},
    )

    for selector in (["--offset", "1"], ["--limit", "1"]):
        args = parser.parse_args(
            ["benchmark", "compare", "--split-manifest", str(split), *selector]
        )
        with pytest.raises(HarnessError, match="derivative manifest"):
            cli_module.cmd_benchmark_compare(args)


@pytest.mark.parametrize(
    "selector",
    [["--task-id", "cell-1"], ["--task-id-file", "ids.txt"]],
)
def test_split_manifest_rejects_task_id_selectors(
    monkeypatch: Any,
    tmp_path: Path,
    selector: list[str],
) -> None:
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"task_ids": ["cell-1"]}), encoding="utf-8")
    (tmp_path / "ids.txt").write_text("cell-1\n", encoding="utf-8")
    parser = cli_module.build_parser()
    tasks = _tasks(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "download_verified", lambda _: tmp_path / "dataset")
    monkeypatch.setattr(cli_module, "load_verified_tasks", lambda _: tasks)
    monkeypatch.setattr(
        cli_module,
        "load_and_verify_trace2skill_split_manifest",
        lambda *_: {"valid": True, "task_ids": ["cell-1"]},
    )
    args = parser.parse_args(
        ["benchmark", "compare", "--split-manifest", str(split), *selector]
    )

    with pytest.raises(HarnessError, match="derivative manifest"):
        cli_module.cmd_benchmark_compare(args)


def test_comparison_uses_verified_manifest_order_without_rereading(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    split = tmp_path / "split.json"
    split.write_text("this is deliberately not JSON", encoding="utf-8")
    tasks = _tasks(tmp_path)
    parser = cli_module.build_parser()
    monkeypatch.setattr(cli_module, "download_verified", lambda _: tmp_path / "dataset")
    monkeypatch.setattr(cli_module, "load_verified_tasks", lambda _: tasks)
    monkeypatch.setattr(
        cli_module,
        "load_and_verify_trace2skill_split_manifest",
        lambda *_: {
            "valid": True,
            "manifest_id": "test-split",
            "schema_version": "test-split-v1",
            "manifest_sha256": "1" * 64,
            "usable_tasks": 2,
            "task_ids": ["sheet-1", "cell-1"],
            "task_ids_sha256": "2" * 64,
            "dataset_json_sha256": "3" * 64,
        },
    )
    monkeypatch.setattr(
        cli_module,
        "trace2skill_split_provenance",
        lambda report: {
            "manifest_id": report["manifest_id"],
            "schema_version": report["schema_version"],
            "manifest_sha256": report["manifest_sha256"],
            "task_count": report["usable_tasks"],
            "task_ids_sha256": report["task_ids_sha256"],
            "dataset_json_sha256": report["dataset_json_sha256"],
        },
    )
    captured: dict[str, Any] = {}

    class FakeRunner:
        def __init__(self, *_: Any, **kwargs: Any) -> None:
            captured["split_provenance"] = kwargs["split_provenance"]

        def run(self, selected: list[SpreadsheetTask]) -> dict[str, Any]:
            captured["task_ids"] = [task.task_id for task in selected]
            return {
                "missing_arm_tasks": 0,
                "arms": {"bare": {"errors": 0}, "ours": {"errors": 0}},
            }

    monkeypatch.setattr(cli_module, "ComparisonBenchmarkRunner", FakeRunner)
    monkeypatch.setattr(
        cli_module,
        "_provider",
        lambda _: ProviderConfig("https://example.test/v1", "key", "model"),
    )
    args = parser.parse_args(
        [
            "benchmark",
            "compare",
            "--split-manifest",
            str(split),
            "--output",
            str(tmp_path / "output"),
        ]
    )

    assert cli_module.cmd_benchmark_compare(args) == 0
    assert captured["task_ids"] == ["sheet-1", "cell-1"]
    assert captured["split_provenance"] == {
        "manifest_id": "test-split",
        "schema_version": "test-split-v1",
        "manifest_sha256": "1" * 64,
        "task_count": 2,
        "task_ids_sha256": "2" * 64,
        "dataset_json_sha256": "3" * 64,
    }


def test_comparison_rejects_unreachable_turn_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_model_calls must be at least"):
        ComparisonBenchmarkRunner(
            ProviderConfig("https://example.test/v1", "key", "model"),
            tmp_path / "unreachable-turns",
            skill_registry=SkillRegistry([]),
            max_model_calls=20,
            max_turns_per_arm=100,
        )


def _pilot_run_spec_runner(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    split_provenance: dict[str, Any] | None = None,
) -> tuple[ComparisonBenchmarkRunner, list[SpreadsheetTask]]:
    source = _tasks(tmp_path)[0]
    spec_path = Path(
        "benchmarks/protocols/qwen35-trace2skill-local-pilot16-run-spec-v1.json"
    )
    document, provenance, raw = load_pilot_run_spec(spec_path)
    if split_provenance is None:
        split_provenance = document["execution"]["split_provenance"]
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.verify_trace2skill_split_provenance",
        lambda _: True,
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison._dataset_manifest_sha256",
        lambda _: split_provenance["dataset_json_sha256"],
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.verify_repository_source_state",
        lambda: {
            "schema_version": 1,
            "git_commit": "1" * 40,
            "git_tree": "2" * 40,
            "remote_tracking_ref": "refs/remotes/origin/main",
            "remote_tracking_commit": "1" * 40,
            "source_fingerprint": {"sha256": "3" * 64, "files": []},
        },
    )
    pilot_ids = json.loads(
        Path(
            "benchmarks/protocols/"
            "qwen35-trace2skill-local-unattempted-pilot16-v2.json"
        ).read_text(encoding="utf-8")
    )["task_ids"]
    tasks = [
        SpreadsheetTask(
            task_id,
            source.instruction,
            source.input_path,
            source.golden_path,
            source.instruction_type,
            source.answer_position,
            source.answer_sheet,
        )
        for task_id in pilot_ids
    ]
    runner = ComparisonBenchmarkRunner(
        ProviderConfig(
            "http://101.37.174.109:8010/v1",
            "not-a-real-key",
            "qwen36-35b-a3b",
            reasoning_effort="none",
            timeout_seconds=700,
            max_retries=0,
            request_interval_seconds=0,
            temperature=1,
            top_p=1,
            seed=41,
            presence_penalty=2,
            top_k=40,
            min_p=0,
            repetition_penalty=1,
            enable_thinking=False,
            api_protocol="chat-completions",
            litellm_timeout_seconds=600,
        ),
        tmp_path / "pilot-output",
        skill_registry=SkillRegistry([Path("skills")]),
        arms=("bare", "ours"),
        max_model_calls=8,
        max_turns_per_arm=8,
        max_total_tokens=120000,
        max_output_tokens=4096,
        task_timeout_seconds=1200,
        recalculate=True,
        arm_order_seed=20260812,
        circuit_breaker_threshold=3,
        split_provenance=split_provenance,
        run_spec_document=document,
        run_spec_provenance=provenance,
        run_spec_bytes=raw,
    )
    return runner, tasks


def test_pilot_run_spec_rejects_noncanonical_bytes(tmp_path: Path) -> None:
    source = Path(
        "benchmarks/protocols/qwen35-trace2skill-local-pilot16-run-spec-v1.json"
    )
    target = tmp_path / source.name
    target.write_bytes(source.read_bytes() + b"\n")

    with pytest.raises(HarnessError, match="checksum"):
        load_pilot_run_spec(target)


def test_pilot_split_requires_canonical_run_spec_in_runner(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    document, _, _ = load_pilot_run_spec(
        Path("benchmarks/protocols/qwen35-trace2skill-local-pilot16-run-spec-v1.json")
    )
    runner, tasks = _pilot_run_spec_runner(tmp_path, monkeypatch)
    runner.run_spec_document = None
    runner.run_spec_provenance = None
    runner.run_spec_bytes = None

    with pytest.raises(HarnessError, match="requires its code-anchored run spec"):
        runner._manifest(tasks)


def test_repository_source_state_requires_clean_head_at_local_origin_main(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, ...]] = []
    outputs = iter(
        [
            str(tmp_path),
            "1" * 40,
            "2" * 40,
            "1" * 40,
            "",
            f"{'1' * 40}\trefs/heads/main",
        ]
    )

    def fake_run(command: list[str], **_: Any) -> Any:
        calls.append(tuple(command[-2:]))
        return type("Completed", (), {"returncode": 0, "stdout": next(outputs)})()

    monkeypatch.setattr("spreadsheet_harness.comparison.subprocess.run", fake_run)
    monkeypatch.setattr(
        "spreadsheet_harness.comparison._source_fingerprint",
        lambda: {"sha256": "3" * 64, "files": []},
    )

    state = verify_repository_source_state(tmp_path)

    assert state["git_commit"] == "1" * 40
    assert state["git_tree"] == "2" * 40
    assert state["remote_tracking_commit"] == state["git_commit"]
    assert state["remote_observed_commit"] == state["git_commit"]
    assert calls[-1] == ("origin", "refs/heads/main")


def test_repository_source_state_rejects_remote_head_drift(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    outputs = iter(
        [
            str(tmp_path),
            "1" * 40,
            "2" * 40,
            "1" * 40,
            "",
            f"{'4' * 40}\trefs/heads/main",
        ]
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.subprocess.run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": next(outputs)}
        )(),
    )

    with pytest.raises(HarnessError, match="observed origin/main"):
        verify_repository_source_state(tmp_path)


def test_repository_source_state_rejects_remote_query_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    outputs = iter([str(tmp_path), "1" * 40, "2" * 40, "1" * 40, ""])

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        if "ls-remote" in command:
            assert kwargs["timeout"] == 30
            return type("Completed", (), {"returncode": 2, "stdout": ""})()
        return type("Completed", (), {"returncode": 0, "stdout": next(outputs)})()

    monkeypatch.setattr("spreadsheet_harness.comparison.subprocess.run", fake_run)

    with pytest.raises(HarnessError, match="repository source state"):
        verify_repository_source_state(tmp_path)


def test_preflight_verifies_source_before_isolation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, tasks = _pilot_run_spec_runner(tmp_path, monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.verify_repository_source_state",
        lambda: events.append("source") or {"schema_version": 1},
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: events.append("isolation") or {},
    )

    manifest = runner.preflight(tasks)

    assert events == ["source", "isolation"]
    assert manifest["repository_source"] == {"schema_version": 1}
    assert not runner.output_dir.exists()


def test_pilot_run_spec_runner_rejects_resolved_contract_mismatch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    document, _, _ = load_pilot_run_spec(
        Path("benchmarks/protocols/qwen35-trace2skill-local-pilot16-run-spec-v1.json")
    )
    runner, tasks = _pilot_run_spec_runner(
        tmp_path,
        monkeypatch,
        split_provenance=document["execution"]["split_provenance"],
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.verify_trace2skill_split_provenance",
        lambda _: True,
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison._dataset_manifest_sha256",
        lambda _: document["execution"]["split_provenance"]["dataset_json_sha256"],
    )
    pilot_ids = json.loads(
        Path(
            "benchmarks/protocols/"
            "qwen35-trace2skill-local-unattempted-pilot16-v2.json"
        ).read_text(encoding="utf-8")
    )["task_ids"]
    source = tasks[0]
    tasks = [
        SpreadsheetTask(
            task_id,
            source.instruction,
            source.input_path,
            source.golden_path,
            source.instruction_type,
            source.answer_position,
            source.answer_sheet,
        )
        for task_id in pilot_ids
    ]
    runner.max_total_tokens += 1

    with pytest.raises(HarnessError, match="execution contract"):
        runner._manifest(tasks)

    assert not runner.output_dir.exists()


def test_comparison_crash_marker_blocks_ambiguous_replay(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, tasks = _pilot_run_spec_runner(tmp_path, monkeypatch)
    runner.output_dir.mkdir()
    runner.run_spec_copy_path.write_bytes(runner.run_spec_bytes or b"")
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner,
        "_run_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("crash")),
    )

    with pytest.raises(SystemExit, match="crash"):
        runner.run(tasks, resume=False)

    marker = json.loads(runner.inflight_path.read_text(encoding="utf-8"))
    assert marker["task_id"] == tasks[0].task_id
    assert marker["run_spec_provenance"] == runner.run_spec_provenance

    with pytest.raises(HarnessError, match="ambiguous in-flight"):
        runner.run(tasks, resume=True)


def test_comparison_seals_interrupted_marker_without_replay(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, tasks = _pilot_run_spec_runner(tmp_path, monkeypatch)
    runner.output_dir.mkdir()
    runner.run_spec_copy_path.write_bytes(runner.run_spec_bytes or b"")
    provider_calls: list[str] = []
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )
    runner.preflight(tasks)
    runner._prepare_manifest(tasks)
    manifest_sha = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    runner._write_inflight(
        tasks[0].task_id,
        "ours",
        comparison_manifest_sha256=manifest_sha,
    )
    marker_bytes = runner.inflight_path.read_bytes()
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.run_arm",
        lambda *_args, **_kwargs: provider_calls.append("run_arm"),
    )

    row = runner.seal_interrupted_inflight(tasks)

    assert row["status"] == "interrupted"
    assert row["passed"] is None
    assert row["error_category"] == "interrupted_unknown_outcome"
    assert row["replay_permitted"] is False
    assert not runner.inflight_path.exists()
    seals = json.loads(runner.interrupted_seals_path.read_text(encoding="utf-8"))
    assert seals == {"schema_version": 1, "seals": [row]}
    assert not runner.results_path.exists()
    continuation = json.loads(
        runner.continuation_source_path.read_text(encoding="utf-8")
    )
    assert continuation == runner.continuation_source_record
    assert continuation["comparison_manifest_sha256"] == manifest_sha
    assert continuation["repository_source"] == runner.repository_source_state
    assert provider_calls == []
    assert runner.seal_interrupted_inflight(tasks) == row

    runner.inflight_path.write_bytes(marker_bytes)
    assert runner.seal_interrupted_inflight(tasks) == row
    assert not runner.inflight_path.exists()

    sampled: list[tuple[str, str]] = []

    def stop_after_one(task: SpreadsheetTask, arm: str, **_: Any) -> dict[str, Any]:
        sampled.append((task.task_id, arm))
        return {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "comparison_manifest_sha256": manifest_sha,
            "split_provenance": runner.split_provenance,
            "run_spec_provenance": runner.run_spec_provenance,
            "status": "error",
            "passed": False,
            "error_category": "provider_fatal",
            "budget": {"used": {"model_calls": 0, "total_tokens": 0}},
        }

    monkeypatch.setattr(runner, "_run_one", stop_after_one)
    monkeypatch.setattr(
        "spreadsheet_harness.audit.audit_comparison",
        lambda *_args, **_kwargs: {
            "audit_valid": False,
            "reasons": ["expected incomplete test run"],
            "manifest_sha256": manifest_sha,
            "results_sha256": None,
        },
    )
    runner.run(tasks, resume=True)
    assert (tasks[0].task_id, "ours") not in sampled


def test_comparison_resume_clears_marker_after_bound_row_fsync(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, tasks = _pilot_run_spec_runner(tmp_path, monkeypatch)
    runner.output_dir.mkdir()
    runner.run_spec_copy_path.write_bytes(runner.run_spec_bytes or b"")
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )
    runner.preflight(tasks)
    runner._prepare_manifest(tasks)
    manifest_sha = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    task = tasks[0]
    arm = "ours"
    row = {
        "task_id": task.task_id,
        "arm": arm,
        "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
        "comparison_manifest_sha256": manifest_sha,
        "split_provenance": runner.split_provenance,
        "run_spec_provenance": runner.run_spec_provenance,
        "continuation_source": runner._prepare_continuation_source(
            comparison_manifest_sha256=manifest_sha
        ),
        "status": "error",
        "passed": False,
        "error_category": "provider_fatal",
        "budget": {"used": {"model_calls": 0, "total_tokens": 0}},
    }
    runner._write_inflight(task.task_id, arm, comparison_manifest_sha256=manifest_sha)
    runner._append(row)
    monkeypatch.setattr(
        "spreadsheet_harness.audit.audit_comparison",
        lambda *_args, **_kwargs: {
            "audit_valid": False,
            "reasons": ["expected incomplete test run"],
            "manifest_sha256": manifest_sha,
            "results_sha256": None,
        },
    )

    runner.run(tasks, resume=True)

    assert not runner.inflight_path.exists()
    assert runner.results_path.read_text(encoding="utf-8").count("\n") == 1


def test_interrupted_seals_reject_extra_document_fields(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, tasks = _pilot_run_spec_runner(tmp_path, monkeypatch)
    runner.output_dir.mkdir()
    runner.run_spec_copy_path.write_bytes(runner.run_spec_bytes or b"")
    runner.preflight(tasks)
    runner._prepare_manifest(tasks)
    manifest_sha = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    runner.interrupted_seals_path.write_text(
        json.dumps({"schema_version": 1, "seals": [], "extra": True}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="document is invalid"):
        runner._validate_interrupted_seals(
            tasks, comparison_manifest_sha256=manifest_sha
        )


def test_comparison_summary_uses_end_to_end_denominator_and_pairing(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path)
    rows = [
        {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "status": "completed",
            "passed": (
                arm == "ours" or (arm == "paper" and task.task_id == "cell-1")
            ),
            "elapsed_seconds": 10,
            "budget": {
                "used": {"model_calls": 2, "total_tokens": 120},
            },
            "agent": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
                "request_timings": [
                    {"turn": 1, "attempts": 1, "attempt_history": [{}]},
                    {"turn": 2, "attempts": 2, "attempt_history": [{}, {}]},
                ],
            },
        }
        for task in tasks
        for arm in COMPARISON_ARMS
        if not (task.task_id == "sheet-1" and arm == "bare")
    ]
    results = tmp_path / "results.jsonl"
    results.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    summary = comparison_summary(results, tasks)

    assert summary["expected_arm_tasks"] == 6
    assert summary["attempted_arm_tasks"] == 5
    assert summary["missing_arm_tasks"] == 1
    assert summary["arms"]["bare"]["end_to_end_accuracy"] == 0
    assert summary["arms"]["paper"]["end_to_end_accuracy"] == 0.5
    assert summary["arms"]["ours"]["end_to_end_accuracy"] == 1
    assert summary["arms"]["ours"]["total_tokens_sum"] == 240
    assert summary["arm_display_names"]["paper"] == "paper-inspired"
    assert summary["arms"]["ours"]["known_http_attempts_sum"] == 6
    assert summary["arms"]["ours"]["known_successful_request_retries_sum"] == 2
    assert summary["arms"]["ours"]["request_attempt_audit_complete"] is True
    assert summary["inference_valid"] is False
    assert "missing_arm_tasks" in summary["inference_invalid_reasons"]
    assert summary["arms"]["ours"]["cell_level"]["end_to_end_accuracy"] == 1
    assert summary["arms"]["ours"]["sheet_level"]["completion_rate"] == 1
    ours_vs_paper = summary["pairwise"]["paper_vs_ours"]
    assert ours_vs_paper["accuracy_delta_right_minus_left"] == 0.5
    assert ours_vs_paper["right_only_passes"] == 1


def test_comparison_summary_treats_sealed_unknown_as_nonprimary_not_missing(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    results = tmp_path / "sealed-summary.jsonl"
    rows = [
        {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "status": "completed",
            "passed": True,
            "elapsed_seconds": 1,
            "budget": {"used": {"model_calls": 1, "total_tokens": 1}},
            "agent": {"usage": {"total_tokens": 1}, "request_timings": []},
        }
        for task in tasks
        for arm in ("bare", "ours")
        if not (task.task_id == tasks[0].task_id and arm == "ours")
    ]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    interrupted = {f"{tasks[0].task_id}::ours"}

    summary = comparison_summary(
        results,
        tasks,
        arms=("bare", "ours"),
        interrupted_keys=interrupted,
    )

    assert summary["missing_arm_tasks"] == 0
    assert summary["interrupted_unknown_arm_tasks"] == 1
    assert summary["inference_valid"] is False
    assert summary["arms"]["ours"]["end_to_end_accuracy"] is None
    assert summary["arms"]["ours"]["wilson_95"] is None
    assert summary["arms"]["ours"]["cell_level"]["end_to_end_accuracy"] is None
    pair = summary["pairwise"]["bare_vs_ours"]
    assert pair["accuracy_delta_right_minus_left"] is None
    assert pair["mcnemar_exact_p"] is None
    assert pair["stratified_bootstrap_95"] is None
    assert pair["holm_adjusted_p"] is None
    assert pair["known_outcome_descriptive"]["primary"] is False


def test_comparison_summary_attaches_known_descriptive_to_every_pair(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    results = tmp_path / "multi-pair-sealed.jsonl"
    rows = [
        {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "status": "completed",
            "passed": True,
            "budget": {"used": {"model_calls": 0, "total_tokens": 0}},
        }
        for task in tasks
        for arm in COMPARISON_ARMS
        if not (task.task_id == tasks[0].task_id and arm == "ours")
    ]
    results.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    summary = comparison_summary(
        results,
        tasks,
        interrupted_keys={f"{tasks[0].task_id}::ours"},
    )

    assert all(
        pair["known_outcome_descriptive"]["primary"] is False
        for pair in summary["pairwise"].values()
    )


def test_comparison_summary_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    task = _tasks(tmp_path)[0]
    results = tmp_path / "duplicate-key.jsonl"
    results.write_text(
        '{"task_id":"cell-1","task_id":"cell-1","arm":"bare"}\n',
        encoding="utf-8",
    )

    summary = comparison_summary(results, [task], arms=("bare",))

    assert summary["invalid_result_rows_ignored"] == 1
    assert "invalid_result_rows" in summary["inference_invalid_reasons"]


def test_comparison_request_attempt_totals_are_known_lower_bounds_for_error_rows(
    tmp_path: Path,
) -> None:
    task = _tasks(tmp_path)[0]
    results = tmp_path / "error-results.jsonl"
    results.write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "arm": "bare",
                "status": "error",
                "passed": False,
                "error_category": "provider_transient",
                "provider_error": {"attempts": 2},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = comparison_summary(results, [task], arms=("bare",))
    bare = summary["arms"]["bare"]

    assert bare["known_successful_request_retries_sum"] == 0
    assert bare["known_http_attempts_sum"] == 2
    assert bare["known_failed_request_attempts_sum"] == 2
    assert bare["request_attempt_audit_rows"] == 1
    assert bare["request_attempt_audit_complete"] is False


@pytest.mark.parametrize("present_rows", [0, 1])
def test_comparison_request_attempt_audit_is_incomplete_when_expected_rows_are_missing(
    tmp_path: Path,
    present_rows: int,
) -> None:
    tasks = _tasks(tmp_path)
    rows = [
        {
            "task_id": task.task_id,
            "arm": "bare",
            "status": "completed",
            "passed": True,
            "budget": {"used": {"model_calls": 1, "total_tokens": 1}},
            "agent": {
                "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                "request_timings": [
                    {"turn": 1, "attempts": 1, "attempt_history": [{}]}
                ],
            },
        }
        for task in tasks[:present_rows]
    ]
    results = tmp_path / "missing-audit-rows.jsonl"
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    summary = comparison_summary(results, tasks, arms=("bare",))
    bare = summary["arms"]["bare"]

    assert bare["expected"] == 2
    assert bare["attempted"] == present_rows
    assert bare["request_attempt_audit_complete"] is False
    assert summary["inference_valid"] is False
    assert "request_attempt_audit_incomplete" in summary["inference_invalid_reasons"]


def test_comparison_summary_rejects_inexact_request_attempt_history(
    tmp_path: Path,
) -> None:
    task = _tasks(tmp_path)[0]
    row = {
        "task_id": task.task_id,
        "arm": "bare",
        "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
        "status": "completed",
        "passed": True,
        "budget": {"used": {"model_calls": 1, "total_tokens": 1}},
        "agent": {
            "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
            "request_timings": [
                {"turn": 1, "attempts": 2, "attempt_history": [{}]}
            ],
        },
    }
    results = tmp_path / "inexact-attempt-results.jsonl"
    results.write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = comparison_summary(results, [task], arms=("bare",))

    assert summary["arms"]["bare"]["request_attempt_audit_complete"] is False
    assert summary["inference_valid"] is False
    assert "request_attempt_audit_incomplete" in summary["inference_invalid_reasons"]


def test_comparison_summary_audits_unexpected_rows_and_disables_inference(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    expected_rows = [
        {
            "task_id": task.task_id,
            "arm": "bare",
            "status": "completed",
            "passed": True,
            "calculation_backend": backend,
        }
        for task, backend in zip(
            tasks, ("libreoffice", "not_recalculated"), strict=True
        )
    ]
    rows = [
        *expected_rows,
        dict(expected_rows[0]),
        {
            "task_id": "unknown-task",
            "arm": "bare",
            "status": "completed",
            "passed": True,
        },
        {
            "task_id": tasks[0].task_id,
            "arm": "paper",
            "status": "completed",
            "passed": True,
        },
        {
            "task_id": tasks[0].task_id,
            "arm": "alien",
            "status": "completed",
            "passed": True,
        },
    ]
    results = tmp_path / "integrity-results.jsonl"
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    summary = comparison_summary(results, tasks, arms=("bare",))

    assert summary["attempted_arm_tasks"] == 2
    assert summary["missing_arm_tasks"] == 0
    assert summary["errored_arm_tasks"] == 0
    assert summary["duplicate_arm_tasks"] == 1
    assert summary["duplicate_arm_task_rows"] == 1
    assert summary["duplicate_arm_task_keys"] == [f"{tasks[0].task_id}::bare"]
    assert summary["unknown_task_rows"] == 1
    assert summary["unknown_task_ids"] == ["unknown-task"]
    assert summary["unknown_arm_rows"] == 1
    assert summary["unknown_arms"] == ["alien"]
    assert summary["unexpected_arm_rows"] == 1
    assert summary["unexpected_arms"] == ["paper"]
    assert summary["style_checked"] is False
    assert summary["calculation_backends"] == {
        "libreoffice": 1,
        "not_recalculated": 1,
    }
    assert summary["inference_valid"] is False
    assert set(summary["inference_invalid_reasons"]) >= {
        "duplicate_arm_tasks",
        "unknown_tasks",
        "unknown_arms",
        "unexpected_arms",
    }
    pair = summary["pairwise"]
    assert pair == {}


def test_comparison_summary_clears_pairwise_inference_on_collection_pollution(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    rows = [
        {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "status": "completed",
            "passed": arm == "ours",
        }
        for task in tasks
        for arm in COMPARISON_ARMS
    ]
    rows.append(dict(rows[0]))
    results = tmp_path / "polluted-pairwise.jsonl"
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    summary = comparison_summary(results, tasks)

    assert summary["inference_valid"] is False
    for pair in summary["pairwise"].values():
        assert pair["inference_valid"] is False
        assert pair["stratified_bootstrap_95"] is None
        assert pair["mcnemar_exact_p"] is None
        assert pair["holm_adjusted_p"] is None
        assert "collection_integrity:duplicate_arm_tasks" in pair[
            "inference_invalid_reasons"
        ]
        for stratum in pair["strata"].values():
            assert stratum["inference_valid"] is False
            assert stratum["stratified_bootstrap_95"] is None
            assert stratum["mcnemar_exact_p"] is None


def test_comparison_summary_supports_preregistered_analysis_subset(
    tmp_path: Path,
) -> None:
    tasks = _tasks(tmp_path)
    rows = [
        {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "status": "completed",
            "passed": task.task_id == "cell-1",
            "calculation_backend": "libreoffice",
            "budget": {"used": {"model_calls": 1, "total_tokens": 1}},
            "agent": {
                "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                "request_timings": [
                    {"turn": 1, "attempts": 1, "attempt_history": [{}]}
                ],
            },
        }
        for task in tasks
        for arm in COMPARISON_ARMS
    ]
    results = tmp_path / "subset-results.jsonl"
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    full = comparison_summary(results, tasks)
    subset = comparison_summary(results, tasks[:1], collection_tasks=tasks)

    assert full["inference_valid"] is True
    assert subset["inference_valid"] is True
    assert subset["task_count"] == 1
    assert subset["expected_arm_tasks"] == subset["completed_arm_tasks"] == 3
    assert subset["unknown_task_rows"] == 0
    assert subset["arms"]["bare"]["end_to_end_accuracy"] == 1


@pytest.mark.parametrize("protocol", [None, "resource_matched_multi_arm_v19"])
def test_comparison_summary_rejects_missing_or_mismatched_row_protocol(
    tmp_path: Path,
    protocol: str | None,
) -> None:
    task = _tasks(tmp_path)[0]
    row = {
        "task_id": task.task_id,
        "arm": "bare",
        "status": "completed",
        "passed": True,
    }
    if protocol is not None:
        row["comparison_protocol_version"] = protocol
    results = tmp_path / "protocol-mismatch-results.jsonl"
    results.write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = comparison_summary(results, [task], arms=("bare",))

    assert summary["inference_valid"] is False
    assert summary["protocol_mismatch_rows"] == 1
    assert summary["observed_protocols"] == [protocol or "<missing>"]
    assert "comparison_protocol_mismatch" in summary["inference_invalid_reasons"]


def test_comparison_summary_rejects_analysis_outside_collection(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path)

    with pytest.raises(ValueError, match="subset of collection tasks"):
        comparison_summary(
            tmp_path / "missing.jsonl",
            tasks,
            collection_tasks=tasks[:1],
        )


def test_comparison_runner_refuses_duplicate_arm_task_rows(tmp_path: Path) -> None:
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "duplicate-resume",
        skill_registry=SkillRegistry([]),
    )
    row = {
        "task_id": "cell-1",
        "arm": "bare",
        "status": "completed",
        "passed": True,
    }
    runner.results_path.write_text(
        json.dumps(row) + "\n" + json.dumps(row) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="duplicate arm-task rows"):
        runner._latest()


def test_comparison_runner_refuses_protocol_mismatched_resume(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "mismatched-resume",
        skill_registry=SkillRegistry([]),
        arms=("bare",),
    )
    runner._prepare_manifest(tasks)
    manifest_sha256 = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    runner.results_path.write_text(
        json.dumps(
            {
                "task_id": tasks[0].task_id,
                "arm": "bare",
                "comparison_protocol_version": "resource_matched_multi_arm_v19",
                "comparison_manifest_sha256": manifest_sha256,
                "status": "completed",
                "passed": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )

    with pytest.raises(HarnessError, match="different or missing protocol"):
        runner.run(tasks)


def test_comparison_runner_refuses_split_provenance_mismatched_resume(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "split-mismatched-resume",
        skill_registry=SkillRegistry([]),
        arms=("bare",),
    )
    runner._prepare_manifest(tasks)
    manifest_sha256 = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    runner.results_path.write_text(
        json.dumps(
            {
                "task_id": tasks[0].task_id,
                "arm": "bare",
                "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
                "comparison_manifest_sha256": manifest_sha256,
                "split_provenance": {"attacker": True},
                "status": "completed",
                "passed": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner,
        "_run_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched split provenance must stop sampling")
        ),
    )

    with pytest.raises(HarnessError, match="split provenance"):
        runner.run(tasks)


@pytest.mark.parametrize(
    "damaged",
    [
        '{"task_id":',
        json.dumps(
            {
                "task_id": "cell-1",
                "arm": "bare",
                "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
                "status": "completed",
                "passed": True,
            }
        ),
    ],
)
def test_comparison_resume_fails_closed_on_damaged_journal_without_mutation(
    tmp_path: Path,
    monkeypatch: Any,
    damaged: str,
) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "damaged-resume",
        skill_registry=SkillRegistry([]),
        arms=("bare",),
    )
    runner._prepare_manifest(tasks)
    runner.results_path.write_text(damaged, encoding="utf-8")
    before = runner.results_path.read_bytes()
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )

    def must_not_run(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("damaged comparison journal must never be resampled")

    monkeypatch.setattr(runner, "_run_one", must_not_run)

    with pytest.raises(HarnessError, match="damaged or non-terminated"):
        runner.run(tasks)

    assert runner.results_path.read_bytes() == before


def test_comparison_resume_fails_closed_on_invalid_utf8_without_sampling(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "invalid-utf8-resume",
        skill_registry=SkillRegistry([]),
        arms=("bare",),
    )
    runner._prepare_manifest(tasks)
    manifest_sha256 = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    row = {
        "task_id": tasks[0].task_id,
        "arm": "bare",
        "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
        "comparison_manifest_sha256": manifest_sha256,
        "status": "completed",
        "agent": {"final_text": "CORRUPT"},
    }
    encoded = json.dumps(row).encode("utf-8").replace(b"CORRUPT", b"\xff") + b"\n"
    runner.results_path.write_bytes(encoded)
    before = runner.results_path.read_bytes()
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )

    def must_not_run(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("invalid UTF-8 comparison journal must stop all sampling")

    monkeypatch.setattr(runner, "_run_one", must_not_run)

    with pytest.raises(HarnessError, match="damaged or non-terminated"):
        runner.run(tasks)

    assert runner.results_path.read_bytes() == before


def test_comparison_runner_calls_arm_without_answer_metadata(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task = _tasks(tmp_path)[0]
    captured: dict[str, Any] = {}
    captured_pacers: list[Any] = []

    def fake_run_arm(**kwargs: Any) -> AgentResult:
        captured.update(kwargs)
        captured_pacers.append(kwargs["pacer"])
        return AgentResult("done", 1, 0, {}, "response")

    monkeypatch.setattr("spreadsheet_harness.comparison.run_arm", fake_run_arm)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "run-one",
        skill_registry=SkillRegistry([]),
        max_model_calls=100,
        max_turns_per_arm=100,
        recalculate=False,
    )

    row = runner._run_one(task, "bare", comparison_manifest_sha256="a" * 64)
    second_row = runner._run_one(task, "ours", comparison_manifest_sha256="a" * 64)

    assert row["status"] == "completed"
    assert second_row["status"] == "completed"
    assert len(row["output_sha256"]) == 64
    assert captured["instruction"] == task.instruction
    assert captured["max_turns_per_arm"] == 100
    assert captured_pacers == [runner.relay_pacer, runner.relay_pacer]
    assert row["max_model_calls"] == 100
    assert row["max_turns_per_arm"] == 100
    assert row["stage_turn_caps"] == {"solve": 100}
    assert row["comparison_manifest_sha256"] == "a" * 64
    assert row["request_interval_seconds"] == 0.0
    assert row["litellm_timeout_seconds"] is None
    assert "TOP_SECRET" not in json.dumps(captured, default=str)
    trajectory = read_trajectory(Path(row["run_dir"]) / "trajectory.jsonl")
    configured = [item for item in trajectory if item["event"] == "benchmark.configured"]
    assert configured[0]["payload"]["max_model_calls"] == 100
    assert configured[0]["payload"]["max_turns_per_arm"] == 100
    assert configured[0]["payload"]["stage_turn_caps"] == {"solve": 100}
    assert configured[0]["payload"]["litellm_timeout_seconds"] is None
    evaluation = [item for item in trajectory if item["event"] == "benchmark.evaluated"]
    assert len(evaluation) == 1
    assert evaluation[0]["payload"]["passed"] is False
    assert evaluation[0]["payload"]["arm"] == "bare"
    assert evaluation[0]["payload"]["difference_count"] == 1
    assert evaluation[0]["payload"]["difference_categories"] == {
        "metadata_or_structure": 1
    }


def test_comparison_run_binds_result_row_to_exact_manifest(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks = _tasks(tmp_path)[:1]
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "manifest-bound",
        skill_registry=SkillRegistry([]),
        arms=("bare",),
        recalculate=False,
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )
    captured: dict[str, Any] = {}

    def fake_run_one(*_: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        task = tasks[0]
        manifest = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
        task_dir = runner.output_dir / "runs" / task.task_id / "bare"
        output = task_dir / "artifacts" / "output.xlsx"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(task.golden_path.read_bytes())
        output_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
        timings = [
            {
                "turn": turn,
                "attempts": 1,
                "attempt_history": [
                    {"api_protocol": "responses", "endpoint": "/responses"}
                ],
            }
            for turn in range(1, 4)
        ]
        budget = {
            "limit": {
                "model_calls": 20,
                "total_tokens": 100_000,
                "elapsed_seconds": 900,
            },
            "used": {"model_calls": 3, "total_tokens": 10, "elapsed_seconds": 1.0},
            "termination": None,
        }
        comparison = compare_workbooks(
            task.golden_path,
            output,
            task.answer_position,
            answer_sheet=task.answer_sheet,
        ).to_dict()
        return {
            "task_id": task.task_id,
            "arm": "bare",
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "comparison_manifest_sha256": kwargs["comparison_manifest_sha256"],
            "instruction_type": task.instruction_type,
            "model": "test-model",
            "api_protocol": "responses",
            "requested_reasoning_effort": manifest["configuration"][
                "requested_reasoning_effort"
            ],
            "reasoning_effort": manifest["configuration"]["reasoning_effort"],
            "request_interval_seconds": 0.0,
            "litellm_timeout_seconds": None,
            "generation": manifest["configuration"]["generation"],
            "max_model_calls": 20,
            "max_turns_per_arm": 20,
            "stage_turn_caps": {"solve": 20},
            "calculation_backend": "not_recalculated",
            "status": "completed",
            "passed": comparison["passed"],
            "comparison": comparison,
            "run_dir": str(task_dir),
            "output_workbook": str(output),
            "output_sha256": output_sha256,
            "budget": budget,
            "agent": {
                "arm": "bare",
                "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
                "request_timings": timings,
                "budget": budget,
                "stages": [
                    {
                        "name": "solve",
                        "max_turns": 20,
                        "allowed_tools": ["code_interpreter"],
                        "first_tool_choice": "code_interpreter",
                        "observed_first_tool": "code_interpreter",
                        "forced_tool_prefix": ["code_interpreter", "code_interpreter"],
                        "observed_forced_tool_prefix": [
                            "code_interpreter",
                            "code_interpreter",
                        ],
                        "terminal_tool": "submit_result",
                        "observed_terminal_tool": "submit_result",
                        "agent": {"turns": 3, "request_timings": timings},
                    }
                ],
            },
        }

    monkeypatch.setattr(runner, "_run_one", fake_run_one)

    summary = runner.run(tasks)

    expected = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    assert captured["comparison_manifest_sha256"] == expected
    row = json.loads(runner.results_path.read_text(encoding="utf-8"))
    assert row["comparison_manifest_sha256"] == expected
    assert row["status"] == "completed"
    assert summary["protocol_audit_valid"] is True
    assert summary["inference_valid"] is True

    row["model"] = "tampered-model"
    runner.results_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    rerun = runner.run(tasks)
    assert rerun["protocol_audit_valid"] is False
    assert rerun["inference_valid"] is False
    assert "comparison_audit_failed" in rerun["inference_invalid_reasons"]


def test_comparison_classifies_paper_stage_validation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task = _tasks(tmp_path)[0]

    def fail_paper(**_: Any) -> AgentResult:
        raise PaperStageValidationError("vision_verify", "view_image was not attached")

    monkeypatch.setattr("spreadsheet_harness.comparison.run_arm", fail_paper)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "paper-validation",
        skill_registry=SkillRegistry([]),
        recalculate=False,
    )

    row = runner._run_one(task, "paper", comparison_manifest_sha256="a" * 64)

    assert row["status"] == "error"
    assert row["error_category"] == "paper_stage_validation"
    assert row["paper_stage"] == "vision_verify"
    assert row["paper_stage_reason"] == "view_image was not attached"


def test_comparison_classifies_required_routing_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task = _tasks(tmp_path)[0]

    def fail_routing(**_: Any) -> AgentResult:
        raise AgentRoutingError("required route returned no function call")

    monkeypatch.setattr("spreadsheet_harness.comparison.run_arm", fail_routing)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "routing-validation",
        skill_registry=SkillRegistry([]),
        recalculate=False,
    )

    row = runner._run_one(task, "bare", comparison_manifest_sha256="a" * 64)

    assert row["status"] == "error"
    assert row["error_category"] == "routing_protocol"


def test_comparison_counts_ambiguous_delivery_as_transient_but_not_retryable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task = _tasks(tmp_path)[0]

    def fail_ambiguous(**_: Any) -> AgentResult:
        raise ProviderError(
            "Responses request timed out during read",
            retryable=True,
            safe_to_retry=False,
            phase="read",
            delivery_state="ambiguous_post_send",
        )

    monkeypatch.setattr("spreadsheet_harness.comparison.run_arm", fail_ambiguous)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "ambiguous-delivery",
        skill_registry=SkillRegistry([]),
        recalculate=False,
    )

    row = runner._run_one(task, "bare", comparison_manifest_sha256="a" * 64)

    assert row["status"] == "error"
    assert row["error_category"] == "provider_transient"
    assert row["error_retryable"] is False
    assert row["provider_error"]["safe_to_retry"] is False
    assert row["provider_error"]["delivery_state"] == "ambiguous_post_send"


def test_resume_preserves_historical_provider_circuit_breaker(
    tmp_path: Path, monkeypatch: Any
) -> None:
    tasks = _tasks(tmp_path)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            request_interval_seconds=20.0,
        ),
        tmp_path / "resume-breaker",
        skill_registry=SkillRegistry([]),
    )
    runner._prepare_manifest(tasks)
    manifest_sha256 = hashlib.sha256(runner.manifest_path.read_bytes()).hexdigest()
    runner.results_path.write_text(
        json.dumps(
            {
                "task_id": tasks[0].task_id,
                "arm": "bare",
                "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
                "comparison_manifest_sha256": manifest_sha256,
                "status": "error",
                "passed": False,
                "error_category": "provider_fatal",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        lambda *_args, **_kwargs: {},
    )

    def must_not_run(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("historical fatal provider error must keep breaker open")

    monkeypatch.setattr(runner, "_run_one", must_not_run)

    summary = runner.run(tasks)

    assert summary["circuit_breaker_tripped"] is True
    assert summary["fatal_provider_arm_tasks"] == 1
    assert summary["attempted_arm_tasks"] == 1


def test_end_to_end_deadline_covers_scoring(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task = _tasks(tmp_path)[0]
    now = [100.0]
    monkeypatch.setattr("spreadsheet_harness.comparison.monotonic", lambda: now[0])
    monkeypatch.setattr("spreadsheet_harness.budget.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.run_arm",
        lambda **_: AgentResult("done", 1, 0, {}, "response"),
    )

    def slow_score(*_: Any, **__: Any) -> Any:
        now[0] = 102.0
        return type("Comparison", (), {"passed": True, "to_dict": lambda self: {}})()

    monkeypatch.setattr("spreadsheet_harness.comparison.compare_workbooks", slow_score)
    runner = ComparisonBenchmarkRunner(
        ProviderConfig("https://example.test/v1", "not-a-real-key", "test-model"),
        tmp_path / "deadline",
        skill_registry=SkillRegistry([]),
        task_timeout_seconds=1,
        recalculate=False,
    )

    row = runner._run_one(task, "bare", comparison_manifest_sha256="a" * 64)

    assert row["status"] == "error"
    assert row["error_category"] == "budget_exhausted"
    assert row["budget"]["termination"]["reason"] == "max_elapsed_seconds"


def test_comparison_fails_before_writes_when_strict_isolation_is_unavailable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks = _tasks(tmp_path)

    def unavailable(*_: Any, **__: Any) -> dict[str, str]:
        raise CodeIsolationError("bwrap probe failed")

    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation",
        unavailable,
    )
    output = tmp_path / "strict-failure"
    runner = ComparisonBenchmarkRunner(
        ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "test-model",
            request_interval_seconds=20.0,
        ),
        output,
        skill_registry=SkillRegistry([]),
        recalculate=False,
    )

    with pytest.raises(CodeIsolationError, match="bwrap probe failed"):
        runner.run(tasks)

    assert not runner.manifest_path.exists()
    assert not runner.results_path.exists()
