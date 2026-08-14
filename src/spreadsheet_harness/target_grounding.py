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
_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
_AUTHORIZATION_CHAIN_GENESIS_SHA256 = "0" * 64
_PROTECTED_TRANSITION_KINDS = frozenset({"mutation", "undo", "external_mutation"})


class TargetGroundingError(RuntimeError):
    """Base error for invalid target-grounding state or input."""


class TargetGroundingRejected(TargetGroundingError):
    """A staged effect footprint was denied by the grounding gate."""

    def __init__(self, record: TargetGroundingProvenance) -> None:
        self.record = record
        super().__init__(f"staged mutation rejected: {record.decision.value}")


class GroundingDecision(str, Enum):
    AUTHORIZED = "authorized"
    AUTHORIZED_NO_OP = "authorized_no_op"
    INCOMPLETE_DIFF = "rejected.incomplete_diff"
    UNKNOWN_EFFECT = "rejected.unknown_effect"
    INVALID_FOOTPRINT = "rejected.invalid_footprint"
    OUTSIDE_DECLARED_TARGET = "rejected.outside_declared_target"

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


def _canonical_json_bytes(value: Any) -> bytes:
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return rendered.encode("ascii")


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


class TargetGroundingStateMachine:
    """Revision-aware, one-use authorization gate for staged workbook edits."""

    def __init__(self, initial_artifact: ArtifactRef) -> None:
        _require_artifact(initial_artifact, label="initial_artifact")
        self._artifact = initial_artifact
        self._observations: dict[int, TrustedTargetObservation] = {}
        self._declarations: dict[int, TargetDeclaration] = {}
        self._declaration_status: dict[int, str] = {}
        self._records: list[TargetGroundingProvenance] = []
        self._committed_authorizations: list[CommittedTargetAuthorization] = []
        self._prepared: dict[int, PreparedTargetAuthorization] = {}
        self._next_observation_id = 1
        self._next_declaration_id = 1
        self._next_validation_id = 1
        self._next_preparation_id = 1

    @property
    def current_artifact(self) -> ArtifactRef:
        return self._artifact

    @property
    def records(self) -> tuple[TargetGroundingProvenance, ...]:
        return tuple(self._records)

    @property
    def committed_authorizations(self) -> tuple[CommittedTargetAuthorization, ...]:
        return tuple(self._committed_authorizations)

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
        return declaration

    def authorize_staged_diff(
        self,
        declaration_id: int,
        diff: WorkbookEffectDiff,
    ) -> TargetGroundingProvenance:
        """Consume a declaration and authorize only an in-scope complete footprint."""

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

    def prepare_staged_diff(
        self,
        declaration_id: int,
        diff: WorkbookEffectDiff,
        *,
        staged_artifact: ArtifactRef,
    ) -> PreparedTargetAuthorization:
        """Reserve one accepted authorization before publishing staged bytes."""

        _require_artifact(staged_artifact, label="staged artifact")
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

        expected_revision = self._artifact.revision + int(
            staged_artifact.sha256 != self._artifact.sha256
        )
        if staged_artifact.revision != expected_revision:
            self._declaration_status[declaration_id] = "consumed"
            raise TargetGroundingError(
                "staged artifact revision does not match its byte transition"
            )

        decision = _classify_footprint(diff, declared_target=declaration.target_scope)
        if diff.semantic_changed and staged_artifact.sha256 == self._artifact.sha256:
            decision = GroundingDecision.INVALID_FOOTPRINT
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
        if not record.accepted:
            self._declaration_status[declaration_id] = "consumed"
            self._records.append(record)
            raise TargetGroundingRejected(record)

        prepared = PreparedTargetAuthorization(
            preparation_id=self._next_preparation_id,
            record=record,
            staged_artifact=staged_artifact,
        )
        self._next_preparation_id += 1
        self._prepared[prepared.preparation_id] = prepared
        self._declaration_status[declaration_id] = "prepared"
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
        elif committed_authorization != expected_authorization:
            raise TargetGroundingError(
                "committed authorization does not match the prepared publication"
            )
        declaration_id = prepared.record.declaration.declaration_id
        self._declaration_status[declaration_id] = "consumed"
        self._prepared.pop(prepared.preparation_id)
        self._records.append(prepared.record)
        self._committed_authorizations.append(committed_authorization)
        if transition is not None:
            self.record_artifact_transition(transition)
        return prepared.record

    def preview_committed_authorization(
        self,
        prepared: PreparedTargetAuthorization,
        transition: ArtifactTransition | None,
    ) -> CommittedTargetAuthorization:
        """Build the exact canonical commit record without advancing any ledger."""

        self.validate_prepared_transition(prepared, transition)
        if transition is None and (
            prepared.record.decision is not GroundingDecision.AUTHORIZED_NO_OP
            or prepared.record.footprint.semantic_changed
            or prepared.staged_artifact != prepared.record.declaration.artifact
        ):
            raise TargetGroundingError(
                "a publication without an artifact transition must be a strict semantic no-op"
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

    def record_artifact_transition(self, transition: ArtifactTransition) -> None:
        """Advance exact lineage and invalidate every unconsumed declaration."""

        if not isinstance(transition, ArtifactTransition):
            raise TypeError("transition must be an ArtifactTransition")
        if transition.before != self._artifact:
            raise TargetGroundingError(
                "artifact transition does not start from the current workbook bytes"
            )
        self._artifact = transition.after
        for declaration_id, status in tuple(self._declaration_status.items()):
            if status == "active":
                self._declaration_status[declaration_id] = "invalidated"


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


__all__ = [
    "CommittedTargetAuthorization",
    "GroundingDecision",
    "PreparedTargetAuthorization",
    "TargetDeclaration",
    "TargetGroundingError",
    "TargetGroundingProvenance",
    "TargetGroundingRejected",
    "TargetGroundingStateMachine",
    "TrustedTargetObservation",
    "committed_authorization_from_dict",
    "validate_committed_authorization_chain",
]
