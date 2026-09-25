#!/usr/bin/env python3
"""Generate strict 50/200-trace candidates without a Fin promotion gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from spreadsheet_harness.continuous_evolution import (
    CandidateProposal,
    ContinuousEvolutionConfig,
    EvolutionRoute,
    RevisionStore,
    _run_adapter_command,
)
from spreadsheet_harness.plugins import default_plugin_registry


REPO = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO / "benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-formal-v2-500-profiled-v7-20260922"
MECHANISMS = {
    "h-only": "general-only",
    "d-only": "domain-only",
    "joint": "coevolution",
}


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def subset_profile(rows: list[dict[str, Any]], full: dict[str, Any], size: int) -> dict[str, Any]:
    rows = rows[:size]
    profiles = []
    for original in full["profiles"]:
        plugin = original["plugin"]
        invoked = [row for row in rows if int((row.get("invoked") or {}).get(plugin, 0)) > 0]
        not_invoked = [row for row in rows if int((row.get("invoked") or {}).get(plugin, 0)) <= 0]
        def mean(items: list[dict[str, Any]], key: str) -> float | None:
            values = [float(item[key]) for item in items if isinstance(item.get(key), (int, float))]
            return sum(values) / len(values) if values else None
        yes, no = mean(invoked, "score"), mean(not_invoked, "score")
        failures = Counter(
            reason for row in invoked if row.get("outcome") == "fail"
            for reason, count in (row.get("failure_reasons") or {}).items()
            for _ in range(int(count or 0))
        )
        profiles.append({
            **original,
            "tasks": size,
            "invoked_tasks": len(invoked),
            "activation_rate": len(invoked) / size,
            "total_invocations": sum(int((row.get("invoked") or {}).get(plugin, 0)) for row in rows),
            "avg_invocations_per_task": sum(int((row.get("invoked") or {}).get(plugin, 0)) for row in rows) / size,
            "evidence_family_count": len(invoked),
            "mean_score_when_invoked": yes,
            "mean_score_when_not_invoked": no,
            "descriptive_score_delta": yes - no if yes is not None and no is not None else None,
            "mean_tokens_per_invoked_task": mean(invoked, "tokens"),
            "mean_model_calls_per_invoked_task": mean(invoked, "model_calls"),
            "mean_elapsed_seconds_per_invoked_task": mean(invoked, "elapsed_seconds"),
            "failure_categories": dict(sorted(failures.items())),
            "weighted_invocation_mass": sum(float(row.get("trace_weight") or 0) for row in invoked),
            "weighted_attribution_mass": sum(
                float(row.get("trace_weight") or 0)
                * int((row.get("attributed_plugins") or {}).get(plugin, 0)) for row in rows
            ),
        })
    return {
        "schema_version": "fin15k-plugin-profile-v1",
        "development_only": True,
        "spreadsheetbench_used": False,
        "task_count": size,
        "subset_policy": f"first-{size}-of-frozen-500-ledger",
        "profiles": profiles,
    }


def evidence_packet(rows: list[dict[str, Any]], size: int) -> dict[str, Any]:
    subset = rows[:size]
    failures = [row for row in subset if row.get("outcome") == "fail"]
    anchors = [row for row in subset if row.get("outcome") == "pass"]
    def compact(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in (
            "task_id", "category", "outcome", "failure_reasons", "selected_plugins",
            "invoked", "trace_weight",
        )}
    return {
        "schema_version": "fin15k-scale-plugin-evidence-v1",
        "input_trace_count": size,
        "failure_count": len(failures),
        "success_count": len(anchors),
        "failure_prototypes": [compact(row) for row in sorted(failures, key=lambda r: -float(r.get("trace_weight") or 0))[:24]],
        "no_regression_anchors": [compact(row) for row in anchors[:8]],
        "redaction": "Plugin-linked normalized Fin-1.5K evidence only; no SpreadsheetBench result included.",
    }


def generate(size: int, output_root: Path) -> list[dict[str, Any]]:
    ledger = [json.loads(line) for line in (PROFILE_ROOT / "plugin-profile-500/plugin-task-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    full = read(PROFILE_ROOT / "plugin-profile-500/plugin-profile.json")
    profile = subset_profile(ledger, full, size)
    write(output_root / f"profile-{size}/plugin-profile.json", profile)
    with (output_root / f"profile-{size}/plugin-task-ledger.jsonl").open("w", encoding="utf-8") as handle:
        for row in ledger[:size]:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    records = []
    registry = default_plugin_registry()
    mechanisms = getattr(generate, "mechanisms", None) or tuple(MECHANISMS)
    for mechanism in mechanisms:
        scope = MECHANISMS[mechanism]
        workspace = PROFILE_ROOT / "workspaces" / f"fin15k-500-{scope}"
        store = RevisionStore(workspace)
        state = store.load_state()
        incumbent = str(state["current_revision_sha256"])
        request = read(workspace / "rounds/000001/proposal/request.json")
        request["candidate_limit"] = 1
        request["plugin_profile"] = profile
        request["evidence_packet"] = evidence_packet(ledger, size)
        request["profile_guidance"] = {
            "heldout_feedback_allowed": False,
            "mechanism": mechanism,
            "source": f"strict first {size} frozen Fin-1.5K plugin traces",
            "instruction": "Generate one materially useful candidate from only this scale-specific plugin trace/profile. Do not use SpreadsheetBench evidence.",
        }
        if mechanism in {"d-only", "joint"}:
            # Keep implementation proposals focused: GLM must edit the
            # domain repair module and return a real unified diff.  The full
            # immutable trace remains represented by evidence hashes/packet.
            request["profile_guidance"]["instruction"] += (
                " Target only the supplied financial_model_repairs.py file. "
                "Return JSON with rationale and a non-empty unified diff using "
                "--- a/src/spreadsheet_harness/financial_model_repairs.py and "
                "+++ b/src/spreadsheet_harness/financial_model_repairs.py."
            )
            request["operator_policy"] = {
                **(request.get("operator_policy") or {}),
                "instruction": "Return exactly one implementation mutation with a concrete unified diff. Never return composition/config-only mutations.",
            }
        request["base_revision_sha256"] = incumbent
        request["base_revision"]["revision_sha256"] = incumbent
        directory = output_root / f"proposals-v2/{size}/{mechanism}"
        config = ContinuousEvolutionConfig.load(PROFILE_ROOT / "configs" / f"fin15k-500-{scope}.json")
        scale_refs = list(config.initial_evidence[:size])
        scale_hashes = sorted(hashlib.sha256(ref.path.read_bytes()).hexdigest() for ref in scale_refs)
        route_doc = dict(request["route"])
        route_doc["evidence_sha256"] = scale_hashes
        route_doc["support_count"] = len(scale_refs)
        mutations = []
        for item in route_doc.get("mutations") or []:
            mutation = dict(item)
            mutation["evidence_sha256"] = scale_hashes
            mutation["support_count"] = len(scale_refs)
            mutations.append(mutation)
        if mutations:
            route_doc["mutations"] = mutations
        request["route"] = route_doc
        # Keep one malformed mechanism proposal from aborting the whole scale.
        # Implementation (D-only/joint) proposals must contain a concrete
        # unified diff; GLM occasionally omits it on the first response, so
        # retry once with an explicit constraint while preserving the same
        # scale-specific evidence packet.
        response = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if attempt:
                    request["profile_guidance"]["instruction"] += (
                        " For implementation edits, patch is mandatory and must be a valid unified diff "
                        "against the supplied editable file; do not return a prose-only implementation proposal."
                    )
                response = _run_adapter_command(
                    config.proposer_command, request=request, directory=directory,
                    timeout=config.command_timeout_seconds,
                )
                break
            except (ValueError, RuntimeError) as exc:
                last_error = exc
                (directory / f"retry-{attempt + 1}-error.txt").write_text(str(exc) + "\n", encoding="utf-8")
        if response is None:
            # Provider outages/empty responses should not discard the scale.
            # Reuse the frozen same-mechanism artifact as a deterministic
            # fallback, while marking its provenance explicitly below.  It is
            # still evaluated as a scale candidate, but never treated as a
            # fresh GLM proposal in the report.
            fallback = workspace / "candidates/r000001-r001-edit"
            if not fallback.is_dir():
                raise RuntimeError(f"GLM proposal failed for {mechanism} scale {size}: {last_error}")
            records.append({
                "evidence_scale": size, "mechanism": mechanism,
                "candidate_id": f"scale{size}-{mechanism}-c01-fallback",
                "candidate_dir": str(fallback.resolve()),
                "revision_sha256": read(fallback / "revision.json")["revision_sha256"],
                "source": "same-mechanism-frozen-artifact-fallback",
                "proposer_error": str(last_error),
            })
            continue
        proposals = response.get("candidates") or []
        if not proposals:
            raise RuntimeError(f"GLM returned no {mechanism} candidate for scale {size}")
        raw = dict(proposals[0])
        raw["candidate_id"] = f"scale{size}-{mechanism}-c01"
        proposal = CandidateProposal.from_document(raw)
        route = EvolutionRoute.from_dict(route_doc)
        try:
            candidate_dir, revision = store.materialize_candidate(
                proposal=proposal, route=route, incumbent_revision=incumbent,
                registry=registry, static_checks=config.static_checks,
                timeout=config.command_timeout_seconds,
            )
        except Exception as exc:
            # Preserve a usable, auditable scale result when a model patch is
            # contract-invalid: evaluate the frozen incumbent under this
            # scale label and report the materialization failure explicitly.
            fallback = workspace / "candidates/r000001-r001-edit"
            records.append({
                "evidence_scale": size, "mechanism": mechanism,
                "candidate_id": f"scale{size}-{mechanism}-c01-invalid-fallback",
                "candidate_dir": str(fallback.resolve()),
                "revision_sha256": read(fallback / "revision.json")["revision_sha256"],
                "source": "invalid-scale-proposal-fallback",
                "materialization_error": str(exc),
            })
            continue
        records.append({
            "evidence_scale": size, "mechanism": mechanism,
            "candidate_id": proposal.candidate_id,
            "candidate_dir": str(candidate_dir.resolve()),
            "revision_sha256": revision["revision_sha256"],
            "source": "strict-scale-specific-glm-proposal",
        })
    write(output_root / f"candidates-{size}.json", records)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, choices=(50, 200, 500), required=True)
    parser.add_argument("--output-root", type=Path, default=REPO / "benchmarks/results/fin15k-scale-candidates-strict-20260924")
    parser.add_argument("--mechanisms", nargs="+", choices=tuple(MECHANISMS))
    args = parser.parse_args()
    generate.mechanisms = tuple(args.mechanisms) if args.mechanisms else None
    print(json.dumps(generate(args.size, args.output_root.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
