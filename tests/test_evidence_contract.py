from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spreadsheet_harness.evidence_contract import (
    PIXEL_SHA256_ALGORITHM,
    ContractMode,
    ContractSpec,
    ContractStateError,
    ContractValidationError,
    EffectKind,
    EventKind,
    EvidenceContractMonitor,
    EvidenceEvent,
    EvidenceScope,
    audit_evidence_certificate,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "spreadsheet-evidence-v1.yaml"
R0 = "0" * 64
R1 = "1" * 64
R2 = "2" * 64
R3 = "3" * 64
MANIFEST_1 = "a" * 64
PAGE_1 = "b" * 64
PIXEL_1 = "e" * 64


def _monitor(*, mode: ContractMode = ContractMode.ENFORCE) -> EvidenceContractMonitor:
    return EvidenceContractMonitor(ContractSpec.load(CONTRACT), R0, mode=mode)


def _mutation(
    before: str,
    after: str,
    *,
    scope: EvidenceScope | None = None,
    effects: frozenset[EffectKind] = frozenset({EffectKind.VALUE}),
    formula_scope: EvidenceScope | None = None,
) -> EvidenceEvent:
    return EvidenceEvent(
        EventKind.MUTATION_COMMITTED,
        before,
        after,
        effects=effects,
        scope=scope or EvidenceScope.one("Sales", "B2:B2"),
        formula_scope=formula_scope or EvidenceScope(),
    )


def _inspection(
    revision: str,
    range_ref: str,
    *,
    predicates: frozenset[str] = frozenset(),
) -> EvidenceEvent:
    return EvidenceEvent(
        EventKind.RANGE_INSPECTED,
        revision,
        scope=EvidenceScope.one("Sales", range_ref),
        predicates=predicates,
    )


def _recalculation(before: str, after: str) -> EvidenceEvent:
    return EvidenceEvent(
        EventKind.WORKBOOK_RECALCULATED,
        before,
        after,
        metadata={
            "producer_tool": "recalculate_and_read",
            "calculation": {
                "backend": "libreoffice-headless",
                "version": "test-version",
                "source_sha256": before,
                "output_sha256": after,
                "atomic_replace": True,
            },
        },
    )


def _render_page(
    page_id: str = "Sales:1",
    *,
    page_index: int = 1,
    file_sha256: str = PAGE_1,
    sheet: str | None = "Sales",
    sheet_page: int | None = 1,
    cell_scope: EvidenceScope | None = None,
) -> dict[str, object]:
    return {
        "page_id": page_id,
        "page_index": page_index,
        "file_sha256": file_sha256,
        "width": 320,
        "height": 240,
        "sheet": sheet,
        "sheet_page": sheet_page,
        "cell_scope": cell_scope.to_dict() if cell_scope is not None else None,
    }


def _render(
    revision: str,
    *,
    render_id: str = "render-current",
    manifest_sha256: str = MANIFEST_1,
    pages: list[dict[str, object]] | None = None,
    mode: str = "per_sheet",
) -> EvidenceEvent:
    inventory = pages or [_render_page()]
    return EvidenceEvent(
        EventKind.WORKBOOK_RENDERED,
        revision,
        render_id=render_id,
        render_manifest_sha256=manifest_sha256,
        metadata={
            "producer_tool": "render_workbook",
            "backend": "libreoffice-pymupdf",
            "version": {"libreoffice": "test", "pymupdf": "test"},
            "mode": mode,
            "dpi": 144,
            "page_count": len(inventory),
            "pages": inventory,
        },
    )


def _view(
    revision: str,
    *,
    page: dict[str, object] | None = None,
    render_id: str = "render-current",
    manifest_sha256: str = MANIFEST_1,
    mode: str = "per_sheet",
    pixel_sha256: str = PIXEL_1,
) -> EvidenceEvent:
    selected = page or _render_page()
    raw_scope = selected["cell_scope"]
    scope = (
        EvidenceScope.from_dict(raw_scope)
        if isinstance(raw_scope, dict)
        else EvidenceScope()
    )
    return EvidenceEvent(
        EventKind.RENDERED_PAGE_VIEWED,
        revision,
        scope=scope,
        related_render_id=render_id,
        related_render_manifest_sha256=manifest_sha256,
        page_id=str(selected["page_id"]),
        page_sha256=str(selected["file_sha256"]),
        metadata={
            "producer_tool": "view_image",
            "delivery_status": "provider_response_confirmed",
            "confirmation_id": f"confirmation-{selected['page_index']}",
            "provider_response_id": f"response-{selected['page_index']}",
            "attachment_file_sha256": selected["file_sha256"],
            "page_file_sha256": selected["file_sha256"],
            "page_pixel_sha256": pixel_sha256,
            "pixel_sha256_algorithm": PIXEL_SHA256_ALGORITHM,
            "width": selected["width"],
            "height": selected["height"],
            "image_mode": "RGB",
            "render_mode": mode,
            "page_index": selected["page_index"],
            "sheet": selected["sheet"],
            "sheet_page": selected["sheet_page"],
            "cell_scope": selected["cell_scope"],
        },
    )


def test_default_contract_loads_with_raw_and_canonical_hashes() -> None:
    spec = ContractSpec.load(CONTRACT)

    assert [rule.id for rule in spec.rules] == [
        "post_write_readback",
        "formula_recalc_no_error",
        "visual_render_view",
    ]
    assert len(spec.canonical_sha256) == 64
    assert len(spec.source_sha256 or "") == 64
    assert spec.source_path == str(CONTRACT)
    assert ContractSpec.load(CONTRACT).canonical_sha256 == spec.canonical_sha256


@pytest.mark.parametrize(
    "contract",
    [
        {"schema_version": 2, "rules": []},
        {"schema_version": 1, "rules": [], "extra": True},
        {
            "schema_version": 1,
            "rules": [
                {
                    "id": "arbitrary",
                    "trigger": "mutation.committed",
                    "require": {
                        "event": "range.inspected",
                        "predicates": ["model_says_correct"],
                    },
                }
            ],
        },
        {
            "schema_version": 1,
            "rules": [
                {
                    "id": "duplicate",
                    "trigger": "mutation.committed",
                    "require": {"event": "range.inspected"},
                },
                {
                    "id": "duplicate",
                    "trigger": "effects.formula_changed",
                    "require": {"event": "workbook.recalculated"},
                },
            ],
        },
    ],
)
def test_contract_validation_fails_closed(contract: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError):
        ContractSpec.from_mapping(contract)


def test_scope_coverage_and_boundary_are_sheet_aware() -> None:
    target = EvidenceScope.one("Sales", "B2:C3").expand(1)

    assert target == EvidenceScope.one("Sales", "A1:D4")
    assert EvidenceScope.one("Sales", "A1:D4").covers(target)
    assert not EvidenceScope.one("Sales", "B2:C3").covers(target)
    assert not EvidenceScope.one("Other", "A1:D4").covers(target)
    assert EvidenceScope.workbook().covers(target)
    assert not target.covers(EvidenceScope.workbook())


def test_write_requires_current_revision_readback_with_boundary() -> None:
    monitor = _monitor()

    state = monitor.observe(_mutation(R0, R1))
    assert state["submission_ready"] is False
    assert monitor.next_required_event() is EventKind.RANGE_INSPECTED
    assert monitor.submission_decision().allowed is False

    monitor.observe(_inspection(R1, "B2:B2"))
    assert monitor.submission_decision().allowed is False

    monitor.observe(_inspection(R1, "A1:C3"))
    decision = monitor.submission_decision()
    assert decision.allowed is True
    assert decision.contract_satisfied is True
    assert decision.certificate is not None
    assert decision.certificate["accepted_revision_sha256"] == R1
    assert len(decision.certificate["certificate_sha256"]) == 64


def test_later_mutation_invalidates_previously_completed_evidence() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R1, scope=EvidenceScope.one("Sales", "B2:B2")))
    monitor.observe(_inspection(R1, "A1:C3"))
    assert monitor.submission_decision().allowed is True

    monitor.observe(_mutation(R1, R2, scope=EvidenceScope.one("Sales", "D4:D4")))

    assert monitor.submission_decision().allowed is False
    assert len(monitor.pending) == 2
    assert all(not obligation.complete for obligation in monitor.obligations)
    with pytest.raises(ContractStateError, match="expected"):
        monitor.observe(_inspection(R1, "A1:E5"))

    monitor.observe(_inspection(R2, "A1:E5"))
    assert monitor.submission_decision().allowed is True


