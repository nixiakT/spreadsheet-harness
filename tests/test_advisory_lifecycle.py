from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from spreadsheet_harness.evidence_contract import (
    ArtifactRef,
    ArtifactTransition,
    EffectKind,
    EvidenceScope,
)
from spreadsheet_harness.target_grounding import (
    CommittedAdvisoryTargetAssessment,
    GroundingDecision,
    TargetGroundingError,
    TargetGroundingMode,
    TargetGroundingStateMachine,
    validate_advisory_lifecycle_chain,
)
from spreadsheet_harness.workbook_diff import WorkbookEffectDiff

R0 = ArtifactRef(0, "0" * 64)
R1 = ArtifactRef(1, "1" * 64)
R1_ALTERNATE = ArtifactRef(1, "a" * 64)
R2 = ArtifactRef(2, "2" * 64)
R3 = ArtifactRef(3, "3" * 64)


def _no_op() -> WorkbookEffectDiff:
    return WorkbookEffectDiff(
        semantic_changed=False,
        complete=True,
        effects=frozenset(),
        scope=EvidenceScope(),
        formula_scope=EvidenceScope(),
        changed_cell_count=0,
        scanned_cell_count=20,
    )


def _changed(scope: EvidenceScope | None = None) -> WorkbookEffectDiff:
    return WorkbookEffectDiff(
        semantic_changed=True,
        complete=True,
        effects=frozenset({EffectKind.VALUE}),
        scope=scope or EvidenceScope.one("Sales", "A1"),
        formula_scope=EvidenceScope(),
        changed_cell_count=1,
        scanned_cell_count=20,
    )


def _advisory_gate(
    artifact: ArtifactRef = R0,
    *,
    initial_transition_count: int = 0,
) -> TargetGroundingStateMachine:
    return TargetGroundingStateMachine(
        artifact,
        mode=TargetGroundingMode.ADVISORY,
        initial_transition_count=initial_transition_count,
    )


def _declare(
    gate: TargetGroundingStateMachine,
    *,
    observed: EvidenceScope | None = None,
    target: EvidenceScope | None = None,
) -> int:
    observation = gate.record_trusted_observation(
        artifact=gate.current_artifact,
        scope=observed or EvidenceScope.one("Sales", "A1:B2"),
    )
    declaration = gate.declare_target(
        artifact=gate.current_artifact,
        target_scope=target or EvidenceScope.one("Sales", "A1"),
        observation_ids=(observation.observation_id,),
    )
    return declaration.declaration_id


def _event_documents(gate: TargetGroundingStateMachine) -> list[dict[str, Any]]:
    return [copy.deepcopy(event.to_dict()) for event in gate.advisory_lifecycle_events]


def _audit(
    gate: TargetGroundingStateMachine,
    *,
    transitions: Sequence[ArtifactTransition] = (),
    initial_artifact: ArtifactRef = R0,
    initial_transition_count: int = 0,
    events: list[dict[str, Any]] | None = None,
    final_counters: Mapping[str, Any] | None = None,
    committed_assessments: Sequence[CommittedAdvisoryTargetAssessment] | None = None,
    genesis_sha256: str | None = None,
):
    return validate_advisory_lifecycle_chain(
        _event_documents(gate) if events is None else events,
        genesis_sha256=(
            gate.advisory_lifecycle_genesis_sha256
            if genesis_sha256 is None
            else genesis_sha256
        ),
        final_counters=(
            gate.advisory_lifecycle_final_counters
            if final_counters is None
            else final_counters
        ),
        committed_assessments=(
            gate.committed_advisory_assessments
            if committed_assessments is None
            else committed_assessments
        ),
        transitions=transitions,
        initial_artifact=initial_artifact,
        initial_transition_count=initial_transition_count,
    )


def _canonical_digest(document: Mapping[str, Any], digest_field: str) -> str:
    payload = {key: value for key, value in document.items() if key != digest_field}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def _rehash_events(
    events: list[dict[str, Any]],
    *,
    genesis_sha256: str,
) -> None:
    previous = genesis_sha256
    for event_id, event in enumerate(events, start=1):
        event["event_id"] = event_id
        event["previous_event_sha256"] = previous
        event["event_sha256"] = _canonical_digest(event, "event_sha256")
        previous = event["event_sha256"]


def _rehash_assessment(assessment: dict[str, Any]) -> None:
    provenance = assessment.get("provenance")
    if provenance is not None:
        provenance["provenance_sha256"] = _canonical_digest(
            provenance,
            "provenance_sha256",
        )
    assessment["assessment_sha256"] = _canonical_digest(
        assessment,
        "assessment_sha256",
    )


