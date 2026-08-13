from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from spreadsheet_harness import cli as cli_module
from spreadsheet_harness.agent import AgentResult
from spreadsheet_harness.arms import PaperStageValidationError
from spreadsheet_harness.benchmark import SpreadsheetTask
from spreadsheet_harness.comparison import (
    AVAILABLE_COMPARISON_ARMS,
    COMPARISON_ARMS,
    COMPARISON_PROTOCOL_VERSION,
    ComparisonBenchmarkRunner,
    _arm_order,
    _balanced_arm_orders,
    comparison_summary,
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
    assert manifest["schema_version"] == 10
    assert manifest["comparison_protocol_version"] == COMPARISON_PROTOCOL_VERSION
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
        "ours": {"solve": ["list_sheets", "inspect_range"]},
    }
    assert manifest["post_prefix_routing"] == {
        "tool_choice": "auto",
        "terminal_tool": "submit_result",
        "applies_to": "comparison stages with workbook tools after forced prefix",
        "direct_text_stages": ["paper.reconcile"],
    }
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
        "verify_trace2skill_heldout_manifest",
        lambda *_: {"valid": True},
    )

    for selector in (["--offset", "1"], ["--limit", "1"]):
        args = parser.parse_args(
            ["benchmark", "compare", "--split-manifest", str(split), *selector]
        )
        with pytest.raises(HarnessError, match="derivative manifest"):
            cli_module.cmd_benchmark_compare(args)


def test_comparison_rejects_unreachable_turn_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_model_calls must be at least"):
        ComparisonBenchmarkRunner(
            ProviderConfig("https://example.test/v1", "key", "model"),
            tmp_path / "unreachable-turns",
            skill_registry=SkillRegistry([]),
            max_model_calls=20,
            max_turns_per_arm=100,
        )


def test_comparison_summary_uses_end_to_end_denominator_and_pairing(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path)
    rows = [
        {
            "task_id": task.task_id,
            "arm": arm,
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
                    {"turn": 1, "attempts": 1},
                    {"turn": 2, "attempts": 2},
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
                "request_timings": [{"turn": 1, "attempts": 1}],
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
            "status": "completed",
            "passed": task.task_id == "cell-1",
            "calculation_backend": "libreoffice",
            "budget": {"used": {"model_calls": 1, "total_tokens": 1}},
            "agent": {
                "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                "request_timings": [{"turn": 1, "attempts": 1}],
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

    row = runner._run_one(task, "bare")
    second_row = runner._run_one(task, "ours")

    assert row["status"] == "completed"
    assert second_row["status"] == "completed"
    assert len(row["output_sha256"]) == 64
    assert captured["instruction"] == task.instruction
    assert captured["max_turns_per_arm"] == 100
    assert captured_pacers == [runner.relay_pacer, runner.relay_pacer]
    assert row["max_model_calls"] == 100
    assert row["max_turns_per_arm"] == 100
    assert row["stage_turn_caps"] == {"solve": 100}
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

    row = runner._run_one(task, "paper")

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

    row = runner._run_one(task, "bare")

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

    row = runner._run_one(task, "bare")

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
    runner.results_path.write_text(
        json.dumps(
            {
                "task_id": tasks[0].task_id,
                "arm": "bare",
                "status": "error",
                "passed": False,
                "error_category": "provider_fatal",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "spreadsheet_harness.comparison.ensure_strict_code_isolation", lambda: {}
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

    row = runner._run_one(task, "bare")

    assert row["status"] == "error"
    assert row["error_category"] == "budget_exhausted"
    assert row["budget"]["termination"]["reason"] == "max_elapsed_seconds"


def test_comparison_fails_before_writes_when_strict_isolation_is_unavailable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tasks = _tasks(tmp_path)

    def unavailable() -> dict[str, str]:
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