def test_formula_contract_requires_recalc_then_error_free_current_readback() -> None:
    monitor = _monitor()
    formula_scope = EvidenceScope.one("Sales", "D2:D10")
    monitor.observe(
        _mutation(
            R0,
            R1,
            scope=formula_scope,
            formula_scope=formula_scope,
            effects=frozenset({EffectKind.FORMULA}),
        )
    )

    assert len(monitor.pending) == 2
    assert monitor.minimum_evidence_calls() == 2

    # A pre-recalculation read can satisfy generic readback, but not formula verification.
    monitor.observe(_inspection(R1, "C1:E11", predicates=frozenset({"no_calc_error"})))
    assert len(monitor.pending) == 1
    assert monitor.next_required_event() is EventKind.WORKBOOK_RECALCULATED

    monitor.observe(_recalculation(R1, R2))
    # Recalculation creates a new artifact revision, so generic readback is stale again.
    assert len(monitor.pending) == 2

    monitor.observe(_inspection(R2, "C1:E11"))
    assert len(monitor.pending) == 1
    assert monitor.next_required_event() is EventKind.RANGE_INSPECTED

    monitor.observe(_inspection(R2, "C1:E11", predicates=frozenset({"no_calc_error"})))
    decision = monitor.submission_decision()
    assert decision.allowed is True
    assert decision.certificate is not None
    assert decision.certificate["revision_index"] == 2


