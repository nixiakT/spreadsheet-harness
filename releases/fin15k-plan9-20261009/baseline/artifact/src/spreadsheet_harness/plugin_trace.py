"""Unified per-plugin runtime trace and outcome ledger.

The ordinary trajectory is an event journal for the whole harness.  This
module adds a deliberately smaller plugin-facing envelope beside it.  It
normalizes loaded/selected/invoked/attributed/outcome signals, keeps counts
per plugin, and records one final outcome for every plugin that participated in
the run.  Evolution planners consume this sidecar instead of reverse
engineering plugin usage from arbitrary harness events.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .plugins import canonical_plugin_name

SCHEMA_VERSION = "plugin-trace-v1"

_VALIDATION_EVENTS = {
    "agent.formula_runtime_validation_passed": ("verification.formula-runtime", "success"),
    "agent.formula_runtime_validation_failed": ("verification.formula-runtime", "failure"),
    "agent.formula_runtime_validation_incomplete": (
        "verification.formula-runtime",
        "incomplete",
    ),
}


def candidate_hook_is_infrastructure(payload: Mapping[str, Any]) -> bool:
    """Classify controller/runtime setup failures, never candidate prose.

    New runtime events carry a typed category. The prefix fallback supports
    early journals written before that category existed; it recognizes only
    the host exception type, not arbitrary candidate error/result content.
    """
    category = str(payload.get("error_category", "")).replace("_", "-")
    if category in {"code-isolation-infrastructure", "candidate-plugin-infrastructure"}:
        return True
    error = payload.get("error")
    return category == "" and isinstance(error, str) and error.startswith("CodeIsolationError:")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _primitive(value: Any, *, limit: int = 160) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:limit]
    return str(value)[:limit]


def _skill_plugin(name: str) -> str:
    if name == "visual-review":
        return canonical_plugin_name("skill-spreadsheet-visualization")
    return canonical_plugin_name(f"skill-{name}")


class PluginTraceLedger:
    """Append normalized plugin events and maintain a resumable run profile."""

    def __init__(
        self,
        trajectory_path: Path,
        run_id: str,
        *,
        write_sidecar: bool = True,
    ) -> None:
        self.trajectory_path = Path(trajectory_path)
        self.run_id = run_id
        self.write_sidecar = write_sidecar
        self.events_path = self.trajectory_path.with_name("plugin-trace.jsonl")
        self.profile_path = self.trajectory_path.with_name("plugin-profile.json")
        self._lock = threading.RLock()
        self._sequence = 0
        self._plugins: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._invoked: set[str] = set()
        self._providers: dict[str, str] = {}
        self._composition_sha256: str | None = None
        self._run_outcome: str | None = None
        self._run_outcome_kind: str | None = None
        self._score: dict[str, Any] | None = None
        self._event_count = 0
        self._failure_reasons: Counter[str] = Counter()
        self._attributed_plugins: Counter[str] = Counter()
        self._hard_failure = False

    def _trace_weight(self) -> tuple[float, dict[str, float]]:
        """Return an auditable learning weight for this trajectory.

        Scored failures are emphasized, successful anchors retain unit weight,
        and infrastructure/unscored traces remain diagnostic but contribute no
        learning weight.  Severity is based only on normalized event classes,
        never on hidden answers or raw evaluator text.
        """

        outcome = self._run_outcome
        base = {
            "success": 1.0,
            "failure": 2.0,
            "infrastructure": 0.0,
            "unscored": 0.0,
        }.get(outcome or "unknown", 0.5)
        severe_reasons = {
            "tool_failure",
            "execution_failure",
            "formula_validation_failure",
            "hard_failure",
            "candidate_hook_failure",
        }
        severity = 1.5 if any(reason in severe_reasons for reason in self._failure_reasons) else 1.0
        attribution = 1.25 if self._attributed_plugins else 1.0
        components = {
            "outcome_base": base,
            "failure_severity": severity,
            "attribution_focus": attribution,
        }
        return base * severity * attribution, components

    def _entry(self, plugin: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        name = canonical_plugin_name(plugin)
        entry = self._plugins.setdefault(
            name,
            {
                "plugin": name,
                "version": None,
                "manifest_sha256": None,
                "kind": None,
                "loaded": False,
                "load_count": 0,
                "selected_count": 0,
                "invocation_count": 0,
                "attribution_count": 0,
                "outcome_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "infrastructure_count": 0,
                "unscored_count": 0,
                "signals": {},
                "operations": {},
                "outcomes": {},
            },
        )
        if details:
            for key in ("version", "manifest_sha256", "kind"):
                if details.get(key) is not None:
                    entry[key] = _primitive(details[key], limit=200)
        return entry

    def _bump(self, plugin: str, field: str, *, signal: str | None = None) -> None:
        entry = self._entry(plugin)
        entry[field] = int(entry.get(field, 0)) + 1
        if signal:
            signals = entry.setdefault("signals", {})
            signals[signal] = int(signals.get(signal, 0)) + 1

    def _append(self, record: Mapping[str, Any]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _write_profile(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "trajectory": str(self.trajectory_path),
            "composition_sha256": self._composition_sha256,
            "providers": dict(sorted(self._providers.items())),
            "active_plugins": sorted(self._active),
            "invoked_plugins": sorted(self._invoked),
            "event_count": self._event_count,
            "outcome": self._run_outcome,
            "outcome_kind": self._run_outcome_kind,
            "score": self._score,
            "trace_weight": self._trace_weight()[0],
            "weight_components": self._trace_weight()[1],
            "failure_reasons": dict(sorted(self._failure_reasons.items())),
            "attributed_plugins": dict(sorted(self._attributed_plugins.items())),
            "plugins": [self._plugins[name] for name in sorted(self._plugins)],
            "updated_at": _now(),
        }
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.profile_path.name}.", dir=self.profile_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.profile_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def snapshot(self) -> dict[str, Any]:
        """Return the current profile with volatile timestamps omitted."""

        weight, components = self._trace_weight()
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "trajectory": str(self.trajectory_path),
            "composition_sha256": self._composition_sha256,
            "providers": dict(sorted(self._providers.items())),
            "active_plugins": sorted(self._active),
            "invoked_plugins": sorted(self._invoked),
            "event_count": self._event_count,
            "outcome": self._run_outcome,
            "outcome_kind": self._run_outcome_kind,
            "score": self._score,
            "trace_weight": weight,
            "weight_components": components,
            "failure_reasons": dict(sorted(self._failure_reasons.items())),
            "attributed_plugins": dict(sorted(self._attributed_plugins.items())),
            "plugins": [self._plugins[name] for name in sorted(self._plugins)],
        }

    @classmethod
    def replay_rows(
        cls,
        trajectory_path: Path,
        rows: list[Mapping[str, Any]],
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Replay an old trajectory through the exact native normalizer.

        ``write_sidecar=False`` keeps historical benchmark directories
        untouched.  The resulting profile has the same counters and plugin
        names as a run that emitted the sidecar live.
        """

        resolved_run_id = run_id or next(
            (
                str(row.get("run_id"))
                for row in rows
                if isinstance(row, Mapping) and row.get("run_id")
            ),
            trajectory_path.stem,
        )
        ledger = cls(trajectory_path, resolved_run_id, write_sidecar=False)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            event = row.get("event")
            if isinstance(event, str):
                payload = row.get("payload")
                ledger.observe(event, payload if isinstance(payload, Mapping) else {})
        return ledger.snapshot()

    def _emit(
        self,
        plugin: str,
        *,
        kind: str,
        signal: str,
        source_event: str,
        payload: Mapping[str, Any],
        success: bool | None = None,
        outcome_kind: str | None = None,
    ) -> None:
        name = canonical_plugin_name(plugin)
        entry = self._entry(name)
        if kind == "loaded":
            entry["loaded"] = True
            self._bump(name, "load_count", signal=signal)
            self._active.add(name)
        elif kind == "selected":
            self._bump(name, "selected_count", signal=signal)
            self._bump(name, "invocation_count")
            self._invoked.add(name)
        elif kind == "invoked":
            self._bump(name, "invocation_count", signal=signal)
            self._invoked.add(name)
        elif kind == "attributed":
            self._bump(name, "attribution_count", signal=signal)
        elif kind == "outcome":
            self._bump(name, "outcome_count", signal=signal)
            outcomes = entry.setdefault("outcomes", {})
            label = outcome_kind or ("success" if success else "failure")
            outcomes[label] = int(outcomes.get(label, 0)) + 1
            if label == "success":
                self._bump(name, "success_count")
            elif label in {"failure", "incomplete"}:
                self._bump(name, "failure_count")
            elif label == "infrastructure":
                self._bump(name, "infrastructure_count")
            elif label == "unscored":
                self._bump(name, "unscored_count")
        operation = payload.get("operation")
        if isinstance(operation, str):
            operations = entry.setdefault("operations", {})
            operations[operation] = int(operations.get(operation, 0)) + 1
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "sequence": self._sequence,
            "timestamp": _now(),
            "event": "plugin.trace",
            "plugin": name,
            "kind": kind,
            "signal": signal,
            "source_event": source_event,
            "phase": _primitive(payload.get("stage") or payload.get("phase")),
            "task_id": _primitive(payload.get("task_id")),
            "category": _primitive(payload.get("task_category") or payload.get("category")),
            "operation": _primitive(operation),
            "success": success,
            "outcome_kind": outcome_kind,
            "score": payload.get("official_score") if isinstance(payload.get("official_score"), Mapping) else None,
        }
        if self.write_sidecar:
            self._append(record)

    def _provider(self, signal: str) -> str | None:
        value = self._providers.get(signal)
        return canonical_plugin_name(value) if value else None

    def _outcome(self, event: str, payload: Mapping[str, Any]) -> None:
        if event not in {
            "spreadsheetbench_v2.evaluated",
            "spreadsheetbench_v1.evaluated",
            "benchmark.evaluated",
            "evaluation.completed",
            "evaluation.failed",
        }:
            return
        passed = payload.get("passed")
        outcome_kind = str(payload.get("outcome_kind") or "")
        if passed is True:
            label = "success"
        elif self._failure_reasons.get("candidate_hook_infrastructure"):
            label = "infrastructure"
        elif outcome_kind == "model_execution_failure" and self._failure_reasons.get("candidate_hook_failure"):
            # The generic evaluator failure envelope does not turn a typed,
            # directly attributable generated-code error into an API outage.
            label = "failure"
        elif outcome_kind in {"model_execution_failure", "provider_failure", "infrastructure"}:
            label = "infrastructure"
        elif outcome_kind in {"not_scored", "pending_official_visual_evaluation"}:
            label = "unscored"
        else:
            label = "failure"
        if label == "failure":
            self._failure_reasons["evaluator_failure"] += 1
        elif label == "infrastructure":
            self._failure_reasons["infrastructure_failure"] += 1
        self._run_outcome = label
        self._run_outcome_kind = outcome_kind or None
        official = payload.get("official_score")
        self._score = dict(official) if isinstance(official, Mapping) else None
        for plugin in sorted(self._invoked):
            self._emit(
                plugin,
                kind="outcome",
                signal="evaluation",
                source_event=event,
                payload=payload,
                success=passed if isinstance(passed, bool) else None,
                outcome_kind=label,
            )

    def observe(self, event: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            self._event_count += 1
            if event == "harness.composition.resolved":
                composition = payload.get("composition")
                if isinstance(composition, Mapping):
                    self._composition_sha256 = _primitive(
                        payload.get("composition_sha256"), limit=200
                    )
                    providers = composition.get("providers")
                    if isinstance(providers, Mapping):
                        self._providers = {
                            str(key): canonical_plugin_name(str(value))
                            for key, value in providers.items()
                            if isinstance(value, str)
                        }
                    for item in composition.get("plugins", []):
                        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                            self._emit(
                                str(item["name"]),
                                kind="loaded",
                                signal="composition",
                                source_event=event,
                                payload=item,
                            )
            elif event == "harness.plugin.activated":
                plugin = payload.get("plugin")
                if isinstance(plugin, str):
                    self._emit(
                        plugin,
                        kind="loaded",
                        signal="activation",
                        source_event=event,
                        payload=payload,
                    )
            elif event in {"candidate.plugin.called", "candidate.plugin.returned", "candidate.plugin.failed"}:
                infrastructure = event == "candidate.plugin.failed" and candidate_hook_is_infrastructure(payload)
                if event == "candidate.plugin.failed":
                    self._failure_reasons["candidate_hook_infrastructure" if infrastructure else "execution_failure"] += 1
                    self._hard_failure = True
                plugin = payload.get("plugin")
                # A diagnostic naming an inactive/unknown plugin must not
                # create an invocation or a causal attribution for it.
                if isinstance(plugin, str) and canonical_plugin_name(plugin) in self._active:
                    plugin = canonical_plugin_name(plugin)
                    hook = payload.get("hook")
                    safe_payload = {"operation": hook if hook in {"before_task", "before_submit", "after_run"} else "candidate-hook"}
                    if event == "candidate.plugin.called":
                        self._emit(plugin, kind="invoked", signal="candidate-hook", source_event=event, payload=safe_payload)
                    else:
                        self._emit(
                            plugin, kind="outcome", signal="candidate-hook-result", source_event=event,
                            payload=safe_payload, success=event == "candidate.plugin.returned",
                            outcome_kind="infrastructure" if infrastructure else "success" if event == "candidate.plugin.returned" else "failure",
                        )
                        if event == "candidate.plugin.failed" and not infrastructure:
                            self._failure_reasons["candidate_hook_failure"] += 1
                            self._emit(plugin, kind="attributed", signal="candidate-hook-failure", source_event=event, payload=safe_payload)
                            self._attributed_plugins[plugin] += 1
            elif event == "harness.skills.routed":
                for selected in payload.get("selected", []) or []:
                    if isinstance(selected, str):
                        self._emit(
                            _skill_plugin(selected),
                            kind="selected",
                            signal="skill-routing",
                            source_event=event,
                            payload=payload,
                        )
            elif event == "preprocess.profile":
                plugin = self._provider("context.workbook-profile")
                if plugin:
                    self._emit(plugin, kind="invoked", signal="profile", source_event=event, payload=payload)
            elif event == "agent.started":
                plugin = self._provider("policy.solve")
                if plugin:
                    self._emit(plugin, kind="invoked", signal="policy", source_event=event, payload=payload)
            elif event == "tool.called":
                tool = str(payload.get("name", ""))
                signal = "tool.recalculate-and-read" if tool == "recalculate_and_read" else "action.spreadsheet"
                plugin = self._provider(signal) or self._provider("action.spreadsheet")
                if plugin:
                    self._emit(
                        plugin,
                        kind="invoked",
                        signal=signal,
                        source_event=event,
                        payload={**payload, "operation": tool},
                    )
            elif event in {"tool.returned", "tool.failed"}:
                tool = str(payload.get("name", ""))
                signal = "tool.recalculate-and-read" if tool == "recalculate_and_read" else "action.spreadsheet"
                plugin = self._provider(signal) or self._provider("action.spreadsheet")
                if plugin:
                    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
                    ok = result.get("ok") is True if isinstance(result, Mapping) else event == "tool.returned"
                    label = "success" if ok else "failure"
                    self._emit(
                        plugin,
                        kind="outcome",
                        signal="tool-result",
                        source_event=event,
                        payload={**payload, "operation": tool},
                        success=ok,
                        outcome_kind=label,
                    )
            elif event in _VALIDATION_EVENTS:
                signal, validation = _VALIDATION_EVENTS[event]
                plugin = self._provider(signal)
                if plugin:
                    self._emit(
                        plugin,
                        kind="invoked",
                        signal=validation,
                        source_event=event,
                        payload=payload,
                        success=validation == "success",
                    )
                    self._emit(
                        plugin,
                        kind="outcome",
                        signal="validation-result",
                        source_event=event,
                        payload=payload,
                        success=validation == "success",
                        outcome_kind="success" if validation == "success" else "failure",
                    )
                if validation in {"failure", "incomplete"}:
                    self._failure_reasons["formula_validation_failure"] += 1
                    self._hard_failure = True
            elif event in {"tool.failed", "agent.execution_failed", "model.failed"}:
                # Provider/model-call failures are infrastructure evidence, not
                # evidence that an activated plugin is defective. Mixing these
                # with agent/workbook execution failures creates spurious
                # plugin attribution (for example, HTTP 429 cooldowns caused
                # every loaded plugin to appear correlated with failure).
                reason = (
                    "tool_failure" if event == "tool.failed"
                    else "provider_failure" if event == "model.failed"
                    else "execution_failure"
                )
                self._failure_reasons[reason] += 1
                self._hard_failure = True
            elif event == "postprocess.date_text_repair":
                plugin = self._provider("repair.date-text")
                if plugin:
                    self._emit(plugin, kind="invoked", signal="repair", source_event=event, payload=payload)
            elif event == "harness.failure.attributed":
                for key in ("target_plugins", "candidate_plugins"):
                    for item in payload.get(key, []) or []:
                        if isinstance(item, Mapping) and isinstance(item.get("plugin"), str):
                            self._emit(
                                str(item["plugin"]),
                                kind="attributed",
                                signal="failure-attribution",
                                source_event=event,
                                payload=payload,
                            )
                            self._attributed_plugins[canonical_plugin_name(str(item["plugin"]))] += 1
            self._outcome(event, payload)
            if self._plugins and self.write_sidecar:
                self._write_profile()


__all__ = ["PluginTraceLedger", "SCHEMA_VERSION", "candidate_hook_is_infrastructure"]
