from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook

import spreadsheet_harness.deliverable as deliverable_module
from spreadsheet_harness.deliverable import (
    COMPARISON_RESULT_SCHEMA_VERSION,
    DeliverableValidationError,
    audit_deliverable_certificate,
    finalize_deliverable,
    score_read_only,
    validate_evidence_certificate,
)
from spreadsheet_harness.evidence_contract import (
    PIXEL_SHA256_ALGORITHM,
    ContractSpec,
    EffectKind,
    EventKind,
    EvidenceContractMonitor,
    EvidenceEvent,
    EvidenceScope,
)
from spreadsheet_harness.session import WorkbookSession


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _book(path: Path) -> None:
    workbook = Workbook()
    workbook.active.title = "Sheet1"
    workbook.active["A1"] = 0
    workbook.save(path)
    workbook.close()


def _candidate(
    tmp_path: Path,
    *,
    target_grounding: bool = False,
) -> tuple[WorkbookSession, dict[str, Any]]:
    source = tmp_path / "source.xlsx"
    _book(source)
    session = WorkbookSession.create(source, tmp_path / "run", run_id="deliverable-test")
    initial = session.artifact_ref()
    if target_grounding:
        session.enable_target_grounding()
        observation = session.record_target_observation(
            artifact=initial,
            scope=EvidenceScope.one("Sheet1", "A1"),
        )
        declaration = session.declare_edit_target(
            target_scope=EvidenceScope.one("Sheet1", "A1"),
            observation_ids=[observation["observation_id"]],
        )
        session.write_range(
            "Sheet1",
            "A1",
            [[42]],
            declaration_id=declaration["declaration_id"],
        )
    else:
        session.write_range("Sheet1", "A1", [[42]])
    candidate = session.artifact_ref()

    spec = ContractSpec.from_mapping(
        {
            "schema_version": 1,
            "rules": [
                {
                    "id": "readback",
                    "trigger": "mutation.committed",
                    "require": {"event": "range.inspected", "artifact": "current"},
                }
            ],
        }
    )
    monitor = EvidenceContractMonitor(spec, initial.sha256)
    monitor.observe(
        EvidenceEvent(
            EventKind.MUTATION_COMMITTED,
            initial.sha256,
            candidate.sha256,
            effects=frozenset({EffectKind.VALUE}),
            scope=EvidenceScope.one("Sheet1", "A1:A1"),
        )
    )
    monitor.observe(
        EvidenceEvent(
            EventKind.RANGE_INSPECTED,
            candidate.sha256,
            scope=EvidenceScope.one("Sheet1", "A1:A1"),
        )
    )
    decision = monitor.submission_decision().to_dict()
    session.enable_completion_attempt_capture()
    attempt = session.capture_completion_attempt(
        stage="solve",
        turn=1,
        response_id="response-1",
        call_id="submit-call-1",
    )
    agent = {
        "final_text": "done",
        "turns": 1,
        "stage": "solve",
        "response_id": "response-1",
        "terminal_tool": "submit_result",
        "observed_terminal_tool": "submit_result",
        "terminal_submissions": 1,
        "terminal_response": {
            "status": "accepted",
            "response_id": "response-1",
            "acknowledgement": {},
            "completion_attempt_id": attempt.attempt_id,
        },
        "completion_attempts": [attempt.to_dict()],
        "evidence_contract": {"status": monitor.status(), "decision": decision},
    }
    return session, agent