def test_formula_error_predicate_cannot_be_replaced_by_tool_prose() -> None:
    monitor = _monitor()
    scope = EvidenceScope.one("Sales", "D2:D3")
    monitor.observe(
        _mutation(
            R0,
            R1,
            scope=scope,
            formula_scope=scope,
            effects=frozenset({EffectKind.FORMULA}),
        )
    )
    monitor.observe(_recalculation(R1, R2))
    monitor.observe(_inspection(R2, "C1:E4"))

    assert monitor.submission_decision().allowed is False
    assert any(
        item.requirement is not None
        and item.requirement.predicates == frozenset({"no_calc_error"})
        for item in monitor.pending
    )


def test_visual_contract_requires_view_from_same_current_render() -> None:
    monitor = _monitor()
    monitor.observe(
        _mutation(
            R0,
            R1,
            effects=frozenset({EffectKind.STYLE}),
        )
    )
    monitor.observe(_render(R1))
    monitor.observe(
        _view(
            R1,
            render_id="render-old",
            manifest_sha256="c" * 64,
            page=_render_page("other:1", file_sha256="d" * 64),
        )
    )
    assert monitor.submission_decision().allowed is False

    monitor.observe(_view(R1))
    # The visual sequence is complete, but post-write range readback remains pending.
    assert monitor.submission_decision().allowed is False
    monitor.observe(_inspection(R1, "A1:C3"))
    assert monitor.submission_decision().allowed is True


def test_visual_evidence_is_reopened_after_a_new_revision() -> None:
    monitor = _monitor()
    monitor.observe(
        _mutation(R0, R1, effects=frozenset({EffectKind.STYLE}))
    )
    monitor.observe(_render(R1, render_id="render-1"))
    monitor.observe(_view(R1, render_id="render-1"))
    monitor.observe(_inspection(R1, "A1:C3"))
    assert monitor.submission_decision().allowed is True

    monitor.observe(_mutation(R1, R2, scope=EvidenceScope.one("Sales", "F2:F2")))

    with pytest.raises(ContractStateError):
        monitor.observe(_view(R1, render_id="render-1"))
    assert monitor.submission_decision().allowed is False
    assert any(item.rule.id == "visual_render_view" for item in monitor.pending)


def test_rollback_blocks_submission_until_successful_correction() -> None:
    monitor = _monitor()
    monitor.observe(
        EvidenceEvent(
            EventKind.MUTATION_ROLLED_BACK,
            R0,
            scope=EvidenceScope.one("Sales", "B2:B2"),
        )
    )

    decision = monitor.submission_decision()
    assert decision.allowed is False
    assert "unresolved_mutation_failure" in decision.reasons

    monitor.observe(_mutation(R0, R1))
    monitor.observe(_inspection(R1, "A1:C3"))
    assert monitor.submission_decision().allowed is True


