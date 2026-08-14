from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spreadsheet_harness.evidence_contract import (
    ArtifactRef,
    ArtifactTransition,
    EffectKind,
    EvidenceScope,
)
from spreadsheet_harness.target_grounding import (
    CommittedAdvisoryTargetAssessment,
    CommittedTargetAuthorization,
    GroundingDecision,
    PreparedAdvisoryTargetAssessment,
    PreparedTargetAuthorization,
    TargetGroundingError,
    TargetGroundingMode,
    TargetGroundingRejected,
    TargetGroundingStateMachine,
    advisory_assessment_from_dict,
    committed_advisory_assessment_from_dict,
    committed_authorization_from_dict,
    validate_committed_advisory_assessment_chain,
    validate_committed_authorization_chain,
)
from spreadsheet_harness.workbook_diff import WorkbookEffectDiff

R0 = ArtifactRef(0, "0" * 64)
R0_OTHER_BYTES = ArtifactRef(0, "f" * 64)
R1 = ArtifactRef(1, "1" * 64)
R1_OTHER_BYTES = ArtifactRef(1, "e" * 64)
R2 = ArtifactRef(2, "2" * 64)


def _canonical_digest(document: dict[str, object], digest_field: str) -> str:
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


def _changed(
    scope: EvidenceScope,
    *,
    effects: frozenset[EffectKind] = frozenset({EffectKind.VALUE}),
    complete: bool = True,
    formula_scope: EvidenceScope | None = None,
) -> WorkbookEffectDiff:
    return WorkbookEffectDiff(
        semantic_changed=True,
        complete=complete,
        effects=effects,
        scope=scope,
        formula_scope=formula_scope or EvidenceScope(),
        changed_cell_count=1,
        scanned_cell_count=20,
    )


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


def _grounded_gate(
    *,
    artifact: ArtifactRef = R0,
    observed: EvidenceScope | None = None,
    target: EvidenceScope | None = None,
) -> tuple[TargetGroundingStateMachine, int]:
    gate = TargetGroundingStateMachine(artifact)
    observation = gate.record_trusted_observation(
        artifact=artifact,
        scope=observed or EvidenceScope.one("Sales", "A1:D10"),
    )
    declaration = gate.declare_target(
        artifact=artifact,
        target_scope=target or EvidenceScope.one("Sales", "B2:C3"),
        observation_ids=(observation.observation_id,),
    )
    return gate, declaration.declaration_id


def _advisory_lifecycle_kwargs(
    gate: TargetGroundingStateMachine,
) -> dict[str, object]:
    return {
        "lifecycle_events": [
            item.to_dict() for item in gate.advisory_lifecycle_events
        ],
        "lifecycle_genesis_sha256": gate.advisory_lifecycle_genesis_sha256,
        "lifecycle_final_counters": gate.advisory_lifecycle_final_counters,
    }


def test_observations_are_monotonic_and_bound_to_exact_artifact() -> None:
    gate = TargetGroundingStateMachine(R0)
    first = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:A2"),
    )
    second = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.worksheet("Inputs"),
    )

    assert (first.observation_id, second.observation_id) == (1, 2)
    with pytest.raises(TargetGroundingError, match="stale"):
        gate.record_trusted_observation(
            artifact=R0_OTHER_BYTES,
            scope=EvidenceScope.one("Sales", "A1"),
        )


@pytest.mark.parametrize("scope", [EvidenceScope(), EvidenceScope.workbook()])
def test_observation_scope_must_be_nonempty_and_finite(scope: EvidenceScope) -> None:
    gate = TargetGroundingStateMachine(R0)

    with pytest.raises(TargetGroundingError):
        gate.record_trusted_observation(artifact=R0, scope=scope)


def test_cumulative_observed_ranges_can_ground_one_larger_target() -> None:
    gate = TargetGroundingStateMachine(R0)
    top = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:B2"),
    )
    bottom = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A3:B4"),
    )

    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "A1:B4"),
        observation_ids=[bottom.observation_id, top.observation_id],
    )

    assert declaration.observation_ids == (1, 2)
    assert declaration.observation_horizon == 2


def test_cumulative_ranges_must_cover_every_cell_not_only_bounding_box() -> None:
    gate = TargetGroundingStateMachine(R0)
    left = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:A4"),
    )
    right = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "C1:C4"),
    )

    with pytest.raises(TargetGroundingError, match="cumulatively cover"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1:C4"),
            observation_ids=(left.observation_id, right.observation_id),
        )


def test_uninspected_target_expansion_and_wrong_sheet_are_rejected() -> None:
    gate = TargetGroundingStateMachine(R0)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:B2"),
    )

    with pytest.raises(TargetGroundingError, match="cumulatively cover"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1:C2"),
            observation_ids=(observation.observation_id,),
        )
    with pytest.raises(TargetGroundingError, match="cumulatively cover"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Other", "A1:B2"),
            observation_ids=(observation.observation_id,),
        )


