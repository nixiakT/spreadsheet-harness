from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spreadsheet_harness import evolution
from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.evolution import (
    PromotionRejected,
    extract_trajectory_evidence,
    generate_candidate,
    promote_candidate,
)


def _write_trajectory(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row(event: str, payload: dict[str, Any], *, run_id: str = "run-1") -> dict[str, Any]:
    return {
        "timestamp": "2026-08-11T00:00:00+00:00",
        "run_id": run_id,
        "event": event,
        "payload": payload,
    }


class FakeResponsesClient:
    def __init__(self, responses: list[str], model: str = "mock-model") -> None:
        self.responses = list(responses)
        self.config = SimpleNamespace(model=model)
        self.payloads: list[dict[str, Any]] = []

    def create(self, payload: dict[str, Any]) -> SimpleNamespace:
        self.payloads.append(payload)
        text = self.responses.pop(0)
        return SimpleNamespace(text=text, response_id=f"response-{len(self.payloads)}")


class ConfiguredFakeResponsesClient(FakeResponsesClient):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.config = ProviderConfig(
            "https://example.test/v1",
            "not-a-real-key",
            "Qwen/Qwen3.5-35B-A3B",
            temperature=1.0,
            top_k=40,
            min_p=0.0,
            enable_thinking=False,
        )


def _candidate_dir(tmp_path: Path, content: str | None = None) -> Path:
    candidate = tmp_path / "candidates" / "candidate-1"
    candidate.mkdir(parents=True)
    skill = content or (
        "---\n"
        "name: spreadsheet-core\n"
        "description: Validated spreadsheet procedure candidate.\n"
        "---\n\n"
        "# Procedure\n\nInspect, edit, and verify.\n"
    )
    (candidate / "SKILL.md").write_text(skill, encoding="utf-8")
    digest = hashlib.sha256(skill.encode("utf-8")).hexdigest()
    (candidate / "provenance.json").write_text(
        json.dumps({"candidate_sha256": digest, "model": "mock-model"}),
        encoding="utf-8",
    )
    return candidate


def _passing_report() -> dict[str, Any]:
    return {
        "paired_results": [
            {"seed": "a", "baseline": 0.50, "candidate": 0.75},
            {"seed": "b", "baseline": 0.60, "candidate": 0.55},
            {"seed": "c", "baseline": 0.40, "candidate": 0.80},
        ],
        "severe_regression": False,
    }


def test_extracts_success_failure_and_tool_error_evidence(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    _write_trajectory(
        trajectory,
        [
            _row("tool.called", {"name": "write_cells", "arguments": {"range": "A1"}}),
            _row(
                "tool.returned",
                {
                    "name": "write_cells",
                    "result": {
                        "ok": False,
                        "error": f"bad formula with {raw_secret}",
                        "type": "ToolInputError",
                    },
                },
            ),
            _row(
                "workbook.mutation.rolled_back",
                {"operation": "write_cells", "error": "Workbook invalid"},
            ),
            _row("evaluation.completed", {"passed": True, "score": 1.0}),
            _row("agent.completed", {"final_text": "Updated and verified", "turns": 2}),
        ],
    )

    evidence = extract_trajectory_evidence(trajectory)

    assert evidence.sha256 == hashlib.sha256(trajectory.read_bytes()).hexdigest()
    assert evidence.event_count == 5
    assert evidence.run_ids == ("run-1",)
    assert {item["event"] for item in evidence.successes} == {"evaluation.completed"}
    assert [item["event"] for item in evidence.failures] == ["workbook.mutation.rolled_back"]
    assert evidence.evaluator_outcome is not None
    assert evidence.evaluator_outcome["passed"] is True
    assert len(evidence.tool_errors) == 1
    assert evidence.tool_errors[0]["tool"] == "write_cells"
    assert evidence.tool_errors[0]["arguments"] == {"range": "A1"}
    assert raw_secret not in json.dumps(evidence.for_prompt())
    assert "[REDACTED]" in evidence.tool_errors[0]["error"]


def test_evaluator_failure_overrides_agent_completion_as_correctness(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(
        trajectory,
        [
            _row("workbook.mutation.committed", {"operation": "write_range"}),
            _row("agent.completed", {"final_text": "done"}),
            _row(
                "benchmark.evaluated",
                {"passed": False, "status": "completed", "checked_cells": 2},
            ),
        ],
    )

    evidence = extract_trajectory_evidence(trajectory)

    assert evidence.successes == ()
    assert [item["event"] for item in evidence.failures] == ["benchmark.evaluated"]
    assert evidence.evaluator_outcome is not None
    assert evidence.evaluator_outcome["passed"] is False
    assert evidence.for_prompt()["evaluator_outcome"]["passed"] is False


def test_conflicting_evaluator_outcomes_are_rejected(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(
        trajectory,
        [
            _row("evaluation.completed", {"passed": True}),
            _row("benchmark.evaluated", {"passed": False}),
        ],
    )

    with pytest.raises(ValueError, match="Conflicting evaluator outcomes"):
        extract_trajectory_evidence(trajectory)


def test_two_stage_generation_writes_only_candidate_with_provenance(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_trajectory(
        first,
        [
            _row("agent.completed", {"final_text": "success"}, run_id="a"),
            _row("benchmark.evaluated", {"passed": True}, run_id="a"),
        ],
    )
    _write_trajectory(
        second,
        [_row("evaluation.failed", {"passed": False, "reason": "formula mismatch"}, run_id="b")],
    )
    production = tmp_path / "skills" / "spreadsheet-core"
    production.mkdir(parents=True)
    production_skill = production / "SKILL.md"
    production_skill.write_text("production remains untouched\n", encoding="utf-8")
    candidate_skill = (
        "---\n"
        "name: spreadsheet-core\n"
        "description: Evidence-derived spreadsheet procedures.\n"
        "---\n\n"
        "# Workflow\n\nAlways verify formulas.\n"
    )
    client = FakeResponsesClient(["lesson one", "lesson two", candidate_skill])

    candidate = generate_candidate(
        [first, second],
        tmp_path,
        client,
        model="mock-model",
        candidate_id="test-candidate",
    )

    assert len(client.payloads) == 3
    assert all(payload["model"] == "mock-model" for payload in client.payloads)
    assert all(payload["store"] is False for payload in client.payloads)
    assert "success_evidence" in client.payloads[0]["input"][0]["content"][0]["text"]
    assert "failure_evidence" in client.payloads[1]["input"][0]["content"][0]["text"]
    assert "lesson one" in client.payloads[2]["input"][0]["content"][0]["text"]
    assert candidate.path == tmp_path / "candidates" / "test-candidate"
    assert {item.name for item in candidate.path.iterdir()} == {
        "SKILL.md",
        "lessons.json",
        "provenance.json",
    }
    assert candidate.skill_path.read_text(encoding="utf-8") == candidate_skill
    provenance = json.loads(candidate.provenance_path.read_text(encoding="utf-8"))
    assert provenance["model"] == "mock-model"
    assert provenance["input_hashes"] == [
        hashlib.sha256(first.read_bytes()).hexdigest(),
        hashlib.sha256(second.read_bytes()).hexdigest(),
    ]
    assert provenance["candidate_sha256"] == candidate.sha256
    assert production_skill.read_text(encoding="utf-8") == "production remains untouched\n"


def test_generation_rejects_trajectory_without_evaluator_outcome(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [_row("agent.completed", {"final_text": "done"})])
    client = FakeResponsesClient([])

    with pytest.raises(ValueError, match="explicit evaluator outcome"):
        generate_candidate([trajectory], tmp_path, client, candidate_id="unevaluated")

    assert client.payloads == []
    assert not (tmp_path / "candidates").exists()


def test_generation_repairs_invalid_frontmatter_without_repeating_lessons(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [_row("benchmark.evaluated", {"passed": False})])
    valid = "---\nname: spreadsheet-core\ndescription: A bounded repair.\n---\n\nInspect target boundaries.\n"
    client = FakeResponsesClient(["lesson", "Missing YAML", valid])
    candidate = generate_candidate([trajectory], tmp_path, client, candidate_id="repair")
    assert len(client.payloads) == 3
    assert candidate.skill_path.read_text() == valid
    provenance = json.loads(candidate.provenance_path.read_text())
    assert provenance["format_repair_count"] == 1


def test_generation_resumes_lessons_after_a_failed_consolidation(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [_row("benchmark.evaluated", {"passed": False})])
    client = FakeResponsesClient(["lesson", "Missing YAML"])
    with pytest.raises(ValueError, match="YAML frontmatter"):
        generate_candidate([trajectory], tmp_path, client, candidate_id="resume", format_retries=0)
    valid = "---\nname: spreadsheet-core\ndescription: A bounded repair.\n---\n\nInspect target boundaries.\n"
    second = FakeResponsesClient([valid])
    candidate = generate_candidate([trajectory], tmp_path, second, candidate_id="resume")
    assert len(second.payloads) == 1
    assert candidate.skill_path.read_text() == valid


def test_candidate_identity_and_empty_body_are_rejected() -> None:
    with pytest.raises(ValueError, match="name must be"):
        evolution._normalize_skill(
            "---\nname: wrong\ndescription: Description.\n---\n\nBody.", expected_name="correct"
        )
    with pytest.raises(ValueError, match="nonempty instruction body"):
        evolution._normalize_skill("---\nname: ok\ndescription: Description.\n---\n\n")


def test_candidate_normalization_recovers_thinking_provider_preamble() -> None:
    wrapped = (
        "I will compare several remedies internally.\n"
        "</think>---\n"
        "name: spreadsheet-core\n"
        "description: A bounded repair.\n"
        "---\n\n"
        "Inspect target boundaries.\n"
    )
    assert evolution._normalize_skill(wrapped, expected_name="spreadsheet-core") == (
        "---\n"
        "name: spreadsheet-core\n"
        "description: A bounded repair.\n"
        "---\n\n"
        "Inspect target boundaries.\n"
    )


def test_candidate_normalization_does_not_recover_wrong_skill_identity() -> None:
    wrapped = (
        "analysis\n---\nname: wrong\ndescription: Wrong coordinate.\n---\n\nBody."
    )
    with pytest.raises(ValueError, match="YAML frontmatter"):
        evolution._normalize_skill(wrapped, expected_name="spreadsheet-core")


def test_consolidation_prompt_distinguishes_repair_and_negative_evidence() -> None:
    prompt = evolution._CONSOLIDATION_INSTRUCTIONS
    assert "repair`` is a failure of the accepted incumbent" in prompt
    assert "rejected-candidate-regression" in prompt
    assert "do not turn those failures into desired\nbehavior" in prompt


def test_evidence_contains_tool_actions_and_original_request(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [
        _row("agent.started", {"instruction": "Fill A1 only", "skills": []}),
        _row("tool.called", {"name": "write", "arguments": {"cell": "A2"}}),
        _row("tool.returned", {"name": "write", "result": {"ok": True}}),
        _row("benchmark.evaluated", {"passed": False}),
    ])
    context = extract_trajectory_evidence(trajectory).for_prompt()["execution_context"]
    assert context[0]["payload"]["instruction"] == "Fill A1 only"
    assert context[1]["payload"]["arguments"]["cell"] == "A2"


def test_early_planner_writes_survive_long_readonly_tail(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    rows = [
        _row("agent.started", {"instruction": "Complete requested formulas."}),
        _row("harness.planner_actions.applied", {"count": 23, "actions": []}),
        _row("workbook.mutation.committed", {}),
    ]
    for _ in range(30):
        rows.extend([
            _row("tool.called", {"name": "code_interpreter", "arguments": {"code": "read()"}}),
            _row("tool.returned", {"name": "code_interpreter", "result": {"ok": True, "workbook_changed": False}}),
        ])
    rows.append(_row("benchmark.evaluated", {"passed": False}))
    _write_trajectory(trajectory, rows)
    evidence = extract_trajectory_evidence(trajectory, max_items_per_category=2)
    assert evidence.diagnostics["planner_applied_writes"] == 23
    assert evidence.diagnostics["max_readonly_code_streak"] == 30
    assert evidence.diagnostics["committed_mutations"] == 1
    assert any(e["event"] == "harness.planner_actions.applied" for e in evidence.decision_events)
    assert evidence.decision_events[0]["event_index"] == 2


def test_stderr_tail_retains_actual_exception_and_redacts_secrets(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    _write_trajectory(trajectory, [
        _row("tool.called", {"name": "code_interpreter", "arguments": {"code": "x[d]"}}),
        _row("tool.returned", {"name": "code_interpreter", "result": {
            "ok": False, "exit_code": 1,
            "stderr": "Traceback\n" + "frame\n" * 1000 + secret + "\nTypeError: unhashable type: 'dict'\n",
        }}),
        _row("benchmark.evaluated", {"passed": False}),
    ])
    evidence = extract_trajectory_evidence(trajectory)
    error = evidence.tool_errors[0]
    assert error["error_type"] == "TypeError"
    assert "unhashable type" in error["error"]
    assert error["event_index"] == 2
    assert secret not in json.dumps(evidence.for_prompt())
    assert "[REDACTED]" in error["stderr_tail"]


def test_proposer_projection_withholds_reference_answers_not_audit_evidence() -> None:
    raw = {"official_score": {"error_message": "Regression error at Income!E34: answer=-126685.714, output=0"},
           "golden_path": "/secret/case_golden.xlsx", "expected_value": 1234,
           "trace_path": "/replay/trajectory.jsonl"}
    clean = evolution.proposer_visible(raw)
    assert "-126685.714" not in json.dumps(clean)
    assert "expected_value" not in clean and "golden_path" not in clean
    assert "output=0" in clean["official_score"]["error_message"]
    assert clean["trace_path"] == raw["trace_path"]
    assert "-126685.714" in raw["official_score"]["error_message"]


def test_proposer_keeps_model_proposed_formula_not_golden_values() -> None:
    raw = {"event": "harness.planner_actions.applied", "payload": {
        "actions": [{"target": "E11", "expected_value": "=SUM(E7:E10)"}]
    }}
    clean = evolution.proposer_visible(raw)
    assert clean["payload"]["actions"][0]["expected_value"] == "=SUM(E7:E10)"


def test_evolution_generation_uses_and_records_provider_controls(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [_row("benchmark.evaluated", {"passed": True})])
    candidate_skill = (
        "---\n"
        "name: spreadsheet-core\n"
        "description: Evidence-derived spreadsheet procedures.\n"
        "---\n\n"
        "# Workflow\n\nInspect and verify.\n"
    )
    client = ConfiguredFakeResponsesClient(["lesson", candidate_skill])

    candidate = generate_candidate(
        [trajectory], tmp_path, client, candidate_id="configured-candidate"
    )

    for payload in client.payloads:
        assert payload["temperature"] == 1.0
        assert payload["extra_body"] == {
            "top_k": 40,
            "min_p": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    provenance = json.loads(candidate.provenance_path.read_text(encoding="utf-8"))
    assert provenance["generation"] == {
        "temperature": 1.0,
        "top_k": 40,
        "min_p": 0.0,
        "enable_thinking": False,
    }


def test_generation_can_omit_output_token_limits(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [_row("benchmark.evaluated", {"passed": False})])
    candidate_skill = (
        "---\n"
        "name: spreadsheet-core\n"
        "description: Evidence-derived spreadsheet procedures.\n"
        "---\n\n"
        "# Workflow\n\nInspect and verify.\n"
    )
    client = FakeResponsesClient(["lesson", candidate_skill])

    candidate = generate_candidate(
        [trajectory],
        tmp_path,
        client,
        candidate_id="unlimited-candidate",
        lesson_max_output_tokens=None,
        consolidation_max_output_tokens=None,
    )

    assert all("max_output_tokens" not in payload for payload in client.payloads)
    provenance = json.loads(candidate.provenance_path.read_text(encoding="utf-8"))
    assert provenance["output_limits"] == {
        "lesson_max_output_tokens": None,
        "consolidation_max_output_tokens": None,
    }


def test_generation_can_constrain_candidate_to_base_skill(tmp_path: Path) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    _write_trajectory(trajectory, [_row("benchmark.evaluated", {"passed": False})])
    base_skill = tmp_path / "base-SKILL.md"
    base_skill.write_text(
        "---\nname: spreadsheet-structure\ndescription: Base structure rules.\n---\n\n"
        "# Structure\n\nInspect bounded ranges and preserve anchors.\n",
        encoding="utf-8",
    )
    candidate_skill = (
        "---\nname: spreadsheet-structure\ndescription: Minimal structure patch.\n---\n\n"
        "# Structure\n\nInspect bounded ranges and preserve anchors before edits.\n"
    )
    client = FakeResponsesClient(["lesson", candidate_skill])

    candidate = generate_candidate(
        [trajectory],
        tmp_path,
        client,
        candidate_id="base-constrained",
        skill_name="spreadsheet-structure",
        base_skill=base_skill,
    )

    consolidation_prompt = client.payloads[-1]["input"][0]["content"][0]["text"]
    assert "base_skill" in consolidation_prompt
    assert "preserve anchors" in consolidation_prompt
    provenance = json.loads(candidate.provenance_path.read_text(encoding="utf-8"))
    assert provenance["base_skill"] == str(base_skill)
    assert provenance["base_skill_sha256"] == hashlib.sha256(base_skill.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("report", "min_delta", "message"),
    [
        (
            {
                "paired_results": [
                    {"seed": 1, "baseline": 0.2, "candidate": 0.8},
                    {"seed": 2, "baseline": 0.2, "candidate": 0.8},
                ]
            },
            0,
            "At least 3",
        ),
        (
            {
                "paired_results": [
                    {"seed": 1, "baseline": 0.5, "candidate": 0.5},
                    {"seed": 2, "baseline": 0.5, "candidate": 0.5},
                    {"seed": 3, "baseline": 0.5, "candidate": 0.5},
                ]
            },
            0,
            "strictly greater",
        ),
        (
            {
                "paired_results": [
                    {"seed": 1, "baseline": 0.1, "candidate": 0.9},
                    {
                        "seed": 2,
                        "baseline": 0.1,
                        "candidate": 0.9,
                        "regression_severity": "critical",
                    },
                    {"seed": 3, "baseline": 0.1, "candidate": 0.9},
                ]
            },
            0,
            "severe regression",
        ),
        (
            {
                "paired_results": [
                    {"seed": 1, "baseline": 0.5, "candidate": 0.6},
                    {"seed": 2, "baseline": 0.5, "candidate": 0.6},
                    {"seed": 3, "baseline": 0.5, "candidate": 0.6},
                ]
            },
            0.1,
            "strictly greater",
        ),
    ],
)
def test_promotion_rejections_never_touch_production(
    tmp_path: Path, report: dict[str, Any], min_delta: float, message: str
) -> None:
    candidate = _candidate_dir(tmp_path)
    skill_root = tmp_path / "production"
    skill_root.mkdir()
    production = skill_root / "SKILL.md"
    production.write_text("old production\n", encoding="utf-8")

    with pytest.raises(PromotionRejected, match=message):
        promote_candidate(candidate, skill_root, report, min_delta=min_delta)

    assert production.read_text(encoding="utf-8") == "old production\n"


def test_promotion_requires_distinct_paired_seeds(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    report = {
        "paired_results": [
            {"seed": 1, "baseline": 0.1, "candidate": 0.9},
            {"seed": 1, "baseline": 0.1, "candidate": 0.9},
            {"seed": 2, "baseline": 0.1, "candidate": 0.9},
        ]
    }

    with pytest.raises(PromotionRejected, match="Duplicate paired seed"):
        promote_candidate(candidate, tmp_path / "production", report)

    assert not (tmp_path / "production").exists()


def test_validated_candidate_is_atomically_promoted(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    skill_root = tmp_path / "production"
    skill_root.mkdir()
    destination = skill_root / "SKILL.md"
    destination.write_text("old production\n", encoding="utf-8")

    promoted = promote_candidate(candidate, skill_root, _passing_report(), min_delta=0.05)

    assert promoted == destination
    assert destination.read_bytes() == (candidate / "SKILL.md").read_bytes()
    assert candidate.is_dir()


def test_atomic_replace_failure_preserves_existing_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate_dir(tmp_path)
    skill_root = tmp_path / "production"
    skill_root.mkdir()
    destination = skill_root / "SKILL.md"
    destination.write_text("old production\n", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(evolution.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        promote_candidate(candidate, skill_root, _passing_report())

    assert destination.read_text(encoding="utf-8") == "old production\n"
    assert not list(skill_root.glob(".SKILL.md.*.tmp"))


def test_modified_candidate_is_rejected_against_provenance(tmp_path: Path) -> None:
    candidate = _candidate_dir(tmp_path)
    (candidate / "SKILL.md").write_text(
        "---\nname: spreadsheet-core\ndescription: tampered\n---\n\nChanged.\n",
        encoding="utf-8",
    )

    with pytest.raises(PromotionRejected, match="provenance hash"):
        promote_candidate(candidate, tmp_path / "production", _passing_report())

    assert not (tmp_path / "production").exists()