def _visual_candidate(
    tmp_path: Path,
    *,
    page_sha256: str,
) -> tuple[WorkbookSession, dict[str, Any]]:
    source = tmp_path / "visual-source.xlsx"
    _book(source)
    session = WorkbookSession.create(
        source,
        tmp_path / "visual-run",
        run_id="visual-deliverable-test",
    )
    initial = session.artifact_ref()
    session.format_range("Sheet1", "A1:A1", {"font": {"bold": True}})
    candidate = session.artifact_ref()
    spec = ContractSpec.from_mapping(
        {
            "schema_version": 1,
            "rules": [
                {
                    "id": "visual",
                    "trigger": "effects.visual_changed",
                    "require_sequence": [
                        {"event": "workbook.rendered", "artifact": "current"},
                        {
                            "event": "rendered_page.viewed",
                            "artifact": "same_render",
                            "scope": "changed_visual_scope",
                        },
                    ],
                }
            ],
        }
    )
    monitor = EvidenceContractMonitor(spec, initial.sha256)
    scope = EvidenceScope.one("Sheet1", "A1:A1")
    render_page = {
        "page_id": "Sheet1:1",
        "page_index": 1,
        "file_sha256": page_sha256,
        "width": 1,
        "height": 1,
        "sheet": "Sheet1",
        "sheet_page": 1,
        "cell_scope": scope.to_dict(),
    }
    monitor.observe(
        EvidenceEvent(
            EventKind.MUTATION_COMMITTED,
            initial.sha256,
            candidate.sha256,
            effects=frozenset({EffectKind.STYLE, EffectKind.VISUAL}),
            scope=scope,
        )
    )
    monitor.observe(
        EvidenceEvent(
            EventKind.WORKBOOK_RENDERED,
            candidate.sha256,
            render_id="candidate-render",
            render_manifest_sha256="a" * 64,
            metadata={
                "producer_tool": "render_workbook",
                "backend": "test-renderer",
                "version": {"renderer": "1"},
                "mode": "per_sheet",
                "dpi": 144,
                "page_count": 1,
                "pages": [render_page],
            },
        )
    )
    monitor.observe(
        EvidenceEvent(
            EventKind.RENDERED_PAGE_VIEWED,
            candidate.sha256,
            scope=scope,
            related_render_id="candidate-render",
            related_render_manifest_sha256="a" * 64,
            page_id="Sheet1:1",
            page_sha256=page_sha256,
            metadata={
                "producer_tool": "view_image",
                "delivery_status": "provider_response_confirmed",
                "confirmation_id": "candidate-confirmation",
                "provider_response_id": "response-visual-view",
                "attachment_file_sha256": page_sha256,
                "page_file_sha256": page_sha256,
                "page_pixel_sha256": "b" * 64,
                "pixel_sha256_algorithm": PIXEL_SHA256_ALGORITHM,
                "width": 1,
                "height": 1,
                "image_mode": "RGB",
                "render_mode": "per_sheet",
                "page_index": 1,
                "sheet": "Sheet1",
                "sheet_page": 1,
                "cell_scope": scope.to_dict(),
            },
        )
    )
    session.enable_completion_attempt_capture()
    attempt = session.capture_completion_attempt(
        stage="solve",
        turn=1,
        response_id="response-visual",
        call_id="submit-call-visual",
    )
    agent = {
        "final_text": "visual done",
        "turns": 1,
        "stage": "solve",
        "response_id": "response-visual",
        "terminal_tool": "submit_result",
        "observed_terminal_tool": "submit_result",
        "terminal_submissions": 1,
        "terminal_response": {
            "status": "accepted",
            "response_id": "response-visual",
            "acknowledgement": {},
            "completion_attempt_id": attempt.attempt_id,
        },
        "completion_attempts": [attempt.to_dict()],
        "evidence_contract": {
            "status": monitor.status(),
            "decision": monitor.submission_decision().to_dict(),
        },
    }
    return session, agent


