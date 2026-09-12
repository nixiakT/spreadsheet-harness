"""Persistent, contract-governed plugin evolution.

The controller in this module owns state transitions, candidate isolation and
promotion.  Proposal and benchmark execution are adapters: neither can alter
the immutable contract, validation policy, held-out split, or current revision
pointer.  This keeps implementation/configuration evolution outside the model
process while still allowing the complete plugin artifact to evolve.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import fmean, pvariance
from typing import Any, Literal, Protocol

from .capability_evolution import FailureAttribution, attribute_trajectory
from .errors import HarnessError
from .plugins import (
    BUILTIN_COMPOSITIONS,
    CompositionSpec,
    EvolutionSurface,
    PluginContract,
    PluginMutation,
    PluginRegistry,
    Scalar,
    default_plugin_registry,
    execution_plan,
)
from .trajectory import read_trajectory

ContextKind = Literal["replay", "transfer", "regression"]
CoordinateGroup = Literal["harness", "domain"]
ProposalOperation = Literal["edit", "enable", "disable", "replace"]

_REQUIRED_CONTEXT_KINDS = frozenset({"replay", "transfer", "regression"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PATCH_HEADER = re.compile(r"^diff --git a/([^\s]+) b/([^\s]+)$")
_SAFE_SCORE_KEYS = (
    "accuracy",
    "modification_accuracy",
    "regression_accuracy",
    "scored_accuracy",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("ascii"))


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Unable to read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"{label} must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _contained_path(root: Path, relative: str) -> Path:
    normalized = str(relative).strip().replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise HarnessError(f"Artifact path is not contained: {relative!r}")
    target = root.joinpath(*parts)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != root):
        raise HarnessError(f"Artifact path contains a symlink: {relative!r}")
    return target


def _tree_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise HarnessError(f"Revision artifacts may not contain symlinks: {path}")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            manifest[relative] = _sha256_bytes(path.read_bytes())
    return manifest


def _composition_from_document(document: Mapping[str, Any]) -> CompositionSpec:
    plugins = document.get("plugins")
    overrides = document.get("overrides") or {}
    if not isinstance(plugins, list) or not all(isinstance(item, str) for item in plugins):
        raise HarnessError("Composition plugins must be a string list")
    if not isinstance(overrides, dict) or not all(
        isinstance(name, str) and isinstance(values, dict) for name, values in overrides.items()
    ):
        raise HarnessError("Composition overrides must be an object of objects")
    try:
        return CompositionSpec.create(str(document.get("name", "evolved")), plugins, overrides)
    except ValueError as exc:
        raise HarnessError(f"Invalid evolved composition: {exc}") from exc


@dataclass(frozen=True)
class EvidenceRef:
    path: Path
    task_id: str
    task_type: str
    workbook_family: str

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> EvidenceRef:
        if not isinstance(raw, Mapping):
            raise HarnessError("Evidence reference must be an object")
        path = Path(str(raw.get("path", ""))).expanduser().resolve()
        task_id = str(raw.get("task_id", "")).strip()
        task_type = str(raw.get("task_type", task_id.partition("/")[0])).strip()
        family = str(raw.get("workbook_family", task_id)).strip()
        if not path.is_file() or not task_id or not task_type or not family:
            raise HarnessError("Evidence needs an existing path, task_id, task_type and family")
        return cls(path, task_id, task_type, family)

    def to_dict(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "task_id": self.task_id,
            "task_type": self.task_type,
            "workbook_family": self.workbook_family,
            "sha256": _sha256_bytes(self.path.read_bytes()),
        }


@dataclass(frozen=True)
class EvolutionContext:
    name: str
    kind: ContextKind
    task_ids: tuple[str, ...]
    workbook_families: tuple[str, ...]
    weight: float
    runner: Mapping[str, Any]

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> EvolutionContext:
        if not isinstance(raw, Mapping):
            raise HarnessError("Evolution context must be an object")
        name = str(raw.get("name", "")).strip()
        kind = str(raw.get("kind", "")).strip()
        task_ids = tuple(str(item).strip() for item in raw.get("task_ids") or ())
        families = tuple(str(item).strip() for item in raw.get("workbook_families") or task_ids)
        weight = float(raw.get("weight", 0))
        runner = raw.get("runner") or {}
        if not name or kind not in _REQUIRED_CONTEXT_KINDS:
            raise HarnessError("Evolution context needs a name and replay/transfer/regression kind")
        if not task_ids or any(not item for item in task_ids) or len(set(task_ids)) != len(task_ids):
            raise HarnessError(f"Context {name!r} needs unique task_ids")
        if len(families) != len(task_ids) or any(not item for item in families):
            raise HarnessError(f"Context {name!r} must bind one workbook family per task")
        if not math.isfinite(weight) or weight <= 0:
            raise HarnessError(f"Context {name!r} weight must be positive and finite")
        if not isinstance(runner, Mapping):
            raise HarnessError(f"Context {name!r} runner must be an object")
        return cls(name, kind, task_ids, families, weight, dict(runner))  # type: ignore[arg-type]

    @property
    def task_set_sha256(self) -> str:
        return _sha256_json(
            {
                "name": self.name,
                "kind": self.kind,
                "task_ids": list(self.task_ids),
                "workbook_families": list(self.workbook_families),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "task_ids": list(self.task_ids),
            "workbook_families": list(self.workbook_families),
            "weight": self.weight,
            "task_set_sha256": self.task_set_sha256,
            "runner": dict(self.runner),
        }


@dataclass(frozen=True)
class PromotionPolicy:
    delta: float = 0.01
    epsilon: float = 0.0
    confidence: float = 0.95
    bootstrap_samples: int = 4_000
    min_pairs_per_context: int = 3

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> PromotionPolicy:
        policy = cls(
            delta=float(raw.get("delta", 0.01)),
            epsilon=float(raw.get("epsilon", 0.0)),
            confidence=float(raw.get("confidence", 0.95)),
            bootstrap_samples=int(raw.get("bootstrap_samples", 4_000)),
            min_pairs_per_context=int(raw.get("min_pairs_per_context", 3)),
        )
        if (
            not math.isfinite(policy.delta)
            or policy.delta <= 0
            or not math.isfinite(policy.epsilon)
            or policy.epsilon < 0
            or not 0.5 < policy.confidence < 1
            or policy.bootstrap_samples < 200
            or policy.min_pairs_per_context < 2
        ):
            raise HarnessError("Invalid continuous evolution promotion policy")
        return policy

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta": self.delta,
            "epsilon": self.epsilon,
            "confidence": self.confidence,
            "bootstrap_samples": self.bootstrap_samples,
            "min_pairs_per_context": self.min_pairs_per_context,
        }


@dataclass(frozen=True)
class ContinuousEvolutionConfig:
    repository_root: Path
    composition: CompositionSpec
    groups: Mapping[CoordinateGroup, tuple[str, ...]]
    contexts: tuple[EvolutionContext, ...]
    heldout_task_ids: tuple[str, ...]
    initial_evidence: tuple[EvidenceRef, ...]
    evaluation_binding: Mapping[str, Any]
    policy: PromotionPolicy
    first_group: CoordinateGroup
    max_rounds: int
    max_candidates_per_round: int
    proposer_command: tuple[str, ...]
    evaluator_command: tuple[str, ...]
    command_timeout_seconds: float
    static_checks: tuple[tuple[str, ...], ...]

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> ContinuousEvolutionConfig:
        repository_root = Path(str(raw.get("repository_root", "."))).expanduser().resolve()
        composition_raw = raw.get("composition", "spreadsheet-harness-financial")
        if isinstance(composition_raw, str):
            try:
                composition = BUILTIN_COMPOSITIONS[composition_raw]
            except KeyError as exc:
                raise HarnessError(f"Unknown base composition: {composition_raw!r}") from exc
        elif isinstance(composition_raw, Mapping):
            composition = _composition_from_document(composition_raw)
        else:
            raise HarnessError("composition must be a built-in name or object")
        raw_groups = raw.get("groups") or {}
        if not isinstance(raw_groups, Mapping):
            raise HarnessError("groups must be an object")
        groups: dict[CoordinateGroup, tuple[str, ...]] = {}
        for name in ("harness", "domain"):
            raw_values = raw_groups.get(name, ())
            if isinstance(raw_values, (str, bytes, bytearray)) or not isinstance(
                raw_values, Sequence
            ):
                raise HarnessError(f"Evolution group {name!r} must be a list")
            values = tuple(str(item).strip() for item in raw_values)
            if any(not item for item in values):
                raise HarnessError(f"Evolution group {name!r} contains an empty plugin")
            if len(values) != len(set(values)):
                raise HarnessError(f"Evolution group {name!r} contains duplicates")
            groups[name] = values  # type: ignore[index]
        if not groups["harness"] or not groups["domain"]:
            raise HarnessError("Both harness and domain plugin groups are required")
        if set(groups["harness"]) & set(groups["domain"]):
            raise HarnessError("Harness and domain plugin groups must be disjoint")
        raw_contexts = raw.get("contexts") or ()
        if isinstance(raw_contexts, (str, bytes, bytearray)) or not isinstance(
            raw_contexts, Sequence
        ):
            raise HarnessError("contexts must be a list")
        contexts = tuple(EvolutionContext.from_document(item) for item in raw_contexts)
        if {context.kind for context in contexts} != _REQUIRED_CONTEXT_KINDS:
            raise HarnessError("Contexts must cover replay, transfer and regression")
        if len({context.name for context in contexts}) != len(contexts):
            raise HarnessError("Context names must be unique")
        task_keys = [
            (task_id, family)
            for context in contexts
            for task_id, family in zip(context.task_ids, context.workbook_families, strict=True)
        ]
        if len(task_keys) != len(set(task_keys)):
            raise HarnessError("Evolution contexts must be disjoint by task and workbook family")
        raw_heldout = raw.get("heldout_task_ids") or ()
        if isinstance(raw_heldout, (str, bytes, bytearray)) or not isinstance(
            raw_heldout, Sequence
        ):
            raise HarnessError("heldout_task_ids must be a list")
        heldout = tuple(str(item).strip() for item in raw_heldout)
        if len(heldout) != len(set(heldout)) or any(not item for item in heldout):
            raise HarnessError("Held-out task IDs must be non-empty and unique")
        dev_ids = {task_id for context in contexts for task_id in context.task_ids}
        if dev_ids & set(heldout):
            raise HarnessError("Held-out tasks may not appear in evolution contexts")
        raw_evidence = raw.get("initial_evidence") or ()
        if isinstance(raw_evidence, (str, bytes, bytearray)) or not isinstance(
            raw_evidence, Sequence
        ):
            raise HarnessError("initial_evidence must be a list")
        evidence = tuple(EvidenceRef.from_document(item) for item in raw_evidence)
        if any(item.task_id not in dev_ids for item in evidence):
            raise HarnessError("Initial evidence must come from declared development contexts")
        family_by_task = {
            task_id: family
            for context in contexts
            for task_id, family in zip(context.task_ids, context.workbook_families, strict=True)
        }
        if any(family_by_task.get(item.task_id) != item.workbook_family for item in evidence):
            raise HarnessError("Initial evidence workbook family does not match its context")
        binding = raw.get("evaluation_binding") or {}
        if not isinstance(binding, Mapping) or not binding:
            raise HarnessError("evaluation_binding must be a non-empty frozen object")
        first_group = str(raw.get("first_group", "harness"))
        max_rounds = int(raw.get("max_rounds", 10))
        max_candidates = int(raw.get("max_candidates_per_round", 3))
        proposer = tuple(str(item) for item in raw.get("proposer_command") or ())
        evaluator = tuple(str(item) for item in raw.get("evaluator_command") or ())
        timeout = float(raw.get("command_timeout_seconds", 7200))
        checks = tuple(tuple(str(part) for part in command) for command in raw.get("static_checks") or ())
        if first_group not in {"harness", "domain"}:
            raise HarnessError("first_group must be harness or domain")
        if max_rounds < 1 or max_candidates < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise HarnessError("Round, candidate and command timeout limits must be positive")
        if (
            not proposer
            or not evaluator
            or any(not item for item in proposer)
            or any(not item for item in evaluator)
            or any(not command for command in checks)
        ):
            raise HarnessError("Proposer/evaluator commands and static checks must be argv arrays")
        result = cls(
            repository_root,
            composition,
            groups,
            contexts,
            heldout,
            evidence,
            dict(binding),
            PromotionPolicy.from_document(raw.get("promotion") or {}),
            first_group,  # type: ignore[arg-type]
            max_rounds,
            max_candidates,
            proposer,
            evaluator,
            timeout,
            checks,
        )
        result.validate(default_plugin_registry())
        return result

    @classmethod
    def load(cls, path: str | Path) -> ContinuousEvolutionConfig:
        return cls.from_document(_read_json(Path(path).expanduser().resolve(), label="evolution config"))

    def validate(self, registry: PluginRegistry) -> None:
        if not (self.repository_root / "src/spreadsheet_harness").is_dir():
            raise HarnessError("repository_root has no src/spreadsheet_harness package")
        if not (self.repository_root / "skills").is_dir():
            raise HarnessError("repository_root has no skills directory")
        resolved = registry.resolve(self.composition)
        execution_plan(resolved)
        known = {contract.name for contract in registry.contracts()}
        grouped = set(self.groups["harness"]) | set(self.groups["domain"])
        if not grouped <= known:
            raise HarnessError(f"Evolution groups contain unknown plugins: {sorted(grouped - known)}")
        active_evolvable = {
            plugin.contract.name
            for plugin in resolved.plugins
            if plugin.contract.evolvable_surfaces and plugin.contract.edit_policies
        }
        if not active_evolvable & grouped:
            raise HarnessError("No active plugin has an executable edit policy")

    @property
    def binding_sha256(self) -> str:
        return _sha256_json(self.evaluation_binding)

    @property
    def contexts_sha256(self) -> str:
        return _sha256_json([context.to_dict() for context in self.contexts])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "continuous-plugin-evolution-config-v1",
            "repository_root": str(self.repository_root),
            "composition": self.composition.to_dict(),
            "groups": {name: list(values) for name, values in self.groups.items()},
            "contexts": [context.to_dict() for context in self.contexts],
            "heldout_task_ids": list(self.heldout_task_ids),
            "initial_evidence": [item.to_dict() for item in self.initial_evidence],
            "evaluation_binding": dict(self.evaluation_binding),
            "evaluation_binding_sha256": self.binding_sha256,
            "contexts_sha256": self.contexts_sha256,
            "promotion": self.policy.to_dict(),
            "first_group": self.first_group,
            "max_rounds": self.max_rounds,
            "max_candidates_per_round": self.max_candidates_per_round,
            "proposer_command": list(self.proposer_command),
            "evaluator_command": list(self.evaluator_command),
            "command_timeout_seconds": self.command_timeout_seconds,
            "static_checks": [list(command) for command in self.static_checks],
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.to_dict())


@dataclass(frozen=True)
class EvolutionRoute:
    group: CoordinateGroup
    operation: ProposalOperation
    target_plugin: str
    surface: EvolutionSurface | None
    evidence_sha256: tuple[str, ...]
    support_count: int
    reasons: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EvolutionRoute:
        group = str(raw.get("group", ""))
        operation = str(raw.get("operation", ""))
        target = str(raw.get("target_plugin", ""))
        surface = raw.get("surface")
        if group not in {"harness", "domain"} or operation not in {
            "edit",
            "enable",
            "disable",
            "replace",
        }:
            raise HarnessError("Invalid persisted evolution route")
        if surface is not None and surface not in {
            "config",
            "implementation",
            "prompt",
            "description",
        }:
            raise HarnessError("Invalid persisted evolution route surface")
        return cls(
            group,  # type: ignore[arg-type]
            operation,  # type: ignore[arg-type]
            target,
            surface,  # type: ignore[arg-type]
            tuple(str(item) for item in raw.get("evidence_sha256") or ()),
            int(raw.get("support_count", 0)),
            tuple(str(item) for item in raw.get("reasons") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "operation": self.operation,
            "target_plugin": self.target_plugin,
            "surface": self.surface,
            "evidence_sha256": list(self.evidence_sha256),
            "support_count": self.support_count,
            "reasons": list(self.reasons),
        }


def _trajectory_signal(path: Path) -> str:
    signals: list[str] = []
    for row in read_trajectory(path):
        event = str(row.get("event", ""))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        signals.extend(
            str(value)
            for value in (event, payload.get("error_category"), payload.get("status"))
            if value is not None
        )
        if event.startswith("tool."):
            signals.append(str(payload.get("name", "")))
    return " ".join(signals).lower()


def _choose_surface(contract: PluginContract, signal: str) -> EvolutionSurface | None:
    available = {policy.surface for policy in contract.edit_policies}
    if not available:
        return None
    if "missing-evidence" in signal or "context-insufficient" in signal:
        order = ("config", "implementation", "description", "prompt")
    elif "tool." in signal or "tool-" in signal or "tool_error" in signal:
        order = ("description", "implementation", "config", "prompt")
    elif contract.kind in {"act", "observe", "verify", "repair"}:
        order = ("implementation", "config", "description", "prompt")
    elif contract.kind == "knowledge" and "implementation" in available:
        order = ("implementation", "prompt", "description", "config")
    else:
        order = ("prompt", "config", "implementation", "description")
    return next((surface for surface in order if surface in available), None)  # type: ignore[return-value]


class DeterministicEvidenceRouter:
    """Aggregate failed development trajectories into exactly one coordinate."""

    def route(
        self,
        evidence: Sequence[EvidenceRef],
        *,
        registry: PluginRegistry,
        composition: CompositionSpec,
        groups: Mapping[CoordinateGroup, tuple[str, ...]],
        preferred_group: CoordinateGroup,
    ) -> EvolutionRoute | None:
        resolved = registry.resolve(composition)
        candidates: Counter[tuple[ProposalOperation, str]] = Counter()
        hashes: dict[tuple[ProposalOperation, str], set[str]] = {}
        reasons: dict[tuple[ProposalOperation, str], set[str]] = {}
        signals: dict[tuple[ProposalOperation, str], list[str]] = {}
        for item in evidence:
            attribution: FailureAttribution
            try:
                attribution = attribute_trajectory(
                    item.path, registry, resolved, task_type=item.task_type
                )
            except (HarnessError, OSError, ValueError):
                continue
            if attribution.source in {"none", "infrastructure", "unknown"}:
                continue
            operation: ProposalOperation = (
                "enable" if attribution.action == "enable-plugin" else "edit"
            )
            routes = (
                attribution.candidate_plugins
                if operation == "enable"
                else attribution.target_plugins
            )
            for route in routes:
                plugin = str(route.get("plugin", ""))
                if not plugin:
                    continue
                key = (operation, plugin)
                candidates[key] += 1
                hashes.setdefault(key, set()).update(attribution.evidence_sha256)
                reasons.setdefault(key, set()).update(attribution.reasons)
                signals.setdefault(key, []).append(_trajectory_signal(item.path))
        if not candidates:
            return None
        plugin_group = {
            plugin: group for group, plugins in groups.items() for plugin in plugins
        }
        preferred = [key for key in candidates if plugin_group.get(key[1]) == preferred_group]
        eligible = preferred or [key for key in candidates if key[1] in plugin_group]
        if not eligible:
            return None
        active = set(composition.plugins)

        def rank(key: tuple[ProposalOperation, str]) -> tuple[Any, ...]:
            operation, name = key
            contract = registry.get(name)
            return (
                -candidates[key],
                0 if operation == "edit" and name in active else 1,
                len(contract.spreadsheet_capabilities),
                name,
            )

        operation, plugin_name = min(eligible, key=rank)
        contract = registry.get(plugin_name)
        surface = None if operation != "edit" else _choose_surface(
            contract, " ".join(signals.get((operation, plugin_name), ()))
        )
        if operation == "edit" and surface is None:
            return None
        return EvolutionRoute(
            plugin_group[plugin_name],
            operation,
            plugin_name,
            surface,
            tuple(sorted(hashes.get((operation, plugin_name), ()))),
            candidates[(operation, plugin_name)],
            tuple(sorted(reasons.get((operation, plugin_name), ()))),
        )


def _safe_evidence_summary(item: EvidenceRef) -> dict[str, Any]:
    rows = read_trajectory(item.path)
    tools = Counter()
    failures = Counter()
    evaluator: dict[str, Any] | None = None
    for row in rows:
        event = str(row.get("event", ""))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if event.startswith("tool."):
            tools[str(payload.get("name", "unknown"))] += 1
        category = payload.get("error_category")
        if isinstance(category, str):
            failures[category] += 1
        if event.lower() in {
            "benchmark.evaluated",
            "evaluation.completed",
            "evaluation.failed",
            "spreadsheetbench_v2.evaluated",
            "spreadsheetbench.evaluated",
        } and isinstance(payload.get("passed"), bool):
            evaluator = {"passed": payload["passed"]}
            for key in _SAFE_SCORE_KEYS:
                value = payload.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    evaluator[key] = float(value)
    return {
        **item.to_dict(),
        "path": item.path.name,
        "sha256": _sha256_bytes(item.path.read_bytes()),
        "event_count": len(rows),
        "tool_events": dict(sorted(tools.items())),
        "error_categories": dict(sorted(failures.items())),
        "evaluator": evaluator,
    }


@dataclass(frozen=True)
class CandidateProposal:
    candidate_id: str
    base_revision_sha256: str
    operation: ProposalOperation
    target_plugin: str
    surface: EvolutionSurface | None
    operator: str | None
    files: tuple[tuple[str, str], ...]
    patch: str | None
    config_patch: Mapping[str, Scalar]
    replacement_plugin: str | None
    rationale: str

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> CandidateProposal:
        candidate_id = str(raw.get("candidate_id", ""))
        if not _SAFE_ID.fullmatch(candidate_id) or candidate_id in {".", ".."}:
            raise HarnessError("Candidate proposal has an unsafe candidate_id")
        operation = str(raw.get("operation", "edit"))
        if operation not in {"edit", "enable", "disable", "replace"}:
            raise HarnessError("Candidate proposal has an unknown operation")
        surface = raw.get("surface")
        if surface is not None and surface not in {
            "config",
            "implementation",
            "prompt",
            "description",
        }:
            raise HarnessError("Candidate proposal has an unknown surface")
        raw_files = raw.get("files") or []
        if not isinstance(raw_files, list) or not all(isinstance(item, Mapping) for item in raw_files):
            raise HarnessError("Candidate files must be a list of objects")
        files: list[tuple[str, str]] = []
        for item in raw_files:
            path, content = item.get("path"), item.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise HarnessError("Candidate file needs string path and content")
            files.append((path, content))
        config_patch = raw.get("config_patch") or {}
        if not isinstance(config_patch, Mapping):
            raise HarnessError("Candidate config_patch must be an object")
        return cls(
            candidate_id,
            str(raw.get("base_revision_sha256", "")),
            operation,  # type: ignore[arg-type]
            str(raw.get("target_plugin", "")),
            surface,  # type: ignore[arg-type]
            str(raw["operator"]) if raw.get("operator") is not None else None,
            tuple(files),
            str(raw["patch"]) if raw.get("patch") is not None else None,
            dict(config_patch),
            str(raw["replacement_plugin"])
            if raw.get("replacement_plugin") is not None
            else None,
            str(raw.get("rationale", ""))[:4_000],
        )

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "base_revision_sha256": self.base_revision_sha256,
            "operation": self.operation,
            "target_plugin": self.target_plugin,
            "surface": self.surface,
            "operator": self.operator,
            "files": [
                {"path": path, "content": content if include_content else "[OMITTED]"}
                for path, content in self.files
            ],
            "patch": self.patch if include_content else ("[OMITTED]" if self.patch else None),
            "config_patch": dict(self.config_patch),
            "replacement_plugin": self.replacement_plugin,
            "rationale": self.rationale,
        }


class ProposalAdapter(Protocol):
    def propose(self, request: Mapping[str, Any], round_dir: Path) -> Sequence[CandidateProposal]: ...


class EvaluationAdapter(Protocol):
    def evaluate(self, request: Mapping[str, Any], candidate_dir: Path) -> Mapping[str, Any]: ...


def _expand_command(command: Sequence[str], values: Mapping[str, str]) -> list[str]:
    expanded: list[str] = []
    for part in command:
        try:
            expanded.append(str(part).format_map(values))
        except KeyError as exc:
            raise HarnessError(f"Unknown command placeholder: {exc.args[0]}") from exc
    return expanded


def _run_adapter_command(
    command: Sequence[str],
    *,
    request: Mapping[str, Any],
    directory: Path,
    timeout: float,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / "request.json"
    response_path = directory / "response.json"
    _atomic_json(request_path, request)
    values = {
        "request": str(request_path),
        "response": str(response_path),
        "directory": str(directory),
    }
    completed = subprocess.run(
        _expand_command(command, values),
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    (directory / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (directory / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise HarnessError(f"Evolution adapter exited with status {completed.returncode}")
    if not response_path.is_file():
        raise HarnessError("Evolution adapter did not create response.json")
    return _read_json(response_path, label="evolution adapter response")


@dataclass(frozen=True)
class CommandProposalAdapter:
    command: tuple[str, ...]
    timeout: float

    def propose(self, request: Mapping[str, Any], round_dir: Path) -> Sequence[CandidateProposal]:
        document = _run_adapter_command(
            self.command, request=request, directory=round_dir / "proposal", timeout=self.timeout
        )
        raw = document.get("candidates")
        if not isinstance(raw, list):
            raise HarnessError("Proposal adapter response needs a candidates list")
        return [CandidateProposal.from_document(item) for item in raw if isinstance(item, Mapping)]


@dataclass(frozen=True)
class CommandEvaluationAdapter:
    command: tuple[str, ...]
    timeout: float

    def evaluate(self, request: Mapping[str, Any], candidate_dir: Path) -> Mapping[str, Any]:
        return _run_adapter_command(
            self.command,
            request=request,
            directory=candidate_dir / "evaluation-adapter",
            timeout=self.timeout,
        )


@dataclass(frozen=True)
class SpreadsheetBenchV2EvaluationAdapter:
    """Official SpreadsheetBench-v2 paired evaluator for continuous rounds.

    The adapter intentionally receives the provider and dataset as constructor
    dependencies rather than reading them from a candidate.  A candidate can
    therefore select only its immutable composition and artifact/skill tree;
    evaluator, model, budgets and task splits remain controller-owned.  Each
    context is run twice (incumbent and candidate) with the same task set and
    pinned official evaluator, and the resulting rows are converted to the
    paired report consumed by :func:`evaluate_validation_report`.
    """

    provider_config: Any
    dataset_root: Path
    evaluator_path: Path
    output_root: Path
    max_model_calls: int = 50
    max_turns_per_arm: int = 50
    max_total_tokens: int | None = 200_000
    max_output_tokens: int | None = 4_096
    task_timeout_seconds: float = 1_800
    arm_order_seed: int = 20_260_820

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_root", Path(self.dataset_root).expanduser().resolve())
        object.__setattr__(self, "evaluator_path", Path(self.evaluator_path).expanduser().resolve())
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser().resolve())

    def evaluate(self, request: Mapping[str, Any], candidate_dir: Path) -> Mapping[str, Any]:
        # Imports are local to keep the continuous controller usable without
        # importing the (heavier) benchmark runner during configuration checks.
        from .spreadsheetbench_v2 import (
            load_spreadsheetbench_v2_tasks,
            select_spreadsheetbench_v2_tasks,
        )

        def composition(revision_dir: Path) -> CompositionSpec:
            return _composition_from_document(
                _read_json(revision_dir / "composition.json", label="revision composition")
            )

        incumbent_dir = Path(str(request.get("incumbent_directory", ""))).resolve()
        if not incumbent_dir.is_dir() or not candidate_dir.is_dir():
            raise HarnessError("Paired evaluator received missing revision directories")
        contexts = request.get("contexts")
        if not isinstance(contexts, list):
            raise HarnessError("Paired evaluator request has no contexts")
        context_reports: list[dict[str, Any]] = []
        output_root = self.output_root / str(request.get("candidate_revision_sha256", "candidate"))
        output_root.mkdir(parents=True, exist_ok=True)

        def run_revision(
            revision_dir: Path, output_dir: Path, tasks: Sequence[Any], spec: CompositionSpec
        ) -> dict[str, Any]:
            """Run the benchmark in a clean interpreter rooted at that revision."""

            payload_dir = output_dir.parent / (output_dir.name + "-request")
            payload_dir.mkdir(parents=True, exist_ok=True)
            provider_payload = dict(vars(self.provider_config))
            provider_payload["api_key"] = "__SPREADSHEET_EVOLUTION_API_KEY__"
            payload = {
                "provider": provider_payload,
                "dataset_root": str(dataset_root),
                "evaluator_path": str(self.evaluator_path),
                "output_dir": str(output_dir),
                "revision_dir": str(revision_dir),
                "task_ids": [task.task_id for task in tasks],
                "composition": spec.to_dict(),
                "max_model_calls": self.max_model_calls,
                "max_turns_per_arm": self.max_turns_per_arm,
                "max_total_tokens": self.max_total_tokens,
                "max_output_tokens": self.max_output_tokens,
                "task_timeout_seconds": self.task_timeout_seconds,
                "arm_order_seed": self.arm_order_seed,
                "summary_path": str(payload_dir / "summary.json"),
            }
            request_path = payload_dir / "request.json"
            _atomic_json(request_path, payload)
            script = textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path
                from spreadsheet_harness.config import ProviderConfig
                from spreadsheet_harness.plugins import CompositionSpec
                from spreadsheet_harness.skills import SkillRegistry
                from spreadsheet_harness.spreadsheetbench_v2 import (
                    load_spreadsheetbench_v2_tasks,
                    run_spreadsheetbench_v2_comparison,
                    select_spreadsheetbench_v2_tasks,
                )
                payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                if payload["provider"].get("api_key") == "__SPREADSHEET_EVOLUTION_API_KEY__":
                    payload["provider"]["api_key"] = os.environ["SPREADSHEET_EVOLUTION_API_KEY"]
                config = ProviderConfig(**payload["provider"])
                tasks = select_spreadsheetbench_v2_tasks(
                    load_spreadsheetbench_v2_tasks(payload["dataset_root"]), payload["task_ids"]
                )
                spec_doc = payload["composition"]
                spec = CompositionSpec.create(
                    spec_doc["name"], spec_doc["plugins"], spec_doc.get("overrides") or {}
                )
                summary = run_spreadsheetbench_v2_comparison(
                    config=config,
                    dataset_root=payload["dataset_root"],
                    evaluator_path=payload["evaluator_path"],
                    output_dir=payload["output_dir"],
                    skill_registry=SkillRegistry([
                        Path(payload["revision_dir"] ) / "artifact" / "skills"
                    ]),
                    tasks=tasks,
                    arms=("ours",),
                    composition_overrides={"ours": spec},
                    max_model_calls=payload["max_model_calls"],
                    max_turns_per_arm=payload["max_turns_per_arm"],
                    max_total_tokens=payload["max_total_tokens"],
                    max_output_tokens=payload["max_output_tokens"],
                    task_timeout_seconds=payload["task_timeout_seconds"],
                    arm_order_seed=payload["arm_order_seed"],
                )
                Path(payload["summary_path"]).write_text(
                    json.dumps(summary, ensure_ascii=False), encoding="utf-8"
                )
                """
            )
            environment = dict(os.environ)
            environment["SPREADSHEET_EVOLUTION_API_KEY"] = str(
                getattr(self.provider_config, "api_key", "")
            )
            environment["PYTHONPATH"] = str(revision_dir / "artifact" / "src")
            completed = subprocess.run(
                [sys.executable, "-c", script, str(request_path)],
                cwd=revision_dir / "artifact",
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.task_timeout_seconds * max(1, len(tasks)),
            )
            (payload_dir / "stdout.log").write_text(completed.stdout, encoding="utf-8")
            (payload_dir / "stderr.log").write_text(completed.stderr, encoding="utf-8")
            if completed.returncode:
                raise HarnessError(
                    f"SpreadsheetBench-v2 revision evaluator exited with status {completed.returncode}"
                )
            return _read_json(payload_dir / "summary.json", label="paired benchmark summary")

        for raw_context in contexts:
            if not isinstance(raw_context, Mapping):
                raise HarnessError("Paired evaluator context must be an object")
            name = str(raw_context.get("name", "")).strip()
            task_ids = tuple(str(item) for item in raw_context.get("task_ids") or ())
            task_families = tuple(
                str(item) for item in raw_context.get("workbook_families") or task_ids
            )
            family_by_task = dict(zip(task_ids, task_families, strict=False))
            if not name or not task_ids:
                raise HarnessError("Paired evaluator context needs a name and task IDs")
            runner = raw_context.get("runner") or {}
            if not isinstance(runner, Mapping):
                raise HarnessError(f"Paired evaluator context {name!r} runner must be an object")
            dataset_value = runner.get("dataset") or self.dataset_root
            dataset_root = Path(str(dataset_value)).expanduser()
            if not dataset_root.is_absolute():
                # Adapter commands run from an isolated evaluation directory,
                # not the repository root. Resolve relative runner paths
                # against the installed harness root first.
                repository_root = Path(__file__).resolve().parents[2]
                dataset_root = (repository_root / dataset_root).resolve()
            else:
                dataset_root = dataset_root.resolve()
            if not dataset_root.is_dir():
                raise HarnessError(
                    f"Paired evaluator context {name!r} dataset does not exist: {dataset_root}"
                )
            all_tasks = load_spreadsheetbench_v2_tasks(dataset_root)
            by_id = {task.task_id: task for task in all_tasks}
            try:
                tasks = select_spreadsheetbench_v2_tasks(
                    all_tasks, task_ids
                )
            except HarnessError:
                # Preserve the controller's exact task order and produce a
                # useful error instead of silently evaluating a different set.
                missing = [task_id for task_id in task_ids if task_id not in by_id]
                raise HarnessError(
                    f"Paired evaluator context {name!r} references unknown tasks: {missing}"
                ) from None
            incumbent_output = output_root / name / "incumbent"
            candidate_output = output_root / name / "candidate"
            run_revision(incumbent_dir, incumbent_output, tasks, composition(incumbent_dir))
            run_revision(candidate_dir, candidate_output, tasks, composition(candidate_dir))
            baseline_rows = json.loads((incumbent_output / "results.json").read_text())
            candidate_rows = json.loads((candidate_output / "results.json").read_text())
            baseline_by_task = {str(row.get("task_id")): row for row in baseline_rows}
            candidate_by_task = {str(row.get("task_id")): row for row in candidate_rows}
            pairs: list[dict[str, Any]] = []
            candidate_evidence: list[dict[str, Any]] = []
            hard_failures: list[str] = []
            for task in tasks:
                baseline = baseline_by_task.get(task.task_id, {})
                candidate = candidate_by_task.get(task.task_id, {})
                baseline_score = (baseline.get("official_score") or {}).get("accuracy")
                candidate_score = (candidate.get("official_score") or {}).get("accuracy")
                baseline_status = "scored" if isinstance(baseline_score, (int, float)) else "unscored"
                candidate_status = "scored" if isinstance(candidate_score, (int, float)) else "unscored"
                if baseline_status != "scored" or candidate_status != "scored":
                    hard_failures.append(task.task_id)
                pairs.append(
                    {
                        "id": task.task_id,
                        "baseline": baseline_score,
                        "candidate": candidate_score,
                        "baseline_status": baseline_status,
                        "candidate_status": candidate_status,
                        "candidate_cost": ((candidate.get("budget") or {}).get("used") or {}).get(
                            "total_tokens", 0
                        ),
                    }
                )
                run_dir = candidate.get("run_dir")
                trajectory = Path(str(run_dir)) / "trajectory.jsonl" if run_dir else None
                if trajectory is not None and trajectory.is_file():
                    candidate_evidence.append(
                        {
                            "path": str(trajectory),
                            "task_id": task.task_id,
                            "task_type": task.category,
                            "workbook_family": family_by_task.get(task.task_id, task.item_id),
                        }
                    )
            context_reports.append(
                {
                    "name": name,
                    "kind": str(raw_context.get("kind", "")),
                    "task_set_sha256": str(raw_context.get("task_set_sha256", "")),
                    "pairs": pairs,
                    "hard_failures": hard_failures,
                }
            )
        return {
            "schema_version": "continuous-plugin-validation-report-v1",
            "incumbent_revision_sha256": request.get("incumbent_revision_sha256"),
            "candidate_revision_sha256": request.get("candidate_revision_sha256"),
            "evaluation_binding_sha256": request.get("evaluation_binding_sha256"),
            "contexts_sha256": request.get("contexts_sha256"),
            "heldout_task_ids_sha256": request.get("heldout_task_ids_sha256"),
            "contexts": context_reports,
            "candidate_evidence": candidate_evidence,
            "hard_failures": any(bool(item["hard_failures"]) for item in context_reports),
        }


