"""Evidence-gated coordinate evolution primitives; no provider/network calls.

This policy is immutable within an experiment. It separates observations from
attribution hypotheses, missing infrastructure from failed tasks, and smoke
results from promotion evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Any

from .continuous_evolution import _bootstrap_mean_lcb
from .evolution import TrajectoryEvidence, proposer_visible

GROUPS = {
    "spreadsheet-structure": "H",
    "spreadsheet-formula": "H",
    "spreadsheet-verification": "H",
    "spreadsheet-coordination": "H",
    "spreadsheet-financial-model": "D",
}
OPERATORS = {"revision", "recomposition", "synthesis"}


def _compact(value: Any, depth: int = 0) -> Any:
    """Hard context bound; timings never help explain a spreadsheet edit."""
    if isinstance(value, str):
        return value if len(value) <= 900 else value[:900] + " [truncated]"
    if depth >= 5:
        return "[nested detail omitted]"
    if isinstance(value, dict):
        excluded = {
            "request_timings",
            "attempt_history",
            "response_headers",
            "pacing",
            "input_item_count",
            "budget",
            "sandbox",
            "tool_names",
            "available_tool_names",
        }
        return {k: _compact(v, depth + 1) for k, v in list(value.items())[:24] if k not in excluded}
    if isinstance(value, (list, tuple)):
        return [_compact(v, depth + 1) for v in value[:8]]
    return value


def _compact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", {})
    name = event["event"]
    if name == "agent.started":
        instruction = str(payload.get("instruction", ""))
        match = re.search(r"<user_task>(.*?)</user_task>", instruction, re.S)
        payload = {
            "instruction": (match.group(1) if match else instruction)[:2500],
            "skills": [
                {"name": s["name"], "sha256": s.get("sha256")} for s in payload.get("skills", [])
            ],
            "stage": payload.get("stage"),
        }
    elif name.startswith("harness.planner_actions."):
        actions = payload.get("actions", [])
        # Keep representatives of EACH kind of action, not the first 8 copies
        # of a formula fill. Original full trace remains the audit source.
        chosen, seen = [], set()
        for action in actions:
            formula = str(action.get("expected_value", ""))
            pattern = re.sub(r"[A-Z]+(?=[0-9])", "COL", formula)
            key = (action.get("sheet"), action.get("action"), pattern)
            if key not in seen:
                chosen.append(action)
                seen.add(key)
        payload = {
            "count": payload.get("count"),
            "status": payload.get("status"),
            "actions": chosen[:8],
            "sampled_action_count": min(8, len(chosen)),
            "omitted_actions": len(actions) - min(8, len(chosen)),
            "verification_semantics": "persistence_only",
        }
    elif name == "agent.execution_failed":
        agent = payload.get("agent") or {}
        payload = {
            "reason": payload.get("reason"),
            "stage": payload.get("stage"),
            "turns": agent.get("turns"),
            "final_text": agent.get("final_text"),
        }
    elif name == "model.failed":
        error = payload.get("provider_error") or {}
        payload = {
            "stage": payload.get("stage"),
            "status_code": error.get("status_code"),
            "message": str(error.get("message", ""))[:500],
        }
    return {"event_index": event.get("event_index"), "event": name, "payload": _compact(payload)}


def evidence_packet(evidence: TrajectoryEvidence) -> dict[str, Any]:
    packet = evidence.for_prompt()
    # Build a deliberate evidence slice, not a recursively huge JSON dump.
    packet["decision_events"] = [
        _compact_event(e)
        for e in evidence.decision_events
        if not e["event"].endswith((".proposed", ".executed", ".verified"))
    ]
    task_events = [e for e in evidence.execution_context if e["event"] == "agent.started"]
    tool_events = [e for e in evidence.execution_context if e["event"] != "agent.started"]
    packet["execution_context"] = [_compact_event(e) for e in task_events + tool_events[-8:]]
    packet["tool_error_evidence"] = [
        {
            "event_index": e.get("event_index"),
            "tool": e["tool"],
            "arguments": _compact(e["arguments"]),
            "error_type": e.get("error_type"),
            "error": str(e.get("stderr_tail") or e["error"])[-1200:],
        }
        for e in evidence.tool_errors[:4]
    ]
    packet["success_evidence"] = []
    packet["failure_evidence"] = [
        _compact_event(e) for e in evidence.failures[-2:] if e["event"] != "agent.execution_failed"
    ]
    packet["evidence_id"] = evidence.sha256
    packet["classification"] = (
        "infrastructure"
        if evidence.diagnostics.get("provider_failures", 0)
        else "evaluated"
        if evidence.evaluator_outcome is not None
        else "unresolved"
    )
    packet["claims_policy"] = {
        "counts_cover_full_trace": True,
        "event_samples_are_not_exhaustive": True,
        "planner_verified_means_persisted_not_correct": True,
        "unknown_causes_must_remain_unknown": True,
        "prompt_projection": "compact-actions-exceptions-counts-v1",
    }
    packet = proposer_visible(packet)
    packet["evaluator_outcome"] = _compact(packet.get("evaluator_outcome"))
    # Variable-sized scripts can still exceed the cap. Drop redundant recent
    # reads first, never the planner action or the actual exception.
    removed = 0
    while len(json.dumps(packet, ensure_ascii=False)) > 24_000:
        disposable = next(
            (i for i, e in enumerate(packet["execution_context"]) if e["event"] != "agent.started"),
            None,
        )
        if disposable is None:
            break
        packet["execution_context"].pop(disposable)
        removed += 1
    packet["claims_policy"]["additional_recent_events_omitted"] = removed
    if len(json.dumps(packet, ensure_ascii=False)) > 30_000:
        raise ValueError("Evidence packet exceeds 30k character budget; refine projection")
    return packet


def route_options(
    packets: Sequence[Mapping[str, Any]], preferred: str = "H"
) -> list[dict[str, Any]]:
    """Fixed routing by observable mechanisms, never final-score keyword blame."""
    routes: dict[str, dict[str, Any]] = {}
    for p in packets:
        if p.get("classification") != "evaluated" or (p.get("evaluator_outcome") or {}).get(
            "passed"
        ):
            continue
        active = {
            s["name"]
            for e in p.get("execution_context", [])
            if e["event"] == "agent.started"
            for s in e["payload"].get("skills", [])
        }
        findings: list[tuple[str, str, list[int]]] = []
        errors = p.get("tool_error_evidence", [])
        if errors:
            findings.append(
                (
                    "spreadsheet-structure",
                    "tool_contract_or_grounding",
                    [e["event_index"] for e in errors if e.get("event_index")],
                )
            )
        writes = [
            e
            for e in p.get("decision_events", [])
            if e["event"] == "harness.planner_actions.applied"
        ]
        if writes:
            findings.append(
                (
                    "spreadsheet-financial-model",
                    "planned_formula_semantics",
                    [e["event_index"] for e in writes],
                )
            )
        d = p.get("diagnostics", {})
        if d.get("max_readonly_code_streak", 0) >= 5:
            findings.append(
                (
                    "spreadsheet-verification",
                    "inspection_without_progress",
                    [
                        e["event_index"]
                        for e in p.get("decision_events", [])
                        if e["event"]
                        in {"agent.execution_failed", "agent.read_only_code_deadline_rejected"}
                    ],
                )
            )
        for plugin, mechanism, indices in findings:
            if plugin not in active or not indices:
                continue
            r = routes.setdefault(
                plugin,
                {
                    "group": GROUPS[plugin],
                    "operator": "revision",
                    "plugin": plugin,
                    "evidence": [],
                    "causal_claim": False,
                },
            )
            r["evidence"].append(
                {"evidence_id": p["evidence_id"], "mechanism": mechanism, "event_indices": indices}
            )
    return sorted(
        routes.values(), key=lambda r: (r["group"] != preferred, -len(r["evidence"]), r["plugin"])
    )


def validate_hypothesis(
    raw: Mapping[str, Any], route: Mapping[str, Any], packets: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if raw.get("group") != route["group"] or raw.get("plugin") != route["plugin"]:
        raise ValueError("Hypothesis changed the routed coordinate")
    if raw.get("operator") != route["operator"] or raw.get("operator") not in OPERATORS:
        raise ValueError("Hypothesis changed the authorized operator")
    for key in ("observation", "suspected_cause", "intervention", "prediction", "falsification"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"Hypothesis requires {key}")
    allowed: dict[str, set[int]] = {}
    counts: dict[str, Mapping[str, Any]] = {}
    for packet in packets:
        if packet.get("classification") != "evaluated":
            continue
        identifier = packet["evidence_id"]
        allowed[identifier] = {
            e["event_index"]
            for key in ("decision_events", "execution_context", "tool_error_evidence")
            for e in packet.get(key, [])
            if isinstance(e.get("event_index"), int)
        }
        counts[identifier] = packet.get("diagnostics", {})
    citations = raw.get("citations")
    if not isinstance(citations, list) or not citations:
        raise ValueError("Hypothesis requires event citations")
    for c in citations:
        if (
            c.get("evidence_id") not in allowed
            or c.get("event_index") not in allowed[c["evidence_id"]]
        ):
            raise ValueError("Hypothesis cites unavailable evidence")
    routed = {(e["evidence_id"], i) for e in route.get("evidence", []) for i in e["event_indices"]}
    cited = {(c["evidence_id"], c["event_index"]) for c in citations}
    if routed and not routed & cited:
        raise ValueError(
            "Hypothesis must cite the routed failure mechanism, not unrelated symptoms"
        )
    # Structured claims catch the specific false reflection seen in our traces.
    for claim in raw.get("count_claims", []):
        actual = counts.get(claim.get("evidence_id"), {}).get(claim.get("field"))
        if actual is None or actual != claim.get("value"):
            raise ValueError("Hypothesis contradicts full-trace counters")
    checks = raw.get("prediction_checks", [])
    allowed_metrics = {
        "exact",
        "modification",
        "regression",
        "tool_errors",
        "max_readonly_code_streak",
        "code_changes",
        "planner_applied_writes",
    }
    for check in checks:
        if not isinstance(check, Mapping) or check.get("metric") not in allowed_metrics:
            raise ValueError("Unknown observable prediction metric")
        if check.get("relation") not in {"increase", "decrease", "not_decrease", "not_increase"}:
            raise ValueError("Unknown observable prediction relation")
    result = dict(raw)
    result["causal_status"] = "hypothesis_pending_controlled_test"
    result["id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:16]
    return result


def check_predictions(
    hypothesis: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    results = []
    for check in hypothesis.get("prediction_checks", []):
        metric, relation = check["metric"], check["relation"]
        b, c = before.get(metric), after.get(metric)
        if not isinstance(b, (float, int)) or not isinstance(c, (float, int)):
            outcome = None
        else:
            outcome = {
                "increase": c > b,
                "decrease": c < b,
                "not_decrease": c >= b,
                "not_increase": c <= b,
            }[relation]
        results.append({**check, "before": b, "after": c, "supported": outcome})
    return results


def patch_objections(patch: str) -> list[str]:
    """Reject known invalid guidance before costly execution; not a semantic proof."""
    objections = []
    patterns = {
        "depends-on-unavailable-evaluator": r"regression error|official evaluator|golden|ground.?truth|reference answer",
        "forces-edit-or-premature-submit": r"make (?:a |an )?(?:corrective |targeted )?edit or submit|require.{0,30}(?:an? edit|submission)",
        "assumes-nonzero-is-correct": r"(?:always|must|ensure|confirm).{0,70}(?:non.zero|positive results)",
        "planner-edits-incorrectly-excluded": r"unless.{0,45}code_interpreter edit",
        "case-identifiers-in-reusable-rule": r"\b[A-Z]{1,3}[1-9][0-9]*\b|answer\s*=|fina_",
    }
    for reason, pattern in patterns.items():
        if re.search(pattern, patch, re.I):
            objections.append(reason)
    return objections


def coordinate_breaches(
    before: Mapping[str, str],
    after: Mapping[str, str],
    plugin: str,
    group: str,
    operator: str = "revision",
) -> list[str]:
    permitted = f"{plugin}/SKILL.md"
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    breaches = []
    if GROUPS.get(plugin) != group:
        breaches.append("plugin-group-mismatch")
    if operator != "revision":
        breaches.append("operator-not-implemented-by-content-adapter")
    if changed != {permitted} or permitted not in before or permitted not in after:
        breaches.append("not-one-authorized-existing-plugin-edit")
    return breaches


@dataclass(frozen=True)
class GatePolicy:
    replay_margin: float = 0.0
    transfer_margin: float = 0.0
    regression_tolerance: float = 0.0
    confidence: float = 0.95
    bootstrap_samples: int = 4000
    min_families: int = 4

    def __post_init__(self):
        if any(
            not math.isfinite(v) or v < 0
            for v in (self.replay_margin, self.transfer_margin, self.regression_tolerance)
        ):
            raise ValueError("Gate margins must be finite and nonnegative")
        if not 0.5 < self.confidence < 1 or self.bootstrap_samples < 200 or self.min_families < 2:
            raise ValueError("Invalid confidence/sample/family policy")


def promotion_gate(
    contexts: Mapping[str, Sequence[Mapping[str, Any]]],
    expected: Mapping[str, Mapping[str, str]],
    policy: GatePolicy,
    breaches: Sequence[str] = (),
) -> dict[str, Any]:
    """Fixed tasks; family macro average and bootstrap. Missing != zero."""
    report: dict[str, Any] = {"status": "rejected", "contexts": {}, "breaches": list(breaches)}
    seen_families: set[str] = set()
    for context in ("replay", "transfer", "regression"):
        declared = expected.get(context, {})
        rows = list(contexts.get(context, []))
        ids = [r.get("task_id") for r in rows]
        families = set(declared.values())
        if seen_families & families:
            raise ValueError("Validation contexts overlap workbook families")
        seen_families |= families
        if not declared or len(ids) != len(set(ids)) or set(ids) != set(declared):
            report["contexts"][context] = {
                "status": "deferred",
                "reason": "missing-or-duplicate-tasks",
            }
            continue
        differences: dict[str, list[float]] = defaultdict(list)
        deferred = False
        for r in rows:
            if r.get("family") != declared[r["task_id"]]:
                raise ValueError("Family binding mismatch")
            if r.get("incumbent_status") != "scored" or r.get("candidate_status") != "scored":
                deferred = True
                break
            try:
                b, c = float(r["incumbent"]), float(r["candidate"])
            except (ValueError, TypeError, KeyError):
                deferred = True
                break
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in (b, c)):
                deferred = True
                break
            differences[r["family"]].append(c - b)
        if deferred or len(differences) < policy.min_families:
            report["contexts"][context] = {
                "status": "deferred",
                "reason": "unscored-or-insufficient-families",
            }
            continue
        deltas = [fmean(differences[f]) for f in sorted(differences)]
        lower = _bootstrap_mean_lcb(
            deltas,
            confidence=policy.confidence,
            samples=policy.bootstrap_samples,
            seed=f"method-v4:{context}",
        )
        threshold = {
            "replay": policy.replay_margin,
            "transfer": policy.transfer_margin,
            "regression": -policy.regression_tolerance,
        }[context]
        passed = lower >= threshold if context == "regression" else lower > threshold
        report["contexts"][context] = {
            "status": "pass" if passed else "fail",
            "family_count": len(deltas),
            "task_count": len(rows),
            "mean_delta": fmean(deltas),
            "lower_bound": lower,
            "threshold": threshold,
        }
    if breaches:
        report["status"] = "rejected_contract"
    elif any(c["status"] == "deferred" for c in report["contexts"].values()):
        report["status"] = "deferred"
    elif all(c["status"] == "pass" for c in report["contexts"].values()):
        report["status"] = "accepted"
    report["interpretation"] = "nominal adaptive-selection bounds; not an unseen-task guarantee"
    return report


def transition(
    state: Mapping[str, Any], decision: Mapping[str, Any], candidate: str
) -> dict[str, Any]:
    """Infrastructure/insufficiency does not consume a round or alter incumbent."""
    result = dict(state)
    if decision["status"] == "deferred":
        result["phase"] = "paused"
        return result
    result["completed_trials"] = result.get("completed_trials", 0) + 1
    if decision["status"] == "accepted":
        result["incumbent"] = candidate
    result["phase"] = "ready"
    return result