def test_worksheet_observation_can_ground_only_a_bounded_range() -> None:
    gate = TargetGroundingStateMachine(R0)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.worksheet("Sales"),
    )

    with pytest.raises(TargetGroundingError, match="bounded cell ranges"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.worksheet("Sales"),
            observation_ids=(observation.observation_id,),
        )
    bounded = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "XFD1048576"),
        observation_ids=(observation.observation_id,),
    )

    assert bounded.declaration_id == 1


def test_wildcard_target_unknown_and_duplicate_references_are_rejected() -> None:
    gate = TargetGroundingStateMachine(R0)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:B2"),
    )

    with pytest.raises(TargetGroundingError, match="wildcard"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.workbook(),
            observation_ids=(observation.observation_id,),
        )
    with pytest.raises(TargetGroundingError, match="unknown observation ID"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=(999,),
        )
    with pytest.raises(TargetGroundingError, match="duplicate"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=(observation.observation_id, observation.observation_id),
        )


def test_observation_is_stale_after_artifact_transition() -> None:
    gate = TargetGroundingStateMachine(R0)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:B2"),
    )
    gate.record_artifact_transition(
        ArtifactTransition(1, "edit", "artifact_rewrite", R0, R1)
    )

    with pytest.raises(TargetGroundingError, match="stale artifact"):
        gate.declare_target(
            artifact=R1,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=(observation.observation_id,),
        )
    with pytest.raises(TargetGroundingError, match="stale"):
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=(observation.observation_id,),
        )


def test_any_artifact_transition_invalidates_unused_declaration() -> None:
    gate, declaration_id = _grounded_gate()
    gate.record_artifact_transition(
        ArtifactTransition(1, "recalculate", "artifact_rewrite", R0, R1)
    )

    with pytest.raises(TargetGroundingError, match="invalidated"):
        gate.authorize_staged_diff(
            declaration_id,
            _changed(EvidenceScope.one("Sales", "B2")),
        )
    assert gate.current_artifact == R1


def test_opaque_semantic_diff_is_authorized_only_within_declared_target() -> None:
    gate, declaration_id = _grounded_gate()

    # The footprint is harness-computed, so an opaque code mutation needs no
    # model-supplied account of the cells it actually changed.
    record = gate.authorize_staged_diff(
        declaration_id,
        _changed(EvidenceScope.one("Sales", "B2:C3")),
    )

    assert record.accepted is True
    assert record.decision is GroundingDecision.AUTHORIZED


def test_wrong_actual_scope_is_rejected_and_declaration_is_consumed() -> None:
    gate, declaration_id = _grounded_gate()
    outside = _changed(EvidenceScope.one("Sales", "C3:D4"))

    with pytest.raises(TargetGroundingRejected) as rejected:
        gate.authorize_staged_diff(declaration_id, outside)

    assert rejected.value.record.decision is GroundingDecision.OUTSIDE_DECLARED_TARGET
    assert rejected.value.record.accepted is False
    assert gate.records == (rejected.value.record,)
    with pytest.raises(TargetGroundingError, match="consumed.*replayed"):
        gate.authorize_staged_diff(declaration_id, outside)


def test_union_target_does_not_authorize_changes_in_an_undeclared_gap() -> None:
    target = EvidenceScope.one("Sales", "A1:A2").merged(EvidenceScope.one("Sales", "C1:C2"))
    gate, declaration_id = _grounded_gate(observed=target, target=target)

    with pytest.raises(TargetGroundingRejected) as rejected:
        gate.authorize_staged_diff(
            declaration_id,
            _changed(EvidenceScope.one("Sales", "A1:C2")),
        )

    assert rejected.value.record.decision is GroundingDecision.OUTSIDE_DECLARED_TARGET


@pytest.mark.parametrize(
    ("diff", "decision"),
    [
        (
            _changed(EvidenceScope.one("Sales", "B2"), complete=False),
            GroundingDecision.INCOMPLETE_DIFF,
        ),
        (
            WorkbookEffectDiff.unknown("opaque comparison exceeded scan budget"),
            GroundingDecision.UNKNOWN_EFFECT,
        ),
        (
            _changed(
                EvidenceScope.one("Sales", "B2"),
                effects=frozenset({EffectKind.UNKNOWN}),
            ),
            GroundingDecision.UNKNOWN_EFFECT,
        ),
    ],
)
def test_incomplete_or_unknown_diff_fails_closed(
    diff: WorkbookEffectDiff,
    decision: GroundingDecision,
) -> None:
    gate, declaration_id = _grounded_gate()

    with pytest.raises(TargetGroundingRejected) as rejected:
        gate.authorize_staged_diff(declaration_id, diff)

    assert rejected.value.record.decision is decision