@dataclass(frozen=True)
class ValidationDecision:
    promoted: bool
    aggregate_mean_delta: float | None
    aggregate_lcb: float | None
    context_results: tuple[dict[str, Any], ...]
    variance: float | None
    cost: float
    blockers: tuple[str, ...]
    candidate_evidence: tuple[EvidenceRef, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ValidationDecision:
        evidence_raw = raw.get("candidate_evidence") or []
        if not isinstance(evidence_raw, list):
            raise HarnessError("Validation decision candidate_evidence must be a list")
        evidence = tuple(
            EvidenceRef.from_document(item)
            for item in evidence_raw
            if isinstance(item, Mapping)
        )
        raw_contexts = raw.get("context_results") or []
        if not isinstance(raw_contexts, list):
            raise HarnessError("Validation decision context_results must be a list")
        return cls(
            bool(raw.get("promoted", False)),
            float(raw["aggregate_mean_delta"])
            if raw.get("aggregate_mean_delta") is not None
            else None,
            float(raw["aggregate_lcb"]) if raw.get("aggregate_lcb") is not None else None,
            tuple(item for item in raw_contexts if isinstance(item, Mapping)),
            float(raw["variance"]) if raw.get("variance") is not None else None,
            float(raw.get("cost", 0.0)),
            tuple(str(item) for item in raw.get("blockers") or ()),
            evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "continuous-plugin-validation-decision-v1",
            "promoted": self.promoted,
            "aggregate_mean_delta": self.aggregate_mean_delta,
            "aggregate_lcb": self.aggregate_lcb,
            "context_results": list(self.context_results),
            "variance": self.variance,
            "cost": self.cost,
            "blockers": list(self.blockers),
            "candidate_evidence": [item.to_dict() for item in self.candidate_evidence],
        }


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.floor(probability * len(ordered))))
    return ordered[index]


