"""Spreadsheet-specific failure routing and plugin lifecycle gates.

This module keeps attribution and lifecycle policy deterministic.  A model may
propose a plugin patch, but it cannot redefine the capability taxonomy, choose
its own validation contexts, or promote itself.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from .plugins import (
    SPREADSHEET_CAPABILITIES,
    PluginContract,
    PluginMutation,
    PluginRegistry,
    ResolvedComposition,
    SpreadsheetCapability,
)
from .trajectory import read_trajectory

FailureSource = Literal[
    "none",
    "infrastructure",
    "composition",
    "composition-interface",
    "capability-gap",
    "capability",
    "unknown",
]
EvolutionAction = Literal[
    "none",
    "retry-infrastructure",
    "repair-composition",
    "enable-plugin",
    "evolve-plugin",
    "collect-evidence",
]
LifecycleState = Literal["ephemeral", "persistent", "retired"]

_INFRASTRUCTURE_CATEGORIES = frozenset(
    {
        "provider-transient",
        "provider-fatal",
        "provider-task",
        "task-timeout",
        "recalculation-infrastructure",
        "scoring-infrastructure",
        "interrupted-unknown-outcome",
    }
)
_COMPOSITION_CATEGORIES = frozenset(
    {"routing-protocol", "missing-provider", "plugin-pending", "tool-unavailable"}
)
_INTERFACE_CATEGORIES = frozenset(
    {"missing-evidence", "context-insufficient", "verification-not-triggered", "interface"}
)
_KIND_PRIORITY = {
    "knowledge": 0,
    "verify": 1,
    "repair": 2,
    "observe": 3,
    "control": 4,
    "act": 5,
    "workflow": 6,
}
_CAPABILITY_KEYWORDS: Mapping[SpreadsheetCapability, tuple[str, ...]] = {
    "structure": (
        "header",
        "table boundary",
        "range",
        "sheet",
        "cross-sheet",
        "join key",
        "foreign key",
        "column match",
    ),
    "formula": (
        "formula",
        "#ref!",
        "#value!",
        "#name?",
        "lookup",
        "vlookup",
        "xlookup",
        "cell reference",
        "dependency",
    ),
    "manipulation": (
        "sort",
        "filter",
        "delete",
        "insert",
        "clear",
        "format",
        "clean",
        "merge cells",
        "conditional formatting",
    ),
    "analysis": (
        "pivot",
        "aggregate",
        "group-by",
        "group by",
        "statistics",
        "summary",
        "cross-table analysis",
    ),
    "visualization": (
        "chart",
        "axis",
        "legend",
        "series",
        "render",
        "visual",
    ),
    "verification": (
        "verify",
        "validation",
        "expected value",
        "cached value",
        "mismatch",
        "false positive",
        "false negative",
        "recalculate",
    ),
    "memory": ("experience", "memory", "workaround", "repeated failure"),
    "composition": (
        "routing",
        "not called",
        "tool unavailable",
        "missing provider",
        "plugin pending",
    ),
}


@dataclass(frozen=True)
class EvolutionCoordinateStep:
    """One accepted mutation in an alternating H/D evolution schedule.

    The partner coordinate is intentionally represented explicitly.  This makes
    it impossible for a caller to accidentally treat a joint proposal as an
    ordinary step and gives manifests a compact, auditable schedule record.
    """

    round: int
    coordinate: Literal["harness", "domain"]
    frozen_coordinate: Literal["harness", "domain"]

    def __post_init__(self) -> None:
        if isinstance(self.round, bool) or not isinstance(self.round, int) or self.round < 1:
            raise ValueError("Evolution rounds are one-based positive integers")
        if self.coordinate not in {"harness", "domain"}:
            raise ValueError("coordinate must be 'harness' or 'domain'")
        if self.frozen_coordinate not in {"harness", "domain"}:
            raise ValueError("frozen_coordinate must be 'harness' or 'domain'")
        if self.coordinate == self.frozen_coordinate:
            raise ValueError("An evolution step must freeze the other coordinate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "coordinate": self.coordinate,
            "frozen_coordinate": self.frozen_coordinate,
            "mutation_count": 1,
        }


def alternating_coordinate_schedule(
    rounds: int, *, first: Literal["harness", "domain"] = "harness"
) -> tuple[EvolutionCoordinateStep, ...]:
    """Return a one-coordinate H/D schedule for ``rounds`` accepted updates.

    ``first`` controls only the first mutation.  Subsequent mutations always
    alternate, so a failed/rejected candidate does not silently consume a
    coordinate turn: callers should advance this schedule only after an
    accepted candidate.
    """

    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        raise ValueError("rounds must be a non-negative integer")
    if first not in {"harness", "domain"}:
        raise ValueError("first must be 'harness' or 'domain'")
    steps: list[EvolutionCoordinateStep] = []
    for index in range(rounds):
        coordinate: Literal["harness", "domain"] = (
            first if index % 2 == 0 else ("domain" if first == "harness" else "harness")
        )
        frozen: Literal["harness", "domain"] = "domain" if coordinate == "harness" else "harness"
        steps.append(EvolutionCoordinateStep(index + 1, coordinate, frozen))
    return tuple(steps)


# Descriptive alias used by experiment scripts and papers.
build_alternating_coordinate_schedule = alternating_coordinate_schedule


_FOUR_ARM_ALIASES: Mapping[str, str] = {
    "h0d0": "h0d0",
    "h1d0": "h1d0",
    "h0d1": "h0d1",
    "h1d1": "h1d1",
    "h0⊕d0": "h0d0",
    "h1⊕d0": "h1d0",
    "h0⊕d1": "h0d1",
    "h1⊕d1": "h1d1",
    "h0+d0": "h0d0",
    "h1+d0": "h1d0",
    "h0+d1": "h0d1",
    "h1+d1": "h1d1",
}


def _four_arm_scores(scores: Mapping[str, float]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_name, raw_score in scores.items():
        name = str(raw_name).strip().lower().replace(" ", "")
        canonical = _FOUR_ARM_ALIASES.get(name)
        if canonical is None:
            raise ValueError(f"Unknown four-arm score name: {raw_name!r}")
        value = float(raw_score)
        if not math.isfinite(value):
            raise ValueError(f"Four-arm score for {raw_name!r} must be finite")
        if canonical in normalized:
            raise ValueError(f"Duplicate four-arm score for {canonical!r}")
        normalized[canonical] = value
    required = set(_FOUR_ARM_ALIASES.values())
    missing = required - set(normalized)
    if missing:
        raise ValueError("Four-arm scores are missing: " + ", ".join(sorted(missing)))
    return normalized


def four_arm_metrics(scores: Mapping[str, float]) -> dict[str, Any]:
    """Compute H/D gains and the 2x2 interaction contrast.

    All four scores must be measured on the same tasks/evaluator.  The
    interaction term is the difference-in-differences requested by the
    co-evolution protocol, not a comparison against the best single arm.
    """

    values = _four_arm_scores(scores)
    baseline = values["h0d0"]
    harness_gain = values["h1d0"] - baseline
    domain_gain = values["h0d1"] - baseline
    joint_gain = values["h1d1"] - baseline
    interaction = values["h1d1"] - values["h1d0"] - values["h0d1"] + baseline
    return {
        "scores": dict(values),
        "baseline": baseline,
        "harness_gain": harness_gain,
        "domain_gain": domain_gain,
        "joint_gain": joint_gain,
        "interaction_gain": interaction,
        "interaction_positive": interaction > 0,
        "joint_gt_max_single": joint_gain > max(harness_gain, domain_gain),
        # Report-friendly notation matching the protocol document.
        "G_H": harness_gain,
        "G_D": domain_gain,
        "G_joint": joint_gain,
        "I": interaction,
    }


def compute_interaction_gain(scores: Mapping[str, float]) -> float:
    """Return only the four-arm interaction gain for compact reporting."""

    return float(four_arm_metrics(scores)["interaction_gain"])


# Short aliases keep experiment notebooks readable without duplicating the
# validation and normalization logic above.
interaction_gain = compute_interaction_gain
calculate_interaction_gain = compute_interaction_gain


def _normalize_category(value: str) -> str:
    return str(value).strip().lower().replace("_", " ").replace(" ", "-")


def _contract_rank(contract: PluginContract) -> tuple[int, int, str]:
    return (
        len(contract.spreadsheet_capabilities),
        _KIND_PRIORITY[contract.kind],
        contract.name,
    )


def _contract_route(contract: PluginContract) -> dict[str, Any]:
    return {
        "plugin": contract.name,
        "version": contract.version,
        "manifest_sha256": contract.manifest_sha256,
        "kind": contract.kind,
        "spreadsheet_capabilities": sorted(contract.spreadsheet_capabilities),
        "evolvable_surfaces": sorted(contract.evolvable_surfaces),
        "evolution_strategy": (
            contract.evolution_strategy.to_dict()
            if contract.evolution_strategy is not None
            else None
        ),
    }


@dataclass(frozen=True)
class FailureAttribution:
    source: FailureSource
    action: EvolutionAction
    capabilities: tuple[SpreadsheetCapability, ...]
    target_plugins: tuple[dict[str, Any], ...]
    candidate_plugins: tuple[dict[str, Any], ...]
    evidence_sha256: tuple[str, ...]
    reasons: tuple[str, ...]
    interface_evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        route_plugins = (
            self.target_plugins if self.source == "capability" else self.candidate_plugins
        )
        if self.source not in {"capability", "capability-gap"}:
            route_plugins = ()
        target_kinds = {
            str(item.get("kind")) for item in route_plugins if isinstance(item, Mapping)
        }
        if self.source in {"capability", "capability-gap"}:
            route = "domain" if "knowledge" in target_kinds else "harness"
        else:
            route = {
                "composition-interface": "composition_interface",
                "infrastructure": "infrastructure",
            }.get(self.source, self.source)
        route_class = {
            "composition-interface": "interface",
            "infrastructure": "infrastructure",
            "capability": route,
            "capability-gap": route,
        }.get(self.source, self.source)
        return {
            "schema_version": "spreadsheet-capability-attribution-v1",
            "failure_source": self.source,
            "evolution_action": self.action,
            "capabilities": list(self.capabilities),
            "target_plugins": list(self.target_plugins),
            "candidate_plugins": list(self.candidate_plugins),
            "evidence_sha256": list(self.evidence_sha256),
            "reasons": list(self.reasons),
            # Keep the historical failure_source value for compatibility while
            # exposing the protocol's four-way route vocabulary to reports.
            "route": route,
            "route_class": route_class,
            "interface_evidence": list(self.interface_evidence),
        }


def infer_capabilities(
    *, task_type: str | None = None, evidence_text: Iterable[str] = ()
) -> tuple[SpreadsheetCapability, ...]:
    text = "\n".join([str(task_type or ""), *(str(value) for value in evidence_text)]).lower()
    inferred: list[SpreadsheetCapability] = []
    for capability in SPREADSHEET_CAPABILITIES:
        if any(keyword in text for keyword in _CAPABILITY_KEYWORDS[capability]):
            inferred.append(capability)
    normalized_task_type = str(task_type or "").strip().lower()
    if "cell-level manipulation" in normalized_task_type and "manipulation" not in inferred:
        inferred.append("manipulation")
    if "sheet-level manipulation" in normalized_task_type:
        for capability in ("structure", "manipulation"):
            if capability not in inferred:
                inferred.append(capability)
    return tuple(capability for capability in SPREADSHEET_CAPABILITIES if capability in inferred)


def _normalize_interface_evidence(
    values: Iterable[Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], ...]:
    """Normalize bounded interface evidence for auditable attribution output."""

    normalized: list[dict[str, Any]] = []
    for raw in values or ():
        if not isinstance(raw, Mapping):
            raise ValueError("Interface evidence entries must be objects")
        item: dict[str, Any] = {}
        for key in (
            "route",
            "required_capability",
            "provider",
            "harness_surface",
            "missing_evidence",
        ):
            if key not in raw:
                continue
            value = raw[key]
            if key == "missing_evidence":
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
                    raise ValueError("Interface missing_evidence must be a sequence")
                value = [str(entry)[:256] for entry in value[:32]]
            elif value is not None:
                value = str(value)[:256]
            item[key] = value
        if item:
            normalized.append(item)
    return tuple(normalized[:32])


def attribute_failure(
    registry: PluginRegistry,
    composition: ResolvedComposition,
    *,
    evaluator_passed: bool,
    task_type: str | None = None,
    evidence_text: Iterable[str] = (),
    error_categories: Iterable[str] = (),
    activated_plugins: Iterable[str] | None = None,
    evidence_sha256: Iterable[str] = (),
    interface_evidence: Iterable[Mapping[str, Any]] | None = None,
) -> FailureAttribution:
    """Separate infrastructure, composition, and plugin-local failures."""

    evidence = tuple(str(value) for value in evidence_text)
    normalized_interface = _normalize_interface_evidence(interface_evidence)
    categories = frozenset(_normalize_category(value) for value in error_categories)
    if normalized_interface:
        categories = categories | {"interface"}
    digests = tuple(str(value) for value in evidence_sha256)
    if any(
        len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
        for digest in digests
    ):
        raise ValueError("Attribution evidence hashes must be lowercase SHA-256 values")
    if evaluator_passed:
        return FailureAttribution(
            "none", "none", (), (), (), digests, ("evaluator-passed",), normalized_interface
        )
    if categories & _INFRASTRUCTURE_CATEGORIES:
        return FailureAttribution(
            "infrastructure",
            "retry-infrastructure",
            (),
            (),
            (),
            digests,
            tuple(sorted(categories & _INFRASTRUCTURE_CATEGORIES)),
            normalized_interface,
        )

    capabilities = infer_capabilities(task_type=task_type, evidence_text=evidence)
    if (
        categories & (_COMPOSITION_CATEGORIES | _INTERFACE_CATEGORIES)
    ) and "composition" not in capabilities:
        capabilities = (*capabilities, "composition")
        capabilities = tuple(
            capability for capability in SPREADSHEET_CAPABILITIES if capability in capabilities
        )
    if not capabilities:
        return FailureAttribution(
            "unknown",
            "collect-evidence",
            (),
            (),
            (),
            digests,
            ("no-capability-evidence",),
            normalized_interface,
        )

    selected = {plugin.contract.name for plugin in composition.plugins}
    activated = selected if activated_plugins is None else {str(name) for name in activated_plugins}
    unknown_activated = activated - selected
    if unknown_activated:
        raise ValueError(
            "Activated plugins are not in the resolved composition: "
            + ", ".join(sorted(unknown_activated))
        )
    raw_matching = [
        contract
        for contract in registry.contracts()
        if contract.spreadsheet_capabilities.intersection(capabilities)
        and contract.evolvable_surfaces
        and contract.evolution_strategy is not None
    ]
    minimum_width = {
        capability: min(
            (
                len(contract.spreadsheet_capabilities)
                for contract in raw_matching
                if capability in contract.spreadsheet_capabilities
            ),
            default=0,
        )
        for capability in capabilities
    }
    matching = [
        contract
        for contract in raw_matching
        if any(
            capability in contract.spreadsheet_capabilities
            and len(contract.spreadsheet_capabilities) == minimum_width[capability]
            for capability in capabilities
        )
    ]
    active_matches = sorted(
        (contract for contract in matching if contract.name in activated), key=_contract_rank
    )
    normalized_task_type = str(task_type or "").strip().casefold()
    if "financial_model" in normalized_task_type or "financial model" in normalized_task_type:
        # The financial specialist has a wider (formula + verification)
        # capability footprint than the generic formula skill and is therefore
        # excluded by the narrowest-capability filter above.  Re-introduce it
        # explicitly for its declared task family so implementation evolution
        # remains reachable.
        specialist = next(
            (
                contract
                for contract in registry.contracts()
                if contract.name == "skill-spreadsheet-financial-model"
                and contract.name in activated
            ),
            None,
        )
        if specialist is not None and specialist not in active_matches:
            active_matches = [*active_matches, specialist]
    selected_not_activated = sorted(
        (
            contract
            for contract in matching
            if contract.name in selected and contract.name not in activated
        ),
        key=_contract_rank,
    )
    inactive_matches = sorted(
        (contract for contract in matching if contract.name not in selected), key=_contract_rank
    )

    if categories & _INTERFACE_CATEGORIES:
        return FailureAttribution(
            "composition-interface",
            "repair-composition",
            capabilities,
            tuple(_contract_route(contract) for contract in selected_not_activated),
            tuple(_contract_route(contract) for contract in inactive_matches),
            digests,
            tuple(sorted(categories & _INTERFACE_CATEGORIES)),
            normalized_interface,
        )
    if categories & _COMPOSITION_CATEGORIES or selected_not_activated:
        policy_targets = sorted(
            (
                plugin.contract
                for plugin in composition.plugins
                if plugin.contract.kind == "control" and plugin.contract.evolution_strategy
            ),
            key=_contract_rank,
        )
        targets = [*selected_not_activated, *policy_targets]
        return FailureAttribution(
            "composition",
            "repair-composition",
            capabilities,
            tuple(_contract_route(contract) for contract in dict.fromkeys(targets)),
            tuple(_contract_route(contract) for contract in inactive_matches),
            digests,
            tuple(sorted(categories & _COMPOSITION_CATEGORIES))
            or ("selected-capability-not-activated",),
            normalized_interface,
        )
    if active_matches:
        # Prefer the narrowest plugin for each capability and retain a verifier
        # alongside the primary capability when the evidence implicates both.
        targets: list[PluginContract] = []
        # Financial_Model is a declared domain task family.  Its specialist
        # plugin owns both the financial prompt and the bounded runtime repair
        # implementation, so do not let the generic formula provider mask the
        # domain coordinate merely because its capability set is narrower.
        if "financial_model" in normalized_task_type or "financial model" in normalized_task_type:
            specialist = next(
                (
                    contract
                    for contract in active_matches
                    if contract.name == "skill-spreadsheet-financial-model"
                ),
                None,
            )
            if specialist is not None:
                targets.append(specialist)
        for capability in capabilities:
            candidates = [
                contract
                for contract in active_matches
                if capability in contract.spreadsheet_capabilities
            ]
            if candidates:
                target = candidates[0]
                if target not in targets:
                    targets.append(target)
        return FailureAttribution(
            "capability",
            "evolve-plugin",
            capabilities,
            tuple(_contract_route(contract) for contract in targets),
            tuple(_contract_route(contract) for contract in inactive_matches),
            digests,
            ("active-provider-produced-failed-workbook",),
            normalized_interface,
        )
    if inactive_matches:
        candidates: list[PluginContract] = []
        for capability in capabilities:
            matches = [
                contract
                for contract in inactive_matches
                if capability in contract.spreadsheet_capabilities
            ]
            if matches and matches[0] not in candidates:
                candidates.append(matches[0])
        return FailureAttribution(
            "capability-gap",
            "enable-plugin",
            capabilities,
            (),
            tuple(_contract_route(contract) for contract in candidates),
            digests,
            ("no-selected-provider-for-inferred-capability",),
            normalized_interface,
        )
    return FailureAttribution(
        "unknown",
        "collect-evidence",
        capabilities,
        (),
        (),
        digests,
        ("no-evolvable-provider",),
        normalized_interface,
    )


def _payload_strings(value: Any, *, limit: int = 256) -> list[str]:
    strings: list[str] = []
    stack = [value]
    while stack and len(strings) < limit:
        current = stack.pop()
        if isinstance(current, str):
            strings.append(current[:1_000])
        elif isinstance(current, Mapping):
            stack.extend(reversed(list(current.values())[:64]))
        elif isinstance(current, Sequence) and not isinstance(current, bytes | bytearray):
            stack.extend(reversed(list(current)[:64]))
    return strings


def attribute_trajectory(
    trajectory: str | Path,
    registry: PluginRegistry,
    composition: ResolvedComposition,
    *,
    task_type: str | None = None,
) -> FailureAttribution:
    """Route one evaluator-labeled trajectory without exposing hidden answers."""

    source = Path(trajectory).expanduser().resolve()
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rows = read_trajectory(source)
    outcomes: list[bool] = []
    categories: list[str] = []
    evidence: list[str] = []
    activated: list[str] = []
    interface_evidence: list[Mapping[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        event = str(row.get("event", ""))
        # SpreadsheetBench adapters emit the explicit v2/v1 evaluator event
        # names; keep the generic events for older trajectories as well.
        if event in {
            "benchmark.evaluated",
            "evaluation.completed",
            "evaluation.failed",
            "spreadsheetbench_v2.evaluated",
            "spreadsheetbench_v1.evaluated",
        }:
            if isinstance(payload.get("passed"), bool):
                outcomes.append(bool(payload["passed"]))
        category = payload.get("error_category")
        if isinstance(category, str):
            categories.append(category)
        if event == "harness.plugin.activated" and isinstance(payload.get("plugin"), str):
            activated.append(str(payload["plugin"]))
        raw_interface = payload.get("interface_evidence")
        if isinstance(raw_interface, Mapping):
            interface_evidence.append(raw_interface)
        elif isinstance(raw_interface, Sequence) and not isinstance(
            raw_interface, bytes | bytearray | str
        ):
            interface_evidence.extend(item for item in raw_interface if isinstance(item, Mapping))
        # A single structured route is also accepted, which keeps trajectory
        # emitters simple while preserving the richer evidence contract.
        if any(key in payload for key in ("required_capability", "missing_evidence")):
            interface_evidence.append(payload)
        evidence.extend(_payload_strings(payload, limit=max(0, 256 - len(evidence))))
    if not outcomes:
        raise ValueError("Trajectory needs an explicit evaluator outcome for attribution")
    if len(set(outcomes)) != 1:
        raise ValueError("Trajectory contains conflicting evaluator outcomes")
    return attribute_failure(
        registry,
        composition,
        evaluator_passed=outcomes[0],
        task_type=task_type,
        evidence_text=evidence,
        error_categories=categories,
        activated_plugins=activated or None,
        evidence_sha256=(digest,),
        interface_evidence=interface_evidence,
    )


@dataclass(frozen=True)
class CandidateLifecycleDecision:
    candidate_artifact_sha256: str
    current_state: LifecycleState
    next_state: LifecycleState
    mean_delta: float | None
    minimum_delta: float | None
    context_kinds: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def promoted(self) -> bool:
        return self.current_state == "ephemeral" and self.next_state == "persistent"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "spreadsheet-plugin-lifecycle-decision-v1",
            "candidate_artifact_sha256": self.candidate_artifact_sha256,
            "current_state": self.current_state,
            "next_state": self.next_state,
            "promoted": self.promoted,
            "mean_delta": self.mean_delta,
            "minimum_delta": self.minimum_delta,
            "context_kinds": list(self.context_kinds),
            "blockers": list(self.blockers),
        }


def evaluate_candidate_lifecycle(
    mutation: PluginMutation,
    validation_report: Mapping[str, Any],
    *,
    min_mean_delta: float = 0.0,
    max_context_regression: float = 0.0,
) -> CandidateLifecycleDecision:
    """Promote only after replay, transfer, and workbook regression contexts pass."""

    if (
        not math.isfinite(min_mean_delta)
        or not math.isfinite(max_context_regression)
        or min_mean_delta < 0
        or max_context_regression < 0
    ):
        raise ValueError("Lifecycle tolerances must be finite and non-negative")
    report_hash = validation_report.get("candidate_artifact_sha256")
    if report_hash != mutation.candidate_artifact_sha256:
        raise ValueError("Validation report candidate hash does not match the mutation")
    raw_contexts = validation_report.get("contexts")
    if not isinstance(raw_contexts, list) or not raw_contexts:
        raise ValueError("Validation report requires a non-empty contexts list")
    required_kinds = {"replay", "transfer", "regression"}
    kinds: list[str] = []
    deltas: list[float] = []
    blockers: list[str] = []
    names: set[str] = set()
    for raw in raw_contexts:
        if not isinstance(raw, Mapping):
            raise ValueError("Validation contexts must be objects")
        name = str(raw.get("name", "")).strip()
        kind = str(raw.get("kind", "")).strip()
        if not name or name in names or kind not in required_kinds:
            raise ValueError("Validation contexts need unique names and a known kind")
        names.add(name)
        kinds.append(kind)
        baseline = float(raw.get("baseline"))
        candidate = float(raw.get("candidate"))
        if not math.isfinite(baseline) or not math.isfinite(candidate):
            raise ValueError("Validation scores must be finite")
        delta = candidate - baseline
        deltas.append(delta)
        if raw.get("failed") is True:
            blockers.append(f"failed-context:{name}")
        if delta < -max_context_regression:
            blockers.append(f"context-regression:{name}")
        if raw.get("severe_regression") is True:
            blockers.append(f"severe-regression:{name}")
    missing_kinds = required_kinds - set(kinds)
    blockers.extend(f"missing-context-kind:{kind}" for kind in sorted(missing_kinds))
    mean_delta = fmean(deltas)
    if mean_delta <= min_mean_delta:
        blockers.append("insufficient-mean-delta")
    next_state: LifecycleState = "persistent" if not blockers else "retired"
    return CandidateLifecycleDecision(
        mutation.candidate_artifact_sha256,
        "ephemeral",
        next_state,
        mean_delta,
        min(deltas),
        tuple(sorted(set(kinds))),
        tuple(blockers),
    )


def persistent_plugin_action(
    *,
    usage_count: int,
    marginal_utility: float,
    redundancy: float,
    severe_regression: bool = False,
) -> Literal["retain", "refine", "merge", "rollback", "retire"]:
    """Choose a bounded persistent-bank maintenance action from long-run telemetry."""

    if usage_count < 0 or not math.isfinite(marginal_utility):
        raise ValueError("Usage and marginal utility are invalid")
    if not math.isfinite(redundancy) or not 0 <= redundancy <= 1:
        raise ValueError("Redundancy must be in [0, 1]")
    if severe_regression or marginal_utility < 0:
        return "rollback"
    if usage_count == 0:
        return "retire"
    if redundancy >= 0.8:
        return "merge"
    if marginal_utility < 0.01:
        return "refine"
    return "retain"


__all__ = [
    "CandidateLifecycleDecision",
    "EvolutionCoordinateStep",
    "FailureAttribution",
    "alternating_coordinate_schedule",
    "attribute_failure",
    "attribute_trajectory",
    "build_alternating_coordinate_schedule",
    "compute_interaction_gain",
    "calculate_interaction_gain",
    "evaluate_candidate_lifecycle",
    "four_arm_metrics",
    "infer_capabilities",
    "interaction_gain",
    "persistent_plugin_action",
]