def test_no_op_is_authorized_but_still_consumes_declaration() -> None:
    gate, declaration_id = _grounded_gate()

    record = gate.authorize_staged_diff(declaration_id, _no_op())

    assert record.decision is GroundingDecision.AUTHORIZED_NO_OP
    assert record.accepted is True
    with pytest.raises(TargetGroundingError, match="consumed.*replayed"):
        gate.authorize_staged_diff(declaration_id, _no_op())


def test_inconsistent_no_op_and_formula_scope_fail_closed() -> None:
    gate, first_id = _grounded_gate()
    inconsistent_no_op = WorkbookEffectDiff(
        semantic_changed=False,
        complete=True,
        effects=frozenset({EffectKind.VALUE}),
        scope=EvidenceScope.one("Sales", "B2"),
        formula_scope=EvidenceScope(),
        changed_cell_count=1,
        scanned_cell_count=20,
    )
    with pytest.raises(TargetGroundingRejected) as rejected_no_op:
        gate.authorize_staged_diff(first_id, inconsistent_no_op)
    assert rejected_no_op.value.record.decision is GroundingDecision.INVALID_FOOTPRINT

    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:D10"),
    )
    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "B2:C3"),
        observation_ids=(observation.observation_id,),
    )
    bad_formula_scope = _changed(
        EvidenceScope.one("Sales", "B2"),
        effects=frozenset({EffectKind.FORMULA}),
        formula_scope=EvidenceScope.one("Sales", "D4"),
    )
    with pytest.raises(TargetGroundingRejected) as rejected_formula:
        gate.authorize_staged_diff(declaration.declaration_id, bad_formula_scope)
    assert rejected_formula.value.record.decision is GroundingDecision.INVALID_FOOTPRINT


def _record_for_artifact(artifact: ArtifactRef):
    gate, declaration_id = _grounded_gate(artifact=artifact)
    return gate.authorize_staged_diff(
        declaration_id,
        _changed(EvidenceScope.one("Sales", "B2:C3")),
    )


def test_canonical_provenance_digest_is_path_independent(tmp_path: Path) -> None:
    left = tmp_path / "machine-a" / "input.xlsx"
    right = tmp_path / "different-root" / "renamed.xlsx"
    left.parent.mkdir()
    right.parent.mkdir()
    workbook_bytes = b"identical-workbook-package"
    left.write_bytes(workbook_bytes)
    right.write_bytes(workbook_bytes)
    left_ref = ArtifactRef(0, hashlib.sha256(left.read_bytes()).hexdigest())
    right_ref = ArtifactRef(0, hashlib.sha256(right.read_bytes()).hexdigest())

    left_record = _record_for_artifact(left_ref)
    right_record = _record_for_artifact(right_ref)

    assert left_record.canonical_sha256 == right_record.canonical_sha256
    assert left_record.canonical_json() == right_record.canonical_json()
    assert str(tmp_path) not in left_record.canonical_json()


def test_provenance_is_canonical_and_disclaims_signature_or_correctness() -> None:
    record = _record_for_artifact(R0)
    document = record.to_dict()
    payload = {key: value for key, value in document.items() if key != "provenance_sha256"}
    expected_digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()

    assert json.loads(record.canonical_json()) == document
    assert document["provenance_sha256"] == expected_digest
    assert document["assurance"] == {
        "digest_purpose": "integrity-checksum-only",
        "is_digital_signature": False,
        "proves_declared_target_correct": False,
        "proves_user_intent_satisfied": False,
    }


def test_transition_lineage_must_start_from_exact_current_artifact() -> None:
    gate = TargetGroundingStateMachine(R0)

    with pytest.raises(TargetGroundingError, match="current workbook bytes"):
        gate.record_artifact_transition(
            ArtifactTransition(2, "edit", "artifact_rewrite", R1, R2)
        )

    assert gate.current_artifact == R0