def test_shadow_mode_records_violations_without_blocking() -> None:
    monitor = _monitor(mode=ContractMode.SHADOW)
    monitor.observe(_mutation(R0, R1))

    decision = monitor.submission_decision()
    assert decision.allowed is True
    assert decision.contract_satisfied is False
    assert decision.certificate is None
    assert decision.pending
    status = monitor.status()
    assert status["contract_satisfied"] is False
    assert status["enforcement_active"] is False
    assert status["would_block"] is True
    assert status["submission_allowed"] is True


def test_shadow_mode_does_not_label_an_unchanged_artifact_satisfied() -> None:
    decision = _monitor(mode=ContractMode.SHADOW).submission_decision()

    assert decision.allowed is True
    assert decision.contract_satisfied is False
    assert decision.artifact_changed is False
    assert decision.reasons == ("artifact_unchanged",)


def test_contract_mode_rejects_ambiguous_boolean() -> None:
    with pytest.raises(ValueError, match="shadow.*enforce"):
        EvidenceContractMonitor(ContractSpec.load(CONTRACT), R0, mode=True)  # type: ignore[arg-type]


def test_noop_mutation_does_not_trigger_obligations_or_certify() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R0))

    assert not monitor.obligations
    decision = monitor.submission_decision()
    assert decision.allowed is False
    assert decision.contract_satisfied is False
    assert decision.reasons == ("artifact_unchanged",)


def test_read_event_cannot_publish_an_artifact_revision() -> None:
    with pytest.raises(ValueError, match="must not publish"):
        EvidenceEvent(
            EventKind.RANGE_INSPECTED,
            R0,
            R1,
            scope=EvidenceScope.one("Sales", "A1:A1"),
        )


def test_recalculation_without_user_mutation_cannot_certify() -> None:
    monitor = _monitor()
    monitor.observe(_recalculation(R0, R1))

    decision = monitor.submission_decision()
    assert decision.allowed is False
    assert decision.contract_satisfied is False
    assert decision.artifact_changed is False
    assert decision.reasons == ("artifact_unchanged",)


def test_changed_mutation_with_empty_scope_fails_closed() -> None:
    with pytest.raises(ValueError, match="non-empty scope"):
        EvidenceEvent(
            EventKind.MUTATION_COMMITTED,
            R0,
            R1,
            effects=frozenset({EffectKind.VALUE}),
        )


def test_disjoint_authenticated_inspections_accumulate_scope_coverage() -> None:
    monitor = _monitor()
    scope = EvidenceScope(
        (
            EvidenceScope.one("Sales", "B2:B2").ranges[0],
            EvidenceScope.one("Sales", "F6:F6").ranges[0],
        )
    )
    monitor.observe(_mutation(R0, R1, scope=scope))

    monitor.observe(_inspection(R1, "A1:C3"))
    assert monitor.submission_decision().allowed is False
    monitor.observe(_inspection(R1, "E5:G7"))

    assert monitor.submission_decision().allowed is True
    witnesses = monitor.obligations[0].witnesses
    assert [item.event_id for item in witnesses] == [2, 3]


def test_visual_contract_requires_every_page_of_the_changed_sheet() -> None:
    monitor = _monitor()
    monitor.observe(
        _mutation(R0, R1, effects=frozenset({EffectKind.STYLE}))
    )
    sales_1 = _render_page()
    sales_2 = _render_page(
        "Sales:2",
        page_index=2,
        file_sha256="c" * 64,
        sheet_page=2,
    )
    other_1 = _render_page(
        "Other:1",
        page_index=3,
        file_sha256="d" * 64,
        sheet="Other",
    )
    monitor.observe(
        _render(
            R1,
            render_id="render-1",
            pages=[sales_1, sales_2, other_1],
        )
    )
    obligation = next(item for item in monitor.pending if item.rule.id == "visual_render_view")
    assert obligation.required_page_ids == ("Sales:1", "Sales:2")
    assert obligation.visual_coverage_policy == "all_pages_for_affected_sheets"

    monitor.observe(_view(R1, render_id="render-1", page=other_1))
    assert any(item.rule.id == "visual_render_view" for item in monitor.pending)

    monitor.observe(_view(R1, render_id="render-1", page=sales_1))
    assert any(item.rule.id == "visual_render_view" for item in monitor.pending)
    assert monitor.minimum_evidence_calls() == 1

    monitor.observe(_view(R1, render_id="render-1", page=sales_2, pixel_sha256="f" * 64))
    assert not any(item.rule.id == "visual_render_view" for item in monitor.pending)