def test_genesis_binds_empty_lifecycle_to_artifact_and_transition_offset() -> None:
    transition = ArtifactTransition(
        1,
        "recalculate",
        "derived_recalculation",
        R0,
        R1,
    )
    gate = _advisory_gate(R1, initial_transition_count=1)

    assert _audit(
        gate,
        transitions=(transition,),
        initial_artifact=R1,
        initial_transition_count=1,
    ) == ()

    with pytest.raises(TargetGroundingError, match="genesis.*initial binding"):
        _audit(
            gate,
            transitions=(transition,),
            initial_artifact=R0,
            initial_transition_count=0,
        )
    with pytest.raises(TargetGroundingError, match="genesis.*initial binding"):
        _audit(
            gate,
            transitions=(transition,),
            initial_artifact=R1,
            initial_transition_count=1,
            genesis_sha256="f" * 64,
        )


def test_prepare_abort_consumes_declaration_and_each_prepare_has_one_terminal() -> None:
    gate = _advisory_gate()
    declaration_id = _declare(gate)
    first = gate.prepare_advisory_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )
    gate.abort_prepared_advisory_assessment(first)
    second = gate.prepare_advisory_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )

    assert second.assessment.declaration_status == "consumed"
    assert second.assessment.decision is GroundingDecision.REPLAYED_DECLARATION
    gate.commit_advisory_assessment(second, None)
    events_before_failed_reuse = gate.advisory_lifecycle_events

    with pytest.raises(TargetGroundingError, match="unknown or stale"):
        gate.abort_prepared_advisory_assessment(first)
    with pytest.raises(TargetGroundingError, match="unknown or stale"):
        gate.commit_advisory_assessment(first, None)

    assert gate.advisory_lifecycle_events == events_before_failed_reuse
    assert [event.event_type for event in gate.advisory_lifecycle_events] == [
        "observation",
        "declaration",
        "preparation",
        "abort",
        "preparation",
        "commitment",
    ]
    assert gate.advisory_lifecycle_final_counters == {
        "observation_count": 1,
        "declaration_count": 1,
        "validation_count": 2,
        "preparation_count": 2,
        "commitment_count": 1,
        "event_count": 6,
        "transition_count": 0,
        "pending_preparation_count": 0,
    }
    assert _audit(gate) == gate.advisory_lifecycle_events


def test_pending_declaration_status_replays_before_original_prepare_aborts() -> None:
    gate = _advisory_gate()
    declaration_id = _declare(gate)
    original = gate.prepare_advisory_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )
    replay = gate.prepare_advisory_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )

    assert replay.assessment.declaration_status == "prepared"
    assert replay.assessment.decision is GroundingDecision.REPLAYED_DECLARATION
    gate.commit_advisory_assessment(replay, None)
    gate.abort_prepared_advisory_assessment(original)

    assert _audit(gate) == gate.advisory_lifecycle_events


def test_newer_commit_supersedes_older_active_declaration() -> None:
    gate = _advisory_gate()
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:B2"),
    )
    declarations = [
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=(observation.observation_id,),
        )
        for _ in range(2)
    ]
    newer = gate.prepare_advisory_staged_diff(
        declarations[1].declaration_id,
        _no_op(),
        staged_artifact=R0,
    )
    gate.commit_advisory_assessment(newer, None)
    older = gate.prepare_advisory_staged_diff(
        declarations[0].declaration_id,
        _no_op(),
        staged_artifact=R0,
    )

    assert older.assessment.declaration_status == "superseded"
    assert older.assessment.decision is GroundingDecision.REPLAYED_DECLARATION
    gate.commit_advisory_assessment(older, None)

    assert _audit(gate) == gate.advisory_lifecycle_events


def _mixed_transition_lifecycle() -> tuple[
    TargetGroundingStateMachine,
    tuple[ArtifactTransition, ...],
]:
    gate = _advisory_gate()
    first = ArtifactTransition(
        1,
        "recalculate",
        "derived_recalculation",
        R0,
        R1,
    )
    gate.record_artifact_transition(first)
    prepared = gate.prepare_advisory_staged_diff(
        None,
        _changed(),
        staged_artifact=R2,
    )
    second = ArtifactTransition(2, "write_range", "mutation", R1, R2)
    gate.commit_advisory_assessment(prepared, second)
    third = ArtifactTransition(3, "save", "artifact_rewrite", R2, R3)
    gate.record_artifact_transition(third)
    return gate, (first, second, third)