def test_two_phase_authorization_binds_exact_staged_artifact_and_transition() -> None:
    gate, declaration_id = _grounded_gate()
    prepared = gate.prepare_staged_diff(
        declaration_id,
        _changed(EvidenceScope.one("Sales", "B2:C3")),
        staged_artifact=R1,
    )

    assert isinstance(prepared, PreparedTargetAuthorization)
    assert gate.current_artifact == R0
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    gate.validate_prepared_transition(prepared, transition)
    record = gate.commit_prepared(prepared, transition)

    assert record.decision is GroundingDecision.AUTHORIZED
    assert gate.current_artifact == R1
    assert gate.records[-1] == record
    committed = gate.committed_authorizations[-1]
    assert isinstance(committed, CommittedTargetAuthorization)
    assert committed.provenance == record
    assert committed.staged_artifact == R1
    assert committed.transition == transition
    assert committed_authorization_from_dict(committed.to_dict()) == committed
    assert validate_committed_authorization_chain(
        [committed.to_dict()],
        transitions=(transition,),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == (committed,)
    with pytest.raises(TargetGroundingError, match="stale|unknown"):
        gate.commit_prepared(prepared, transition)


def test_committed_authorization_rejects_non_string_effects_fail_closed() -> None:
    gate, declaration_id = _grounded_gate()
    prepared = gate.prepare_staged_diff(
        declaration_id,
        _changed(EvidenceScope.one("Sales", "B2:C3")),
        staged_artifact=R1,
    )
    committed = gate.preview_committed_authorization(
        prepared,
        ArtifactTransition(1, "write_range", "mutation", R0, R1),
    ).to_dict()
    committed["provenance"]["staged_footprint"]["effects"] = [{"malicious": "unhashable"}]

    with pytest.raises(TargetGroundingError, match="footprint effects are invalid"):
        committed_authorization_from_dict(committed)


def test_committed_strict_noop_has_canonical_chained_record() -> None:
    gate, declaration_id = _grounded_gate()
    prepared = gate.prepare_staged_diff(
        declaration_id,
        _no_op(),
        staged_artifact=R0,
    )

    preview = gate.preview_committed_authorization(prepared, None)
    provenance = gate.commit_prepared(
        prepared,
        None,
        committed_authorization=preview,
    )

    assert provenance.decision is GroundingDecision.AUTHORIZED_NO_OP
    assert preview.publication_kind == "strict_no_op"
    assert preview.transition is None
    assert preview.previous_authorization_sha256 == "0" * 64
    assert gate.current_artifact == R0
    assert gate.committed_authorizations == (preview,)
    assert validate_committed_authorization_chain(
        [preview.to_dict()],
        transitions=(),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == (preview,)


def test_enforced_commit_accepts_reason_redacted_preview_round_trip() -> None:
    gate, declaration_id = _grounded_gate()
    footprint = WorkbookEffectDiff(
        semantic_changed=True,
        complete=True,
        effects=frozenset({EffectKind.VALUE}),
        scope=EvidenceScope.one("Sales", "B2:C3"),
        formula_scope=EvidenceScope(),
        changed_cell_count=1,
        scanned_cell_count=20,
        reasons=("diagnostic path is not certificate data",),
    )
    prepared = gate.prepare_staged_diff(
        declaration_id,
        footprint,
        staged_artifact=R1,
    )
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    preview = gate.preview_committed_authorization(prepared, transition)
    normalized = committed_authorization_from_dict(preview.to_dict())

    assert normalized != preview
    assert normalized.to_dict() == preview.to_dict()
    gate.commit_prepared(
        prepared,
        transition,
        committed_authorization=normalized,
    )

    assert gate.committed_authorizations == (preview,)


def test_prepared_authorization_rejects_mismatched_publish_transition() -> None:
    gate, declaration_id = _grounded_gate()
    prepared = gate.prepare_staged_diff(
        declaration_id,
        _changed(EvidenceScope.one("Sales", "B2")),
        staged_artifact=R1,
    )

    with pytest.raises(TargetGroundingError, match="does not match"):
        gate.validate_prepared_transition(
            prepared,
            ArtifactTransition(1, "write_range", "mutation", R0, R1_OTHER_BYTES),
        )

    gate.abort_prepared(prepared)
    assert gate.current_artifact == R0
    assert gate.records == ()
    with pytest.raises(TargetGroundingError, match="consumed.*replayed"):
        gate.authorize_staged_diff(
            declaration_id,
            _changed(EvidenceScope.one("Sales", "B2")),
        )


@pytest.mark.parametrize(
    ("diff", "staged_artifact", "error"),
    [
        (object(), R1, TypeError),
        (_no_op(), R2, TargetGroundingError),
    ],
)
def test_enforced_prepare_preserves_legacy_malformed_input_consumption(
    diff: object,
    staged_artifact: ArtifactRef,
    error: type[Exception],
) -> None:
    gate, declaration_id = _grounded_gate()

    with pytest.raises(error):
        gate.prepare_staged_diff(  # type: ignore[arg-type]
            declaration_id,
            diff,
            staged_artifact=staged_artifact,
        )

    with pytest.raises(TargetGroundingError, match="consumed.*replayed"):
        gate.authorize_staged_diff(
            declaration_id,
            _changed(EvidenceScope.one("Sales", "B2")),
        )


@pytest.mark.parametrize(
    "diff",
    [
        WorkbookEffectDiff(
            semantic_changed=True,
            complete=True,
            effects=frozenset({EffectKind.VALUE}),
            scope=EvidenceScope.one("Sales", "B2"),
            formula_scope=EvidenceScope(),
            changed_cell_count=2,
            scanned_cell_count=1,
        ),
        WorkbookEffectDiff(
            semantic_changed=True,
            complete=True,
            effects=frozenset({EffectKind.STYLE}),
            scope=EvidenceScope.one("Sales", "B2"),
            formula_scope=EvidenceScope(),
            changed_cell_count=0,
            scanned_cell_count=20,
        ),
        WorkbookEffectDiff(
            semantic_changed=True,
            complete=True,
            effects=frozenset({EffectKind.FORMULA}),
            scope=EvidenceScope.one("Sales", "B2"),
            formula_scope=EvidenceScope(),
            changed_cell_count=1,
            scanned_cell_count=20,
        ),
        WorkbookEffectDiff(
            semantic_changed=True,
            complete=True,
            effects=frozenset({EffectKind.VALUE}),
            scope=EvidenceScope.one("Sales", "B2"),
            formula_scope=EvidenceScope.one("Sales", "B2"),
            changed_cell_count=1,
            scanned_cell_count=20,
        ),
        WorkbookEffectDiff(
            semantic_changed=True,
            complete=True,
            effects=frozenset({EffectKind.FORMULA}),
            scope=EvidenceScope.one("Sales", "B2"),
            formula_scope=EvidenceScope.one("Sales", "C3"),
            changed_cell_count=1,
            scanned_cell_count=20,
        ),
    ],
    ids=[
        "changed-exceeds-scanned",
        "cell-effect-zero-count",
        "formula-missing-scope",
        "nonformula-has-formula-scope",
        "formula-outside-scope",
    ],
)
def test_authorizer_enforces_strict_workbook_footprint_schema(
    diff: WorkbookEffectDiff,
) -> None:
    gate, declaration_id = _grounded_gate()

    with pytest.raises(TargetGroundingRejected) as rejected:
        gate.authorize_staged_diff(declaration_id, diff)

    assert rejected.value.record.decision is GroundingDecision.INVALID_FOOTPRINT


def test_advisory_assessment_records_counterfactual_rejection_without_authorization() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1:D10"),
    )
    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "B2:C3"),
        observation_ids=(observation.observation_id,),
    )
    prepared = gate.prepare_advisory_staged_diff(
        declaration.declaration_id,
        _changed(EvidenceScope.one("Sales", "D4")),
        staged_artifact=R1,
    )

    assert isinstance(prepared, PreparedAdvisoryTargetAssessment)
    assert prepared.assessment.would_reject is True
    assert prepared.assessment.decision is GroundingDecision.OUTSIDE_DECLARED_TARGET
    assert prepared.assessment.to_dict()["mode"] == "advisory"
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    committed = gate.preview_committed_advisory_assessment(prepared, transition)
    assessment = gate.commit_advisory_assessment(
        prepared,
        transition,
        committed_assessment=committed,
    )

    assert isinstance(committed, CommittedAdvisoryTargetAssessment)
    assert committed.to_dict()["decision"] == "published_after_advisory_assessment"
    assert assessment == prepared.assessment
    assert gate.current_artifact == R1
    assert gate.committed_authorizations == ()
    assert gate.committed_advisory_assessments == (committed,)
    assert advisory_assessment_from_dict(assessment.to_dict()) == assessment
    assert committed_advisory_assessment_from_dict(committed.to_dict()) == committed
    assert validate_committed_advisory_assessment_chain(
        [committed.to_dict()],
        **_advisory_lifecycle_kwargs(gate),
        transitions=(transition,),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == (committed,)


def test_advisory_commit_accepts_reason_redacted_preview_round_trip() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "A1"),
        observation_ids=(observation.observation_id,),
    )
    footprint = WorkbookEffectDiff.unknown("/machine-local/private/workbook.xml")
    prepared = gate.prepare_advisory_staged_diff(
        declaration.declaration_id,
        footprint,
        staged_artifact=R1,
    )
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    preview = gate.preview_committed_advisory_assessment(prepared, transition)
    normalized = committed_advisory_assessment_from_dict(preview.to_dict())

    assert normalized != preview
    assert normalized.to_dict() == preview.to_dict()
    gate.commit_advisory_assessment(
        prepared,
        transition,
        committed_assessment=normalized,
    )

    assert gate.committed_advisory_assessments == (preview,)
    assert validate_committed_advisory_assessment_chain(
        [preview.to_dict()],
        **_advisory_lifecycle_kwargs(gate),
        transitions=(transition,),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == (normalized,)


@pytest.mark.parametrize(
    ("declaration_id", "decision"),
    [
        (None, GroundingDecision.MISSING_DECLARATION),
        (0, GroundingDecision.INVALID_DECLARATION),
        (99, GroundingDecision.UNKNOWN_DECLARATION),
    ],
)
def test_advisory_assessment_is_total_for_unavailable_declarations(
    declaration_id: int | None,
    decision: GroundingDecision,
) -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    prepared = gate.prepare_advisory_staged_diff(
        declaration_id,
        _changed(EvidenceScope.one("Sales", "D4")),
        staged_artifact=R1,
    )

    assert prepared.assessment.decision is decision
    assert prepared.assessment.would_reject is True
    diagnostic = prepared.assessment.model_diagnostic()
    assert diagnostic["decision"] == decision.value
    assert diagnostic["declaration_status"] in {"missing", "invalid", "unknown"}
    assert "mode" not in diagnostic
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    gate.commit_advisory_assessment(prepared, transition)
    assert gate.current_artifact == R1
    assert gate.committed_authorizations == ()


def test_enforced_chain_rejects_rehashed_strict_noop_reordering() -> None:
    gate = TargetGroundingStateMachine(R0)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    for _ in range(2):
        declaration = gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=[observation.observation_id],
        )
        prepared = gate.prepare_staged_diff(
            declaration.declaration_id,
            _no_op(),
            staged_artifact=R0,
        )
        gate.commit_prepared(prepared, None)

    original = [item.to_dict() for item in gate.committed_authorizations]
    reordered = [json.loads(json.dumps(original[1])), json.loads(json.dumps(original[0]))]
    reordered[0]["authorization_id"] = 1
    reordered[0]["previous_authorization_sha256"] = "0" * 64
    reordered[0]["authorization_sha256"] = _canonical_digest(
        reordered[0],
        "authorization_sha256",
    )
    reordered[1]["authorization_id"] = 2
    reordered[1]["previous_authorization_sha256"] = reordered[0][
        "authorization_sha256"
    ]
    reordered[1]["authorization_sha256"] = _canonical_digest(
        reordered[1],
        "authorization_sha256",
    )

    with pytest.raises(TargetGroundingError, match="strictly increasing"):
        validate_committed_authorization_chain(
            reordered,
            transitions=(),
            initial_artifact=R0,
            initial_transition_count=0,
        )


