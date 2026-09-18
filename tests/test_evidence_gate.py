from __future__ import annotations

import pytest

from spreadsheet_harness.evidence_gate import (
    GatePolicy,
    check_predictions,
    coordinate_breaches,
    evidence_packet,
    patch_objections,
    promotion_gate,
    transition,
    validate_hypothesis,
)


def contexts():
    expected = {
        c: {f"{c}-{i}": f"{c}-family-{i}" for i in range(4)}
        for c in ("replay", "transfer", "regression")
    }
    data = {
        c: [
            {
                "task_id": t,
                "family": f,
                "incumbent": 0.0,
                "candidate": 0.0 if c == "regression" else 1.0,
                "incumbent_status": "scored",
                "candidate_status": "scored",
            }
            for t, f in tasks.items()
        ]
        for c, tasks in expected.items()
    }
    return expected, data


def test_requires_positive_replay_and_transfer_not_just_aggregate():
    expected, data = contexts()
    policy = GatePolicy(bootstrap_samples=200)
    assert promotion_gate(data, expected, policy)["status"] == "accepted"
    for row in data["transfer"]:
        row["candidate"] = 0.0
    assert promotion_gate(data, expected, policy)["status"] == "rejected"


def test_provider_failure_defers_without_consuming_round():
    expected, data = contexts()
    data["replay"][0]["candidate_status"] = "infrastructure"
    d = promotion_gate(data, expected, GatePolicy(bootstrap_samples=200))
    assert d["status"] == "deferred"
    state = transition({"incumbent": "parent", "completed_trials": 1}, d, "child")
    assert state["incumbent"] == "parent"
    assert state["completed_trials"] == 1


def test_regression_or_contract_breach_prevents_acceptance():
    expected, data = contexts()
    data["regression"][0].update(incumbent=1.0, candidate=0.0)
    assert promotion_gate(data, expected, GatePolicy(bootstrap_samples=200))["status"] == "rejected"
    _, data = contexts()
    assert (
        promotion_gate(data, expected, GatePolicy(bootstrap_samples=200), ["unauthorized"])[
            "status"
        ]
        == "rejected_contract"
    )


def test_family_aggregation_not_task_weighted():
    expected, data = contexts()
    # Add many cases in one family: they do not change its macro weight.
    for i in range(10):
        t = f"repeat-{i}"
        expected["replay"][t] = "replay-family-0"
        data["replay"].append(
            {
                "task_id": t,
                "family": "replay-family-0",
                "incumbent": 0,
                "candidate": 0,
                "incumbent_status": "scored",
                "candidate_status": "scored",
            }
        )
    data["replay"][0]["candidate"] = 0
    report = promotion_gate(data, expected, GatePolicy(bootstrap_samples=200))
    assert report["contexts"]["replay"]["mean_delta"] == 0.75


def test_family_overlap_is_rejected():
    expected, data = contexts()
    expected["transfer"]["transfer-0"] = "replay-family-0"
    data["transfer"][0]["family"] = "replay-family-0"
    with pytest.raises(ValueError, match="overlap"):
        promotion_gate(data, expected, GatePolicy(bootstrap_samples=200))


def test_revision_changes_exactly_one_plugin_no_partner_drift():
    before = {"spreadsheet-structure/SKILL.md": "a", "spreadsheet-financial-model/SKILL.md": "b"}
    after = {**before, "spreadsheet-structure/SKILL.md": "c"}
    assert coordinate_breaches(before, after, "spreadsheet-structure", "H") == []
    after["spreadsheet-financial-model/SKILL.md"] = "changed-partner"
    assert coordinate_breaches(before, after, "spreadsheet-structure", "H")