def _bootstrap_mean_lcb(
    values: Sequence[float], *, confidence: float, samples: int, seed: str
) -> float:
    if not values:
        raise ValueError("Cannot bootstrap an empty sequence")
    generator = random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))
    means = [
        fmean(generator.choice(values) for _ in values)
        for _ in range(samples)
    ]
    return _quantile(means, 1 - confidence)


def evaluate_validation_report(
    report: Mapping[str, Any],
    *,
    config: ContinuousEvolutionConfig,
    incumbent_revision: str,
    candidate_revision: str,
) -> ValidationDecision:
    blockers: list[str] = []
    if report.get("schema_version") != "continuous-plugin-validation-report-v1":
        blockers.append("validation-schema-mismatch")
    if report.get("incumbent_revision_sha256") != incumbent_revision:
        blockers.append("incumbent-revision-mismatch")
    if report.get("candidate_revision_sha256") != candidate_revision:
        blockers.append("candidate-revision-mismatch")
    if report.get("evaluation_binding_sha256") != config.binding_sha256:
        blockers.append("evaluation-binding-mismatch")
    if report.get("contexts_sha256") != config.contexts_sha256:
        blockers.append("contexts-binding-mismatch")
    if report.get("heldout_task_ids_sha256") != _sha256_json(list(config.heldout_task_ids)):
        blockers.append("heldout-binding-mismatch")
    raw_contexts = report.get("contexts")
    if not isinstance(raw_contexts, list):
        raw_contexts = []
        blockers.append("missing-contexts")
    by_name = {
        str(item.get("name")): item for item in raw_contexts if isinstance(item, Mapping)
    }
    if len(by_name) != len(raw_contexts):
        blockers.append("duplicate-or-invalid-contexts")
    expected_names = {context.name for context in config.contexts}
    blockers.extend(f"unexpected-context:{name}" for name in sorted(set(by_name) - expected_names))
    context_results: list[dict[str, Any]] = []
    context_deltas: dict[str, list[float]] = {}
    total_cost = 0.0
    for context in config.contexts:
        raw = by_name.get(context.name)
        if raw is None:
            blockers.append(f"missing-context:{context.name}")
            continue
        if raw.get("kind") != context.kind:
            blockers.append(f"context-kind-mismatch:{context.name}")
        if raw.get("task_set_sha256") != context.task_set_sha256:
            blockers.append(f"task-set-mismatch:{context.name}")
        if raw.get("hard_failures"):
            blockers.append(f"hard-failure:{context.name}")
        pairs = raw.get("pairs")
        if not isinstance(pairs, list):
            blockers.append(f"missing-pairs:{context.name}")
            continue
        seen: set[str] = set()
        deltas: list[float] = []
        for pair in pairs:
            if not isinstance(pair, Mapping):
                blockers.append(f"invalid-pair:{context.name}")
                continue
            pair_id = str(pair.get("id", ""))
            if not pair_id or pair_id in seen:
                blockers.append(f"duplicate-or-missing-pair:{context.name}")
                continue
            seen.add(pair_id)
            if "baseline_status" not in pair or "candidate_status" not in pair:
                blockers.append(f"missing-pair-status:{context.name}:{pair_id}")
                continue
            if pair.get("baseline_status") != "scored" or pair.get("candidate_status") != "scored":
                blockers.append(f"unscored-pair:{context.name}:{pair_id}")
                continue
            try:
                baseline = float(pair.get("baseline"))
                candidate = float(pair.get("candidate"))
            except (TypeError, ValueError):
                blockers.append(f"invalid-score:{context.name}:{pair_id}")
                continue
            if not math.isfinite(baseline) or not math.isfinite(candidate):
                blockers.append(f"invalid-score:{context.name}:{pair_id}")
                continue
            deltas.append(candidate - baseline)
            raw_cost = pair.get("candidate_cost", 0)
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError):
                cost = math.nan
            if not math.isfinite(cost) or cost < 0:
                blockers.append(f"invalid-cost:{context.name}:{pair_id}")
            else:
                total_cost += cost
        if len(deltas) < config.policy.min_pairs_per_context:
            blockers.append(f"insufficient-pairs:{context.name}")
            continue
        mean_delta = fmean(deltas)
        lcb = _bootstrap_mean_lcb(
            deltas,
            confidence=config.policy.confidence,
            samples=config.policy.bootstrap_samples,
            seed=f"{candidate_revision}:{context.name}",
        )
        if lcb < -config.policy.epsilon:
            blockers.append(f"context-regression:{context.name}")
        context_deltas[context.name] = deltas
        context_results.append(
            {
                "name": context.name,
                "kind": context.kind,
                "pair_count": len(deltas),
                "mean_delta": mean_delta,
                "lcb": lcb,
                "weight": context.weight,
            }
        )
    aggregate_mean: float | None = None
    aggregate_lcb: float | None = None
    variance: float | None = None
    if len(context_deltas) == len(config.contexts):
        weight_total = sum(context.weight for context in config.contexts)
        normalized_weights = {
            context.name: context.weight / weight_total for context in config.contexts
        }
        aggregate_mean = sum(
            normalized_weights[name] * fmean(values)
            for name, values in context_deltas.items()
        )
        generator = random.Random(
            int(hashlib.sha256(candidate_revision.encode("ascii")).hexdigest()[:16], 16)
        )
        bootstrapped: list[float] = []
        for _ in range(config.policy.bootstrap_samples):
            bootstrapped.append(
                sum(
                    normalized_weights[name]
                    * fmean(generator.choice(values) for _ in values)
                    for name, values in context_deltas.items()
                )
            )
        aggregate_lcb = _quantile(bootstrapped, 1 - config.policy.confidence)
        all_deltas = [value for values in context_deltas.values() for value in values]
        variance = pvariance(all_deltas) if len(all_deltas) > 1 else 0.0
        if aggregate_lcb <= config.policy.delta:
            blockers.append("aggregate-lcb-below-delta")
    if report.get("hard_failures"):
        blockers.append("hard-validation-failure")
    raw_evidence = report.get("candidate_evidence") or []
    candidate_evidence: list[EvidenceRef] = []
    if isinstance(raw_evidence, list):
        declared_families = {
            task_id: family
            for context in config.contexts
            for task_id, family in zip(
                context.task_ids, context.workbook_families, strict=True
            )
        }
        for item in raw_evidence:
            if not isinstance(item, Mapping):
                blockers.append("invalid-candidate-evidence")
                continue
            try:
                reference = EvidenceRef.from_document(item)
            except HarnessError:
                blockers.append("invalid-candidate-evidence")
                continue
            declared_sha = item.get("sha256")
            if declared_sha is not None:
                actual_sha = _sha256_bytes(reference.path.read_bytes())
                if str(declared_sha) != actual_sha:
                    blockers.append("candidate-evidence-hash-mismatch")
                    continue
            declared = {task for context in config.contexts for task in context.task_ids}
            if (
                reference.task_id not in declared
                or reference.task_id in config.heldout_task_ids
                or declared_families.get(reference.task_id) != reference.workbook_family
            ):
                blockers.append("candidate-evidence-outside-development-split")
                continue
            candidate_evidence.append(reference)
    if not candidate_evidence:
        blockers.append("missing-candidate-evidence")
    unique_blockers = tuple(sorted(set(blockers)))
    return ValidationDecision(
        not unique_blockers,
        aggregate_mean,
        aggregate_lcb,
        tuple(context_results),
        variance,
        total_cost,
        unique_blockers,
        tuple(candidate_evidence),
    )