def test_enforced_state_machine_refuses_out_of_order_prepared_commits() -> None:
    gate = TargetGroundingStateMachine(R0)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    declarations = [
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=[observation.observation_id],
        )
        for _ in range(2)
    ]
    prepared = [
        gate.prepare_staged_diff(
            declaration.declaration_id,
            _no_op(),
            staged_artifact=R0,
        )
        for declaration in declarations
    ]

    gate.commit_prepared(prepared[1], None)
    with pytest.raises(TargetGroundingError, match="committed chronology"):
        gate.commit_prepared(prepared[0], None)
    gate.abort_prepared(prepared[0])

    committed = gate.committed_authorizations
    assert validate_committed_authorization_chain(
        [item.to_dict() for item in committed],
        transitions=(),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == committed


def test_advisory_chain_rejects_rehash_reordering_and_missing_transition() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    for _ in range(2):
        declaration = gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=[observation.observation_id],
        )
        prepared = gate.prepare_advisory_staged_diff(
            declaration.declaration_id,
            _no_op(),
            staged_artifact=R0,
        )
        gate.commit_advisory_assessment(prepared, None)

    original = [item.to_dict() for item in gate.committed_advisory_assessments]
    reordered = [json.loads(json.dumps(original[1])), json.loads(json.dumps(original[0]))]
    reordered[0]["commitment_id"] = 1
    reordered[0]["previous_commitment_sha256"] = "0" * 64
    reordered[0]["commitment_sha256"] = _canonical_digest(
        reordered[0],
        "commitment_sha256",
    )
    reordered[1]["commitment_id"] = 2
    reordered[1]["previous_commitment_sha256"] = reordered[0][
        "commitment_sha256"
    ]
    reordered[1]["commitment_sha256"] = _canonical_digest(
        reordered[1],
        "commitment_sha256",
    )
    with pytest.raises(TargetGroundingError):
        validate_committed_advisory_assessment_chain(
            reordered,
            **_advisory_lifecycle_kwargs(gate),
            transitions=(),
            initial_artifact=R0,
            initial_transition_count=0,
        )

    transition_gate = TargetGroundingStateMachine(
        R0,
        mode=TargetGroundingMode.ADVISORY,
    )
    prepared = transition_gate.prepare_advisory_staged_diff(
        None,
        _changed(EvidenceScope.one("Sales", "A1")),
        staged_artifact=R1,
    )
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    transition_gate.commit_advisory_assessment(prepared, transition)
    with pytest.raises(TargetGroundingError):
        validate_committed_advisory_assessment_chain(
            [],
            **_advisory_lifecycle_kwargs(transition_gate),
            transitions=(transition,),
            initial_artifact=R0,
            initial_transition_count=0,
        )