def test_visual_contract_stays_pending_when_the_changed_sheet_was_not_rendered() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R1, effects=frozenset({EffectKind.STYLE})))
    other = _render_page(
        "Other:1",
        file_sha256="d" * 64,
        sheet="Other",
    )
    monitor.observe(_render(R1, pages=[other]))

    obligation = next(item for item in monitor.pending if item.rule.id == "visual_render_view")
    assert obligation.required_page_ids == ("<missing-sheet>:Sales",)
    assert obligation.visual_coverage_policy == "missing_affected_sheet_pages"

    monitor.observe(_view(R1, page=other))

    assert any(item.rule.id == "visual_render_view" for item in monitor.pending)


def test_visual_page_cannot_claim_an_entire_worksheet() -> None:
    page = _render_page()

    with pytest.raises(ValueError, match="cannot promote one page"):
        EvidenceEvent(
            EventKind.RENDERED_PAGE_VIEWED,
            R1,
            scope=EvidenceScope.worksheet("Sales"),
            related_render_id="render-current",
            related_render_manifest_sha256=MANIFEST_1,
            page_id="Sales:1",
            page_sha256=PAGE_1,
            metadata=_view(R1, page=page).metadata,
        )


def test_visual_cell_mapping_can_select_one_exact_page() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R1, effects=frozenset({EffectKind.STYLE})))
    mapped = _render_page(
        cell_scope=EvidenceScope.one("Sales", "A1:C10"),
    )
    unrelated = _render_page(
        "Sales:2",
        page_index=2,
        file_sha256="c" * 64,
        sheet_page=2,
        cell_scope=EvidenceScope.one("Sales", "A11:C20"),
    )
    monitor.observe(_render(R1, pages=[mapped, unrelated]))

    obligation = next(item for item in monitor.pending if item.rule.id == "visual_render_view")
    assert obligation.required_page_ids == ("Sales:1",)
    assert obligation.visual_coverage_policy == "authenticated_cell_mapping"
    monitor.observe(_view(R1, page=mapped))

    assert not any(item.rule.id == "visual_render_view" for item in monitor.pending)
    witness = obligation.witnesses[-1]
    assert witness.page_file_sha256 == PAGE_1
    assert witness.page_pixel_sha256 == PIXEL_1
    assert witness.pixel_sha256_algorithm == PIXEL_SHA256_ALGORITHM
    assert (witness.width, witness.height, witness.image_mode) == (320, 240, "RGB")
    monitor.observe(_inspection(R1, "A1:C3"))
    certificate = monitor.certificate()
    visual_witness = next(
        item
        for obligation_record in certificate["obligations"]
        for item in obligation_record["witnesses"]
        if item["kind"] == EventKind.RENDERED_PAGE_VIEWED.value
    )
    assert visual_witness["page_file_sha256"] == PAGE_1
    assert visual_witness["page_pixel_sha256"] == PIXEL_1
    assert visual_witness["pixel_sha256_algorithm"] == PIXEL_SHA256_ALGORITHM
    assert audit_evidence_certificate(certificate)["valid"] is True


def test_whole_workbook_render_requires_every_page_without_cell_mapping() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R1, effects=frozenset({EffectKind.STYLE})))
    first = _render_page(
        "workbook:first.png",
        sheet=None,
        sheet_page=None,
    )
    second = _render_page(
        "workbook:second.png",
        page_index=2,
        file_sha256="c" * 64,
        sheet=None,
        sheet_page=None,
    )
    monitor.observe(_render(R1, pages=[first, second], mode="whole_workbook"))

    obligation = next(item for item in monitor.pending if item.rule.id == "visual_render_view")
    assert obligation.required_page_ids == (
        "workbook:first.png",
        "workbook:second.png",
    )
    assert obligation.visual_coverage_policy == "all_rendered_pages"
    monitor.observe(_view(R1, page=first, mode="whole_workbook"))
    assert not obligation.complete
    monitor.observe(
        _view(
            R1,
            page=second,
            mode="whole_workbook",
            pixel_sha256="f" * 64,
        )
    )
    assert obligation.complete


