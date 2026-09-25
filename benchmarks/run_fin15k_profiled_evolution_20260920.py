#!/usr/bin/env python3
"""Profile-aware Fin-1.5K plugin evolution pilot.

The existing Fin-1.5K baseline trajectories are reused as frozen development
evidence; no SpreadsheetBench result is read.  A target plugin is selected from
the plugin profile rather than letting raw failure support always choose the
same formula skill.  Each target gets its own immutable evolution workspace and
the normal replay/transfer/regression gate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import run_fin15k_scaling_evolution_20260919 as experiment
from spreadsheet_harness.continuous_evolution import (
    ContinuousEvolutionConfig,
    ContinuousEvolutionEngine,
    EvolutionRoute,
    EvidenceRef,
    RouteMutation,
    _choose_surface,
    _kernel_manifest_sha256,
)
from spreadsheet_harness.plugins import canonical_plugin_name, default_plugin_registry
from spreadsheet_harness.profile_evolution import ProfileGuidedEvolutionPlanner


REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "benchmarks/results/fin15k-scaling-coevolution-20260919"
DEFAULT_ROOT = REPO / "benchmarks/results/fin15k-plugin-profile-evolution-20260920"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ProfileTargetRouter:
    """Route one controlled round to a selected active plugin."""

    def __init__(self, target: str, profile_ledger: Path):
        self.target = canonical_plugin_name(target)
        self.by_task: dict[str, dict[str, Any]] = {}
        with profile_ledger.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("task_id"):
                    self.by_task[str(row["task_id"])] = row

    def route(
        self,
        evidence: Sequence[EvidenceRef],
        *,
        registry: Any,
        composition: Any,
        groups: Mapping[str, tuple[str, ...]],
        preferred_group: str | None = None,
        allowed_operators: Sequence[str] = ("revision", "recomposition", "synthesis"),
        allowed_update_scopes: Sequence[str] = ("harness", "domain", "joint"),
    ) -> EvolutionRoute | None:
        resolved = registry.resolve(composition)
        active = {plugin.contract.name for plugin in resolved.plugins}
        if self.target not in active:
            raise RuntimeError(f"Profile target is not active in the composition: {self.target}")
        group = next(
            (name for name, values in groups.items() if self.target in values),
            None,
        )
        if group not in set(allowed_update_scopes) and "joint" not in set(allowed_update_scopes):
            return None
        contract = registry.get(self.target)
        target_refs = [
            item
            for item in evidence
            if any(
                canonical_plugin_name(str(name)) == self.target and int(value or 0) > 0
                for name, value in (self.by_task.get(item.task_id, {}).get("invoked") or {}).items()
            )
        ]
        if not target_refs:
            target_refs = list(evidence)
        support = len(target_refs)
        preferred_surfaces = {
            "verify-formula-runtime": "config",
            "observe-profile-compact": "config",
            "knowledge-financial-model": "prompt",
            "knowledge-formula": "prompt",
            "knowledge-verification": "prompt",
            "knowledge-manipulation": "prompt",
            "knowledge-analysis": "prompt",
        }
        preferred = preferred_surfaces.get(self.target)
        surface = (
            preferred
            if preferred in contract.evolvable_surfaces
            else _choose_surface(contract, "profile-targeted evidence")
        )
        if surface is None:
            return None
        hashes = tuple(sorted({experiment.digest(item.path) for item in target_refs}))
        mutation = RouteMutation(
            group,  # type: ignore[arg-type]
            "edit",
            self.target,
            surface,
            hashes,
            support,
            ("profile-targeted-plugin", "runtime-usage", "fin15k-development-only"),
            None,
        )
        return EvolutionRoute(
            mutation.group,
            mutation.operation,
            mutation.target_plugin,
            mutation.surface,
            mutation.evidence_sha256,
            mutation.support_count,
            mutation.reasons,
            None,
            group,  # type: ignore[arg-type]
            (mutation,),
        )


class ProfileMechanismRouter(ProfileTargetRouter):
    """Select an H-only, D-only, or joint route from Fin-1.5K plugin profiles."""

    def __init__(self, profile_path: Path, profile_ledger: Path, mechanism: str):
        profile = load(profile_path)
        self.mechanism = mechanism
        self.planner = ProfileGuidedEvolutionPlanner(profile)
        self.profile_rows = tuple(profile.get("profiles") or ())
        super().__init__("knowledge-financial-model", profile_ledger)

    def _row_for(self, plugin: str) -> Mapping[str, Any]:
        canonical = canonical_plugin_name(plugin)
        return next(
            (
                row
                for row in self.profile_rows
                if canonical_plugin_name(str(row.get("plugin", ""))) == canonical
            ),
            {},
        )

    def _refs_for(
        self, evidence: Sequence[EvidenceRef], plugin: str
    ) -> list[EvidenceRef]:
        canonical = canonical_plugin_name(plugin)
        refs = []
        for item in evidence:
            invoked = self.by_task.get(item.task_id, {}).get("invoked") or {}
            if any(
                canonical_plugin_name(str(name)) == canonical and int(value or 0) > 0
                for name, value in invoked.items()
            ):
                refs.append(item)
        return refs or list(evidence)

    def _surface(self, registry: Any, plugin: str) -> str | None:
        contract = registry.get(plugin)
        preferred = {
            "verify-formula-runtime": "config",
            "observe-profile-compact": "config",
            "knowledge-financial-model": "prompt",
            "knowledge-formula": "prompt",
            "knowledge-verification": "prompt",
            "knowledge-manipulation": "prompt",
            "knowledge-analysis": "prompt",
        }.get(plugin)
        return (
            preferred
            if preferred in contract.evolvable_surfaces
            else _choose_surface(contract, "profile-mechanism")
        )

    def route(
        self,
        evidence: Sequence[EvidenceRef],
        *,
        registry: Any,
        composition: Any,
        groups: Mapping[str, tuple[str, ...]],
        preferred_group: str | None = None,
        allowed_operators: Sequence[str] = ("revision", "recomposition", "synthesis"),
        allowed_update_scopes: Sequence[str] = ("harness", "domain", "joint"),
    ) -> EvolutionRoute | None:
        if self.mechanism not in {"h-only", "d-only", "joint"}:
            raise ValueError(f"Unknown profile evolution mechanism: {self.mechanism}")
        resolved = registry.resolve(composition)
        active = {plugin.contract.name for plugin in resolved.plugins}
        canonical_groups = {
            name: tuple(canonical_plugin_name(value) for value in values)
            for name, values in groups.items()
        }
        hashes = tuple(sorted({experiment.digest(item.path) for item in evidence}))

        if self.mechanism == "joint":
            coordination = "knowledge-coordination"
            domain = "knowledge-financial-model"
            if coordination not in canonical_groups.get("harness", ()):
                return None
            if domain not in active or domain not in canonical_groups.get("domain", ()):
                return None
            domain_refs = self._refs_for(evidence, domain)
            domain_hashes = tuple(sorted({experiment.digest(item.path) for item in domain_refs}))
            mutations = (
                RouteMutation(
                    "harness",
                    "synthesize",
                    coordination,
                    "prompt",
                    hashes,
                    len(evidence),
                    ("profile-guided-joint", "multi-plugin-handoff", "fin15k-development-only"),
                    None,
                ),
                RouteMutation(
                    "domain",
                    "edit",
                    domain,
                    self._surface(registry, domain),
                    domain_hashes,
                    len(domain_refs),
                    ("profile-guided-joint", "domain-plugin-profile", "fin15k-development-only"),
                    None,
                ),
            )
            return EvolutionRoute(
                "harness",
                "synthesize",
                coordination,
                "prompt",
                hashes,
                len(evidence),
                mutations[0].reasons,
                None,
                "joint",
                mutations,
            )

        group = "harness" if self.mechanism == "h-only" else "domain"
        if group not in set(allowed_update_scopes):
            return None
        candidates = [
            row
            for row in self.profile_rows
            if row.get("group") == group
            and canonical_plugin_name(str(row.get("plugin", ""))) in active
            and self._surface(registry, canonical_plugin_name(str(row.get("plugin", ""))))
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda row: (
                -(float(row.get("evidence_family_count") or 0)),
                -abs(float(row.get("descriptive_score_delta") or 0)),
                str(row.get("plugin", "")),
            )
        )
        target = canonical_plugin_name(str(candidates[0]["plugin"]))
        operation = "edit"
        replacement = None
        if self.mechanism == "h-only":
            dormant = [
                row
                for row in candidates
                if int(row.get("invoked_tasks") or 0) == 0
                and canonical_plugin_name(str(row.get("plugin", ""))) in active
            ]
            if dormant:
                target = canonical_plugin_name(str(dormant[0]["plugin"]))
                operation = "disable"
        # A profile truncation signal is a composition/recomposition action,
        # not another prompt edit.  The full profile plugin is slot-compatible.
        if self.mechanism == "h-only" and "observe-profile-compact" in active:
            truncations = 0
            for item in evidence:
                try:
                    for line in item.path.read_text(encoding="utf-8").splitlines():
                        row = json.loads(line)
                        if row.get("event") == "preprocess.profile":
                            trunc = (row.get("payload") or {}).get("truncation") or {}
                            truncations += int(any(bool(value) for value in trunc.values()))
                except (OSError, json.JSONDecodeError):
                    continue
            if truncations >= 3 and "observe-profile-full" in canonical_groups.get("harness", ()):
                target = "observe-profile-compact"
                operation = "replace"
                replacement = "observe-profile-full"
        refs = self._refs_for(evidence, target)
        target_hashes = tuple(sorted({experiment.digest(item.path) for item in refs}))
        mutation = RouteMutation(
            group,  # type: ignore[arg-type]
            operation,  # type: ignore[arg-type]
            target,
            None if operation in {"replace", "disable"} else self._surface(registry, target),
            target_hashes,
            len(refs),
            (f"profile-guided-{self.mechanism}", "fin15k-development-only"),
            replacement,
        )
        return EvolutionRoute(
            mutation.group,
            mutation.operation,
            mutation.target_plugin,
            mutation.surface,
            mutation.evidence_sha256,
            mutation.support_count,
            mutation.reasons,
            mutation.replacement_plugin,
            mutation.group,
            (mutation,),
        )


def build_config(
    source_root: Path,
    output_root: Path,
    *,
    size: int,
    target: str | None = None,
    mechanism: str | None = None,
) -> Path:
    if mechanism not in {"h-only", "d-only", "joint"}:
        if not target:
            raise ValueError("target or mechanism is required")
        group = "domain" if canonical_plugin_name(target) == "knowledge-financial-model" else "harness"
        source_scope = "domain-only" if group == "domain" else "general-only"
    else:
        group = "domain" if mechanism == "d-only" else "harness"
        source_scope = {
            "h-only": "general-only",
            "d-only": "domain-only",
            "joint": "coevolution",
        }[mechanism]
    source_config = source_root / "configs" / f"fin15k-{size}-{source_scope}.json"
    if not source_config.is_file():
        # A formal new protocol root has a frozen split/protocol but no
        # legacy evolution configs yet.  Build the current config from its
        # own fresh baseline trajectories rather than borrowing old-run paths.
        source_config = experiment.build_config(source_root, size, source_scope)
    raw = load(source_config)
    raw["max_rounds"] = 1
    raw["max_candidates_per_round"] = 1
    raw["allowed_update_scopes"] = (
        ["harness", "domain", "joint"] if mechanism == "joint" else [group]
    )
    # This is a new profiled protocol.  Bind it to the live kernel rather than
    # silently reusing the older scaling protocol's source hash.
    raw["kernel_manifest_sha256"] = _kernel_manifest_sha256(
        REPO, default_plugin_registry()
    )
    binding = dict(raw.get("evaluation_binding") or {})
    action_label = mechanism or canonical_plugin_name(str(target))
    binding["incumbent_cache_root"] = str(
        output_root / "selection-incumbent-cache" / f"fin15k-{size}-{action_label}"
    )
    if mechanism:
        binding["evolution_mechanism"] = mechanism
    raw["evaluation_binding"] = binding
    profile_path = output_root / "profile-v2/plugin-profile.json"
    if profile_path.is_file():
        raw["plugin_profile_path"] = str(profile_path)
    path = output_root / "configs" / f"fin15k-{size}-{action_label}.json"
    atomic_json(path, raw)
    # Force validation now, before any model/evaluator process is launched.
    ContinuousEvolutionConfig.load(path)
    return path


def run_one(
    source_root: Path,
    output_root: Path,
    *,
    size: int,
    target: str | None = None,
    mechanism: str | None = None,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    profile_ledger = output_root / "profile-v2/plugin-task-ledger.jsonl"
    config_path = build_config(
        source_root,
        output_root,
        size=size,
        target=target,
        mechanism=mechanism,
    )
    config = ContinuousEvolutionConfig.load(config_path)
    action_label = mechanism or canonical_plugin_name(str(target))
    workspace = output_root / "workspaces" / f"fin15k-{size}-{action_label}"
    router = (
        ProfileMechanismRouter(
            output_root / "profile-v2/plugin-profile.json",
            profile_ledger,
            mechanism,
        )
        if mechanism
        else ProfileTargetRouter(str(target), profile_ledger)
    )
    engine = ContinuousEvolutionEngine(config, workspace, router=router)
    state = engine.run(rounds=1)
    frozen = engine.freeze(config)
    result = {
        "size": size,
        "target": target,
        "mechanism": mechanism,
        "status": state.get("status"),
        "attempted_rounds": state.get("attempted_rounds", 0),
        "accepted_rounds": state.get("accepted_rounds", 0),
        "initial_revision_sha256": state.get("history", [None])[0],
        "frozen_revision_sha256": frozen.get("revision_sha256"),
        "workspace": str(workspace),
        "config": str(config_path),
        "state": state,
        "frozen": frozen,
        "source_evidence_root": str(source_root),
        "spreadsheetbench_used": False,
    }
    atomic_json(output_root / "cells" / f"fin15k-{size}-{action_label}.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--size", type=int, choices=(50, 200, 500), default=50)
    parser.add_argument("--target")
    parser.add_argument("--mechanism", choices=("h-only", "d-only", "joint"))
    args = parser.parse_args()
    result = run_one(
        args.source_root.expanduser().resolve(),
        args.root.expanduser().resolve(),
        size=args.size,
        target=args.target,
        mechanism=args.mechanism,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