def test_advisory_state_machine_refuses_out_of_order_prepared_commits() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    declarations = [
        gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=[observation.observation_id],
        )
        for _ in range(2)
    ]
    prepared = [
        gate.prepare_advisory_staged_diff(
            declaration.declaration_id,
            _no_op(),
            staged_artifact=R0,
        )
        for declaration in declarations
    ]

    gate.commit_advisory_assessment(prepared[1], None)
    with pytest.raises(TargetGroundingError, match="committed chronology"):
        gate.commit_advisory_assessment(prepared[0], None)
    gate.abort_prepared_advisory_assessment(prepared[0])

    committed = gate.committed_advisory_assessments
    assert validate_committed_advisory_assessment_chain(
        [item.to_dict() for item in committed],
        **_advisory_lifecycle_kwargs(gate),
        transitions=(),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == committed


def test_advisory_parser_rejects_rehashed_assurance_tampering() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    prepared = gate.prepare_advisory_staged_diff(
        None,
        _no_op(),
        staged_artifact=R0,
    )
    committed = gate.preview_committed_advisory_assessment(prepared, None).to_dict()
    assessment = committed["assessment"]
    assessment["assurance"]["authorized_publication"] = True
    assessment["assessment_sha256"] = _canonical_digest(
        assessment,
        "assessment_sha256",
    )
    committed["commitment_sha256"] = _canonical_digest(
        committed,
        "commitment_sha256",
    )

    with pytest.raises(TargetGroundingError, match="digest or fields"):
        committed_advisory_assessment_from_dict(committed)


def test_advisory_chain_rejects_rehashed_current_declaration_on_stale_bytes() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "A1"),
        observation_ids=[observation.observation_id],
    )
    prepared = gate.prepare_advisory_staged_diff(
        declaration.declaration_id,
        _no_op(),
        staged_artifact=R0,
    )
    committed = gate.preview_committed_advisory_assessment(prepared, None).to_dict()
    assessment = committed["assessment"]
    stale_artifact = R1.to_dict()
    assessment["declaration"]["artifact"] = stale_artifact
    assessment["observations"][0]["artifact"] = stale_artifact
    provenance = assessment["provenance"]
    provenance["declaration"]["artifact"] = stale_artifact
    provenance["observations"][0]["artifact"] = stale_artifact
    provenance["provenance_sha256"] = _canonical_digest(
        provenance,
        "provenance_sha256",
    )
    assessment["assessment_sha256"] = _canonical_digest(
        assessment,
        "assessment_sha256",
    )
    committed["commitment_sha256"] = _canonical_digest(
        committed,
        "commitment_sha256",
    )

    with pytest.raises(TargetGroundingError):
        validate_committed_advisory_assessment_chain(
            [committed],
            **_advisory_lifecycle_kwargs(gate),
            transitions=(),
            initial_artifact=R0,
            initial_transition_count=0,
        )


