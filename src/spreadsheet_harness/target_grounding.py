"""Pre-edit target grounding for staged spreadsheet mutations.

This module is deliberately a pure state machine.  Harness code records trusted
observations, while model-provided declarations may only cite those observations.
The resulting provenance digest is an integrity checksum, not a signature or a
proof that the declared target is semantically correct.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .evidence_contract import (
    ArtifactRef,
    ArtifactTransition,
    CellRange,
    EffectKind,
    EvidenceScope,
)
from .workbook_diff import WorkbookEffectDiff

_PROVENANCE_SCHEMA = "target-grounding-provenance-v1"
_COMMITTED_AUTHORIZATION_SCHEMA = "target-grounding-committed-authorization-v1"
_ADVISORY_ASSESSMENT_SCHEMA = "target-grounding-advisory-assessment-v1"
_COMMITTED_ADVISORY_ASSESSMENT_SCHEMA = (
    "target-grounding-committed-advisory-assessment-v1"
)
_PREPARED_ADVISORY_ASSESSMENT_SCHEMA = (
    "target-grounding-prepared-advisory-assessment-v1"
)
_ADVISORY_LIFECYCLE_EVENT_SCHEMA = "target-grounding-advisory-lifecycle-event-v1"
_ADVISORY_LIFECYCLE_GENESIS_SCHEMA = (
    "target-grounding-advisory-lifecycle-genesis-v1"
)
_ADVISORY_LIFECYCLE_DOMAIN = "spreadsheet-harness.target-grounding.advisory-lifecycle"
_ADVISORY_LIFECYCLE_EVENT_TYPES = frozenset(
    {"observation", "declaration", "preparation", "abort", "commitment", "transition"}
)
_MODEL_DIAGNOSTIC_SCHEMA = "target-grounding-model-diagnostic-v1"
_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
_AUTHORIZATION_CHAIN_GENESIS_SHA256 = "0" * 64
_ADVISORY_ASSESSMENT_CHAIN_GENESIS_SHA256 = "0" * 64
_PROTECTED_TRANSITION_KINDS = frozenset({"mutation", "undo", "external_mutation"})


def is_target_grounding_protected_transition_kind(kind: str) -> bool:
    """Return whether a transition must carry a target assessment/authorization."""

    return kind in _PROTECTED_TRANSITION_KINDS


class TargetGroundingError(RuntimeError):
    """Base error for invalid target-grounding state or input."""


class TargetGroundingRejected(TargetGroundingError):
    """A staged effect footprint was denied by the grounding gate."""

    def __init__(
        self,
        record: TargetGroundingProvenance | None,
        *,
        decision: GroundingDecision | None = None,
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        resolved_decision = record.decision if record is not None else decision
        if not isinstance(resolved_decision, GroundingDecision):
            raise TypeError("a grounding rejection requires a decision")
        self.record = record
        self.decision = resolved_decision
        self.model_diagnostic = dict(diagnostic or {})
        super().__init__(f"staged mutation rejected: {resolved_decision.value}")


class TargetGroundingMode(str, Enum):
    """Runtime treatment applied to target-grounding assessments."""

    OFF = "off"
    ADVISORY = "advisory"
    ENFORCE = "enforce"


class GroundingDecision(str, Enum):
    AUTHORIZED = "authorized"
    AUTHORIZED_NO_OP = "authorized_no_op"
    INCOMPLETE_DIFF = "rejected.incomplete_diff"
    UNKNOWN_EFFECT = "rejected.unknown_effect"
    INVALID_FOOTPRINT = "rejected.invalid_footprint"
    OUTSIDE_DECLARED_TARGET = "rejected.outside_declared_target"
    MISSING_DECLARATION = "rejected.missing_declaration"
    INVALID_DECLARATION = "rejected.invalid_declaration"
    UNKNOWN_DECLARATION = "rejected.unknown_declaration"
    STALE_DECLARATION = "rejected.stale_declaration"
    REPLAYED_DECLARATION = "rejected.replayed_declaration"

    @property
    def accepted(self) -> bool:
        return self in {self.AUTHORIZED, self.AUTHORIZED_NO_OP}


@dataclass(frozen=True)
class TrustedTargetObservation:
    """A harness-recorded observation of exact workbook bytes and finite scope."""

    observation_id: int
    artifact: ArtifactRef
    scope: EvidenceScope

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "artifact": self.artifact.to_dict(),
            "scope": self.scope.to_dict(),
        }


@dataclass(frozen=True)
class TargetDeclaration:
    """A one-use, model-provided edit target grounded in prior observations."""

    declaration_id: int
    artifact: ArtifactRef
    target_scope: EvidenceScope
    observation_ids: tuple[int, ...]
    observation_horizon: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "declaration_id": self.declaration_id,
            "artifact": self.artifact.to_dict(),
            "target_scope": self.target_scope.to_dict(),
            "observation_ids": list(self.observation_ids),
            "observation_horizon": self.observation_horizon,
        }


@dataclass(frozen=True)
class TargetGroundingProvenance:
    """Canonical authorization record for one staged semantic footprint."""

    validation_id: int
    declaration: TargetDeclaration
    observations: tuple[TrustedTargetObservation, ...]
    footprint: WorkbookEffectDiff
    decision: GroundingDecision

    @property
    def accepted(self) -> bool:
        return self.decision.accepted

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _PROVENANCE_SCHEMA,
            "validation_id": self.validation_id,
            "declaration": self.declaration.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "staged_footprint": _footprint_dict(self.footprint),
            "decision": self.decision.value,
            "assurance": {
                "digest_purpose": "integrity-checksum-only",
                "is_digital_signature": False,
                "proves_declared_target_correct": False,
                "proves_user_intent_satisfied": False,
            },
            "digest_algorithm": _DIGEST_ALGORITHM,
        }

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "provenance_sha256": self.canonical_sha256}

    def canonical_json(self) -> str:
        return _canonical_json_bytes(self.to_dict()).decode("ascii")

    def model_diagnostic(self) -> dict[str, Any]:
        """Project an assessed valid declaration without treatment-arm metadata."""

        return _model_diagnostic(
            requested_declaration_id=self.declaration.declaration_id,
            declaration_status="valid",
            footprint=self.footprint,
            decision=self.decision,
        )


@dataclass(frozen=True)
class PreparedTargetAuthorization:
    """An accepted footprint bound to staged bytes but not yet published."""

    preparation_id: int
    record: TargetGroundingProvenance
    staged_artifact: ArtifactRef


@dataclass(frozen=True)
class CommittedTargetAuthorization:
    """One durable accepted authorization bound to its publication result."""

    authorization_id: int
    preparation_id: int
    previous_authorization_sha256: str
    provenance: TargetGroundingProvenance
    staged_artifact: ArtifactRef
    transition: ArtifactTransition | None

    @property
    def publication_kind(self) -> str:
        return "artifact_transition" if self.transition is not None else "strict_no_op"

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _COMMITTED_AUTHORIZATION_SCHEMA,
            "authorization_id": self.authorization_id,
            "preparation_id": self.preparation_id,
            "previous_authorization_sha256": self.previous_authorization_sha256,
            "provenance": self.provenance.to_dict(),
            "staged_artifact": self.staged_artifact.to_dict(),
            "publication": {
                "kind": self.publication_kind,
                "transition": (self.transition.to_dict() if self.transition is not None else None),
            },
            "digest_algorithm": _DIGEST_ALGORITHM,
        }

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.payload_dict(),
            "authorization_sha256": self.canonical_sha256,
        }

    def canonical_json(self) -> str:
        return _canonical_json_bytes(self.to_dict()).decode("ascii")


@dataclass(frozen=True)
class AdvisoryTargetAssessment:
    """Observer-only counterfactual enforcement result for one staged edit."""

    validation_id: int
    requested_declaration_id: int | None
    declaration_status: str
    declaration: TargetDeclaration | None
    observations: tuple[TrustedTargetObservation, ...]
    footprint: WorkbookEffectDiff
    decision: GroundingDecision
    provenance: TargetGroundingProvenance | None

    @property
    def would_reject(self) -> bool:
        return not self.decision.accepted

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _ADVISORY_ASSESSMENT_SCHEMA,
            "mode": TargetGroundingMode.ADVISORY.value,
            "validation_id": self.validation_id,
            "requested_declaration_id": self.requested_declaration_id,
            "declaration_status": self.declaration_status,
            "declaration": (
                self.declaration.to_dict() if self.declaration is not None else None
            ),
            "observations": [item.to_dict() for item in self.observations],
            "staged_footprint": _footprint_dict(self.footprint),
            "counterfactual_enforcement_decision": self.decision.value,
            "would_reject": self.would_reject,
            "provenance": (
                self.provenance.to_dict() if self.provenance is not None else None
            ),
            "assurance": {
                "observer_assessment_only": True,
                "authorized_publication": False,
                "digest_purpose": "integrity-checksum-only",
                "is_digital_signature": False,
            },
            "digest_algorithm": _DIGEST_ALGORITHM,
        }

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "assessment_sha256": self.canonical_sha256}

    def canonical_json(self) -> str:
        return _canonical_json_bytes(self.to_dict()).decode("ascii")

    def model_diagnostic(self) -> dict[str, Any]:
        """Return the total, mode-neutral decision exposed to the model."""

        return _model_diagnostic(
            requested_declaration_id=self.requested_declaration_id,
            declaration_status=self.declaration_status,
            footprint=self.footprint,
            decision=self.decision,
        )


@dataclass(frozen=True)
class PreparedAdvisoryTargetAssessment:
    """An observer assessment bound to staged bytes, pending publication."""

    preparation_id: int
    source_artifact: ArtifactRef
    staged_artifact: ArtifactRef
    assessment: AdvisoryTargetAssessment
    reserved_declaration: bool
    declaration_request_kind: str
    declaration_request_id: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _PREPARED_ADVISORY_ASSESSMENT_SCHEMA,
            "mode": TargetGroundingMode.ADVISORY.value,
            "preparation_id": self.preparation_id,
            "source_artifact": self.source_artifact.to_dict(),
            "staged_artifact": self.staged_artifact.to_dict(),
            "assessment": self.assessment.to_dict(),
            "reserved_declaration": self.reserved_declaration,
            "declaration_request": {
                "kind": self.declaration_request_kind,
                "declaration_id": self.declaration_request_id,
            },
        }


@dataclass(frozen=True)
class CommittedAdvisoryTargetAssessment:
    """One chained observer assessment bound to its published artifact result."""

    commitment_id: int
    preparation_id: int
    previous_commitment_sha256: str
    assessment: AdvisoryTargetAssessment
    source_artifact: ArtifactRef
    staged_artifact: ArtifactRef
    transition: ArtifactTransition | None

    @property
    def publication_kind(self) -> str:
        return "artifact_transition" if self.transition is not None else "strict_no_op"

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _COMMITTED_ADVISORY_ASSESSMENT_SCHEMA,
            "mode": TargetGroundingMode.ADVISORY.value,
            "decision": "published_after_advisory_assessment",
            "commitment_id": self.commitment_id,
            "preparation_id": self.preparation_id,
            "previous_commitment_sha256": self.previous_commitment_sha256,
            "assessment": self.assessment.to_dict(),
            "source_artifact": self.source_artifact.to_dict(),
            "staged_artifact": self.staged_artifact.to_dict(),
            "publication": {
                "kind": self.publication_kind,
                "transition": (
                    self.transition.to_dict() if self.transition is not None else None
                ),
            },
            "digest_algorithm": _DIGEST_ALGORITHM,
        }

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "commitment_sha256": self.canonical_sha256}

    def canonical_json(self) -> str:
        return _canonical_json_bytes(self.to_dict()).decode("ascii")


@dataclass(frozen=True)
class AdvisoryLifecycleEvent:
    """One hash-chained state-machine event used for complete fresh replay."""

    event_id: int
    previous_event_sha256: str
    event_type: str
    payload_json: str

    @classmethod
    def create(
        cls,
        *,
        event_id: int,
        previous_event_sha256: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> AdvisoryLifecycleEvent:
        return cls(
            event_id=event_id,
            previous_event_sha256=previous_event_sha256,
            event_type=event_type,
            payload_json=_canonical_json_bytes(payload).decode("ascii"),
        )

    @property
    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise TargetGroundingError("advisory lifecycle payload must be an object")
        return value

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _ADVISORY_LIFECYCLE_EVENT_SCHEMA,
            "domain": _ADVISORY_LIFECYCLE_DOMAIN,
            "mode": TargetGroundingMode.ADVISORY.value,
            "event_id": self.event_id,
            "previous_event_sha256": self.previous_event_sha256,
            "event_type": self.event_type,
            "payload": self.payload,
            "digest_algorithm": _DIGEST_ALGORITHM,
        }

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.payload_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "event_sha256": self.canonical_sha256}


def _canonical_json_bytes(value: Any) -> bytes:
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return rendered.encode("ascii")


def _advisory_lifecycle_genesis_sha256(
    initial_artifact: ArtifactRef,
    initial_transition_count: int,
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": _ADVISORY_LIFECYCLE_GENESIS_SCHEMA,
                "domain": _ADVISORY_LIFECYCLE_DOMAIN,
                "mode": TargetGroundingMode.ADVISORY.value,
                "initial_artifact": initial_artifact.to_dict(),
                "initial_transition_count": initial_transition_count,
                "digest_algorithm": _DIGEST_ALGORITHM,
            }
        )
    ).hexdigest()


def _canonical_document_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _footprint_dict(diff: WorkbookEffectDiff) -> dict[str, Any]:
    # Free-form diagnostic reasons are intentionally excluded: they may contain
    # machine-local paths and are not part of the authorization semantics.
    return {
        "semantic_changed": diff.semantic_changed,
        "complete": diff.complete,
        "effects": sorted(item.value for item in diff.effects),
        "scope": diff.scope.to_dict(),
        "formula_scope": diff.formula_scope.to_dict(),
        "changed_cell_count": diff.changed_cell_count,
        "scanned_cell_count": diff.scanned_cell_count,
    }


def _model_diagnostic(
    *,
    requested_declaration_id: int | None,
    declaration_status: str,
    footprint: WorkbookEffectDiff,
    decision: GroundingDecision,
) -> dict[str, Any]:
    """Project one assessment without revealing whether publication is enforced."""

    return {
        "schema_version": _MODEL_DIAGNOSTIC_SCHEMA,
        "requested_declaration_id": requested_declaration_id,
        "declaration_status": declaration_status,
        "staged_footprint": _footprint_dict(footprint),
        "decision": decision.value,
        "would_reject": not decision.accepted,
    }


def _declaration_request_identity(value: Any) -> tuple[str, int | None]:
    if value is None:
        return "missing", None
    if type(value) is int and value >= 1:
        return "id", value
    return "invalid", None


def _require_artifact(value: ArtifactRef, *, label: str) -> None:
    if not isinstance(value, ArtifactRef):
        raise TypeError(f"{label} must be an ArtifactRef")


def _require_finite_scope(scope: EvidenceScope, *, label: str) -> None:
    if not isinstance(scope, EvidenceScope):
        raise TypeError(f"{label} must be an EvidenceScope")
    if scope.wildcard:
        raise TargetGroundingError(f"{label} cannot use a workbook wildcard")
    if scope.empty:
        raise TargetGroundingError(f"{label} must not be empty")


def _require_bounded_target_scope(scope: EvidenceScope) -> None:
    _require_finite_scope(scope, label="declared target scope")
    if scope.sheets or not scope.ranges:
        raise TargetGroundingError("declared target scope must contain only bounded cell ranges")


def _range_union_covers(target: CellRange, available: tuple[CellRange, ...]) -> bool:
    """Return whether a rectangle union covers every cell in ``target``."""

    clipped: list[CellRange] = []
    row_edges = {target.min_row, target.max_row + 1}
    for item in available:
        if item.sheet != target.sheet:
            continue
        min_row = max(target.min_row, item.min_row)
        max_row = min(target.max_row, item.max_row)
        min_col = max(target.min_col, item.min_col)
        max_col = min(target.max_col, item.max_col)
        if min_row > max_row or min_col > max_col:
            continue
        clipped_item = CellRange(
            target.sheet,
            min_col,
            min_row,
            max_col,
            max_row,
        )
        clipped.append(clipped_item)
        row_edges.update({min_row, max_row + 1})

    ordered_edges = sorted(row_edges)
    for strip_start, strip_end in zip(ordered_edges, ordered_edges[1:], strict=False):
        if strip_start >= strip_end:
            continue
        intervals = sorted(
            (item.min_col, item.max_col)
            for item in clipped
            if item.min_row <= strip_start and item.max_row >= strip_end - 1
        )
        covered_through = target.min_col - 1
        for min_col, max_col in intervals:
            if min_col > covered_through + 1:
                break
            covered_through = max(covered_through, max_col)
            if covered_through >= target.max_col:
                break
        if covered_through < target.max_col:
            return False
    return True


def _scope_union_covers(scopes: tuple[EvidenceScope, ...], target: EvidenceScope) -> bool:
    """Check target containment against the true union of multiple scopes."""

    if target.empty:
        return True
    if any(scope.wildcard for scope in scopes):
        return True
    if target.wildcard:
        return False

    observed_sheets = {sheet for scope in scopes for sheet in scope.sheets}
    if any(sheet not in observed_sheets for sheet in target.sheets):
        return False
    ranges = tuple(item for scope in scopes for item in scope.ranges)
    return all(
        target_range.sheet in observed_sheets or _range_union_covers(target_range, ranges)
        for target_range in target.ranges
    )


def _validate_diff_shape(diff: WorkbookEffectDiff) -> bool:
    if not isinstance(diff.semantic_changed, bool) or not isinstance(diff.complete, bool):
        return False
    if not isinstance(diff.effects, frozenset) or not all(
        isinstance(item, EffectKind) for item in diff.effects
    ):
        return False
    if not isinstance(diff.scope, EvidenceScope) or not isinstance(
        diff.formula_scope, EvidenceScope
    ):
        return False
    if any(
        type(value) is not int or value < 0
        for value in (diff.changed_cell_count, diff.scanned_cell_count)
    ):
        return False
    return True


def _classify_footprint(
    diff: WorkbookEffectDiff,
    *,
    declared_target: EvidenceScope,
) -> GroundingDecision:
    if not _validate_diff_shape(diff):
        return GroundingDecision.INVALID_FOOTPRINT
    if diff.changed_cell_count > diff.scanned_cell_count:
        return GroundingDecision.INVALID_FOOTPRINT
    if EffectKind.UNKNOWN in diff.effects:
        return GroundingDecision.UNKNOWN_EFFECT
    if not diff.complete:
        return GroundingDecision.INCOMPLETE_DIFF

    if not diff.semantic_changed:
        if (
            diff.effects
            or not diff.scope.empty
            or not diff.formula_scope.empty
            or diff.changed_cell_count != 0
        ):
            return GroundingDecision.INVALID_FOOTPRINT
        return GroundingDecision.AUTHORIZED_NO_OP

    if not diff.effects or diff.scope.empty:
        return GroundingDecision.INVALID_FOOTPRINT
    cell_effects = {EffectKind.VALUE, EffectKind.FORMULA, EffectKind.STYLE}
    if diff.effects & cell_effects and diff.changed_cell_count == 0:
        return GroundingDecision.INVALID_FOOTPRINT
    if EffectKind.FORMULA in diff.effects:
        if diff.formula_scope.empty:
            return GroundingDecision.INVALID_FOOTPRINT
        if not _scope_union_covers((diff.scope,), diff.formula_scope):
            return GroundingDecision.INVALID_FOOTPRINT
    elif not diff.formula_scope.empty:
        return GroundingDecision.INVALID_FOOTPRINT
    if not _scope_union_covers((declared_target,), diff.scope):
        return GroundingDecision.OUTSIDE_DECLARED_TARGET
    return GroundingDecision.AUTHORIZED


def _validate_staged_artifact_binding(
    diff: WorkbookEffectDiff,
    *,
    source_artifact: ArtifactRef,
    staged_artifact: ArtifactRef,
) -> None:
    """Validate the byte/revision identity shared by every assessment."""

    _require_artifact(source_artifact, label="source artifact")
    _require_artifact(staged_artifact, label="staged artifact")
    if not isinstance(diff, WorkbookEffectDiff):
        raise TypeError("diff must be a WorkbookEffectDiff")
    if not _validate_diff_shape(diff):
        raise TargetGroundingError("staged footprint schema is invalid")
    expected_revision = source_artifact.revision + int(
        staged_artifact.sha256 != source_artifact.sha256
    )
    if staged_artifact.revision != expected_revision:
        raise TargetGroundingError(
            "staged artifact revision does not match its byte transition"
        )


def _validate_unclassified_footprint_consistency(
    diff: WorkbookEffectDiff,
    *,
    source_artifact: ArtifactRef,
    staged_artifact: ArtifactRef,
) -> None:
    """Require coherence when declaration status, not footprint, sets the decision."""

    intrinsic_decision = _classify_footprint(
        diff,
        declared_target=diff.scope,
    )
    if intrinsic_decision is GroundingDecision.INVALID_FOOTPRINT:
        raise TargetGroundingError("staged footprint is internally inconsistent")
    if diff.semantic_changed and staged_artifact.sha256 == source_artifact.sha256:
        raise TargetGroundingError(
            "semantic change cannot preserve exact artifact bytes"
        )


class TargetGroundingStateMachine:
    """Revision-aware target assessment and optional publication gate."""

    def __init__(
        self,
        initial_artifact: ArtifactRef,
        *,
        mode: TargetGroundingMode = TargetGroundingMode.ENFORCE,
        initial_transition_count: int = 0,
    ) -> None:
        _require_artifact(initial_artifact, label="initial_artifact")
        if not isinstance(mode, TargetGroundingMode):
            raise TypeError("mode must be a TargetGroundingMode")
        if mode is TargetGroundingMode.OFF:
            raise ValueError("an off target-grounding mode does not need a state machine")
        if type(initial_transition_count) is not int or initial_transition_count < 0:
            raise ValueError("initial_transition_count must be a non-negative integer")
        self._mode = mode
        self._artifact = initial_artifact
        self._initial_artifact = initial_artifact
        self._initial_transition_count = initial_transition_count
        self._observations: dict[int, TrustedTargetObservation] = {}
        self._declarations: dict[int, TargetDeclaration] = {}
        self._declaration_status: dict[int, str] = {}
        self._records: list[TargetGroundingProvenance] = []
        self._committed_authorizations: list[CommittedTargetAuthorization] = []
        self._prepared: dict[int, PreparedTargetAuthorization] = {}
        self._advisory_assessments: list[AdvisoryTargetAssessment] = []
        self._committed_advisory_assessments: list[
            CommittedAdvisoryTargetAssessment
        ] = []
        self._advisory_prepared: dict[int, PreparedAdvisoryTargetAssessment] = {}
        self._advisory_lifecycle_events: list[AdvisoryLifecycleEvent] = []
        self._next_observation_id = 1
        self._next_declaration_id = 1
        self._next_validation_id = 1
        self._next_preparation_id = 1
        self._next_transition_id = initial_transition_count + 1

    @property
    def current_artifact(self) -> ArtifactRef:
        return self._artifact

    @property
    def mode(self) -> TargetGroundingMode:
        return self._mode

    @property
    def initial_transition_count(self) -> int:
        return self._initial_transition_count

    @property
    def records(self) -> tuple[TargetGroundingProvenance, ...]:
        return tuple(self._records)

    @property
    def committed_authorizations(self) -> tuple[CommittedTargetAuthorization, ...]:
        return tuple(self._committed_authorizations)

    @property
    def advisory_assessments(self) -> tuple[AdvisoryTargetAssessment, ...]:
        return tuple(self._advisory_assessments)

    @property
    def committed_advisory_assessments(
        self,
    ) -> tuple[CommittedAdvisoryTargetAssessment, ...]:
        return tuple(self._committed_advisory_assessments)

    @property
    def advisory_lifecycle_events(self) -> tuple[AdvisoryLifecycleEvent, ...]:
        return tuple(self._advisory_lifecycle_events)

    @property
    def advisory_lifecycle_genesis_sha256(self) -> str:
        return _advisory_lifecycle_genesis_sha256(
            self._initial_artifact,
            self._initial_transition_count,
        )

    @property
    def advisory_lifecycle_final_counters(self) -> dict[str, int]:
        return {
            "observation_count": self._next_observation_id - 1,
            "declaration_count": self._next_declaration_id - 1,
            "validation_count": self._next_validation_id - 1,
            "preparation_count": self._next_preparation_id - 1,
            "commitment_count": len(self._committed_advisory_assessments),
            "event_count": len(self._advisory_lifecycle_events),
            "transition_count": (
                self._next_transition_id - self._initial_transition_count - 1
            ),
            "pending_preparation_count": len(self._advisory_prepared),
        }

    def _append_advisory_lifecycle_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self._mode is not TargetGroundingMode.ADVISORY:
            return
        if event_type not in _ADVISORY_LIFECYCLE_EVENT_TYPES:
            raise TargetGroundingError("advisory lifecycle event type is invalid")
        previous_sha256 = (
            self._advisory_lifecycle_events[-1].canonical_sha256
            if self._advisory_lifecycle_events
            else self.advisory_lifecycle_genesis_sha256
        )
        self._advisory_lifecycle_events.append(
            AdvisoryLifecycleEvent.create(
                event_id=len(self._advisory_lifecycle_events) + 1,
                previous_event_sha256=previous_sha256,
                event_type=event_type,
                payload=payload,
            )
        )

    def record_trusted_observation(
        self,
        *,
        artifact: ArtifactRef,
        scope: EvidenceScope,
    ) -> TrustedTargetObservation:
        """Record a harness-trusted pre-edit observation on the current bytes."""

        _require_artifact(artifact, label="observation artifact")
        _require_finite_scope(scope, label="observation scope")
        if artifact != self._artifact:
            raise TargetGroundingError(
                "observation artifact is stale or does not match current workbook bytes"
            )
        observation = TrustedTargetObservation(
            observation_id=self._next_observation_id,
            artifact=artifact,
            scope=scope,
        )
        self._observations[observation.observation_id] = observation
        self._next_observation_id += 1
        self._append_advisory_lifecycle_event(
            "observation",
            {"observation": observation.to_dict()},
        )
        return observation

    def declare_target(
        self,
        *,
        artifact: ArtifactRef,
        target_scope: EvidenceScope,
        observation_ids: tuple[int, ...] | list[int],
    ) -> TargetDeclaration:
        """Declare a finite edit target grounded in earlier trusted observations."""

        _require_artifact(artifact, label="declaration artifact")
        _require_bounded_target_scope(target_scope)
        if artifact != self._artifact:
            raise TargetGroundingError(
                "declaration artifact is stale or does not match current workbook bytes"
            )
        if not isinstance(observation_ids, tuple | list) or not observation_ids:
            raise TargetGroundingError("declaration must cite observation IDs")
        if any(type(item) is not int or item < 1 for item in observation_ids):
            raise TargetGroundingError("observation IDs must be positive integers")
        if len(set(observation_ids)) != len(observation_ids):
            raise TargetGroundingError("declaration contains duplicate observation IDs")

        normalized_ids = tuple(sorted(observation_ids))
        cited: list[TrustedTargetObservation] = []
        for observation_id in normalized_ids:
            observation = self._observations.get(observation_id)
            if observation is None:
                raise TargetGroundingError(
                    f"declaration cites unknown observation ID {observation_id}"
                )
            if observation.artifact != artifact:
                raise TargetGroundingError(
                    f"observation ID {observation_id} belongs to a stale artifact"
                )
            cited.append(observation)

        if not _scope_union_covers(tuple(item.scope for item in cited), target_scope):
            raise TargetGroundingError(
                "cited observations do not cumulatively cover the declared target"
            )

        declaration = TargetDeclaration(
            declaration_id=self._next_declaration_id,
            artifact=artifact,
            target_scope=target_scope,
            observation_ids=normalized_ids,
            observation_horizon=self._next_observation_id - 1,
        )
        self._declarations[declaration.declaration_id] = declaration
        self._declaration_status[declaration.declaration_id] = "active"
        self._next_declaration_id += 1
        self._append_advisory_lifecycle_event(
            "declaration",
            {"declaration": declaration.to_dict()},
        )
        return declaration

    def authorize_staged_diff(
        self,
        declaration_id: int,
        diff: WorkbookEffectDiff,
    ) -> TargetGroundingProvenance:
        """Consume a declaration and authorize only an in-scope complete footprint."""

        if self._mode is not TargetGroundingMode.ENFORCE:
            raise TargetGroundingError(
                "advisory target grounding cannot issue enforced authorizations"
            )
        if type(declaration_id) is not int or declaration_id < 1:
            raise TargetGroundingError("declaration_id must be a positive integer")
        declaration = self._declarations.get(declaration_id)
        if declaration is None:
            raise TargetGroundingError(f"unknown declaration ID {declaration_id}")
        status = self._declaration_status[declaration_id]
        if status != "active":
            raise TargetGroundingError(
                f"declaration ID {declaration_id} is {status} and cannot be replayed"
            )
        if declaration.artifact != self._artifact:
            self._declaration_status[declaration_id] = "invalidated"
            raise TargetGroundingError("declaration artifact is no longer current")
        if not isinstance(diff, WorkbookEffectDiff):
            self._declaration_status[declaration_id] = "consumed"
            raise TypeError("diff must be a WorkbookEffectDiff")

        # Every authorization attempt consumes its declaration, including denials.
        self._declaration_status[declaration_id] = "consumed"
        decision = _classify_footprint(diff, declared_target=declaration.target_scope)
        observations = tuple(
            self._observations[observation_id] for observation_id in declaration.observation_ids
        )
        record = TargetGroundingProvenance(
            validation_id=self._next_validation_id,
            declaration=declaration,
            observations=observations,
            footprint=diff,
            decision=decision,
        )
        self._next_validation_id += 1
        self._records.append(record)
        if not record.accepted:
            raise TargetGroundingRejected(record)
        return record

    def _assess_staged_diff(
        self,
        declaration_id: int | None,
        diff: WorkbookEffectDiff,
        *,
        staged_artifact: ArtifactRef,
    ) -> tuple[AdvisoryTargetAssessment, bool]:
        """Build one total counterfactual decision without applying mode policy."""

        _validate_staged_artifact_binding(
            diff,
            source_artifact=self._artifact,
            staged_artifact=staged_artifact,
        )

        normalized_id = (
            declaration_id
            if type(declaration_id) is int and declaration_id >= 1
            else None
        )
        declaration: TargetDeclaration | None = None
        observations: tuple[TrustedTargetObservation, ...] = ()
        reserved_declaration = False
        invalidate_declaration = False
        if declaration_id is None:
            declaration_status = "missing"
            decision = GroundingDecision.MISSING_DECLARATION
        elif normalized_id is None:
            declaration_status = "invalid"
            decision = GroundingDecision.INVALID_DECLARATION
        else:
            declaration = self._declarations.get(normalized_id)
            if declaration is None:
                declaration_status = "unknown"
                decision = GroundingDecision.UNKNOWN_DECLARATION
            else:
                observations = tuple(
                    self._observations[observation_id]
                    for observation_id in declaration.observation_ids
                )
                status = self._declaration_status[normalized_id]
                if declaration.artifact != self._artifact or status == "invalidated":
                    declaration_status = "stale"
                    decision = GroundingDecision.STALE_DECLARATION
                    invalidate_declaration = True
                elif status != "active":
                    declaration_status = status
                    decision = GroundingDecision.REPLAYED_DECLARATION
                else:
                    declaration_status = "valid"
                    decision = _classify_footprint(
                        diff,
                        declared_target=declaration.target_scope,
                    )
                    if (
                        diff.semantic_changed
                        and staged_artifact.sha256 == self._artifact.sha256
                    ):
                        decision = GroundingDecision.INVALID_FOOTPRINT
                    reserved_declaration = True

        if declaration_status != "valid":
            _validate_unclassified_footprint_consistency(
                diff,
                source_artifact=self._artifact,
                staged_artifact=staged_artifact,
            )
        if invalidate_declaration:
            assert normalized_id is not None
            self._declaration_status[normalized_id] = "invalidated"

        provenance = (
            TargetGroundingProvenance(
                validation_id=self._next_validation_id,
                declaration=declaration,
                observations=observations,
                footprint=diff,
                decision=decision,
            )
            if declaration is not None
            else None
        )
        assessment = AdvisoryTargetAssessment(
            validation_id=self._next_validation_id,
            requested_declaration_id=normalized_id,
            declaration_status=declaration_status,
            declaration=declaration,
            observations=observations,
            footprint=diff,
            decision=decision,
            provenance=provenance,
        )
        self._next_validation_id += 1
        return assessment, reserved_declaration

    def prepare_staged_diff(
        self,
        declaration_id: int | None,
        diff: WorkbookEffectDiff,
        *,
        staged_artifact: ArtifactRef,
    ) -> PreparedTargetAuthorization:
        """Reserve one accepted authorization before publishing staged bytes."""

        if self._mode is not TargetGroundingMode.ENFORCE:
            raise TargetGroundingError(
                "advisory target grounding cannot issue enforced authorizations"
            )
        # Preserve the legacy fail-closed consumption semantics for malformed
        # inputs after a valid current declaration has been selected.
        _require_artifact(staged_artifact, label="staged artifact")
        if type(declaration_id) is int and declaration_id >= 1:
            declaration = self._declarations.get(declaration_id)
            if (
                declaration is not None
                and self._declaration_status[declaration_id] == "active"
                and declaration.artifact == self._artifact
            ):
                if not isinstance(diff, WorkbookEffectDiff):
                    self._declaration_status[declaration_id] = "consumed"
                    raise TypeError("diff must be a WorkbookEffectDiff")
                expected_revision = self._artifact.revision + int(
                    staged_artifact.sha256 != self._artifact.sha256
                )
                if staged_artifact.revision != expected_revision:
                    self._declaration_status[declaration_id] = "consumed"
                    raise TargetGroundingError(
                        "staged artifact revision does not match its byte transition"
                    )
        assessment, reserved_declaration = self._assess_staged_diff(
            declaration_id,
            diff,
            staged_artifact=staged_artifact,
        )
        record = assessment.provenance
        if not assessment.decision.accepted:
            if reserved_declaration:
                assert assessment.declaration is not None
                self._declaration_status[assessment.declaration.declaration_id] = "consumed"
            if record is not None:
                self._records.append(record)
            raise TargetGroundingRejected(
                record,
                decision=assessment.decision,
                diagnostic=assessment.model_diagnostic(),
            )
        if not reserved_declaration or record is None:
            raise TargetGroundingError(
                "an accepted grounding assessment is missing a valid declaration"
            )

        prepared = PreparedTargetAuthorization(
            preparation_id=self._next_preparation_id,
            record=record,
            staged_artifact=staged_artifact,
        )
        self._next_preparation_id += 1
        self._prepared[prepared.preparation_id] = prepared
        self._declaration_status[record.declaration.declaration_id] = "prepared"
        return prepared

    def validate_prepared_transition(
        self,
        prepared: PreparedTargetAuthorization,
        transition: ArtifactTransition | None,
    ) -> None:
        """Validate the exact transition before the filesystem publish point."""

        if not isinstance(prepared, PreparedTargetAuthorization):
            raise TypeError("prepared must be a PreparedTargetAuthorization")
        registered = self._prepared.get(prepared.preparation_id)
        if registered != prepared:
            raise TargetGroundingError("unknown or stale prepared authorization")
        declaration_id = prepared.record.declaration.declaration_id
        if self._declaration_status.get(declaration_id) != "prepared":
            raise TargetGroundingError("prepared declaration is no longer pending")
        if prepared.record.declaration.artifact != self._artifact:
            raise TargetGroundingError("prepared declaration artifact is no longer current")

        bytes_changed = prepared.staged_artifact != self._artifact
        if not bytes_changed:
            if transition is not None:
                raise TargetGroundingError("unchanged staged bytes cannot publish a transition")
            return
        if transition is None:
            raise TargetGroundingError("changed staged bytes require an artifact transition")
        if transition.before != self._artifact or transition.after != prepared.staged_artifact:
            raise TargetGroundingError(
                "artifact transition does not match the prepared staged bytes"
            )
        self._validate_next_artifact_transition(transition)

    def commit_prepared(
        self,
        prepared: PreparedTargetAuthorization,
        transition: ArtifactTransition | None,
        *,
        committed_authorization: CommittedTargetAuthorization | None = None,
    ) -> TargetGroundingProvenance:
        """Commit a prevalidated authorization after the staged bytes are published."""

        expected_authorization = self.preview_committed_authorization(prepared, transition)
        if committed_authorization is None:
            committed_authorization = expected_authorization
        elif (
            not isinstance(committed_authorization, CommittedTargetAuthorization)
            or committed_authorization.to_dict() != expected_authorization.to_dict()
        ):
            raise TargetGroundingError(
                "committed authorization does not match the prepared publication"
            )
        else:
            committed_authorization = expected_authorization
        declaration_id = prepared.record.declaration.declaration_id
        self._declaration_status[declaration_id] = "consumed"
        self._invalidate_older_active_declarations(declaration_id)
        self._prepared.pop(prepared.preparation_id)
        self._records.append(prepared.record)
        self._committed_authorizations.append(committed_authorization)
        if transition is not None:
            self._advance_artifact_transition(transition)
        return prepared.record

    def preview_committed_authorization(
        self,
        prepared: PreparedTargetAuthorization,
        transition: ArtifactTransition | None,
    ) -> CommittedTargetAuthorization:
        """Build the exact canonical commit record without advancing any ledger."""

        self.validate_prepared_transition(prepared, transition)
        if self._committed_authorizations:
            previous = self._committed_authorizations[-1]
            if (
                prepared.preparation_id <= previous.preparation_id
                or prepared.record.validation_id <= previous.provenance.validation_id
                or prepared.record.declaration.declaration_id
                <= previous.provenance.declaration.declaration_id
            ):
                raise TargetGroundingError(
                    "prepared authorization would violate committed chronology"
                )
        if transition is None and (
            prepared.record.decision is not GroundingDecision.AUTHORIZED_NO_OP
            or prepared.record.footprint.semantic_changed
            or prepared.staged_artifact != prepared.record.declaration.artifact
        ):
            raise TargetGroundingError(
                "a publication without an artifact transition must be a strict semantic no-op"
            )
        if transition is not None and transition.kind not in _PROTECTED_TRANSITION_KINDS:
            raise TargetGroundingError(
                "an authorization can carry only a protected artifact transition"
            )
        previous_sha256 = (
            self._committed_authorizations[-1].canonical_sha256
            if self._committed_authorizations
            else _AUTHORIZATION_CHAIN_GENESIS_SHA256
        )
        return CommittedTargetAuthorization(
            authorization_id=len(self._committed_authorizations) + 1,
            preparation_id=prepared.preparation_id,
            previous_authorization_sha256=previous_sha256,
            provenance=prepared.record,
            staged_artifact=prepared.staged_artifact,
            transition=transition,
        )

    def abort_prepared(self, prepared: PreparedTargetAuthorization) -> None:
        """Consume a reservation when publication fails before it becomes visible."""

        if not isinstance(prepared, PreparedTargetAuthorization):
            raise TypeError("prepared must be a PreparedTargetAuthorization")
        registered = self._prepared.get(prepared.preparation_id)
        if registered != prepared:
            raise TargetGroundingError("unknown or stale prepared authorization")
        declaration_id = prepared.record.declaration.declaration_id
        self._declaration_status[declaration_id] = "consumed"
        self._prepared.pop(prepared.preparation_id)

    def prepare_advisory_staged_diff(
        self,
        declaration_id: int | None,
        diff: WorkbookEffectDiff,
        *,
        staged_artifact: ArtifactRef,
    ) -> PreparedAdvisoryTargetAssessment:
        """Assess and reserve staged bytes without authorizing or blocking publication."""

        if self._mode is not TargetGroundingMode.ADVISORY:
            raise TargetGroundingError(
                "enforced target grounding cannot create advisory assessments"
            )
        assessment, reserved_declaration = self._assess_staged_diff(
            declaration_id,
            diff,
            staged_artifact=staged_artifact,
        )
        request_kind, request_id = _declaration_request_identity(declaration_id)
        prepared = PreparedAdvisoryTargetAssessment(
            preparation_id=self._next_preparation_id,
            source_artifact=self._artifact,
            staged_artifact=staged_artifact,
            assessment=assessment,
            reserved_declaration=reserved_declaration,
            declaration_request_kind=request_kind,
            declaration_request_id=request_id,
        )
        self._next_preparation_id += 1
        self._advisory_prepared[prepared.preparation_id] = prepared
        if reserved_declaration:
            declaration = assessment.declaration
            assert declaration is not None
            self._declaration_status[declaration.declaration_id] = "prepared"
        self._append_advisory_lifecycle_event(
            "preparation",
            {"preparation": prepared.to_dict()},
        )
        return prepared

    def validate_prepared_advisory_transition(
        self,
        prepared: PreparedAdvisoryTargetAssessment,
        transition: ArtifactTransition | None,
    ) -> None:
        """Bind an advisory assessment to the exact staged publication transition."""

        if not isinstance(prepared, PreparedAdvisoryTargetAssessment):
            raise TypeError("prepared must be a PreparedAdvisoryTargetAssessment")
        registered = self._advisory_prepared.get(prepared.preparation_id)
        if registered != prepared:
            raise TargetGroundingError("unknown or stale prepared advisory assessment")
        if prepared.source_artifact != self._artifact:
            raise TargetGroundingError("prepared advisory assessment is no longer current")
        if prepared.reserved_declaration:
            declaration = prepared.assessment.declaration
            if (
                declaration is None
                or self._declaration_status.get(declaration.declaration_id) != "prepared"
            ):
                raise TargetGroundingError(
                    "prepared advisory declaration is no longer pending"
                )

        bytes_changed = prepared.staged_artifact.sha256 != prepared.source_artifact.sha256
        if not bytes_changed:
            if transition is not None:
                raise TargetGroundingError("unchanged staged bytes cannot publish a transition")
            return
        if transition is None:
            raise TargetGroundingError("changed staged bytes require an artifact transition")
        if (
            transition.before != prepared.source_artifact
            or transition.after != prepared.staged_artifact
        ):
            raise TargetGroundingError(
                "artifact transition does not match the advisory-assessed staged bytes"
            )
        self._validate_next_artifact_transition(transition)

    def preview_committed_advisory_assessment(
        self,
        prepared: PreparedAdvisoryTargetAssessment,
        transition: ArtifactTransition | None,
    ) -> CommittedAdvisoryTargetAssessment:
        """Build a canonical observer commitment without advancing either ledger."""

        self.validate_prepared_advisory_transition(prepared, transition)
        if self._committed_advisory_assessments:
            previous = self._committed_advisory_assessments[-1]
            if (
                prepared.preparation_id <= previous.preparation_id
                or prepared.assessment.validation_id
                <= previous.assessment.validation_id
            ):
                raise TargetGroundingError(
                    "prepared advisory assessment would violate committed chronology"
                )
            declaration = prepared.assessment.declaration
            if prepared.assessment.declaration_status == "valid":
                assert declaration is not None
                previous_valid_ids = [
                    item.assessment.declaration.declaration_id
                    for item in self._committed_advisory_assessments
                    if item.assessment.declaration_status == "valid"
                    and item.assessment.declaration is not None
                ]
                if previous_valid_ids and declaration.declaration_id <= previous_valid_ids[-1]:
                    raise TargetGroundingError(
                        "prepared advisory declaration would violate committed chronology"
                    )
        if transition is None and (
            prepared.assessment.footprint.semantic_changed
            or prepared.staged_artifact != prepared.source_artifact
        ):
            raise TargetGroundingError(
                "an advisory publication without a transition must be a strict semantic no-op"
            )
        if transition is not None and transition.kind not in _PROTECTED_TRANSITION_KINDS:
            raise TargetGroundingError(
                "an advisory commitment can carry only a protected artifact transition"
            )
        previous_sha256 = (
            self._committed_advisory_assessments[-1].canonical_sha256
            if self._committed_advisory_assessments
            else _ADVISORY_ASSESSMENT_CHAIN_GENESIS_SHA256
        )
        return CommittedAdvisoryTargetAssessment(
            commitment_id=len(self._committed_advisory_assessments) + 1,
            preparation_id=prepared.preparation_id,
            previous_commitment_sha256=previous_sha256,
            assessment=prepared.assessment,
            source_artifact=prepared.source_artifact,
            staged_artifact=prepared.staged_artifact,
            transition=transition,
        )

    def commit_advisory_assessment(
        self,
        prepared: PreparedAdvisoryTargetAssessment,
        transition: ArtifactTransition | None,
        *,
        committed_assessment: CommittedAdvisoryTargetAssessment | None = None,
    ) -> AdvisoryTargetAssessment:
        """Commit an observer-only assessment after publication becomes visible."""

        expected = self.preview_committed_advisory_assessment(prepared, transition)
        if committed_assessment is None:
            committed_assessment = expected
        elif (
            not isinstance(committed_assessment, CommittedAdvisoryTargetAssessment)
            or committed_assessment.to_dict() != expected.to_dict()
        ):
            raise TargetGroundingError(
                "committed advisory assessment does not match the prepared publication"
            )
        else:
            committed_assessment = expected
        if prepared.reserved_declaration:
            declaration = prepared.assessment.declaration
            assert declaration is not None
            self._declaration_status[declaration.declaration_id] = "consumed"
            self._invalidate_older_active_declarations(declaration.declaration_id)
        self._advisory_prepared.pop(prepared.preparation_id)
        self._advisory_assessments.append(prepared.assessment)
        self._committed_advisory_assessments.append(committed_assessment)
        if transition is not None:
            self._advance_artifact_transition(transition)
        self._append_advisory_lifecycle_event(
            "commitment",
            {
                "prepared_sha256": _canonical_document_sha256(prepared.to_dict()),
                "commitment": committed_assessment.to_dict(),
            },
        )
        return prepared.assessment

    def abort_prepared_advisory_assessment(
        self,
        prepared: PreparedAdvisoryTargetAssessment,
    ) -> None:
        """Consume a reserved advisory declaration after non-grounding publication failure."""

        if not isinstance(prepared, PreparedAdvisoryTargetAssessment):
            raise TypeError("prepared must be a PreparedAdvisoryTargetAssessment")
        registered = self._advisory_prepared.get(prepared.preparation_id)
        if registered != prepared:
            raise TargetGroundingError("unknown or stale prepared advisory assessment")
        if prepared.reserved_declaration:
            declaration = prepared.assessment.declaration
            assert declaration is not None
            self._declaration_status[declaration.declaration_id] = "consumed"
        self._advisory_prepared.pop(prepared.preparation_id)
        self._append_advisory_lifecycle_event(
            "abort",
            {
                "preparation_id": prepared.preparation_id,
                "prepared_sha256": _canonical_document_sha256(prepared.to_dict()),
            },
        )

    def record_artifact_transition(self, transition: ArtifactTransition) -> None:
        """Advance exact lineage and invalidate every unconsumed declaration."""

        if (
            isinstance(transition, ArtifactTransition)
            and transition.kind in _PROTECTED_TRANSITION_KINDS
        ):
            raise TargetGroundingError(
                "protected transition requires a committed target assessment"
            )
        self._advance_artifact_transition(transition)
        self._append_advisory_lifecycle_event(
            "transition",
            {"transition": transition.to_dict()},
        )

    def _advance_artifact_transition(self, transition: ArtifactTransition) -> None:
        """Advance state without classifying how the transition was published."""

        self._validate_next_artifact_transition(transition)
        self._artifact = transition.after
        self._next_transition_id += 1
        for declaration_id, status in tuple(self._declaration_status.items()):
            if status in {"active", "superseded"}:
                self._declaration_status[declaration_id] = "invalidated"

    def _validate_next_artifact_transition(
        self,
        transition: ArtifactTransition,
    ) -> None:
        """Check the next lineage edge without mutating any state."""

        if not isinstance(transition, ArtifactTransition):
            raise TypeError("transition must be an ArtifactTransition")
        if transition.before != self._artifact:
            raise TargetGroundingError(
                "artifact transition does not start from the current workbook bytes"
            )
        if transition.transition_id != self._next_transition_id:
            raise TargetGroundingError(
                "artifact transition ID does not continue the ledger sequence"
            )

    def _invalidate_older_active_declarations(self, declaration_id: int) -> None:
        """Keep committed declaration chronology strictly increasing at one revision."""

        for candidate_id, status in tuple(self._declaration_status.items()):
            if candidate_id < declaration_id and status == "active":
                self._declaration_status[candidate_id] = "superseded"


def _require_document_fields(
    value: Any,
    fields: set[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise TargetGroundingError(f"{label} fields are invalid")
    return value


def _artifact_from_document(value: Any, *, label: str) -> ArtifactRef:
    document = _require_document_fields(value, {"revision", "sha256"}, label=label)
    try:
        artifact = ArtifactRef(document["revision"], document["sha256"])
    except (TypeError, ValueError) as exc:
        raise TargetGroundingError(f"{label} is invalid") from exc
    if artifact.to_dict() != dict(document):
        raise TargetGroundingError(f"{label} is not canonical")
    return artifact


def _transition_from_document(value: Any, *, label: str) -> ArtifactTransition:
    document = _require_document_fields(
        value,
        {"transition_id", "operation", "kind", "before", "after"},
        label=label,
    )
    try:
        transition = ArtifactTransition(
            transition_id=document["transition_id"],
            operation=document["operation"],
            kind=document["kind"],
            before=_artifact_from_document(document["before"], label=f"{label}.before"),
            after=_artifact_from_document(document["after"], label=f"{label}.after"),
        )
    except (TypeError, ValueError) as exc:
        raise TargetGroundingError(f"{label} is invalid") from exc
    if transition.to_dict() != dict(document):
        raise TargetGroundingError(f"{label} is not canonical")
    return transition


def _validate_artifact_transition_sequence(
    transitions: Sequence[ArtifactTransition],
    *,
    label: str,
) -> None:
    for expected_id, transition in enumerate(transitions, start=1):
        if transition.transition_id != expected_id:
            raise TargetGroundingError(f"{label} transition IDs are not contiguous")
        if expected_id > 1 and transition.before != transitions[expected_id - 2].after:
            raise TargetGroundingError(f"{label} artifact lineage is disconnected")


def _scope_from_document(value: Any, *, label: str) -> EvidenceScope:
    try:
        scope = EvidenceScope.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise TargetGroundingError(f"{label} is invalid") from exc
    if scope.to_dict() != dict(value):
        raise TargetGroundingError(f"{label} is not canonical")
    return scope


def _footprint_from_document(value: Any) -> WorkbookEffectDiff:
    document = _require_document_fields(
        value,
        {
            "semantic_changed",
            "complete",
            "effects",
            "scope",
            "formula_scope",
            "changed_cell_count",
            "scanned_cell_count",
        },
        label="committed staged footprint",
    )
    raw_effects = document["effects"]
    if (
        not isinstance(raw_effects, list)
        or not all(isinstance(item, str) for item in raw_effects)
        or len(raw_effects) != len(set(raw_effects))
    ):
        raise TargetGroundingError("committed staged footprint effects are invalid")
    try:
        footprint = WorkbookEffectDiff(
            semantic_changed=document["semantic_changed"],
            complete=document["complete"],
            effects=frozenset(EffectKind(item) for item in raw_effects),
            scope=_scope_from_document(document["scope"], label="committed footprint scope"),
            formula_scope=_scope_from_document(
                document["formula_scope"], label="committed formula scope"
            ),
            changed_cell_count=document["changed_cell_count"],
            scanned_cell_count=document["scanned_cell_count"],
        )
    except (TypeError, ValueError) as exc:
        raise TargetGroundingError("committed staged footprint is invalid") from exc
    if not _validate_diff_shape(footprint) or _footprint_dict(footprint) != dict(document):
        raise TargetGroundingError("committed staged footprint is not canonical")
    return footprint


def _observation_from_document(value: Any) -> TrustedTargetObservation:
    document = _require_document_fields(
        value,
        {"observation_id", "artifact", "scope"},
        label="committed target observation",
    )
    observation_id = document["observation_id"]
    if type(observation_id) is not int or observation_id < 1:
        raise TargetGroundingError("committed observation ID is invalid")
    observation = TrustedTargetObservation(
        observation_id=observation_id,
        artifact=_artifact_from_document(
            document["artifact"], label="committed observation artifact"
        ),
        scope=_scope_from_document(document["scope"], label="committed observation scope"),
    )
    if observation.to_dict() != dict(document):
        raise TargetGroundingError("committed target observation is not canonical")
    return observation


def _declaration_from_document(value: Any) -> TargetDeclaration:
    document = _require_document_fields(
        value,
        {
            "declaration_id",
            "artifact",
            "target_scope",
            "observation_ids",
            "observation_horizon",
        },
        label="committed target declaration",
    )
    declaration_id = document["declaration_id"]
    observation_ids = document["observation_ids"]
    observation_horizon = document["observation_horizon"]
    if (
        type(declaration_id) is not int
        or declaration_id < 1
        or not isinstance(observation_ids, list)
        or not observation_ids
        or any(type(item) is not int or item < 1 for item in observation_ids)
        or len(observation_ids) != len(set(observation_ids))
        or type(observation_horizon) is not int
        or observation_horizon < max(observation_ids)
    ):
        raise TargetGroundingError("committed target declaration values are invalid")
    declaration = TargetDeclaration(
        declaration_id=declaration_id,
        artifact=_artifact_from_document(
            document["artifact"], label="committed declaration artifact"
        ),
        target_scope=_scope_from_document(document["target_scope"], label="committed target scope"),
        observation_ids=tuple(observation_ids),
        observation_horizon=observation_horizon,
    )
    _require_bounded_target_scope(declaration.target_scope)
    if declaration.to_dict() != dict(document):
        raise TargetGroundingError("committed target declaration is not canonical")
    return declaration


def _provenance_from_document(value: Any) -> TargetGroundingProvenance:
    document = _require_document_fields(
        value,
        {
            "schema_version",
            "validation_id",
            "declaration",
            "observations",
            "staged_footprint",
            "decision",
            "assurance",
            "digest_algorithm",
            "provenance_sha256",
        },
        label="committed target provenance",
    )
    validation_id = document["validation_id"]
    raw_observations = document["observations"]
    if (
        type(validation_id) is not int
        or validation_id < 1
        or not isinstance(raw_observations, list)
    ):
        raise TargetGroundingError("committed target provenance values are invalid")
    try:
        decision = GroundingDecision(document["decision"])
    except (TypeError, ValueError) as exc:
        raise TargetGroundingError("committed target decision is invalid") from exc
    provenance = TargetGroundingProvenance(
        validation_id=validation_id,
        declaration=_declaration_from_document(document["declaration"]),
        observations=tuple(_observation_from_document(item) for item in raw_observations),
        footprint=_footprint_from_document(document["staged_footprint"]),
        decision=decision,
    )
    if provenance.to_dict() != dict(document):
        raise TargetGroundingError("committed target provenance digest or fields are invalid")
    return provenance


def advisory_assessment_from_dict(value: Any) -> AdvisoryTargetAssessment:
    """Parse one canonical observer-only assessment without trusting its digest."""

    document = _require_document_fields(
        value,
        {
            "schema_version",
            "mode",
            "validation_id",
            "requested_declaration_id",
            "declaration_status",
            "declaration",
            "observations",
            "staged_footprint",
            "counterfactual_enforcement_decision",
            "would_reject",
            "provenance",
            "assurance",
            "digest_algorithm",
            "assessment_sha256",
        },
        label="advisory target assessment",
    )
    validation_id = document["validation_id"]
    requested_declaration_id = document["requested_declaration_id"]
    declaration_status = document["declaration_status"]
    raw_observations = document["observations"]
    if (
        type(validation_id) is not int
        or validation_id < 1
        or (
            requested_declaration_id is not None
            and (
                type(requested_declaration_id) is not int
                or requested_declaration_id < 1
            )
        )
        or declaration_status
        not in {
            "missing",
            "invalid",
            "unknown",
            "valid",
            "stale",
            "prepared",
            "consumed",
            "superseded",
        }
        or not isinstance(raw_observations, list)
    ):
        raise TargetGroundingError("advisory target assessment values are invalid")
    try:
        decision = GroundingDecision(document["counterfactual_enforcement_decision"])
    except (TypeError, ValueError) as exc:
        raise TargetGroundingError("advisory target assessment decision is invalid") from exc
    raw_declaration = document["declaration"]
    declaration = (
        None
        if raw_declaration is None
        else _declaration_from_document(raw_declaration)
    )
    raw_provenance = document["provenance"]
    provenance = (
        None
        if raw_provenance is None
        else _provenance_from_document(raw_provenance)
    )
    assessment = AdvisoryTargetAssessment(
        validation_id=validation_id,
        requested_declaration_id=requested_declaration_id,
        declaration_status=declaration_status,
        declaration=declaration,
        observations=tuple(
            _observation_from_document(item) for item in raw_observations
        ),
        footprint=_footprint_from_document(document["staged_footprint"]),
        decision=decision,
        provenance=provenance,
    )
    if assessment.to_dict() != dict(document):
        raise TargetGroundingError(
            "advisory target assessment digest or fields are invalid"
        )
    return assessment


def prepared_advisory_assessment_from_dict(
    value: Any,
) -> PreparedAdvisoryTargetAssessment:
    """Parse one canonical pending advisory assessment."""

    document = _require_document_fields(
        value,
        {
            "schema_version",
            "mode",
            "preparation_id",
            "source_artifact",
            "staged_artifact",
            "assessment",
            "reserved_declaration",
            "declaration_request",
        },
        label="prepared advisory target assessment",
    )
    preparation_id = document["preparation_id"]
    reserved_declaration = document["reserved_declaration"]
    request = _require_document_fields(
        document["declaration_request"],
        {"kind", "declaration_id"},
        label="prepared advisory declaration request",
    )
    request_kind = request["kind"]
    request_id = request["declaration_id"]
    if (
        type(preparation_id) is not int
        or preparation_id < 1
        or type(reserved_declaration) is not bool
        or not isinstance(request_kind, str)
        or request_kind not in {"missing", "invalid", "id"}
        or (
            request_kind == "id"
            and (type(request_id) is not int or request_id < 1)
        )
        or (request_kind != "id" and request_id is not None)
    ):
        raise TargetGroundingError("prepared advisory values are invalid")
    prepared = PreparedAdvisoryTargetAssessment(
        preparation_id=preparation_id,
        source_artifact=_artifact_from_document(
            document["source_artifact"],
            label="prepared advisory source artifact",
        ),
        staged_artifact=_artifact_from_document(
            document["staged_artifact"],
            label="prepared advisory staged artifact",
        ),
        assessment=advisory_assessment_from_dict(document["assessment"]),
        reserved_declaration=reserved_declaration,
        declaration_request_kind=request_kind,
        declaration_request_id=request_id,
    )
    if prepared.to_dict() != dict(document):
        raise TargetGroundingError("prepared advisory assessment is not canonical")
    return prepared


def advisory_lifecycle_event_from_dict(value: Any) -> AdvisoryLifecycleEvent:
    """Parse one canonical lifecycle event and reproduce its digest."""

    document = _require_document_fields(
        value,
        {
            "schema_version",
            "domain",
            "mode",
            "event_id",
            "previous_event_sha256",
            "event_type",
            "payload",
            "digest_algorithm",
            "event_sha256",
        },
        label="advisory lifecycle event",
    )
    event_id = document["event_id"]
    previous_sha256 = document["previous_event_sha256"]
    event_type = document["event_type"]
    payload = document["payload"]
    if (
        type(event_id) is not int
        or event_id < 1
        or not isinstance(previous_sha256, str)
        or len(previous_sha256) != 64
        or any(character not in "0123456789abcdef" for character in previous_sha256)
        or not isinstance(event_type, str)
        or event_type not in _ADVISORY_LIFECYCLE_EVENT_TYPES
        or not isinstance(payload, Mapping)
    ):
        raise TargetGroundingError("advisory lifecycle event values are invalid")
    event = AdvisoryLifecycleEvent.create(
        event_id=event_id,
        previous_event_sha256=previous_sha256,
        event_type=event_type,
        payload=payload,
    )
    if event.to_dict() != dict(document):
        raise TargetGroundingError(
            "advisory lifecycle event digest or fields are invalid"
        )
    return event


def committed_advisory_assessment_from_dict(
    value: Any,
) -> CommittedAdvisoryTargetAssessment:
    """Parse one canonical observer commitment and reproduce every nested digest."""

    document = _require_document_fields(
        value,
        {
            "schema_version",
            "mode",
            "decision",
            "commitment_id",
            "preparation_id",
            "previous_commitment_sha256",
            "assessment",
            "source_artifact",
            "staged_artifact",
            "publication",
            "digest_algorithm",
            "commitment_sha256",
        },
        label="committed advisory target assessment",
    )
    commitment_id = document["commitment_id"]
    preparation_id = document["preparation_id"]
    previous_sha256 = document["previous_commitment_sha256"]
    if (
        type(commitment_id) is not int
        or commitment_id < 1
        or type(preparation_id) is not int
        or preparation_id < 1
        or not isinstance(previous_sha256, str)
        or len(previous_sha256) != 64
        or any(character not in "0123456789abcdef" for character in previous_sha256)
    ):
        raise TargetGroundingError("committed advisory identity is invalid")
    publication = _require_document_fields(
        document["publication"],
        {"kind", "transition"},
        label="committed advisory publication",
    )
    publication_kind = publication["kind"]
    raw_transition = publication["transition"]
    if publication_kind == "artifact_transition":
        transition = _transition_from_document(
            raw_transition,
            label="committed advisory transition",
        )
    elif publication_kind == "strict_no_op" and raw_transition is None:
        transition = None
    else:
        raise TargetGroundingError("committed advisory publication is invalid")
    record = CommittedAdvisoryTargetAssessment(
        commitment_id=commitment_id,
        preparation_id=preparation_id,
        previous_commitment_sha256=previous_sha256,
        assessment=advisory_assessment_from_dict(document["assessment"]),
        source_artifact=_artifact_from_document(
            document["source_artifact"],
            label="committed advisory source artifact",
        ),
        staged_artifact=_artifact_from_document(
            document["staged_artifact"],
            label="committed advisory staged artifact",
        ),
        transition=transition,
    )
    if record.to_dict() != dict(document):
        raise TargetGroundingError(
            "committed advisory assessment digest or fields are invalid"
        )
    return record


def validate_advisory_lifecycle_chain(
    values: Any,
    *,
    genesis_sha256: str,
    final_counters: Mapping[str, Any],
    committed_assessments: Sequence[CommittedAdvisoryTargetAssessment],
    transitions: Sequence[ArtifactTransition],
    initial_artifact: ArtifactRef,
    initial_transition_count: int,
) -> tuple[AdvisoryLifecycleEvent, ...]:
    """Re-execute every advisory state transition from a domain-bound genesis."""

    if not isinstance(values, list):
        raise TargetGroundingError("advisory lifecycle events must be a list")
    expected_counter_fields = {
        "observation_count",
        "declaration_count",
        "validation_count",
        "preparation_count",
        "commitment_count",
        "event_count",
        "transition_count",
        "pending_preparation_count",
    }
    if (
        not isinstance(final_counters, Mapping)
        or set(final_counters) != expected_counter_fields
        or any(type(value) is not int or value < 0 for value in final_counters.values())
    ):
        raise TargetGroundingError("advisory lifecycle final counters are invalid")
    if not isinstance(committed_assessments, Sequence) or not all(
        isinstance(item, CommittedAdvisoryTargetAssessment)
        for item in committed_assessments
    ):
        raise TargetGroundingError("advisory lifecycle commitments are invalid")
    if not isinstance(transitions, Sequence) or not all(
        isinstance(item, ArtifactTransition) for item in transitions
    ):
        raise TargetGroundingError("advisory lifecycle transitions are invalid")
    _validate_artifact_transition_sequence(
        transitions,
        label="advisory lifecycle",
    )
    _require_artifact(initial_artifact, label="advisory lifecycle initial artifact")
    if (
        type(initial_transition_count) is not int
        or initial_transition_count < 0
        or initial_transition_count > len(transitions)
    ):
        raise TargetGroundingError(
            "advisory lifecycle initial transition count is invalid"
        )
    expected_genesis_sha256 = _advisory_lifecycle_genesis_sha256(
        initial_artifact,
        initial_transition_count,
    )
    if genesis_sha256 != expected_genesis_sha256:
        raise TargetGroundingError(
            "advisory lifecycle genesis does not match its initial binding"
        )
    if transitions:
        lineage_states = [transitions[0].before, *(item.after for item in transitions)]
    else:
        lineage_states = [initial_artifact]
    if lineage_states[initial_transition_count] != initial_artifact:
        raise TargetGroundingError(
            "advisory lifecycle genesis does not match artifact lineage"
        )

    events = tuple(advisory_lifecycle_event_from_dict(item) for item in values)
    previous_sha256 = expected_genesis_sha256
    replay = TargetGroundingStateMachine(
        initial_artifact,
        mode=TargetGroundingMode.ADVISORY,
        initial_transition_count=initial_transition_count,
    )
    pending: dict[int, PreparedAdvisoryTargetAssessment] = {}
    replayed_transitions: list[ArtifactTransition] = []

    for expected_id, event in enumerate(events, start=1):
        if event.event_id != expected_id:
            raise TargetGroundingError(
                "advisory lifecycle event IDs are not contiguous"
            )
        if event.previous_event_sha256 != previous_sha256:
            raise TargetGroundingError(
                "advisory lifecycle event hash chain is broken or cyclic"
            )
        payload = event.payload
        if event.event_type == "observation":
            document = _require_document_fields(
                payload,
                {"observation"},
                label="advisory observation event payload",
            )
            expected = _observation_from_document(document["observation"])
            observed = replay.record_trusted_observation(
                artifact=expected.artifact,
                scope=expected.scope,
            )
            if observed != expected:
                raise TargetGroundingError(
                    "advisory observation event does not replay exactly"
                )
        elif event.event_type == "declaration":
            document = _require_document_fields(
                payload,
                {"declaration"},
                label="advisory declaration event payload",
            )
            expected = _declaration_from_document(document["declaration"])
            declared = replay.declare_target(
                artifact=expected.artifact,
                target_scope=expected.target_scope,
                observation_ids=expected.observation_ids,
            )
            if declared != expected:
                raise TargetGroundingError(
                    "advisory declaration event does not replay exactly"
                )
        elif event.event_type == "preparation":
            document = _require_document_fields(
                payload,
                {"preparation"},
                label="advisory preparation event payload",
            )
            expected = prepared_advisory_assessment_from_dict(
                document["preparation"]
            )
            if expected.declaration_request_kind == "missing":
                declaration_argument: int | None = None
            elif expected.declaration_request_kind == "invalid":
                declaration_argument = 0
            else:
                declaration_argument = expected.declaration_request_id
                assert declaration_argument is not None
            prepared = replay.prepare_advisory_staged_diff(
                declaration_argument,
                expected.assessment.footprint,
                staged_artifact=expected.staged_artifact,
            )
            if prepared != expected or prepared.preparation_id in pending:
                raise TargetGroundingError(
                    "advisory preparation event does not replay exactly"
                )
            pending[prepared.preparation_id] = prepared
        elif event.event_type == "abort":
            document = _require_document_fields(
                payload,
                {"preparation_id", "prepared_sha256"},
                label="advisory abort event payload",
            )
            preparation_id = document["preparation_id"]
            prepared_sha256 = document["prepared_sha256"]
            if type(preparation_id) is not int or not isinstance(
                prepared_sha256, str
            ):
                raise TargetGroundingError("advisory abort reference is invalid")
            prepared = pending.pop(preparation_id, None)
            if prepared is None or prepared_sha256 != _canonical_document_sha256(
                prepared.to_dict()
            ):
                raise TargetGroundingError(
                    "advisory abort does not reference one pending preparation"
                )
            replay.abort_prepared_advisory_assessment(prepared)
        elif event.event_type == "commitment":
            document = _require_document_fields(
                payload,
                {"prepared_sha256", "commitment"},
                label="advisory commitment event payload",
            )
            prepared_sha256 = document["prepared_sha256"]
            if not isinstance(prepared_sha256, str):
                raise TargetGroundingError(
                    "advisory commitment preparation digest is invalid"
                )
            commitment = committed_advisory_assessment_from_dict(
                document["commitment"]
            )
            prepared = pending.pop(commitment.preparation_id, None)
            if prepared is None or prepared_sha256 != _canonical_document_sha256(
                prepared.to_dict()
            ):
                raise TargetGroundingError(
                    "advisory commitment does not reference one pending preparation"
                )
            if commitment.transition is not None:
                if commitment.transition.kind not in _PROTECTED_TRANSITION_KINDS:
                    raise TargetGroundingError(
                        "advisory commitment carries a non-protected transition"
                    )
                replayed_transitions.append(commitment.transition)
            replay.commit_advisory_assessment(
                prepared,
                commitment.transition,
                committed_assessment=commitment,
            )
        else:
            document = _require_document_fields(
                payload,
                {"transition"},
                label="advisory transition event payload",
            )
            transition = _transition_from_document(
                document["transition"],
                label="advisory standalone transition",
            )
            if transition.kind in _PROTECTED_TRANSITION_KINDS:
                raise TargetGroundingError(
                    "protected transition is not carried by an advisory commitment"
                )
            replay.record_artifact_transition(transition)
            replayed_transitions.append(transition)

        if not replay.advisory_lifecycle_events or (
            replay.advisory_lifecycle_events[-1] != event
        ):
            raise TargetGroundingError(
                "advisory lifecycle event does not match state-machine replay"
            )
        previous_sha256 = event.canonical_sha256

    if pending:
        raise TargetGroundingError(
            "advisory lifecycle ends with unresolved preparations"
        )
    if tuple(replayed_transitions) != tuple(transitions[initial_transition_count:]):
        raise TargetGroundingError(
            "advisory lifecycle does not cover each post-genesis transition exactly once"
        )
    replayed_commitment_documents = tuple(
        item.to_dict() for item in replay.committed_advisory_assessments
    )
    observer_commitment_documents = tuple(
        item.to_dict() for item in committed_assessments
    )
    if replayed_commitment_documents != observer_commitment_documents:
        raise TargetGroundingError(
            "advisory lifecycle commitments do not match the observer ledger"
        )
    if replay.advisory_lifecycle_events != events:
        raise TargetGroundingError("advisory lifecycle replay is not canonical")
    if replay.advisory_lifecycle_final_counters != dict(final_counters):
        raise TargetGroundingError(
            "advisory lifecycle final counters do not match replay"
        )
    if final_counters["pending_preparation_count"] != 0:
        raise TargetGroundingError(
            "advisory lifecycle finalization cannot retain pending preparations"
        )
    return events


def _validate_advisory_assessment_semantics(
    assessment: AdvisoryTargetAssessment,
    *,
    source_artifact: ArtifactRef,
    staged_artifact: ArtifactRef,
) -> None:
    _validate_staged_artifact_binding(
        assessment.footprint,
        source_artifact=source_artifact,
        staged_artifact=staged_artifact,
    )

    declaration = assessment.declaration
    status = assessment.declaration_status
    decision = assessment.decision
    if status != "valid":
        _validate_unclassified_footprint_consistency(
            assessment.footprint,
            source_artifact=source_artifact,
            staged_artifact=staged_artifact,
        )
    expected_without_declaration = {
        "missing": (None, GroundingDecision.MISSING_DECLARATION),
        "invalid": (None, GroundingDecision.INVALID_DECLARATION),
        "unknown": (assessment.requested_declaration_id, GroundingDecision.UNKNOWN_DECLARATION),
    }
    if status in expected_without_declaration:
        requested_id, expected_decision = expected_without_declaration[status]
        if (
            declaration is not None
            or assessment.observations
            or assessment.provenance is not None
            or assessment.requested_declaration_id != requested_id
            or decision is not expected_decision
            or (status == "unknown" and requested_id is None)
        ):
            raise TargetGroundingError(
                "advisory unavailable-declaration assessment is inconsistent"
            )
        return

    if declaration is None or assessment.requested_declaration_id != declaration.declaration_id:
        raise TargetGroundingError("advisory declaration binding is invalid")
    observed_ids = tuple(item.observation_id for item in assessment.observations)
    if (
        observed_ids != declaration.observation_ids
        or len(observed_ids) != len(set(observed_ids))
        or any(item.artifact != declaration.artifact for item in assessment.observations)
        or not _scope_union_covers(
            tuple(item.scope for item in assessment.observations),
            declaration.target_scope,
        )
    ):
        raise TargetGroundingError("advisory observations do not cover the declaration")
    provenance = assessment.provenance
    if (
        provenance is None
        or provenance.validation_id != assessment.validation_id
        or provenance.declaration != declaration
        or provenance.observations != assessment.observations
        or provenance.footprint != assessment.footprint
        or provenance.decision is not decision
    ):
        raise TargetGroundingError("advisory provenance does not match its assessment")

    if status == "stale":
        if declaration.artifact == source_artifact:
            raise TargetGroundingError("advisory stale declaration is still current")
    elif declaration.artifact != source_artifact:
        raise TargetGroundingError(
            "advisory current declaration does not match its source artifact"
        )

    if status == "valid":
        expected_decision = _classify_footprint(
            assessment.footprint,
            declared_target=declaration.target_scope,
        )
        if (
            assessment.footprint.semantic_changed
            and staged_artifact.sha256 == source_artifact.sha256
        ):
            expected_decision = GroundingDecision.INVALID_FOOTPRINT
    elif status == "stale":
        expected_decision = GroundingDecision.STALE_DECLARATION
    elif status in {"prepared", "consumed", "superseded"}:
        expected_decision = GroundingDecision.REPLAYED_DECLARATION
    else:
        raise TargetGroundingError("advisory declaration status is invalid")
    if decision is not expected_decision:
        raise TargetGroundingError("advisory decision does not reproduce")


def committed_authorization_from_dict(value: Any) -> CommittedTargetAuthorization:
    """Parse and independently reproduce one canonical committed record."""

    document = _require_document_fields(
        value,
        {
            "schema_version",
            "authorization_id",
            "preparation_id",
            "previous_authorization_sha256",
            "provenance",
            "staged_artifact",
            "publication",
            "digest_algorithm",
            "authorization_sha256",
        },
        label="committed target authorization",
    )
    authorization_id = document["authorization_id"]
    preparation_id = document["preparation_id"]
    previous_sha256 = document["previous_authorization_sha256"]
    if (
        type(authorization_id) is not int
        or authorization_id < 1
        or type(preparation_id) is not int
        or preparation_id < 1
        or not isinstance(previous_sha256, str)
        or len(previous_sha256) != 64
        or any(character not in "0123456789abcdef" for character in previous_sha256)
    ):
        raise TargetGroundingError("committed authorization identity is invalid")
    publication = _require_document_fields(
        document["publication"],
        {"kind", "transition"},
        label="committed authorization publication",
    )
    publication_kind = publication["kind"]
    raw_transition = publication["transition"]
    if publication_kind == "artifact_transition":
        transition = _transition_from_document(
            raw_transition, label="committed authorization transition"
        )
    elif publication_kind == "strict_no_op" and raw_transition is None:
        transition = None
    else:
        raise TargetGroundingError("committed authorization publication is invalid")
    record = CommittedTargetAuthorization(
        authorization_id=authorization_id,
        preparation_id=preparation_id,
        previous_authorization_sha256=previous_sha256,
        provenance=_provenance_from_document(document["provenance"]),
        staged_artifact=_artifact_from_document(
            document["staged_artifact"], label="committed staged artifact"
        ),
        transition=transition,
    )
    if record.to_dict() != dict(document):
        raise TargetGroundingError("committed authorization digest or fields are invalid")
    return record


def validate_committed_authorization_chain(
    values: Any,
    *,
    transitions: Sequence[ArtifactTransition],
    initial_artifact: ArtifactRef,
    initial_transition_count: int,
) -> tuple[CommittedTargetAuthorization, ...]:
    """Replay a portable authorization chain against exact artifact lineage."""

    if not isinstance(values, list):
        raise TargetGroundingError("committed authorizations must be a list")
    if not isinstance(transitions, Sequence) or not all(
        isinstance(item, ArtifactTransition) for item in transitions
    ):
        raise TargetGroundingError("authorization audit transitions are invalid")
    _validate_artifact_transition_sequence(transitions, label="authorization audit")
    _require_artifact(initial_artifact, label="authorization initial artifact")
    if (
        type(initial_transition_count) is not int
        or initial_transition_count < 0
        or initial_transition_count > len(transitions)
    ):
        raise TargetGroundingError("authorization initial transition count is invalid")

    if transitions:
        lineage_states = [transitions[0].before, *(item.after for item in transitions)]
    else:
        lineage_states = [initial_artifact]
    if lineage_states[initial_transition_count] != initial_artifact:
        raise TargetGroundingError("authorization start does not match artifact lineage")
    state_positions = {artifact: index for index, artifact in enumerate(lineage_states)}
    if len(state_positions) != len(lineage_states):
        raise TargetGroundingError("artifact lineage contains duplicate revision identities")

    records = tuple(committed_authorization_from_dict(item) for item in values)
    previous_sha256 = _AUTHORIZATION_CHAIN_GENESIS_SHA256
    last_position = initial_transition_count
    seen_preparations: set[int] = set()
    seen_declarations: set[int] = set()
    seen_validations: set[int] = set()
    last_preparation_id = 0
    last_declaration_id = 0
    last_validation_id = 0
    transition_authorizations: dict[int, CommittedTargetAuthorization] = {}
    for expected_id, record in enumerate(records, start=1):
        provenance = record.provenance
        declaration = provenance.declaration
        if record.authorization_id != expected_id:
            raise TargetGroundingError("authorization IDs are not contiguous")
        if record.previous_authorization_sha256 != previous_sha256:
            raise TargetGroundingError("authorization hash chain is broken or cyclic")
        if record.preparation_id in seen_preparations:
            raise TargetGroundingError("authorization preparation ID is duplicated")
        if declaration.declaration_id in seen_declarations:
            raise TargetGroundingError("authorization declaration ID is duplicated")
        if provenance.validation_id in seen_validations:
            raise TargetGroundingError("authorization validation ID is duplicated")
        if record.preparation_id <= last_preparation_id:
            raise TargetGroundingError(
                "authorization preparation IDs are not strictly increasing"
            )
        if declaration.declaration_id <= last_declaration_id:
            raise TargetGroundingError(
                "authorization declaration IDs are not strictly increasing"
            )
        if provenance.validation_id <= last_validation_id:
            raise TargetGroundingError(
                "authorization validation IDs are not strictly increasing"
            )
        seen_preparations.add(record.preparation_id)
        seen_declarations.add(declaration.declaration_id)
        seen_validations.add(provenance.validation_id)

        if not provenance.accepted:
            raise TargetGroundingError("committed authorization contains a rejected decision")
        observed_ids = tuple(item.observation_id for item in provenance.observations)
        if (
            observed_ids != declaration.observation_ids
            or len(observed_ids) != len(set(observed_ids))
            or any(item.artifact != declaration.artifact for item in provenance.observations)
            or not _scope_union_covers(
                tuple(item.scope for item in provenance.observations),
                declaration.target_scope,
            )
        ):
            raise TargetGroundingError(
                "committed observations do not uniquely cover the declaration"
            )
        expected_decision = _classify_footprint(
            provenance.footprint,
            declared_target=declaration.target_scope,
        )
        if expected_decision is not provenance.decision:
            raise TargetGroundingError("committed footprint decision or target coverage is invalid")

        position = state_positions.get(declaration.artifact)
        if position is None or position < last_position:
            raise TargetGroundingError("authorization records are not in lineage order")
        if record.transition is None:
            if (
                provenance.decision is not GroundingDecision.AUTHORIZED_NO_OP
                or provenance.footprint.semantic_changed
                or record.staged_artifact != declaration.artifact
            ):
                raise TargetGroundingError("committed strict no-op is invalid")
            last_position = position
        else:
            transition = record.transition
            if (
                transition.transition_id <= initial_transition_count
                or transition.transition_id > len(transitions)
                or transitions[transition.transition_id - 1] != transition
                or transition.kind not in _PROTECTED_TRANSITION_KINDS
                or transition.before != declaration.artifact
                or transition.after != record.staged_artifact
                or transition.transition_id in transition_authorizations
                or position != transition.transition_id - 1
            ):
                raise TargetGroundingError(
                    "committed authorization transition does not match lineage"
                )
            transition_authorizations[transition.transition_id] = record
            last_position = transition.transition_id
        previous_sha256 = record.canonical_sha256
        last_preparation_id = record.preparation_id
        last_declaration_id = declaration.declaration_id
        last_validation_id = provenance.validation_id

    expected_transition_ids = {
        transition.transition_id
        for transition in transitions[initial_transition_count:]
        if transition.kind in _PROTECTED_TRANSITION_KINDS
    }
    if set(transition_authorizations) != expected_transition_ids:
        raise TargetGroundingError(
            "protected artifact transitions lack exactly one committed authorization"
        )
    return records


def validate_committed_advisory_assessment_chain(
    values: Any,
    *,
    lifecycle_events: Any,
    lifecycle_genesis_sha256: str,
    lifecycle_final_counters: Mapping[str, Any],
    transitions: Sequence[ArtifactTransition],
    initial_artifact: ArtifactRef,
    initial_transition_count: int,
) -> tuple[CommittedAdvisoryTargetAssessment, ...]:
    """Replay a canonical observer ledger against every protected transition."""

    if not isinstance(values, list):
        raise TargetGroundingError("committed advisory assessments must be a list")
    if not isinstance(transitions, Sequence) or not all(
        isinstance(item, ArtifactTransition) for item in transitions
    ):
        raise TargetGroundingError("advisory audit transitions are invalid")
    _validate_artifact_transition_sequence(transitions, label="advisory audit")
    _require_artifact(initial_artifact, label="advisory initial artifact")
    if (
        type(initial_transition_count) is not int
        or initial_transition_count < 0
        or initial_transition_count > len(transitions)
    ):
        raise TargetGroundingError("advisory initial transition count is invalid")

    if transitions:
        lineage_states = [transitions[0].before, *(item.after for item in transitions)]
    else:
        lineage_states = [initial_artifact]
    if lineage_states[initial_transition_count] != initial_artifact:
        raise TargetGroundingError("advisory start does not match artifact lineage")
    state_positions = {artifact: index for index, artifact in enumerate(lineage_states)}
    if len(state_positions) != len(lineage_states):
        raise TargetGroundingError("artifact lineage contains duplicate revision identities")

    records = tuple(committed_advisory_assessment_from_dict(item) for item in values)
    validate_advisory_lifecycle_chain(
        lifecycle_events,
        genesis_sha256=lifecycle_genesis_sha256,
        final_counters=lifecycle_final_counters,
        committed_assessments=records,
        transitions=transitions,
        initial_artifact=initial_artifact,
        initial_transition_count=initial_transition_count,
    )
    previous_sha256 = _ADVISORY_ASSESSMENT_CHAIN_GENESIS_SHA256
    last_position = initial_transition_count
    last_preparation_id = 0
    last_validation_id = 0
    last_valid_declaration_id = 0
    transition_assessments: dict[int, CommittedAdvisoryTargetAssessment] = {}
    for expected_id, record in enumerate(records, start=1):
        assessment = record.assessment
        if record.commitment_id != expected_id:
            raise TargetGroundingError("advisory commitment IDs are not contiguous")
        if record.previous_commitment_sha256 != previous_sha256:
            raise TargetGroundingError("advisory commitment hash chain is broken or cyclic")
        if record.preparation_id <= last_preparation_id:
            raise TargetGroundingError(
                "advisory preparation IDs are not strictly increasing"
            )
        if assessment.validation_id <= last_validation_id:
            raise TargetGroundingError(
                "advisory validation IDs are not strictly increasing"
            )
        if assessment.declaration_status == "valid":
            declaration = assessment.declaration
            if (
                declaration is None
                or declaration.declaration_id <= last_valid_declaration_id
            ):
                raise TargetGroundingError(
                    "advisory valid declaration IDs are not strictly increasing"
                )
            last_valid_declaration_id = declaration.declaration_id
        _validate_advisory_assessment_semantics(
            assessment,
            source_artifact=record.source_artifact,
            staged_artifact=record.staged_artifact,
        )

        position = state_positions.get(record.source_artifact)
        if position is None or position < last_position:
            raise TargetGroundingError("advisory records are not in lineage order")
        if assessment.declaration_status == "stale":
            declaration = assessment.declaration
            assert declaration is not None
            declaration_position = state_positions.get(declaration.artifact)
            if (
                declaration_position is None
                or declaration_position < initial_transition_count
                or declaration_position >= position
            ):
                raise TargetGroundingError(
                    "advisory stale declaration is not from an earlier ledger artifact"
                )
        if record.transition is None:
            if (
                assessment.footprint.semantic_changed
                or record.staged_artifact != record.source_artifact
            ):
                raise TargetGroundingError("committed advisory strict no-op is invalid")
            last_position = position
        else:
            transition = record.transition
            if (
                transition.transition_id <= initial_transition_count
                or transition.transition_id > len(transitions)
                or transitions[transition.transition_id - 1] != transition
                or transition.kind not in _PROTECTED_TRANSITION_KINDS
                or transition.before != record.source_artifact
                or transition.after != record.staged_artifact
                or transition.transition_id in transition_assessments
                or position != transition.transition_id - 1
            ):
                raise TargetGroundingError(
                    "committed advisory transition does not match lineage"
                )
            transition_assessments[transition.transition_id] = record
            last_position = transition.transition_id
        last_preparation_id = record.preparation_id
        last_validation_id = assessment.validation_id
        previous_sha256 = record.canonical_sha256

    expected_transition_ids = {
        transition.transition_id
        for transition in transitions[initial_transition_count:]
        if transition.kind in _PROTECTED_TRANSITION_KINDS
    }
    if set(transition_assessments) != expected_transition_ids:
        raise TargetGroundingError(
            "protected artifact transitions lack exactly one advisory assessment"
        )
    return records


__all__ = [
    "AdvisoryLifecycleEvent",
    "AdvisoryTargetAssessment",
    "CommittedAdvisoryTargetAssessment",
    "CommittedTargetAuthorization",
    "GroundingDecision",
    "PreparedAdvisoryTargetAssessment",
    "PreparedTargetAuthorization",
    "TargetDeclaration",
    "TargetGroundingError",
    "TargetGroundingProvenance",
    "TargetGroundingRejected",
    "TargetGroundingMode",
    "TargetGroundingStateMachine",
    "TrustedTargetObservation",
    "advisory_assessment_from_dict",
    "advisory_lifecycle_event_from_dict",
    "committed_advisory_assessment_from_dict",
    "committed_authorization_from_dict",
    "is_target_grounding_protected_transition_kind",
    "prepared_advisory_assessment_from_dict",
    "validate_advisory_lifecycle_chain",
    "validate_committed_advisory_assessment_chain",
    "validate_committed_authorization_chain",
]