def test_hypothesis_needs_real_event_and_truthful_counts():
    packets = [
        {
            "classification": "evaluated",
            "evidence_id": "abc",
            "decision_events": [{"event_index": 27}],
            "diagnostics": {"planner_applied_writes": 23},
        }
    ]
    route = {"group": "D", "plugin": "spreadsheet-financial-model", "operator": "revision"}
    h = {
        **route,
        **{
            k: "test"
            for k in (
                "observation",
                "suspected_cause",
                "intervention",
                "prediction",
                "falsification",
            )
        },
        "citations": [{"evidence_id": "abc", "event_index": 27}],
        "count_claims": [{"evidence_id": "abc", "field": "planner_applied_writes", "value": 0}],
    }
    with pytest.raises(ValueError, match="contradicts"):
        validate_hypothesis(h, route, packets)
    h["count_claims"][0]["value"] = 23
    assert (
        validate_hypothesis(h, route, packets)["causal_status"]
        == "hypothesis_pending_controlled_test"
    )
    h["citations"][0]["event_index"] = 999
    with pytest.raises(ValueError, match="unavailable"):
        validate_hypothesis(h, route, packets)


def test_accepted_version_persists_rejected_does_not_replace():
    state = {"incumbent": "parent", "completed_trials": 0}
    accepted = transition(state, {"status": "accepted"}, "child")
    assert accepted["incumbent"] == "child"
    rejected = transition(accepted, {"status": "rejected"}, "bad")
    assert rejected["incumbent"] == "child"
    assert rejected["completed_trials"] == 2


def test_compact_packet_retains_actions_but_drops_nested_request_timings(tmp_path):
    import json

    from spreadsheet_harness.evolution import extract_trajectory_evidence

    rows = [
        {
            "event": "harness.planner_actions.applied",
            "payload": {
                "count": 23,
                "actions": [{"sheet": "Sheet", "target": "E11", "expected_value": "=SUM(E7:E10)"}],
            },
        },
        {
            "event": "agent.execution_failed",
            "payload": {
                "reason": "edit_recovery_exhausted",
                "agent": {"turns": 49, "request_timings": [{"detail": "huge" * 10000}] * 49},
            },
        },
        {
            "event": "benchmark.evaluated",
            "payload": {
                "passed": False,
                "official_score": {
                    "error_message": "Regression at Sheet!E11: answer=123456, output=0"
                },
            },
        },
    ]
    for r in rows:
        r.update(run_id="test", timestamp="2026-09-17T00:00:00+00:00")
    trace = tmp_path / "trajectory.jsonl"
    trace.write_text("\n".join(json.dumps(r) for r in rows))
    packet = evidence_packet(extract_trajectory_evidence(trace, max_items_per_category=8))
    text = json.dumps(packet)
    assert len(text) < 6000
    assert "request_timings" not in text and "123456" not in text
    assert "=SUM(E7:E10)" in text
    assert packet["diagnostics"]["planner_applied_writes"] == 23


def test_patch_rejects_evaluator_dependency_and_forced_edits():
    bad = "If a regression error names a cell, make a corrective edit or submit_result."
    assert "depends-on-unavailable-evaluator" in patch_objections(bad)
    assert "forces-edit-or-premature-submit" in patch_objections(bad)
    assert (
        patch_objections("Verify operands using the workbook labels. Preserve existing inputs.")
        == []
    )


def test_hypothesis_cannot_switch_to_unrelated_symptom():
    route = {
        "group": "D",
        "plugin": "spreadsheet-financial-model",
        "operator": "revision",
        "evidence": [{"evidence_id": "abc", "event_indices": [27]}],
    }
    packet = {
        "classification": "evaluated",
        "evidence_id": "abc",
        "decision_events": [{"event_index": 27}, {"event_index": 85}],
    }
    h = {k: route[k] for k in ("group", "plugin", "operator")}
    h.update(
        {
            k: "test"
            for k in (
                "observation",
                "suspected_cause",
                "intervention",
                "prediction",
                "falsification",
            )
        }
    )
    h["citations"] = [{"evidence_id": "abc", "event_index": 85}]
    with pytest.raises(ValueError, match="routed failure"):
        validate_hypothesis(h, route, [packet])
    h["citations"][0]["event_index"] = 27
    assert validate_hypothesis(h, route, [packet])["id"]


def test_hypothesis_predictions_are_measured_not_inferred_from_score():
    h = {
        "prediction_checks": [
            {"metric": "tool_errors", "relation": "decrease"},
            {"metric": "modification", "relation": "increase"},
        ]
    }
    result = check_predictions(
        h, {"tool_errors": 2, "modification": 0.4}, {"tool_errors": 3, "modification": 0.8}
    )
    assert result[0]["supported"] is False
    assert result[1]["supported"] is True
