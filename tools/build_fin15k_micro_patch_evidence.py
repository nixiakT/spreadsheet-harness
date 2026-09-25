#!/usr/bin/env python3
"""Build plugin-level, micro-patch evidence from a frozen Fin-1.5K ledger.

This deliberately emits hypotheses, not causal claims. A plugin issue becomes
confirmed only after a bounded patch wins on its support cases without
regressing its success anchors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


FAILURE_MODES: dict[str, dict[str, Any]] = {
    "formula_validation_failure": {
        "primary": ["knowledge-formula", "verify-formula-runtime"],
        "supporting": ["act-code-plus-formula-validation"],
        "surfaces": ["prompt", "implementation"],
        "budget": {"max_plugins": 1, "max_files": 1, "max_changed_lines": 24},
    },
    "execution_failure": {
        "primary": ["knowledge-financial-model", "knowledge-formula"],
        "supporting": ["knowledge-manipulation", "act-code-plus-formula-validation"],
        "surfaces": ["prompt", "implementation"],
        "budget": {"max_plugins": 1, "max_files": 1, "max_changed_lines": 30},
    },
    "evaluator_failure": {
        "primary": [],
        "supporting": ["knowledge-verification"],
        "surfaces": [],
        "budget": {"max_plugins": 0, "max_files": 0, "max_changed_lines": 0},
    },
    "infrastructure_failure": {
        "primary": [],
        "supporting": [],
        "surfaces": [],
        "budget": {"max_plugins": 0, "max_files": 0, "max_changed_lines": 0},
    },
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": row.get("task_id"),
        "workbook_family": row.get("workbook_family"),
        "outcome": row.get("outcome"),
        "score": row.get("score"),
        "failure_reasons": row.get("failure_reasons") or {},
        "invoked": row.get("invoked") or {},
        "selected_plugins": row.get("selected_plugins") or [],
        "trace_weight": row.get("trace_weight"),
    }


def build(rows: list[dict[str, Any]], max_support: int, max_anchors: int) -> dict[str, Any]:
    hypotheses: list[dict[str, Any]] = []
    for mode, policy in FAILURE_MODES.items():
        support = [r for r in rows if int((r.get("failure_reasons") or {}).get(mode, 0)) > 0]
        support.sort(key=lambda r: (-float(r.get("trace_weight") or 0), str(r.get("task_id"))))
        invoked_primary = [
            plugin for plugin in policy["primary"]
            if any(int((r.get("invoked") or {}).get(plugin, 0)) > 0 for r in support)
        ]
        anchors = [
            r for r in rows if r.get("outcome") == "pass"
            and any(int((r.get("invoked") or {}).get(p, 0)) > 0 for p in invoked_primary)
        ]
        anchors.sort(key=lambda r: str(r.get("task_id")))
        status = "non-actionable" if not policy["surfaces"] else "suspected"
        hypotheses.append({
            "hypothesis_id": f"micro-{mode}",
            "failure_mode": mode,
            "status": status,
            "causal_claim_allowed": False,
            "primary_suspect_plugins": invoked_primary,
            "supporting_plugins": policy["supporting"],
            "exonerated_plugins": [],
            "allowed_surfaces": policy["surfaces"],
            "patch_budget": policy["budget"],
            "support_count": len(support),
            "support_cases": [compact(r) for r in support[:max_support]],
            "success_anchors": [compact(r) for r in anchors[:max_anchors]],
            "promotion_rule": {
                "status_after_test": "confirmed" ,
                "requires_target_wins": 1,
                "max_anchor_regressions": 0,
                "record": ["wins", "losses", "unchanged", "new_regressions", "paired_score_delta"],
            },
            "interpretation": (
                "Plugin co-occurrence is suspicion only. Confirm this issue only through a bounded "
                "patch evaluated on its support cases and success anchors."
            ),
        })
    packet = {
        "schema_version": "fin15k-plugin-micro-patch-evidence-v1",
        "input_trace_count": len(rows),
        "experimental_unit": "one failure mode x one primary plugin x one bounded patch",
        "composition_policy": {
            "h-only": "one or more independently validated harness micro-patches",
            "d-only": "one or more independently validated domain micro-patches",
            "joint": "compose validated H and D patches; retain each component's attribution",
        },
        "hypotheses": hypotheses,
        "plugin_issue_schema": {
            "required": [
                "plugin", "failure_mode", "status", "support_case_ids", "anchor_case_ids",
                "patch_revision", "wins", "losses", "unchanged", "new_regressions",
            ],
            "status_values": ["suspected", "supported", "confirmed", "rejected"],
        },
    }
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    packet["packet_sha256"] = hashlib.sha256(encoded).hexdigest()
    return packet


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-support", type=int, default=24)
    parser.add_argument("--max-anchors", type=int, default=12)
    args = parser.parse_args()
    packet = build(load_rows(args.ledger), args.max_support, args.max_anchors)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "hypotheses": len(packet["hypotheses"]), "sha256": packet["packet_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
