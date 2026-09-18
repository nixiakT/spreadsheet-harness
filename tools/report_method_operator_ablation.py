#!/usr/bin/env python3
"""Audit and summarize the development-only Method operator ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from statistics import fmean
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "benchmarks/results/method-operator-ablation-v3-qwen36plus-glm52-20260918"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def family_means(config: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    contexts = {item["name"]: item for item in config["contexts"]}
    result = {}
    for context_report in report.get("contexts") or []:
        context = contexts[context_report["name"]]
        family_by_task = dict(zip(context["task_ids"], context["workbook_families"], strict=True))
        grouped: dict[str, list[tuple[float, float]]] = {}
        for pair in context_report.get("pairs") or []:
            if pair.get("baseline_status") != "scored" or pair.get("candidate_status") != "scored":
                continue
            grouped.setdefault(family_by_task[pair["id"]], []).append(
                (float(pair["baseline"]), float(pair["candidate"]))
            )
        baseline = [fmean(value[0] for value in values) for values in grouped.values()]
        candidate = [fmean(value[1] for value in values) for values in grouped.values()]
        result[context_report["name"]] = {
            "family_count": len(grouped),
            "baseline_mean": fmean(baseline) if baseline else None,
            "candidate_mean": fmean(candidate) if candidate else None,
            "mean_delta": (
                fmean(c - b for b, c in zip(baseline, candidate, strict=True)) if baseline else None
            ),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    protocol = load(root / "protocol.json")
    issues = []
    arms = {}
    for arm, frozen in protocol["configs"].items():
        config_path = REPO / frozen["path"]
        if digest(config_path) != frozen["sha256"]:
            issues.append(f"config-hash-mismatch:{arm}")
        config = load(config_path)
        workspace = root / "workspaces" / arm
        state_path = workspace / "state.json"
        if not state_path.is_file():
            arms[arm] = {"status": "not-started"}
            continue
        state = load(state_path)
        summary: dict[str, Any] = {
            "status": state.get("status"),
            "attempted_rounds": state.get("attempted_rounds"),
            "accepted_rounds": state.get("accepted_rounds"),
        }
        round_path = workspace / "rounds/000001/round.json"
        if round_path.is_file():
            round_report = load(round_path)
            summary.update(
                route=round_report.get("route"),
                outcome=round_report.get("outcome"),
                winner_revision_sha256=round_report.get("winner_revision_sha256"),
                candidate_failures=round_report.get("candidate_failures") or [],
            )
        candidate_dirs = sorted((workspace / "candidates").glob("r000001-*"))
        if len(candidate_dirs) == 1:
            candidate = candidate_dirs[0]
            decision_path = candidate / "decision.json"
            validation_path = candidate / "validation-report.json"
            if decision_path.is_file():
                decision = load(decision_path)
                summary.update(
                    promoted=decision.get("promoted"),
                    aggregate_mean_delta=decision.get("aggregate_mean_delta"),
                    aggregate_lcb=decision.get("aggregate_lcb"),
                    variance=decision.get("variance"),
                    blockers=decision.get("blockers") or [],
                    context_results=decision.get("context_results") or [],
                )
            if validation_path.is_file():
                validation = load(validation_path)
                summary["family_scores"] = family_means(config, validation)
                if validation.get("score_weights") != config["evaluation_binding"]["score_weights"]:
                    issues.append(f"score-definition-mismatch:{arm}")
        replay_ids = {
            task_id
            for context in config["contexts"]
            if context["kind"] == "replay"
            for task_id in context["task_ids"]
        }
        leaked = [
            item.get("task_id")
            for item in state.get("evidence") or []
            if item.get("task_id") not in replay_ids
        ]
        if leaked:
            issues.append(f"non-replay-state-evidence:{arm}:{','.join(leaked)}")
        arms[arm] = summary
    complete = all(item.get("attempted_rounds") == 1 for item in arms.values())
    if args.require_complete and not complete:
        raise SystemExit("Ablation is incomplete")
    result = {
        "schema_version": "method-operator-ablation-report-v1",
        "complete": complete,
        "issues": issues,
        "heldout_opened": False,
        "artifact_score": protocol["artifact_score"],
        "arms": arms,
    }
    atomic_json(root / "report.json", result)
    lines = [
        "# Method operator ablation",
        "",
        f"Complete: `{str(complete).lower()}`  ",
        f"Audit issues: `{len(issues)}`  ",
        f"Artifact score: `{protocol['artifact_score']}`",
        "",
        "| Arm | Operator selected | Outcome | Accepted | Replay Δ / LCB | Transfer Δ / LCB | Regression Δ / LCB |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for arm, item in arms.items():
        contexts = {value["kind"]: value for value in item.get("context_results") or []}

        def cell(kind: str, contexts: dict[str, Any] = contexts) -> str:
            value = contexts.get(kind)
            return "—" if value is None else f"{value['mean_delta']:.4f} / {value['lcb']:.4f}"

        route = item.get("route") or {}
        lines.append(
            f"| {arm} | {route.get('method_operator', '—')} | {item.get('outcome', item['status'])} | "
            f"{item.get('promoted', '—')} | {cell('replay')} | {cell('transfer')} | {cell('regression')} |"
        )
    (root / "TABLE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"complete": complete, "issues": issues, "root": str(root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
