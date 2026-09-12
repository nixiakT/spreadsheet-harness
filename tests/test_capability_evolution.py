from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spreadsheet_harness.capability_evolution import (
    alternating_coordinate_schedule,
    attribute_failure,
    attribute_trajectory,
    compute_interaction_gain,
    evaluate_candidate_lifecycle,
    four_arm_metrics,
    persistent_plugin_action,
)
from spreadsheet_harness.plugins import (
    ARM_COMPOSITIONS,
    PLUGEOLVE_SEED_COMPOSITION,
    PluginMutation,
    default_plugin_registry,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _mutation() -> PluginMutation:
    contract = default_plugin_registry().get("skill-spreadsheet-formula")
    return PluginMutation.create(
        target_plugin=contract.name,
        base_version=contract.version,
        base_manifest_sha256=contract.manifest_sha256,
        surface="prompt",
        candidate_artifact_sha256=_digest("formula-candidate"),
        changed_paths=("SKILL.md",),
        evidence_sha256=(_digest("failed-trajectory"),),
    )


def test_failure_attribution_distinguishes_gap_capability_and_infrastructure() -> None:
    registry = default_plugin_registry()
    ours = registry.resolve(ARM_COMPOSITIONS["ours"])
    seed = registry.resolve(PLUGEOLVE_SEED_COMPOSITION)

    gap = attribute_failure(
        registry,
        ours,
        evaluator_passed=False,
        evidence_text=("lookup formula returned the wrong expected value",),
    )
    capability = attribute_failure(
        registry,
        seed,
        evaluator_passed=False,
        evidence_text=("lookup formula returned the wrong expected value",),
    )
    infrastructure = attribute_failure(
        registry,
        seed,
        evaluator_passed=False,
        error_categories=("provider_transient",),
    )

    assert gap.source == "capability-gap"
    assert gap.action == "enable-plugin"
    assert gap.candidate_plugins[0]["plugin"] == "skill-spreadsheet-formula"
    assert capability.source == "capability"
    assert capability.action == "evolve-plugin"
    assert {item["plugin"] for item in capability.target_plugins} == {
        "skill-spreadsheet-formula",
        "skill-spreadsheet-verification",
    }
    assert infrastructure.source == "infrastructure"
    assert infrastructure.action == "retry-infrastructure"
    assert infrastructure.target_plugins == ()


def test_unactivated_selected_provider_routes_to_composition_repair() -> None:
    registry = default_plugin_registry()
    composition = registry.resolve(ARM_COMPOSITIONS["ours"])

    attribution = attribute_failure(
        registry,
        composition,
        evaluator_passed=False,
        evidence_text=("header and table boundary were identified incorrectly",),
        activated_plugins=("runtime-code-interpreter", "policy-ours", "repair-date-text"),
    )

    assert attribution.source == "composition"
    assert attribution.action == "repair-composition"
    assert {item["plugin"] for item in attribution.target_plugins} == {
        "profile-deterministic-compact",
        "policy-ours",
    }


def test_interface_attribution_preserves_structured_evidence_and_route() -> None:
    registry = default_plugin_registry()
    composition = registry.resolve(PLUGEOLVE_SEED_COMPOSITION)
    attribution = attribute_failure(
        registry,
        composition,
        evaluator_passed=False,
        interface_evidence=(
            {
                "route": "composition_interface",
                "required_capability": "financial.scenario-selector",
                "provider": "skill-spreadsheet-financial-model",
                "missing_evidence": ["Assumptions!B4", "Model!C7:F7"],
                "harness_surface": "context.workbook-profile",
            },
        ),
    )

    assert attribution.source == "composition-interface"
    payload = attribution.to_dict()
    assert payload["route"] == "composition_interface"
    assert payload["route_class"] == "interface"
    assert payload["interface_evidence"][0]["required_capability"] == (
        "financial.scenario-selector"
    )
    assert payload["interface_evidence"][0]["missing_evidence"] == [
        "Assumptions!B4",
        "Model!C7:F7",
    ]


def test_active_general_provider_is_reported_as_harness_route() -> None:
    registry = default_plugin_registry()
    attribution = attribute_failure(
        registry,
        registry.resolve(ARM_COMPOSITIONS["ours"]),
        evaluator_passed=False,
        evidence_text=("workbook profile omitted the table boundary",),
    )
    assert attribution.source == "capability"
    assert attribution.to_dict()["route"] == "harness"


def test_alternating_schedule_mutates_one_coordinate_at_a_time() -> None:
    schedule = alternating_coordinate_schedule(5)
    assert [step.coordinate for step in schedule] == [
        "harness",
        "domain",
        "harness",
        "domain",
        "harness",
    ]
    assert all(step.frozen_coordinate != step.coordinate for step in schedule)
    assert all(step.to_dict()["mutation_count"] == 1 for step in schedule)
    assert alternating_coordinate_schedule(2, first="domain")[0].coordinate == "domain"


def test_four_arm_metrics_report_difference_in_differences() -> None:
    scores = {"H0 ⊕ D0": 0.50, "H1 ⊕ D0": 0.60, "H0 ⊕ D1": 0.65, "H1 ⊕ D1": 0.80}
    metrics = four_arm_metrics(scores)
    assert metrics["harness_gain"] == pytest.approx(0.10)
    assert metrics["domain_gain"] == pytest.approx(0.15)
    assert metrics["joint_gain"] == pytest.approx(0.30)
    assert metrics["interaction_gain"] == pytest.approx(0.05)
    assert metrics["G_H"] == pytest.approx(0.10)
    assert metrics["G_D"] == pytest.approx(0.15)
    assert metrics["G_joint"] == pytest.approx(0.30)
    assert metrics["I"] == pytest.approx(0.05)
    assert metrics["joint_gt_max_single"] is True
    assert compute_interaction_gain(scores) == pytest.approx(0.05)
    assert four_arm_metrics(
        {"h0d0": 0.50, "h1d0": 0.90, "h0d1": 0.80, "h1d1": 0.85}
    )["joint_gt_max_single"] is False

    with pytest.raises(ValueError, match="missing"):
        four_arm_metrics({"h0d0": 0.5, "h1d0": 0.6, "h0d1": 0.65})
    with pytest.raises(ValueError, match="Unknown"):
        four_arm_metrics({**scores, "joint": 0.8})


def test_trajectory_attribution_requires_explicit_evaluator_and_hashes_evidence(
    tmp_path: Path,
) -> None:
    trajectory = tmp_path / "trajectory.jsonl"
    rows = [
        {
            "event": "benchmark.evaluated",
            "run_id": "run-1",
            "timestamp": "2026-08-20T00:00:00Z",
            "payload": {"passed": False, "artifact_score_passed": False},
        }
    ]
    trajectory.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    registry = default_plugin_registry()

    attribution = attribute_trajectory(
        trajectory,
        registry,
        registry.resolve(PLUGEOLVE_SEED_COMPOSITION),
        task_type="Cell-Level Manipulation: formula lookup",
    )

    assert attribution.source == "capability"
    assert attribution.evidence_sha256 == (hashlib.sha256(trajectory.read_bytes()).hexdigest(),)

    trajectory.write_text(json.dumps({"event": "agent.completed", "payload": {}}) + "\n")
    with pytest.raises(ValueError, match="explicit evaluator outcome"):
        attribute_trajectory(
            trajectory,
            registry,
            registry.resolve(PLUGEOLVE_SEED_COMPOSITION),
        )


def test_candidate_lifecycle_requires_replay_transfer_and_regression() -> None:
    mutation = _mutation()
    report = {
        "candidate_artifact_sha256": mutation.candidate_artifact_sha256,
        "contexts": [
            {"name": "failed-case", "kind": "replay", "baseline": 0.0, "candidate": 1.0},
            {"name": "neighbor", "kind": "transfer", "baseline": 0.4, "candidate": 0.6},
            {"name": "history", "kind": "regression", "baseline": 0.8, "candidate": 0.8},
        ],
    }

    accepted = evaluate_candidate_lifecycle(mutation, report, min_mean_delta=0.01)
    assert accepted.promoted is True
    assert accepted.next_state == "persistent"

    report["contexts"][2]["candidate"] = 0.7
    rejected = evaluate_candidate_lifecycle(mutation, report, max_context_regression=0.0)
    assert rejected.promoted is False
    assert rejected.next_state == "retired"
    assert "context-regression:history" in rejected.blockers


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"usage_count": 5, "marginal_utility": 0.1, "redundancy": 0.1}, "retain"),
        ({"usage_count": 5, "marginal_utility": 0.0, "redundancy": 0.1}, "refine"),
        ({"usage_count": 5, "marginal_utility": 0.1, "redundancy": 0.9}, "merge"),
        ({"usage_count": 5, "marginal_utility": -0.1, "redundancy": 0.1}, "rollback"),
        ({"usage_count": 0, "marginal_utility": 0.0, "redundancy": 0.1}, "retire"),
    ],
)
def test_persistent_plugin_bank_actions(kwargs: dict[str, float], expected: str) -> None:
    assert persistent_plugin_action(**kwargs) == expected