def test_advisory_chain_rejects_rehashed_inconsistent_unavailable_footprint() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    prepared = gate.prepare_advisory_staged_diff(
        None,
        _no_op(),
        staged_artifact=R0,
    )
    committed = gate.preview_committed_advisory_assessment(prepared, None).to_dict()
    assessment = committed["assessment"]
    footprint = assessment["staged_footprint"]
    footprint.update(
        {
            "effects": [EffectKind.VALUE.value],
            "scope": EvidenceScope.one("Sales", "A1").to_dict(),
            "changed_cell_count": 1,
            "scanned_cell_count": 1,
        }
    )
    assessment["assessment_sha256"] = _canonical_digest(
        assessment,
        "assessment_sha256",
    )
    committed["commitment_sha256"] = _canonical_digest(
        committed,
        "commitment_sha256",
    )

    with pytest.raises(TargetGroundingError):
        validate_committed_advisory_assessment_chain(
            [committed],
            **_advisory_lifecycle_kwargs(gate),
            transitions=(),
            initial_artifact=R0,
            initial_transition_count=0,
        )


@pytest.mark.parametrize("declaration_id", [None, 0, 99])
@pytest.mark.parametrize(
    ("diff", "error"),
    [
        (
            WorkbookEffectDiff(
                semantic_changed=False,
                complete=True,
                effects=frozenset({EffectKind.VALUE}),
                scope=EvidenceScope.one("Sales", "A1"),
                formula_scope=EvidenceScope(),
                changed_cell_count=1,
                scanned_cell_count=1,
            ),
            "internally inconsistent",
        ),
        (
            _changed(EvidenceScope.one("Sales", "A1")),
            "cannot preserve exact artifact bytes",
        ),
    ],
)
def test_advisory_live_producer_rejects_nonreplayable_unavailable_footprint(
    declaration_id: int | None,
    diff: WorkbookEffectDiff,
    error: str,
) -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)

    with pytest.raises(TargetGroundingError, match=error):
        gate.prepare_advisory_staged_diff(
            declaration_id,
            diff,
            staged_artifact=R0,
        )

    assert gate.committed_advisory_assessments == ()