def test_protected_and_nonprotected_transitions_replay_in_exact_roles_and_order() -> None:
    gate, transitions = _mixed_transition_lifecycle()

    assert [event.event_type for event in gate.advisory_lifecycle_events] == [
        "transition",
        "preparation",
        "commitment",
        "transition",
    ]
    assert gate.current_artifact == R3
    assert _audit(gate, transitions=transitions) == gate.advisory_lifecycle_events


def test_transition_ledger_rejects_id_gaps_and_disconnected_prefix() -> None:
    gate = _advisory_gate()
    gapped = ArtifactTransition(2, "save", "artifact_rewrite", R0, R1)
    with pytest.raises(TargetGroundingError, match="transition IDs are not contiguous"):
        _audit(gate, transitions=(gapped,))

    first = ArtifactTransition(1, "save", "artifact_rewrite", R0, R1)
    disconnected = ArtifactTransition(
        2,
        "save",
        "artifact_rewrite",
        R1_ALTERNATE,
        R2,
    )
    post_prefix_gate = _advisory_gate(R2, initial_transition_count=2)
    with pytest.raises(TargetGroundingError, match="artifact lineage is disconnected"):
        _audit(
            post_prefix_gate,
            transitions=(first, disconnected),
            initial_artifact=R2,
            initial_transition_count=2,
        )


def test_live_producer_rejects_transition_role_mismatches_before_mutation() -> None:
    advisory = _advisory_gate()
    prepared = advisory.prepare_advisory_staged_diff(
        None,
        _changed(),
        staged_artifact=R1,
    )
    before_events = advisory.advisory_lifecycle_events
    before_counters = advisory.advisory_lifecycle_final_counters
    nonprotected = ArtifactTransition(
        1,
        "recalculate",
        "derived_recalculation",
        R0,
        R1,
    )

    with pytest.raises(TargetGroundingError, match="only a protected"):
        advisory.commit_advisory_assessment(prepared, nonprotected)

    assert advisory.current_artifact == R0
    assert advisory.advisory_lifecycle_events == before_events
    assert advisory.advisory_lifecycle_final_counters == before_counters

    standalone = _advisory_gate()
    protected = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    with pytest.raises(TargetGroundingError, match="requires a committed"):
        standalone.record_artifact_transition(protected)
    assert standalone.current_artifact == R0
    assert standalone.advisory_lifecycle_events == ()


@pytest.mark.parametrize("terminal", ["commitment", "transition"])
def test_live_producer_rejects_transition_id_gap_before_mutation(terminal: str) -> None:
    gate = _advisory_gate()
    transition = ArtifactTransition(
        2,
        "write_range" if terminal == "commitment" else "save",
        "mutation" if terminal == "commitment" else "artifact_rewrite",
        R0,
        R1,
    )
    before_events = gate.advisory_lifecycle_events
    before_counters = gate.advisory_lifecycle_final_counters

    with pytest.raises(TargetGroundingError, match="transition ID.*ledger sequence"):
        if terminal == "commitment":
            prepared = gate.prepare_advisory_staged_diff(
                None,
                _changed(),
                staged_artifact=R1,
            )
            before_events = gate.advisory_lifecycle_events
            before_counters = gate.advisory_lifecycle_final_counters
            gate.commit_advisory_assessment(prepared, transition)
        else:
            gate.record_artifact_transition(transition)

    assert gate.current_artifact == R0
    assert gate.advisory_lifecycle_events == before_events
    assert gate.advisory_lifecycle_final_counters == before_counters


def test_strict_audit_rejects_pending_prepare_and_tail_counter_truncation() -> None:
    pending_gate = _advisory_gate()
    pending_gate.prepare_advisory_staged_diff(None, _no_op(), staged_artifact=R0)
    with pytest.raises(TargetGroundingError, match="unresolved preparations"):
        _audit(pending_gate)

    closed_gate = _advisory_gate()
    prepared = closed_gate.prepare_advisory_staged_diff(
        None,
        _no_op(),
        staged_artifact=R0,
    )
    closed_gate.abort_prepared_advisory_assessment(prepared)
    with pytest.raises(TargetGroundingError, match="final counters do not match replay"):
        _audit(closed_gate, events=[])