def test_unrelated_successful_mutation_does_not_clear_rollback_failure() -> None:
    monitor = _monitor()
    monitor.observe(
        EvidenceEvent(
            EventKind.MUTATION_ROLLED_BACK,
            R0,
            scope=EvidenceScope.one("Sales", "B2:B2"),
        )
    )

    monitor.observe(
        _mutation(R0, R1, scope=EvidenceScope.one("Sales", "F6:F6"))
    )
    assert monitor.failures

    monitor.observe(
        _mutation(R1, R2, scope=EvidenceScope.one("Sales", "B2:B2"))
    )
    assert not monitor.failures


def test_certificate_digest_is_independent_of_contract_file_path() -> None:
    mapping = {
        "schema_version": 1,
        "rules": [
            {
                "id": "readback",
                "trigger": "mutation.committed",
                "require": {
                    "event": "range.inspected",
                    "artifact": "current",
                    "scope": "changed_cells_plus_boundary",
                },
            }
        ],
    }
    source_bytes = b"same contract bytes"
    certificates = []
    for path in ("/first/contract.yaml", "/second/contract.yaml"):
        spec = ContractSpec.from_mapping(
            mapping,
            source_bytes=source_bytes,
            source_path=path,
        )
        monitor = EvidenceContractMonitor(spec, R0)
        monitor.observe(_mutation(R0, R1))
        monitor.observe(_inspection(R1, "A1:C3"))
        certificates.append(monitor.certificate())

    assert certificates[0]["certificate_sha256"] == certificates[1]["certificate_sha256"]
    assert certificates[0]["contract"] == certificates[1]["contract"]


def test_certificate_contains_auditable_event_chain() -> None:
    monitor = _monitor()
    monitor.observe(
        EvidenceEvent(
            EventKind.MUTATION_COMMITTED,
            R0,
            R1,
            effects=frozenset({EffectKind.VALUE}),
            scope=EvidenceScope.one("Sales", "B2:B2"),
            metadata={"producer": "write_range", "trusted": True},
        )
    )
    monitor.observe(_inspection(R1, "A1:C3"))

    certificate = monitor.certificate()
    assert certificate["event_count"] == 2
    assert certificate["event_chain_sha256"] == certificate["events"][-1][
        "event_chain_sha256"
    ]
    assert certificate["events"][0]["metadata"] == {
        "producer": "write_range",
        "trusted": True,
    }
    assert certificate["event_chain_algorithm"] == "sha256-canonical-json-chain-v1"
    assert certificate["event_chain_genesis_sha256"] == "0" * 64

    audit = audit_evidence_certificate(certificate)
    assert audit == {
        "valid": True,
        "certificate_sha256": certificate["certificate_sha256"],
        "accepted_revision_sha256": R1,
        "revision_index": 1,
        "event_count": 2,
        "obligation_count": 1,
    }