def test_advisory_valid_invalid_footprint_commit_freshly_replays() -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "A1"),
        observation_ids=[observation.observation_id],
    )
    invalid = WorkbookEffectDiff(
        semantic_changed=True,
        complete=True,
        effects=frozenset({EffectKind.VALUE}),
        scope=EvidenceScope.one("Sales", "A1"),
        formula_scope=EvidenceScope(),
        changed_cell_count=2,
        scanned_cell_count=1,
    )
    prepared = gate.prepare_advisory_staged_diff(
        declaration.declaration_id,
        invalid,
        staged_artifact=R1,
    )
    transition = ArtifactTransition(1, "write_range", "mutation", R0, R1)
    gate.commit_advisory_assessment(prepared, transition)

    assert prepared.assessment.decision is GroundingDecision.INVALID_FOOTPRINT
    assert validate_committed_advisory_assessment_chain(
        [item.to_dict() for item in gate.committed_advisory_assessments],
        **_advisory_lifecycle_kwargs(gate),
        transitions=(transition,),
        initial_artifact=R0,
        initial_transition_count=0,
    ) == gate.committed_advisory_assessments


@pytest.mark.parametrize("stale_artifact", [R0_OTHER_BYTES, R2])
def test_advisory_chain_rejects_rehashed_stale_declaration_outside_prior_lineage(
    stale_artifact: ArtifactRef,
) -> None:
    gate = TargetGroundingStateMachine(R0, mode=TargetGroundingMode.ADVISORY)
    observation = gate.record_trusted_observation(
        artifact=R0,
        scope=EvidenceScope.one("Sales", "A1"),
    )
    declaration = gate.declare_target(
        artifact=R0,
        target_scope=EvidenceScope.one("Sales", "A1"),
        observation_ids=[observation.observation_id],
    )
    transition = ArtifactTransition(1, "recalculate", "recalculation", R0, R1)
    gate.record_artifact_transition(transition)
    prepared = gate.prepare_advisory_staged_diff(
        declaration.declaration_id,
        _no_op(),
        staged_artifact=R1,
    )
    committed = gate.preview_committed_advisory_assessment(prepared, None).to_dict()
    assessment = committed["assessment"]
    replacement = stale_artifact.to_dict()
    assessment["declaration"]["artifact"] = replacement
    assessment["observations"][0]["artifact"] = replacement
    provenance = assessment["provenance"]
    provenance["declaration"]["artifact"] = replacement
    provenance["observations"][0]["artifact"] = replacement
    provenance["provenance_sha256"] = _canonical_digest(
        provenance,
        "provenance_sha256",
    )
    assessment["assessment_sha256"] = _canonical_digest(
        assessment,
        "assessment_sha256",
    )
    committed["commitment_sha256"] = _canonical_digest(
        committed,
        "commitment_sha256",
    )

    with pytest.raises(TargetGroundingError):
        validate_committed_advisory_assessment_chain(
            [committed],
            **_advisory_lifecycle_kwargs(gate),
            transitions=(transition,),
            initial_artifact=R0,
            initial_transition_count=0,
        )


def test_advisory_and_enforce_replay_diagnostics_are_mode_neutral() -> None:
    gates = {
        mode: TargetGroundingStateMachine(R0, mode=mode)
        for mode in (TargetGroundingMode.ADVISORY, TargetGroundingMode.ENFORCE)
    }
    declaration_ids: dict[TargetGroundingMode, int] = {}
    for mode, gate in gates.items():
        observation = gate.record_trusted_observation(
            artifact=R0,
            scope=EvidenceScope.one("Sales", "A1"),
        )
        declaration = gate.declare_target(
            artifact=R0,
            target_scope=EvidenceScope.one("Sales", "A1"),
            observation_ids=[observation.observation_id],
        )
        declaration_ids[mode] = declaration.declaration_id
        if mode is TargetGroundingMode.ADVISORY:
            prepared = gate.prepare_advisory_staged_diff(
                declaration.declaration_id,
                _no_op(),
                staged_artifact=R0,
            )
            gate.commit_advisory_assessment(prepared, None)
        else:
            prepared = gate.prepare_staged_diff(
                declaration.declaration_id,
                _no_op(),
                staged_artifact=R0,
            )
            gate.commit_prepared(prepared, None)

    advisory_replay = gates[
        TargetGroundingMode.ADVISORY
    ].prepare_advisory_staged_diff(
        declaration_ids[TargetGroundingMode.ADVISORY],
        _no_op(),
        staged_artifact=R0,
    )
    with pytest.raises(TargetGroundingRejected) as enforced_replay:
        gates[TargetGroundingMode.ENFORCE].prepare_staged_diff(
            declaration_ids[TargetGroundingMode.ENFORCE],
            _no_op(),
            staged_artifact=R0,
        )

    assert (
        advisory_replay.assessment.model_diagnostic()
        == enforced_replay.value.model_diagnostic
    )
    assert (
        advisory_replay.assessment.model_diagnostic()["decision"]
        == "rejected.replayed_declaration"
    )