def test_missing_and_invalid_requests_remain_distinct_under_fresh_replay() -> None:
    gate = _advisory_gate()
    missing = gate.prepare_advisory_staged_diff(None, _no_op(), staged_artifact=R0)
    gate.commit_advisory_assessment(missing, None)
    invalid = gate.prepare_advisory_staged_diff(0, _no_op(), staged_artifact=R0)
    gate.commit_advisory_assessment(invalid, None)

    assert (
        missing.declaration_request_kind,
        missing.declaration_request_id,
        missing.assessment.declaration_status,
        missing.assessment.decision,
    ) == ("missing", None, "missing", GroundingDecision.MISSING_DECLARATION)
    assert (
        invalid.declaration_request_kind,
        invalid.declaration_request_id,
        invalid.assessment.declaration_status,
        invalid.assessment.decision,
    ) == ("invalid", None, "invalid", GroundingDecision.INVALID_DECLARATION)

    preparation_requests = [
        event.payload["preparation"]["declaration_request"]
        for event in gate.advisory_lifecycle_events
        if event.event_type == "preparation"
    ]
    assert preparation_requests == [
        {"kind": "missing", "declaration_id": None},
        {"kind": "invalid", "declaration_id": None},
    ]
    assert _audit(gate) == gate.advisory_lifecycle_events


@pytest.mark.parametrize(
    ("footprint", "decision"),
    [
        (
            WorkbookEffectDiff.unknown("/machine-local/private/workbook.xml"),
            GroundingDecision.UNKNOWN_EFFECT,
        ),
        (
            WorkbookEffectDiff(
                semantic_changed=True,
                complete=False,
                effects=frozenset({EffectKind.VALUE}),
                scope=EvidenceScope.one("Sales", "A1"),
                formula_scope=EvidenceScope(),
                changed_cell_count=1,
                scanned_cell_count=20,
                reasons=("scan stopped at a machine-local path",),
            ),
            GroundingDecision.INCOMPLETE_DIFF,
        ),
    ],
)
def test_fresh_replay_compares_reason_redacted_commitments_canonically(
    footprint: WorkbookEffectDiff,
    decision: GroundingDecision,
) -> None:
    gate = _advisory_gate()
    declaration_id = _declare(gate)
    prepared = gate.prepare_advisory_staged_diff(
        declaration_id,
        footprint,
        staged_artifact=R1,
    )
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    gate.commit_advisory_assessment(prepared, transition)

    assert prepared.assessment.decision is decision
    assert footprint.reasons
    assert "reasons" not in prepared.assessment.to_dict()["staged_footprint"]
    assert _audit(gate, transitions=(transition,)) == gate.advisory_lifecycle_events


@pytest.mark.parametrize("substitution", ["observation", "declaration"])
def test_fresh_replay_rejects_cross_record_state_object_substitution(
    substitution: str,
) -> None:
    gate = _advisory_gate()
    declaration_id = _declare(gate)
    prepared = gate.prepare_advisory_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )
    gate.commit_advisory_assessment(prepared, None)
    events = _event_documents(gate)
    preparation = events[2]["payload"]["preparation"]
    assessment = preparation["assessment"]
    provenance = assessment["provenance"]
    assert provenance is not None

    if substitution == "observation":
        replacement_scope = EvidenceScope.one("Sales", "A1").to_dict()
        assessment["observations"][0]["scope"] = replacement_scope
        provenance["observations"][0]["scope"] = replacement_scope
    else:
        replacement_target = EvidenceScope.one("Sales", "A1:B1").to_dict()
        assessment["declaration"]["target_scope"] = replacement_target
        provenance["declaration"]["target_scope"] = replacement_target

    _rehash_assessment(assessment)
    _rehash_events(
        events,
        genesis_sha256=gate.advisory_lifecycle_genesis_sha256,
    )

    with pytest.raises(TargetGroundingError, match="preparation event does not replay exactly"):
        _audit(gate, events=events)


def test_fresh_replay_rejects_commitment_object_substitution() -> None:
    gate = _advisory_gate()
    declaration_id = _declare(gate)
    prepared = gate.prepare_advisory_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )
    gate.commit_advisory_assessment(prepared, None)
    events = _event_documents(gate)
    commitment = events[-1]["payload"]["commitment"]
    assessment = commitment["assessment"]
    provenance = assessment["provenance"]
    assert provenance is not None

    assessment["staged_footprint"]["scanned_cell_count"] = 21
    provenance["staged_footprint"]["scanned_cell_count"] = 21
    _rehash_assessment(assessment)
    commitment["commitment_sha256"] = _canonical_digest(
        commitment,
        "commitment_sha256",
    )
    _rehash_events(
        events,
        genesis_sha256=gate.advisory_lifecycle_genesis_sha256,
    )

    with pytest.raises(
        TargetGroundingError,
        match="committed advisory assessment does not match the prepared publication",
    ):
        _audit(gate, events=events)