class RevisionStore:
    def __init__(self, workspace: str | Path) -> None:
        self.root = Path(workspace).expanduser().resolve()
        self.revisions = self.root / "revisions"
        self.candidates = self.root / "candidates"
        self.rounds = self.root / "rounds"
        self.state_path = self.root / "state.json"
        self.policy_path = self.root / "policy.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"

    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _event(self, event: str, payload: Mapping[str, Any]) -> None:
        row = {"event": event, "payload": dict(payload)}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def initialize(self, config: ContinuousEvolutionConfig, registry: PluginRegistry) -> dict[str, Any]:
        with self.lock() as lock_handle:
            del lock_handle
            if self.state_path.exists():
                state = self.load_state()
                if state.get("config_sha256") != config.sha256:
                    raise HarnessError("Existing evolution workspace uses a different frozen config")
                return state
            self.revisions.mkdir(parents=True, exist_ok=True)
            self.candidates.mkdir(parents=True, exist_ok=True)
            self.rounds.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".initial-", dir=self.revisions))
            artifact = staging / "artifact"
            (artifact / "src").mkdir(parents=True)
            shutil.copytree(
                config.repository_root / "src/spreadsheet_harness",
                artifact / "src/spreadsheet_harness",
            )
            shutil.copytree(config.repository_root / "skills", artifact / "skills")
            revision = self._finalize_revision(
                staging,
                parent_revision=None,
                composition=config.composition,
                registry=registry,
                mutation=None,
            )
            destination = self.revisions / revision["revision_sha256"]
            os.replace(staging, destination)
            _atomic_json(self.policy_path, config.to_dict())
            state = {
                "schema_version": "continuous-plugin-evolution-state-v1",
                "config_sha256": config.sha256,
                "status": "active",
                "attempted_rounds": 0,
                "accepted_rounds": 0,
                "next_group": config.first_group,
                "current_revision_sha256": revision["revision_sha256"],
                "history": [revision["revision_sha256"]],
                "rejected_candidates": [],
                "evidence": [item.to_dict() for item in config.initial_evidence],
            }
            _atomic_json(self.state_path, state)
            self._event("evolution.initialized", {"revision": revision["revision_sha256"]})
            return state

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            raise HarnessError("Evolution workspace is not initialized")
        return _read_json(self.state_path, label="evolution state")

    def add_evidence(
        self, config: ContinuousEvolutionConfig, evidence: Sequence[EvidenceRef]
    ) -> dict[str, Any]:
        """Append development trajectories and reopen an abstained workspace."""

        with self.lock() as lock_handle:
            del lock_handle
            state = self.load_state()
            if state.get("config_sha256") != config.sha256:
                raise HarnessError("Evolution config does not match the workspace")
            family_by_task = {
                task_id: family
                for context in config.contexts
                for task_id, family in zip(
                    context.task_ids, context.workbook_families, strict=True
                )
            }
            declared = {task_id for context in config.contexts for task_id in context.task_ids}
            for item in evidence:
                if (
                    item.task_id not in declared
                    or item.task_id in config.heldout_task_ids
                    or family_by_task.get(item.task_id) != item.workbook_family
                ):
                    raise HarnessError("Evidence must belong to the frozen development split")
            existing = {
                str(item.get("sha256"))
                for item in state.get("evidence", [])
                if isinstance(item, Mapping)
            }
            appended = [item for item in evidence if _sha256_bytes(item.path.read_bytes()) not in existing]
            state["evidence"] = [
                *state.get("evidence", []),
                *(item.to_dict() for item in appended),
            ]
            if state.get("status") == "stalled" and appended:
                state["status"] = "active"
            _atomic_json(self.state_path, state)
            self._event(
                "evolution.evidence.added",
                {"count": len(appended), "sha256": [item.to_dict()["sha256"] for item in appended]},
            )
            return state

    def revision_dir(self, revision_sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", revision_sha256):
            raise HarnessError("Invalid revision SHA-256")
        path = self.revisions / revision_sha256
        if not path.is_dir():
            raise HarnessError(f"Evolution revision is missing: {revision_sha256}")
        return path

    def load_composition(self, revision_sha256: str) -> CompositionSpec:
        document = _read_json(
            self.revision_dir(revision_sha256) / "composition.json",
            label="revision composition",
        )
        return _composition_from_document(document)

    def _finalize_revision(
        self,
        directory: Path,
        *,
        parent_revision: str | None,
        composition: CompositionSpec,
        registry: PluginRegistry,
        mutation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        resolved = registry.resolve(composition)
        execution_plan(resolved)
        composition_document = composition.to_dict()
        _atomic_json(directory / "composition.json", composition_document)
        artifact_manifest = _tree_manifest(directory / "artifact")
        revision_sha256 = _sha256_json(
            {
                "parent_revision_sha256": parent_revision,
                "composition": composition_document,
                "resolved_composition_sha256": resolved.sha256,
                "artifact_manifest": artifact_manifest,
            }
        )
        document = {
            "schema_version": "continuous-plugin-revision-v1",
            "revision_sha256": revision_sha256,
            "parent_revision_sha256": parent_revision,
            "composition": composition_document,
            "resolved_composition_sha256": resolved.sha256,
            "artifact_manifest_sha256": _sha256_json(artifact_manifest),
            "artifact_manifest": artifact_manifest,
            "mutation": dict(mutation) if mutation is not None else None,
        }
        _atomic_json(directory / "revision.json", document)
        return document

    def materialize_candidate(
        self,
        *,
        proposal: CandidateProposal,
        route: EvolutionRoute,
        incumbent_revision: str,
        registry: PluginRegistry,
        static_checks: Sequence[Sequence[str]],
        timeout: float,
    ) -> tuple[Path, dict[str, Any]]:
        if proposal.base_revision_sha256 != incumbent_revision:
            raise HarnessError("Proposal targets a stale incumbent revision")
        if proposal.operation != route.operation or proposal.target_plugin != route.target_plugin:
            raise HarnessError("Proposal changed the deterministic route coordinate")
        if proposal.surface != route.surface:
            raise HarnessError("Proposal changed the deterministic route surface")
        incumbent_dir = self.revision_dir(incumbent_revision)
        destination = self.candidates / f"r{self.load_state()['attempted_rounds'] + 1:06d}-{proposal.candidate_id}"
        if destination.exists():
            raise HarnessError(f"Candidate already exists: {destination.name}")
        staging = Path(tempfile.mkdtemp(prefix=f".{proposal.candidate_id}-", dir=self.candidates))
        try:
            shutil.copytree(incumbent_dir / "artifact", staging / "artifact")
            composition = self.load_composition(incumbent_revision)
            before = _tree_manifest(staging / "artifact")
            contract = registry.get(proposal.target_plugin)
            mutation_document: dict[str, Any]
            if proposal.operation == "edit":
                if proposal.target_plugin not in composition.plugins:
                    raise HarnessError("An edit proposal must target an active plugin")
                assert proposal.surface is not None
                policy = contract.edit_policy(proposal.surface)
                operator = proposal.operator or (
                    "bounded-config" if proposal.surface == "config" else "unified-diff"
                )
                payload_bytes = len((proposal.patch or "").encode("utf-8")) + sum(
                    len(content.encode("utf-8")) for _path, content in proposal.files
                )
                paths = tuple(path for path, _content in proposal.files)
                if proposal.patch:
                    paths = _patch_paths(proposal.patch)
                policy.validate_edit(
                    operator=operator, changed_paths=paths, patch_bytes=payload_bytes
                )
                if proposal.surface == "config":
                    if proposal.files or proposal.patch:
                        raise HarnessError("Config proposal may not contain file content")
                    contract.configure(proposal.config_patch)
                    overrides = {
                        name: dict(values) for name, values in composition.overrides.items()
                    }
                    overrides[contract.name] = {
                        **overrides.get(contract.name, {}),
                        **dict(proposal.config_patch),
                    }
                    composition = CompositionSpec.create(
                        composition.name, composition.plugins, overrides
                    )
                elif operator == "replace-file":
                    if proposal.patch or not proposal.files:
                        raise HarnessError("replace-file requires files and forbids patch")
                    for relative, content in proposal.files:
                        target = _contained_path(staging / "artifact", relative)
                        if not target.is_file():
                            raise HarnessError(f"replace-file target does not exist: {relative}")
                        target.write_text(content, encoding="utf-8")
                elif operator == "unified-diff":
                    if not proposal.patch or proposal.files:
                        raise HarnessError("unified-diff requires patch and forbids files")
                    _apply_patch(staging / "artifact", proposal.patch)
                else:
                    raise HarnessError(f"Unsupported file operator: {operator}")
                after = _tree_manifest(staging / "artifact")
                changed = tuple(
                    sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
                )
                if proposal.surface == "config" and changed:
                    raise HarnessError("Config proposal changed artifact files")
                if proposal.surface != "config" and set(changed) != set(paths):
                    raise HarnessError("Materialized file changes differ from the proposal")
                artifact_hash = _sha256_json(after)
                mutation = PluginMutation.create(
                    target_plugin=contract.name,
                    base_version=contract.version,
                    base_manifest_sha256=contract.manifest_sha256,
                    surface=proposal.surface,
                    candidate_artifact_sha256=artifact_hash,
                    changed_paths=changed,
                    evidence_sha256=route.evidence_sha256,
                    config_patch=proposal.config_patch or None,
                    operator=operator,
                )
                mutation.validate(registry)
                contract.edit_policy(proposal.surface).validate_edit(
                    operator=operator,
                    changed_paths=changed,
                    patch_bytes=payload_bytes,
                )
                mutation_document = mutation.to_dict()
            else:
                composition = _apply_composition_operation(proposal, composition, registry)
                if proposal.files or proposal.patch or proposal.config_patch:
                    raise HarnessError("Composition proposal may not edit artifact files or config")
                mutation_document = {
                    "schema_version": "plugevolve-composition-mutation-v1",
                    "operation": proposal.operation,
                    "target_plugin": proposal.target_plugin,
                    "replacement_plugin": proposal.replacement_plugin,
                    "evidence_sha256": list(route.evidence_sha256),
                }
            _run_static_checks(
                staging / "artifact", static_checks=static_checks, timeout=timeout
            )
            post_check = _tree_manifest(staging / "artifact")
            post_check_changed = tuple(
                sorted(
                    path
                    for path in set(before) | set(post_check)
                    if before.get(path) != post_check.get(path)
                )
            )
            if set(post_check_changed) != set(
                changed if proposal.operation == "edit" else ()
            ):
                raise HarnessError("Static checks changed files outside the candidate mutation")
            revision = self._finalize_revision(
                staging,
                parent_revision=incumbent_revision,
                composition=composition,
                registry=registry,
                mutation=mutation_document,
            )
            _atomic_json(staging / "proposal.json", proposal.to_dict(include_content=False))
            os.replace(staging, destination)
            return destination, revision
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def promote(
        self,
        *,
        candidate_dir: Path,
        revision: Mapping[str, Any],
        decision: ValidationDecision,
        route: EvolutionRoute,
        config: ContinuousEvolutionConfig,
    ) -> dict[str, Any]:
        revision_sha256 = str(revision["revision_sha256"])
        destination = self.revisions / revision_sha256
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix=f".{revision_sha256[:12]}-", dir=self.revisions))
            try:
                shutil.copytree(candidate_dir / "artifact", staging / "artifact")
                shutil.copy2(candidate_dir / "composition.json", staging / "composition.json")
                shutil.copy2(candidate_dir / "revision.json", staging / "revision.json")
                os.replace(staging, destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        state = self.load_state()
        if state["current_revision_sha256"] != revision.get("parent_revision_sha256"):
            raise HarnessError("Incumbent changed before candidate promotion")
        state["current_revision_sha256"] = revision_sha256
        state["accepted_rounds"] = int(state["accepted_rounds"]) + 1
        state["history"] = [*state.get("history", []), revision_sha256]
        state["next_group"] = "domain" if route.group == "harness" else "harness"
        state["evidence"] = [item.to_dict() for item in decision.candidate_evidence]
        if int(state["attempted_rounds"]) >= config.max_rounds:
            state["status"] = "complete"
        _atomic_json(self.state_path, state)
        self._event(
            "evolution.promoted",
            {"revision": revision_sha256, "route": route.to_dict(), "decision": decision.to_dict()},
        )
        return state

    def record_round(
        self,
        *,
        round_number: int,
        report: Mapping[str, Any],
        rejected: Sequence[str],
        max_rounds: int,
        status: str | None = None,
    ) -> dict[str, Any]:
        round_dir = self.rounds / f"{round_number:06d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(round_dir / "round.json", report)
        state = self.load_state()
        state["attempted_rounds"] = round_number
        state["rejected_candidates"] = [*state.get("rejected_candidates", []), *rejected]
        if status is not None:
            state["status"] = status
        elif round_number >= max_rounds:
            state["status"] = "complete"
        _atomic_json(self.state_path, state)
        self._event("evolution.round.completed", report)
        return state

    def freeze(self, config: ContinuousEvolutionConfig) -> dict[str, Any]:
        with self.lock() as lock_handle:
            del lock_handle
            state = self.load_state()
            if state.get("config_sha256") != config.sha256:
                raise HarnessError("Evolution config does not match the workspace")
            frozen = {
                "schema_version": "continuous-plugin-frozen-composition-v1",
                "config_sha256": config.sha256,
                "revision_sha256": state["current_revision_sha256"],
                "heldout_task_ids_sha256": _sha256_json(list(config.heldout_task_ids)),
                "accepted_rounds": state["accepted_rounds"],
            }
            _atomic_json(self.root / "frozen.json", frozen)
            state["status"] = "frozen"
            _atomic_json(self.state_path, state)
            self._event("evolution.frozen", frozen)
            return frozen

    def rollback(self) -> dict[str, Any]:
        with self.lock() as lock_handle:
            del lock_handle
            state = self.load_state()
            if state.get("status") == "frozen" or (self.root / "frozen.json").is_file():
                raise HarnessError("Frozen evolution workspaces cannot be rolled back")
            history = list(state.get("history", []))
            if len(history) < 2:
                raise HarnessError("No prior promoted revision is available")
            removed = history.pop()
            state["history"] = history
            state["current_revision_sha256"] = history[-1]
            state["accepted_rounds"] = max(0, int(state.get("accepted_rounds", 0)) - 1)
            state["status"] = "active"
            _atomic_json(self.state_path, state)
            self._event("evolution.rolled_back", {"from": removed, "to": history[-1]})
            return state


def _patch_paths(patch: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in patch.splitlines():
        match = _PATCH_HEADER.fullmatch(line)
        if match:
            left, right = match.groups()
            left_path = left[2:] if left.startswith("a/") else left
            right_path = right[2:] if right.startswith("b/") else right
            if left_path != right_path:
                raise HarnessError("Evolution patches may not rename files")
            _contained_path(Path("/artifact"), left_path)
            paths.append(left_path)
        if line.startswith("--- ") or line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0]
            if value == "/dev/null":
                raise HarnessError("Evolution patches may not add or delete files")
    if not paths or len(paths) != len(set(paths)):
        raise HarnessError("Evolution patch needs unique git diff headers")
    return tuple(paths)


def _apply_patch(artifact: Path, patch: str) -> None:
    _patch_paths(patch)
    patch_path = artifact.parent / "candidate.patch"
    patch_path.write_text(patch, encoding="utf-8")
    # Candidate artifacts live below the repository and therefore inherit the
    # repository's .git directory. Force git-apply to operate as a plain
    # path patch so it cannot silently apply to the incumbent checkout.
    patch_environment = dict(os.environ)
    patch_environment["GIT_DIR"] = os.devnull
    for check in (True, False):
        command = ["git", "apply", "--recount", "--whitespace=error-all"]
        if check:
            command.append("--check")
        command.append(str(patch_path))
        completed = subprocess.run(
            command,
            cwd=artifact,
            env=patch_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise HarnessError(f"Candidate patch rejected: {completed.stderr.strip()[:1000]}")


def _apply_composition_operation(
    proposal: CandidateProposal, composition: CompositionSpec, registry: PluginRegistry
) -> CompositionSpec:
    plugins = list(composition.plugins)
    overrides = {name: dict(values) for name, values in composition.overrides.items()}
    if proposal.operation == "enable":
        if proposal.target_plugin in plugins:
            raise HarnessError("Enable proposal targets an active plugin")
        registry.get(proposal.target_plugin)
        plugins.append(proposal.target_plugin)
    elif proposal.operation == "disable":
        if proposal.target_plugin not in plugins:
            raise HarnessError("Disable proposal targets an inactive plugin")
        plugins.remove(proposal.target_plugin)
        overrides.pop(proposal.target_plugin, None)
    elif proposal.operation == "replace":
        replacement = proposal.replacement_plugin
        if proposal.target_plugin not in plugins or not replacement:
            raise HarnessError("Replace proposal needs an active target and replacement")
        current_contract = registry.get(proposal.target_plugin)
        replacement_contract = registry.get(replacement)
        if (
            replacement in plugins
            or replacement_contract.kind != current_contract.kind
            or replacement_contract.provides != current_contract.provides
        ):
            raise HarnessError("Replacement plugin is not slot-compatible")
        plugins[plugins.index(proposal.target_plugin)] = replacement
        overrides.pop(proposal.target_plugin, None)
    else:
        raise HarnessError("Expected a composition operation")
    candidate = CompositionSpec.create(composition.name, plugins, overrides)
    execution_plan(registry.resolve(candidate))
    return candidate


def _run_static_checks(
    artifact: Path, *, static_checks: Sequence[Sequence[str]], timeout: float
) -> None:
    source = artifact / "src"
    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(source / "spreadsheet_harness")],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if compile_result.returncode:
        raise HarnessError(f"Candidate does not compile: {compile_result.stderr[:1000]}")
    values = {"artifact": str(artifact), "source": str(source), "skills": str(artifact / "skills")}
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    for command in static_checks:
        completed = subprocess.run(
            _expand_command(command, values),
            cwd=artifact,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode:
            raise HarnessError(
                "Candidate static check failed: "
                + (completed.stderr or completed.stdout)[-1000:]
            )


class ContinuousEvolutionEngine:
    def __init__(
        self,
        config: ContinuousEvolutionConfig,
        workspace: str | Path,
        *,
        proposer: ProposalAdapter | None = None,
        evaluator: EvaluationAdapter | None = None,
        router: DeterministicEvidenceRouter | None = None,
        registry: PluginRegistry | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or default_plugin_registry()
        self.store = RevisionStore(workspace)
        self.proposer = proposer or CommandProposalAdapter(
            config.proposer_command, config.command_timeout_seconds
        )
        self.evaluator = evaluator or CommandEvaluationAdapter(
            config.evaluator_command, config.command_timeout_seconds
        )
        self.router = router or DeterministicEvidenceRouter()

    def initialize(self) -> dict[str, Any]:
        return self.store.initialize(self.config, self.registry)

    def add_evidence(self, evidence: Sequence[EvidenceRef]) -> dict[str, Any]:
        return self.store.add_evidence(self.config, evidence)

    def _recover_pending_promotion(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Finish a promotion whose round record was durable before the pointer.

        A process may be terminated between ``record_round`` and ``promote``.
        The round report is deliberately sufficient to replay that final state
        transition, so resume never silently drops an accepted candidate.
        """

        if state.get("status") == "frozen":
            return dict(state)
        attempted = int(state.get("attempted_rounds", 0))
        accepted = int(state.get("accepted_rounds", 0))
        if attempted <= accepted or attempted < 1:
            return dict(state)
        report_path = self.store.rounds / f"{attempted:06d}" / "round.json"
        if not report_path.is_file():
            return dict(state)
        report = _read_json(report_path, label="evolution round")
        winner_sha = report.get("winner_revision_sha256")
        if report.get("outcome") != "promoted" or not isinstance(winner_sha, str):
            return dict(state)
        if str(state.get("current_revision_sha256")) != str(report.get("incumbent_revision_sha256")):
            raise HarnessError("Pending promotion incumbent does not match current state")
        candidate_dirs = sorted(self.store.candidates.glob(f"r{attempted:06d}-*"))
        candidate_dir: Path | None = None
        revision: dict[str, Any] | None = None
        for directory in candidate_dirs:
            revision_path = directory / "revision.json"
            if not revision_path.is_file():
                continue
            candidate_revision = _read_json(revision_path, label="candidate revision")
            if candidate_revision.get("revision_sha256") == winner_sha:
                candidate_dir, revision = directory, candidate_revision
                break
        if candidate_dir is None or revision is None:
            raise HarnessError("Pending promotion candidate artifacts are missing")
        validation_path = candidate_dir / "validation-report.json"
        if not validation_path.is_file():
            raise HarnessError("Pending promotion validation report is missing")
        decision = evaluate_validation_report(
            _read_json(validation_path, label="validation report"),
            config=self.config,
            incumbent_revision=str(state["current_revision_sha256"]),
            candidate_revision=str(winner_sha),
        )
        if not decision.promoted:
            raise HarnessError("Pending promotion no longer passes validation")
        route = EvolutionRoute.from_dict(report.get("route") or {})
        return self.store.promote(
            candidate_dir=candidate_dir,
            revision=revision,
            decision=decision,
            route=route,
            config=self.config,
        )

    def step(self) -> dict[str, Any]:
        with self.store.lock() as lock_handle:
            del lock_handle
            state = self.store.load_state()
            if state.get("config_sha256") != self.config.sha256:
                raise HarnessError("Evolution config does not match the initialized workspace")
            if self.store.root.joinpath("frozen.json").is_file() and state.get("status") != "frozen":
                # Freeze is written before the state pointer.  Treat the
                # durable seal as authoritative if a process died in between.
                state["status"] = "frozen"
                _atomic_json(self.store.state_path, state)
            recovered = self._recover_pending_promotion(state)
            if recovered != state:
                return recovered
            if state.get("status") != "active":
                return state
            round_number = int(state["attempted_rounds"]) + 1
            if round_number > self.config.max_rounds:
                state["status"] = "complete"
                _atomic_json(self.store.state_path, state)
                return state
            incumbent = str(state["current_revision_sha256"])
            composition = self.store.load_composition(incumbent)
            evidence = tuple(EvidenceRef.from_document(item) for item in state.get("evidence", []))
            route = self.router.route(
                evidence,
                registry=self.registry,
                composition=composition,
                groups=self.config.groups,
                preferred_group=state["next_group"],
            )
            round_dir = self.store.rounds / f"{round_number:06d}"
            if route is None:
                report = {
                    "schema_version": "continuous-plugin-evolution-round-v1",
                    "round": round_number,
                    "incumbent_revision_sha256": incumbent,
                    "route": None,
                    "outcome": "abstained",
                }
                return self.store.record_round(
                    round_number=round_number,
                    report=report,
                    rejected=(),
                    max_rounds=self.config.max_rounds,
                    status="stalled",
                )
            contract = self.registry.get(route.target_plugin)
            request = {
                "schema_version": "continuous-plugin-proposal-request-v1",
                "round": round_number,
                "base_revision_sha256": incumbent,
                "base_revision": _read_json(
                    self.store.revision_dir(incumbent) / "revision.json", label="base revision"
                ),
                "route": route.to_dict(),
                "plugin_contract": {
                    **contract.to_dict(),
                    "manifest_sha256": contract.manifest_sha256,
                },
                "evidence": [_safe_evidence_summary(item) for item in evidence],
                "rejected_candidate_sha256": state.get("rejected_candidates", []),
                "candidate_limit": self.config.max_candidates_per_round,
            }
            proposals = list(self.proposer.propose(request, round_dir))[
                : self.config.max_candidates_per_round
            ]
            evaluated: list[tuple[CandidateProposal, Path, dict[str, Any], ValidationDecision]] = []
            rejected: list[str] = []
            failures: list[dict[str, str]] = []
            for proposal in proposals:
                try:
                    candidate_dir, revision = self.store.materialize_candidate(
                        proposal=proposal,
                        route=route,
                        incumbent_revision=incumbent,
                        registry=self.registry,
                        static_checks=self.config.static_checks,
                        timeout=self.config.command_timeout_seconds,
                    )
                    evaluation_request = {
                        "schema_version": "continuous-plugin-validation-request-v1",
                        "round": round_number,
                        "incumbent_revision_sha256": incumbent,
                        "incumbent_directory": str(self.store.revision_dir(incumbent)),
                        "candidate_revision_sha256": revision["revision_sha256"],
                        "candidate_directory": str(candidate_dir),
                        "contexts": [context.to_dict() for context in self.config.contexts],
                        "evaluation_binding": dict(self.config.evaluation_binding),
                        "evaluation_binding_sha256": self.config.binding_sha256,
                        "contexts_sha256": self.config.contexts_sha256,
                        "heldout_task_ids_sha256": _sha256_json(
                            list(self.config.heldout_task_ids)
                        ),
                    }
                    validation_report = self.evaluator.evaluate(evaluation_request, candidate_dir)
                    _atomic_json(candidate_dir / "validation-report.json", validation_report)
                    decision = evaluate_validation_report(
                        validation_report,
                        config=self.config,
                        incumbent_revision=incumbent,
                        candidate_revision=str(revision["revision_sha256"]),
                    )
                    _atomic_json(candidate_dir / "decision.json", decision.to_dict())
                    if decision.promoted:
                        evaluated.append((proposal, candidate_dir, revision, decision))
                    else:
                        rejected.append(str(revision["revision_sha256"]))
                except (HarnessError, OSError, subprocess.SubprocessError, ValueError) as exc:
                    failures.append({"candidate_id": proposal.candidate_id, "error": str(exc)[:1000]})
            winner = None
            if evaluated:
                winner = min(
                    evaluated,
                    key=lambda item: (
                        -float(item[3].aggregate_lcb),
                        -float(item[3].aggregate_mean_delta),
                        float(item[3].variance),
                        item[3].cost,
                        str(item[2]["revision_sha256"]),
                    ),
                )
                rejected.extend(
                    str(item[2]["revision_sha256"]) for item in evaluated if item is not winner
                )
            round_report = {
                "schema_version": "continuous-plugin-evolution-round-v1",
                "round": round_number,
                "incumbent_revision_sha256": incumbent,
                "route": route.to_dict(),
                "proposal_count": len(proposals),
                "eligible_count": len(evaluated),
                "winner_revision_sha256": (
                    str(winner[2]["revision_sha256"]) if winner is not None else None
                ),
                "rejected_revision_sha256": sorted(set(rejected)),
                "candidate_failures": failures,
                "outcome": "promoted" if winner is not None else "rejected",
            }
            state = self.store.record_round(
                round_number=round_number,
                report=round_report,
                rejected=sorted(set(rejected)),
                max_rounds=self.config.max_rounds,
            )
            if winner is not None:
                state = self.store.promote(
                    candidate_dir=winner[1],
                    revision=winner[2],
                    decision=winner[3],
                    route=route,
                    config=self.config,
                )
            return state

    def run(self, *, rounds: int | None = None) -> dict[str, Any]:
        self.initialize()
        remaining = self.config.max_rounds if rounds is None else rounds
        if isinstance(remaining, bool) or remaining < 1:
            raise ValueError("rounds must be a positive integer")
        state = self.store.load_state()
        for _ in range(remaining):
            if state.get("status") != "active":
                break
            state = self.step()
        return state


__all__ = [
    "CandidateProposal",
    "CommandEvaluationAdapter",
    "CommandProposalAdapter",
    "ContinuousEvolutionConfig",
    "ContinuousEvolutionEngine",
    "DeterministicEvidenceRouter",
    "EvidenceRef",
    "EvolutionContext",
    "EvolutionRoute",
    "PromotionPolicy",
    "RevisionStore",
    "SpreadsheetBenchV2EvaluationAdapter",
    "ValidationDecision",
    "evaluate_validation_report",
]