def test_certificate_audit_rejects_rehashed_event_chain_tampering() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R1))
    monitor.observe(_inspection(R1, "A1:C3"))
    certificate = monitor.certificate()

    certificate["events"][0]["metadata"] = {"forged": True}
    unsigned = {key: value for key, value in certificate.items() if key != "certificate_sha256"}
    certificate["certificate_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()

    with pytest.raises(ContractValidationError, match="event chain"):
        audit_evidence_certificate(certificate)


def test_certificate_audit_rejects_rehashed_witness_tampering() -> None:
    monitor = _monitor()
    monitor.observe(_mutation(R0, R1))
    monitor.observe(_inspection(R1, "A1:C3"))
    certificate = monitor.certificate()

    certificate["obligations"][0]["witnesses"][0]["event_id"] = 1
    unsigned = {key: value for key, value in certificate.items() if key != "certificate_sha256"}
    certificate["certificate_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()

    with pytest.raises(ContractValidationError, match="replay"):
        audit_evidence_certificate(certificate)


@pytest.mark.parametrize("invalid_sha", ["revision", "A" * 64, "0" * 63])
def test_events_reject_noncanonical_revision_hashes(invalid_sha: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        EvidenceEvent(
            EventKind.RANGE_INSPECTED,
            invalid_sha,
            scope=EvidenceScope.one("Sales", "A1:A1"),
        )


def test_contract_rejects_boolean_schema_version_and_impossible_sequences() -> None:
    with pytest.raises(ContractValidationError, match="schema_version"):
        ContractSpec.from_mapping({"schema_version": True, "rules": [{}]})

    with pytest.raises(ContractValidationError, match="earlier recalculation"):
        ContractSpec.from_mapping(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "id": "wrong-order",
                        "trigger": "effects.formula_changed",
                        "require_sequence": [
                            {
                                "event": "range.inspected",
                                "artifact": "recalculated_revision",
                            },
                            {"event": "workbook.recalculated"},
                        ],
                    }
                ],
            }
        )

    with pytest.raises(ContractValidationError, match="not a trusted evidence event"):
        ContractSpec.from_mapping(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "id": "self-witnessing",
                        "trigger": "mutation.committed",
                        "require": {"event": "mutation.committed"},
                    }
                ],
            }
        )

    with pytest.raises(ContractValidationError, match="earlier render"):
        ContractSpec.from_mapping(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "id": "wrong-render-order",
                        "trigger": "effects.visual_changed",
                        "require": {
                            "event": "rendered_page.viewed",
                            "artifact": "same_render",
                        },
                    }
                ],
            }
        )


def test_event_runtime_types_and_recalculation_metadata_fail_closed() -> None:
    with pytest.raises(TypeError, match="kind must be EventKind"):
        EvidenceEvent(  # type: ignore[arg-type]
            "mutation.committed",
            R0,
            R1,
            effects=frozenset({EffectKind.VALUE}),
            scope=EvidenceScope.one("Sales", "B2:B2"),
        )

    with pytest.raises(TypeError, match="EffectKind"):
        EvidenceEvent(
            EventKind.MUTATION_COMMITTED,
            R0,
            R1,
            effects=frozenset({"value"}),  # type: ignore[arg-type]
            scope=EvidenceScope.one("Sales", "B2:B2"),
        )

    with pytest.raises(ValueError, match="portable recalculation metadata"):
        EvidenceEvent(
            EventKind.WORKBOOK_RECALCULATED,
            R0,
            R1,
            metadata={
                "producer_tool": "recalculate_and_read",
                "calculation": {
                    "backend": "libreoffice-headless",
                    "version": "LibreOffice 25.2",
                    "source_sha256": R0,
                    "output_sha256": R1,
                    "atomic_replace": True,
                    "source_path": "/tmp/private/input.xlsx",
                },
            },
        )

    with pytest.raises(ValueError, match="requires portable recalculation metadata"):
        EvidenceEvent(EventKind.WORKBOOK_RECALCULATED, R0, R1)

    with pytest.raises(ValueError, match="absolute path"):
        EvidenceEvent(
            EventKind.WORKBOOK_RECALCULATED,
            R0,
            R1,
            metadata={
                "producer_tool": "recalculate_and_read",
                "calculation": {
                    "backend": "libreoffice-headless",
                    "version": "/private/bin/libreoffice",
                    "source_sha256": R0,
                    "output_sha256": R1,
                    "atomic_replace": True,
                },
            },
        )


def test_scope_rejects_forged_serialized_cell_count() -> None:
    with pytest.raises(ValueError, match="cell_count"):
        EvidenceScope.from_dict(
            {
                "wildcard": False,
                "sheets": [],
                "ranges": [
                    {"sheet": "Sales", "range": "A1:B2", "cell_count": 1}
                ],
            }
        )


def test_scope_runtime_types_fail_closed() -> None:
    with pytest.raises(TypeError, match="wildcard must be boolean"):
        EvidenceScope(wildcard=1)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="ranges must contain CellRange"):
        EvidenceScope(ranges=("Sales!A1",))  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="sheets must contain strings"):
        EvidenceScope(sheets=(7,))  # type: ignore[arg-type]