def _fake_render(
    page_bytes: bytes,
):
    def render(
        workbook_path: Path,
        output_dir: Path,
        *,
        workspace: Path,
        artifact: Any,
        timeout_seconds: float,
        dpi: int,
    ) -> dict[str, Any]:
        del workbook_path, timeout_seconds
        output_dir.mkdir(parents=True)
        page = output_dir / "page-001.png"
        page.write_bytes(page_bytes)
        pages = [
            {
                "index": 1,
                "relative_path": page.relative_to(workspace).as_posix(),
                "sha256": _sha256(page),
                "width": 1,
                "height": 1,
                "sheet": "Sheet1",
                "sheet_page": 1,
            }
        ]
        portable_manifest = {
            "schema_version": "spreadsheet-portable-render-manifest-v1",
            "artifact": artifact.to_dict(),
            "backend": "test-renderer",
            "version": {"renderer": "1"},
            "mode": "per_sheet",
            "dpi": dpi,
            "page_count": 1,
            "pages": pages,
        }
        manifest = output_dir / "portable-render-manifest.json"
        manifest.write_text(
            json.dumps(
                portable_manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="ascii",
        )
        record = {
            **portable_manifest,
            "schema_version": "spreadsheet-final-render-witness-v1",
            "manifest_relative_path": manifest.relative_to(workspace).as_posix(),
            "manifest_sha256": _sha256(manifest),
        }
        return {
            **record,
            "witness_sha256": hashlib.sha256(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest(),
        }

    return render


def _unchanged_recalculation(session: WorkbookSession) -> dict[str, Any]:
    artifact = session.artifact_ref()
    return {
        "backend": "test-recalculator",
        "version": "1",
        "profile": "isolated",
        "format": "xlsx",
        "source_sha256": artifact.sha256,
        "output_sha256": artifact.sha256,
        "atomic_replace": True,
        "workbook_sha256_before": artifact.sha256,
        "workbook_sha256_after": artifact.sha256,
        "workbook_changed": False,
        "artifact_revision_before": artifact.revision,
        "artifact_revision_after": artifact.revision,
        "artifact_transition_id": None,
        "workbook_effects": {
            "schema_version": "workbook-effect-diff-v1",
            "semantic_changed": False,
            "complete": True,
            "effects": [],
            "scope": EvidenceScope().to_dict(),
            "formula_scope": EvidenceScope().to_dict(),
            "changed_cell_count": 0,
            "scanned_cell_count": 1,
            "reasons": [],
        },
    }


def _changed_recalculation(session: WorkbookSession) -> dict[str, Any]:
    before = session.artifact_ref()
    workbook = load_workbook(session.workbook_path)
    try:
        workbook.calculation.fullCalcOnLoad = not bool(workbook.calculation.fullCalcOnLoad)
        workbook.save(session.workbook_path)
    finally:
        workbook.close()
    transition = session.reconcile_external_artifact(
        before,
        operation="recalculate",
        kind="derived_recalculation",
    )
    assert transition is not None
    after = session.artifact_ref()
    return {
        "backend": "test-recalculator",
        "version": "1",
        "profile": "isolated",
        "format": "xlsx",
        "source_sha256": before.sha256,
        "output_sha256": after.sha256,
        "atomic_replace": True,
        "workbook_sha256_before": before.sha256,
        "workbook_sha256_after": after.sha256,
        "workbook_changed": True,
        "artifact_revision_before": before.revision,
        "artifact_revision_after": after.revision,
        "artifact_transition_id": transition.transition_id,
        "workbook_effects": {
            "schema_version": "workbook-effect-diff-v1",
            "semantic_changed": False,
            "complete": True,
            "effects": [],
            "scope": EvidenceScope().to_dict(),
            "formula_scope": EvidenceScope().to_dict(),
            "changed_cell_count": 0,
            "scanned_cell_count": 1,
            "reasons": [],
        },
    }


def _semantic_recalculation(session: WorkbookSession) -> dict[str, Any]:
    before = session.artifact_ref()
    workbook = load_workbook(session.workbook_path)
    try:
        workbook.active["A1"] = 99
        workbook.save(session.workbook_path)
    finally:
        workbook.close()
    transition = session.reconcile_external_artifact(
        before,
        operation="recalculate",
        kind="derived_recalculation",
    )
    assert transition is not None
    after = session.artifact_ref()
    return {
        "backend": "test-recalculator",
        "version": "1",
        "profile": "isolated",
        "format": "xlsx",
        "source_sha256": before.sha256,
        "output_sha256": after.sha256,
        "atomic_replace": True,
        "workbook_sha256_before": before.sha256,
        "workbook_sha256_after": after.sha256,
        "workbook_changed": True,
        "artifact_revision_before": before.revision,
        "artifact_revision_after": after.revision,
        "artifact_transition_id": transition.transition_id,
        "workbook_effects": {
            "schema_version": "workbook-effect-diff-v1",
            "semantic_changed": True,
            "complete": True,
            "effects": [EffectKind.VALUE.value],
            "scope": EvidenceScope.one("Sheet1", "A1:A1").to_dict(),
            "formula_scope": EvidenceScope().to_dict(),
            "changed_cell_count": 1,
            "scanned_cell_count": 1,
            "reasons": [],
        },
    }


def _unknown_recalculation(session: WorkbookSession) -> dict[str, Any]:
    metadata = _semantic_recalculation(session)
    metadata["workbook_effects"] = {
        "schema_version": "workbook-effect-diff-v1",
        "semantic_changed": True,
        "complete": False,
        "effects": [EffectKind.UNKNOWN.value],
        "scope": EvidenceScope.workbook().to_dict(),
        "formula_scope": EvidenceScope().to_dict(),
        "changed_cell_count": 0,
        "scanned_cell_count": 0,
        "reasons": ["unsupported OOXML extension content changed"],
    }
    return metadata


def _certificate_digest(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "certificate_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _nested_digest(document: dict[str, Any], digest_field: str) -> str:
    payload = {key: value for key, value in document.items() if key != digest_field}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _witness_digest(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "witness_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def test_finalization_binds_candidate_final_copy_and_read_only_score(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )

    assert COMPARISON_RESULT_SCHEMA_VERSION == "spreadsheet-comparison-result-v28"
    assert bundle.candidate_artifact == bundle.final_artifact
    assert bundle.certificate["final_artifact"] == bundle.final_artifact.to_dict()
    assert bundle.certificate["scoring_copy"]["sha256"] == bundle.final_artifact.sha256
    assert bundle.certificate["scoring_copy"]["artifact_role"] == "same_revision_replica"
    assert bundle.certificate["scoring_copy"]["creates_artifact_transition"] is False
    assert bundle.certificate["lineage"]["transition_count"] == len(session.artifact_transitions)
    submission = bundle.certificate["candidate"]["submission"]
    assert submission["completion_attempt_id"] == 1
    assert submission["completion_attempt_count"] == 1
    assert (
        submission["completion_attempt_record_sha256"]
        == agent["completion_attempts"][0]["record_sha256"]
    )
    assert str(tmp_path) not in json.dumps(bundle.certificate, ensure_ascii=True)
    assert _sha256(bundle.scoring_copy) == _sha256(session.workbook_path)
    assert bundle.scoring_copy.stat().st_mode & 0o222 == 0
    assert score_read_only(bundle, lambda path: load_workbook(path).active["A1"].value) == 42

    audit = audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    )
    assert audit.valid is True
    assert audit.final_artifact == bundle.final_artifact
    assert audit.scoring_copy == bundle.scoring_copy


def test_v28_rejects_missing_completion_attempt_id(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path)
    del agent["terminal_response"]["completion_attempt_id"]

    with pytest.raises(DeliverableValidationError, match="accepted terminal response"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


def test_v28_certifies_noncompletion_without_claiming_accepted_evidence(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    agent["final_text"] = "Evidence obligations remained unsatisfied."
    agent["terminal_response"] = None
    agent["evidence_contract"] = {
        "status": {"submission_ready": False},
        "decision": {"allowed": False, "contract_satisfied": False},
    }

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )

    candidate = bundle.certificate["candidate"]
    assert candidate["outcome"] == "audited_noncompletion"
    assert candidate["evidence_certificate"] is None
    assert candidate["submission"]["accepted_submission"] is False
    assert candidate["submission"]["accepted_evidence_certificate"] is False
    assert candidate["submission"]["completion_attempt_count"] == 1
    assert bundle.certificate["evidence_policy"]["accepted_candidate_evidence"] is False
    assert bundle.certificate["evidence_policy"]["candidate_evidence_carried_forward"] is False
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_v28_noncompletion_records_semantic_recalculation_without_claiming_correctness(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    agent["final_text"] = "Evidence obligations remained unsatisfied."
    agent["terminal_response"] = None
    agent["evidence_contract"] = {
        "status": {"submission_ready": False},
        "decision": {"allowed": False, "contract_satisfied": False},
    }

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _semantic_recalculation(session),
    )

    assert bundle.certificate["candidate"]["outcome"] == "audited_noncompletion"
    effects = bundle.certificate["postprocess"]["recalculation"]["workbook_effects"]
    assert effects["semantic_changed"] is True
    assert effects["effects"] == [EffectKind.VALUE.value]
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_v28_accepted_candidate_rejects_semantic_postprocess_recalculation(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)

    with pytest.raises(
        DeliverableValidationError,
        match="Postprocess recalculation changed workbook semantics",
    ):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _semantic_recalculation(session),
        )


def test_v28_noncompletion_records_fail_closed_unknown_recalculation(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    agent["final_text"] = "Postprocess semantics could not be classified."
    agent["terminal_response"] = None
    agent["evidence_contract"] = {
        "status": {"submission_ready": False},
        "decision": {"allowed": False, "contract_satisfied": False},
    }

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unknown_recalculation(session),
    )

    effects = bundle.certificate["postprocess"]["recalculation"]["workbook_effects"]
    assert effects["complete"] is False
    assert effects["effects"] == [EffectKind.UNKNOWN.value]
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_v28_accepted_candidate_rejects_unknown_postprocess_recalculation(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)

    with pytest.raises(
        DeliverableValidationError,
        match="Recalculation workbook effects are incomplete",
    ):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unknown_recalculation(session),
        )


def test_v28_certifies_early_stage_noncompletion_with_no_captured_attempts(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    agent.update(
        {
            "final_text": "The read-only preprocessing stage failed.",
            "response_id": None,
            "terminal_response": None,
            "terminal_submissions": 0,
            "completion_attempts": [],
            "evidence_contract": None,
            "stages": [
                {
                    "name": "extract",
                    "agent": {
                        "completion_attempts": None,
                        "terminal_response": None,
                    },
                }
            ],
        }
    )

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )

    assert bundle.certificate["candidate"]["outcome"] == "audited_noncompletion"
    assert bundle.certificate["candidate"]["submission"]["completion_attempt_count"] == 0
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_v28_rejects_unknown_completion_attempt_id(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path)
    agent["terminal_response"]["completion_attempt_id"] = 99

    with pytest.raises(DeliverableValidationError, match="not bound"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


def test_v28_rejects_completion_attempt_response_id_mismatch(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path)
    attempt = agent["completion_attempts"][0]
    attempt["response_id"] = "different-response"
    attempt["record_sha256"] = _nested_digest(attempt, "record_sha256")

    with pytest.raises(DeliverableValidationError, match="not bound"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


def test_v28_rejects_attempt_that_was_not_the_accepted_terminal_call(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    second = session.capture_completion_attempt(
        stage="solve",
        turn=2,
        response_id="response-1",
        call_id="submit-call-2",
    )
    agent["completion_attempts"].append(second.to_dict())
    agent["terminal_submissions"] = 2
    agent["turns"] = 2
    agent["terminal_response"]["completion_attempt_id"] = 1

    with pytest.raises(DeliverableValidationError, match="not bound"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


@pytest.mark.parametrize(
    ("render_timeout", "recalculation_timeout", "expected_timeout"),
    [
        (7.25, None, 7.25),
        (7.25, 2.5, 2.5),
    ],
)
def test_finalization_passes_exact_recalculation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    render_timeout: float,
    recalculation_timeout: float | None,
    expected_timeout: float,
) -> None:
    session, agent = _candidate(tmp_path)
    observed: list[float] = []

    def fake_recalculate(*, timeout_seconds: float) -> dict[str, Any]:
        observed.append(timeout_seconds)
        return _unchanged_recalculation(session)

    monkeypatch.setattr(session, "recalculate", fake_recalculate)

    bundle = finalize_deliverable(
        session,
        agent,
        render_timeout_seconds=render_timeout,
        recalculation_timeout_seconds=recalculation_timeout,
    )

    assert observed == [expected_timeout]
    assert bundle.certificate["postprocess"]["timeout_seconds"] == expected_timeout


def test_final_certificate_exposes_and_replays_committed_target_authorization(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path, target_grounding=True)

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )

    grounding = bundle.certificate["target_grounding"]
    assert grounding["enabled"] is True
    assert grounding["initial_artifact"]["revision"] == 0
    assert grounding["initial_transition_count"] == 0
    assert grounding["authorization_count"] == 1
    authorization = grounding["authorizations"][0]
    assert authorization["publication"]["kind"] == "artifact_transition"
    assert (
        authorization["publication"]["transition"]
        == bundle.certificate["lineage"]["transitions"][0]
    )
    assert grounding["authorization_chain_head_sha256"] == authorization["authorization_sha256"]
    trajectory = [json.loads(line) for line in session.paths.trajectory.read_text().splitlines()]
    durable = [
        json.loads(row["payload"]["target_grounding_commit_json"])
        for row in trajectory
        if row["event"] == "artifact.transition"
        and "target_grounding_commit_json" in row["payload"]
    ]
    assert durable == [authorization]
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_strict_noop_has_one_durable_authorization_without_a_transition(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path, target_grounding=True)
    artifact = session.artifact_ref()
    observation = session.record_target_observation(
        artifact=artifact,
        scope=EvidenceScope.one("Sheet1", "A1"),
    )
    declaration = session.declare_edit_target(
        target_scope=EvidenceScope.one("Sheet1", "A1"),
        observation_ids=[observation["observation_id"]],
    )
    transition_count = len(session.artifact_transitions)

    result = session.run_staged_external_mutation(
        operation="code_interpreter",
        declaration_id=declaration["declaration_id"],
        runner=lambda _path: {"ok": True},
    )

    assert result["target_grounding"]["decision"] == "authorized_no_op"
    assert len(session.artifact_transitions) == transition_count
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    grounding = bundle.certificate["target_grounding"]
    assert grounding["authorization_count"] == 2
    no_op = grounding["authorizations"][1]
    assert no_op["publication"] == {"kind": "strict_no_op", "transition": None}
    assert no_op["staged_artifact"] == artifact.to_dict()
    assert (
        no_op["previous_authorization_sha256"]
        == grounding["authorizations"][0]["authorization_sha256"]
    )
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_fresh_audit_rejects_rehashed_target_scope_tampering(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path, target_grounding=True)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    tampered = json.loads(json.dumps(bundle.certificate))
    authorization = tampered["target_grounding"]["authorizations"][0]
    target_range = authorization["provenance"]["declaration"]["target_scope"]["ranges"][0]
    target_range.update({"range": "B1:B1", "cell_count": 1})
    provenance = authorization["provenance"]
    provenance["provenance_sha256"] = _nested_digest(provenance, "provenance_sha256")
    authorization["authorization_sha256"] = _nested_digest(authorization, "authorization_sha256")
    tampered["target_grounding"]["authorization_chain_head_sha256"] = authorization[
        "authorization_sha256"
    ]
    tampered["certificate_sha256"] = _certificate_digest(tampered)

    assert not audit_deliverable_certificate(
        tampered,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_fresh_audit_rejects_missing_committed_authorization(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path, target_grounding=True)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    tampered = json.loads(json.dumps(bundle.certificate))
    grounding = tampered["target_grounding"]
    grounding["authorizations"] = []
    grounding["authorization_count"] = 0
    grounding["authorization_chain_head_sha256"] = "0" * 64
    tampered["certificate_sha256"] = _certificate_digest(tampered)

    assert not audit_deliverable_certificate(
        tampered,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_fresh_audit_rejects_duplicate_committed_authorization(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path, target_grounding=True)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    tampered = json.loads(json.dumps(bundle.certificate))
    grounding = tampered["target_grounding"]
    grounding["authorizations"].append(json.loads(json.dumps(grounding["authorizations"][0])))
    grounding["authorization_count"] = 2
    grounding["authorization_chain_head_sha256"] = grounding["authorizations"][1][
        "authorization_sha256"
    ]
    tampered["certificate_sha256"] = _certificate_digest(tampered)

    assert not audit_deliverable_certificate(
        tampered,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_fresh_audit_rejects_authorization_hash_cycle(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path, target_grounding=True)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    tampered = json.loads(json.dumps(bundle.certificate))
    authorization = tampered["target_grounding"]["authorizations"][0]
    authorization["previous_authorization_sha256"] = authorization["authorization_sha256"]
    authorization["authorization_sha256"] = _nested_digest(authorization, "authorization_sha256")
    tampered["target_grounding"]["authorization_chain_head_sha256"] = authorization[
        "authorization_sha256"
    ]
    tampered["certificate_sha256"] = _certificate_digest(tampered)

    assert not audit_deliverable_certificate(
        tampered,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_candidate_certificate_cannot_replay_after_artifact_hash_cycle(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    certified_bytes = session.workbook_path.read_bytes()
    certified_sha256 = _sha256(session.workbook_path)

    session.write_range("Sheet1", "A1", [[43]])
    before_restore = session.artifact_ref()
    session.workbook_path.write_bytes(certified_bytes)
    transition = session.reconcile_external_artifact(
        before_restore,
        operation="test_restore_old_bytes",
    )

    assert transition is not None
    assert session.artifact_ref().sha256 == certified_sha256
    assert session.artifact_ref().revision == 3
    assert agent["evidence_contract"]["decision"]["certificate"]["revision_index"] == 1

    with pytest.raises(ValueError, match="stale for the managed artifact revision"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("terminal_tool", "assistant_text", "terminal submission"),
        ("observed_terminal_tool", "assistant_text", "terminal submission"),
        ("terminal_submissions", 0, "terminal submission"),
        ("response_id", "different-response", "accepted terminal response"),
    ],
)
def test_finalization_rejects_unbound_terminal_evidence(
    tmp_path: Path,
    field: str,
    value: Any,
    message: str,
) -> None:
    session, agent = _candidate(tmp_path)
    agent[field] = value

    with pytest.raises(ValueError, match=message):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


def test_scoring_replica_creation_cannot_publish_an_artifact_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, agent = _candidate(tmp_path)
    original_copy = deliverable_module._atomic_scoring_copy

    def copy_with_unexpected_transition(source: Path, destination: Path) -> str:
        digest = original_copy(source, destination)
        session.write_range("Sheet1", "A1", [[43]])
        return digest

    monkeypatch.setattr(
        deliverable_module,
        "_atomic_scoring_copy",
        copy_with_unexpected_transition,
    )

    with pytest.raises(ValueError, match="must not publish an artifact transition"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _unchanged_recalculation(session),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_role", "new_artifact_revision"),
        ("creates_artifact_transition", True),
    ],
)
def test_audit_rejects_scoring_replica_revision_semantics_tampering(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    session, agent = _candidate(tmp_path)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    tampered = json.loads(json.dumps(bundle.certificate))
    tampered["scoring_copy"][field] = value
    tampered["certificate_sha256"] = _certificate_digest(tampered)

    assert not audit_deliverable_certificate(
        tampered,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", 7),
        ("version", {"local": "1"}),
        ("profile", "/tmp/unsafe-profile"),
        ("format", "/tmp/output.xlsx"),
    ],
)
def test_recalculation_certificate_metadata_is_strict_and_path_free(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    session, agent = _candidate(tmp_path)

    def invalid_recalculation() -> dict[str, Any]:
        metadata = _unchanged_recalculation(session)
        metadata[field] = value
        return metadata

    with pytest.raises(ValueError, match="Recalculation"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=invalid_recalculation,
        )


def test_recalculation_effects_reject_absolute_diagnostic_paths(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)

    def invalid_recalculation() -> dict[str, Any]:
        metadata = _unchanged_recalculation(session)
        metadata["workbook_effects"]["reasons"] = ["ValueError: /tmp/private/workbook.xlsx"]
        return metadata

    with pytest.raises(ValueError, match="absolute path"):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=invalid_recalculation,
        )


def test_changed_recalculation_advances_lineage_and_invalidates_carry_forward(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)

    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _changed_recalculation(session),
    )

    assert bundle.final_artifact.revision == bundle.candidate_artifact.revision + 1
    assert bundle.final_artifact.sha256 != bundle.candidate_artifact.sha256
    assert bundle.certificate["evidence_policy"] == {
        "accepted_candidate_evidence": True,
        "candidate_evidence_carried_forward": False,
        "changed_recalculation_invalidates_candidate_evidence": True,
        "fresh_final_revision_readback": True,
        "candidate_visual_evidence_present": False,
        "pixel_equivalence_required_for_visual_carry": False,
        "pixel_equivalence_observed": False,
        "unviewed_final_render_never_counts_as_viewed": True,
    }
    assert bundle.certificate["lineage"]["transition_count"] == 2
    assert bundle.certificate["postprocess"]["recalculation"]["artifact_transition_id"] == 2
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_evidence_certificate_replay_rejects_scope_and_witness_tampering(
    tmp_path: Path,
) -> None:
    _, agent = _candidate(tmp_path)
    certificate = json.loads(json.dumps(agent["evidence_contract"]["decision"]["certificate"]))
    certificate["events"][-1]["scope"] = EvidenceScope.one("Other", "A1:A1").to_dict()
    event_payload = {
        key: value
        for key, value in certificate["events"][-1].items()
        if key != "event_chain_sha256"
    }
    previous_chain = certificate["events"][-2]["event_chain_sha256"]
    event_bytes = json.dumps(
        event_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    new_chain = hashlib.sha256(bytes.fromhex(previous_chain) + event_bytes).hexdigest()
    certificate["events"][-1]["event_chain_sha256"] = new_chain
    certificate["event_chain_sha256"] = new_chain
    certificate["certificate_sha256"] = _certificate_digest(certificate)

    try:
        validate_evidence_certificate(certificate)
    except ValueError as exc:
        assert "scope, witnesses, or obligations" in str(exc)
    else:
        raise AssertionError("tampered scope unexpectedly passed certificate replay")


def test_audit_rejects_tampered_scoring_copy_and_outer_certificate(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )

    tampered_certificate = json.loads(json.dumps(bundle.certificate))
    tampered_certificate["final_artifact"]["sha256"] = "f" * 64
    assert not audit_deliverable_certificate(
        tampered_certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid

    os.chmod(bundle.scoring_copy, 0o600)
    workbook = load_workbook(bundle.scoring_copy)
    try:
        workbook.active["A1"] = 7
        workbook.save(bundle.scoring_copy)
    finally:
        workbook.close()
    assert not audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_scoring_copy_must_remain_read_only_even_when_bytes_match(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    bundle.scoring_copy.chmod(0o600)

    with pytest.raises(ValueError, match="read-only regular non-symbolic"):
        score_read_only(bundle, lambda _: None)
    assert not audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_score_read_only_reports_mutation_when_scorer_also_raises(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    scorer_error = RuntimeError("scorer failed after writing")

    def mutate_then_raise(path: Path) -> None:
        path.chmod(0o600)
        path.write_bytes(b"tampered scoring bytes")
        raise scorer_error

    with pytest.raises(DeliverableValidationError, match="during scoring") as caught:
        score_read_only(bundle, mutate_then_raise)

    assert caught.value.__cause__ is scorer_error


def test_score_read_only_preserves_unmutated_scorer_exception(tmp_path: Path) -> None:
    session, agent = _candidate(tmp_path)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    scorer_error = RuntimeError("scorer failed without writing")

    def raise_without_mutation(_: Path) -> None:
        raise scorer_error

    with pytest.raises(RuntimeError, match="without writing") as caught:
        score_read_only(bundle, raise_without_mutation)

    assert caught.value is scorer_error


def test_fresh_audit_rejects_internal_scoring_copy_symlink(
    tmp_path: Path,
) -> None:
    session, agent = _candidate(tmp_path)
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _unchanged_recalculation(session),
    )
    relocated = bundle.scoring_copy.parent / "relocated-final.xlsx"
    bundle.scoring_copy.replace(relocated)
    bundle.scoring_copy.symlink_to(relocated.name)

    audit = audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    )

    assert audit.valid is False
    assert audit.reasons == ("deliverable_lineage_invalid:DeliverableValidationError",)


def test_changed_visual_revision_fails_when_required_viewed_page_pixels_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_page = b"\x89PNG\r\n\x1a\ncandidate-pixels"
    candidate_sha256 = hashlib.sha256(candidate_page).hexdigest()
    session, agent = _visual_candidate(tmp_path, page_sha256=candidate_sha256)
    monkeypatch.setattr(
        deliverable_module,
        "_render_final_revision",
        _fake_render(b"\x89PNG\r\n\x1a\nchanged-final-pixels"),
    )

    with pytest.raises(
        ValueError,
        match="required viewed page changed",
    ):
        finalize_deliverable(
            session,
            agent,
            recalculation_callback=lambda: _changed_recalculation(session),
        )


def test_pixel_identical_visual_equivalence_is_auditable_and_tamper_evident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = b"\x89PNG\r\n\x1a\npixel-identical"
    page_sha256 = hashlib.sha256(page_bytes).hexdigest()
    session, agent = _visual_candidate(tmp_path, page_sha256=page_sha256)
    monkeypatch.setattr(
        deliverable_module,
        "_render_final_revision",
        _fake_render(page_bytes),
    )
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _changed_recalculation(session),
    )

    equivalence = bundle.certificate["visual_equivalence_witness"]
    assert equivalence["all_required_pages_pixel_identical"] is True
    assert (
        equivalence["render_groups"][0]["pages"][0]["candidate_page_sha256"]
        == equivalence["render_groups"][0]["pages"][0]["final_page_sha256"]
    )
    assert audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid

    final_page = (
        session.workspace / equivalence["render_groups"][0]["pages"][0]["final_page_relative_path"]
    )
    final_page.write_bytes(b"\x89PNG\r\n\x1a\ntampered-final-page")
    assert not audit_deliverable_certificate(
        bundle.certificate,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid


def test_visual_equivalence_mapping_tamper_fails_even_with_recomputed_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = b"\x89PNG\r\n\x1a\npixel-identical"
    page_sha256 = hashlib.sha256(page_bytes).hexdigest()
    session, agent = _visual_candidate(tmp_path, page_sha256=page_sha256)
    monkeypatch.setattr(
        deliverable_module,
        "_render_final_revision",
        _fake_render(page_bytes),
    )
    bundle = finalize_deliverable(
        session,
        agent,
        recalculation_callback=lambda: _changed_recalculation(session),
    )
    tampered = json.loads(json.dumps(bundle.certificate))
    equivalence = tampered["visual_equivalence_witness"]
    equivalence["render_groups"][0]["pages"][0]["page_id"] = "Other:1"
    equivalence["witness_sha256"] = _witness_digest(equivalence)
    tampered["certificate_sha256"] = _certificate_digest(tampered)

    assert not audit_deliverable_certificate(
        tampered,
        agent_evidence=agent,
        run_root=session.workspace,
        output_workbook=session.workbook_path,
    ).valid
