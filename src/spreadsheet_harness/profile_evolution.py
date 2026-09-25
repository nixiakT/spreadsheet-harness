"""Profile-guided H/D/joint evolution planning primitives.

The planner is intentionally model-agnostic.  A GLM-5.2 adapter may turn the
returned action candidates into proposals, while the controller still checks
plugin contracts and runs the Fin-1.5K promotion gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

EvolutionMechanism = Literal["h-only", "d-only", "joint"]


@dataclass(frozen=True)
class PluginAction:
    mechanism: EvolutionMechanism
    operation: Literal["edit", "disable", "replace", "synthesize", "joint"]
    target_plugins: tuple[str, ...]
    replacement_plugins: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    reason: str = ""
    priority: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "operation": self.operation,
            "target_plugins": list(self.target_plugins),
            "replacement_plugins": list(self.replacement_plugins),
            "evidence": list(self.evidence),
            "reason": self.reason,
            "priority": self.priority,
        }


class ProfileGuidedEvolutionPlanner:
    """Create bounded action candidates from plugin profiles and ledgers."""

    def __init__(self, profile: Mapping[str, Any]) -> None:
        self.profile = profile
        self.rows = tuple(profile.get("profiles") or ())

    @classmethod
    def load(cls, path: str | Path) -> "ProfileGuidedEvolutionPlanner":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _ranked(self, group: str) -> list[Mapping[str, Any]]:
        rows = [row for row in self.rows if row.get("group") == group]
        return sorted(
            rows,
            key=lambda row: (
                -(float(row.get("evidence_family_count") or 0)),
                -(float(row.get("invoked_tasks") or 0)),
                -abs(float(row.get("descriptive_score_delta") or 0)),
                str(row.get("plugin", "")),
            ),
        )

    def plan(self, mechanism: EvolutionMechanism, *, max_actions: int = 4) -> list[PluginAction]:
        if mechanism not in {"h-only", "d-only", "joint"}:
            raise ValueError(f"unknown evolution mechanism: {mechanism}")
        actions: list[PluginAction] = []
        if mechanism in {"h-only", "joint"}:
            for row in self._ranked("harness"):
                plugin = str(row.get("plugin", ""))
                if not plugin or not float(row.get("invoked_tasks") or 0):
                    continue
                actions.append(
                    PluginAction(
                        mechanism,
                        "edit",
                        (plugin,),
                        evidence=(f"families:{row.get('evidence_family_count', 0)}",),
                        reason="profile-ranked harness plugin with reusable Fin-1.5K evidence",
                        priority=float(row.get("evidence_family_count") or 0),
                    )
                )
                if len(actions) >= max_actions and mechanism == "h-only":
                    break
        if mechanism in {"d-only", "joint"}:
            for row in self._ranked("domain"):
                plugin = str(row.get("plugin", ""))
                if not plugin:
                    continue
                actions.append(
                    PluginAction(
                        mechanism,
                        "edit",
                        (plugin,),
                        evidence=(f"families:{row.get('evidence_family_count', 0)}",),
                        reason="profile-ranked domain plugin with reusable Fin-1.5K evidence",
                        priority=float(row.get("evidence_family_count") or 0),
                    )
                )
                if len(actions) >= max_actions and mechanism == "d-only":
                    break
        if mechanism == "joint":
            # Explicit interface evidence is represented by a coordination
            # synthesis plus one active domain edit.  The controller will still
            # reject it unless the contract and family-disjoint gate allow it.
            domain = next(iter(self._ranked("domain")), None)
            if domain:
                actions.append(
                    PluginAction(
                        "joint",
                        "joint",
                        ("knowledge-coordination", str(domain.get("plugin", ""))),
                        evidence=("multi-plugin-handoff", "local-validation-then-failure"),
                        reason="synthesize coordination skill and co-evolve the active domain plugin",
                        priority=1.0,
                    )
                )
        return sorted(actions, key=lambda item: -item.priority)[:max_actions]


__all__ = ["EvolutionMechanism", "PluginAction", "ProfileGuidedEvolutionPlanner"]
