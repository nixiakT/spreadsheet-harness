#!/usr/bin/env python3
"""Fail-closed harness around GLM-5.2 evolution proposals.

The proposer supplies content only. This wrapper owns retry, route/schema
validation, and an audit trail; it never promotes an invalid candidate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from spreadsheet_harness.continuous_evolution import CandidateProposal, _run_adapter_command


def validate(raw: dict[str, Any], request: dict[str, Any]) -> CandidateProposal:
    if str(raw.get("base_revision_sha256")) != str(request.get("base_revision_sha256")):
        raise ValueError("stale base_revision_sha256")
    proposal = CandidateProposal.from_document(raw)
    route_items = request.get("route", {}).get("mutations") or [request["route"]]
    items = proposal.mutation_items()
    if len(items) != len(route_items):
        raise ValueError(f"mutation count {len(items)} != route count {len(route_items)}")
    for mutation, route in zip(items, route_items, strict=True):
        expected = (str(route.get("target_plugin")), route.get("surface"), route.get("operation"))
        actual = (mutation.target_plugin, mutation.surface, mutation.operation)
        if actual != expected:
            raise ValueError(f"route mismatch: actual={actual} expected={expected}")
        if mutation.surface == "implementation":
            if mutation.operator != "unified-diff" or not mutation.patch or "--- a/" not in mutation.patch or "+++ b/" not in mutation.patch:
                raise ValueError("implementation mutation must contain a concrete unified diff")
        elif mutation.surface in {"prompt", "description"}:
            if mutation.operator not in {"replace-file", "unified-diff"} or not mutation.files and not mutation.patch:
                raise ValueError("prompt/description mutation has no content")
    if len(items) > 1 and proposal.scope != "joint":
        raise ValueError("multi-target mutation must declare joint scope")
    return proposal


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", type=Path)
    ap.add_argument("response", type=Path)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key-file", type=Path, required=True)
    ap.add_argument("--model", default="dashscope/glm-5.2")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--max-attempts", type=int, default=4)
    args = ap.parse_args()
    args.request = args.request.resolve()
    args.response = args.response.resolve()
    request = json.loads(args.request.read_text())
    audit: list[dict[str, Any]] = []
    directory = args.request.parent.resolve()
    command = [
        str(Path(__file__).resolve().parents[1] / ".venv/bin/python"),
        str(Path(__file__).resolve().parents[0] / "propose_method_candidate.py"),
        "{request}", "{response}", "--base-url", args.base_url,
        "--api-key-file", str(args.api_key_file), "--model", args.model,
        "--timeout", str(args.timeout), "--max-tokens", "12000",
    ]
    for attempt in range(1, args.max_attempts + 1):
        req = dict(request)
        guidance = dict(req.get("profile_guidance") or {})
        guidance["instruction"] = str(guidance.get("instruction", "")) + (
            " Return exactly one candidate. The controller will reject any route mismatch, "
            "missing mutation, composition-only fallback, or non-unified implementation patch."
        )
        if attempt > 1:
            guidance["repair_error"] = audit[-1].get("error", "")[-2500:]
        req["profile_guidance"] = guidance
        try:
            document = _run_adapter_command(command, request=req, directory=directory, timeout=args.timeout)
            candidates = document.get("candidates") or []
            if len(candidates) != 1:
                raise ValueError(f"expected one candidate, got {len(candidates)}")
            raw = dict(candidates[0])
            validate(raw, request)
            document["harness_validation"] = {"status": "schema-valid", "attempt": attempt, "audit": audit}
            args.response.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            (directory / "harness-audit.json").write_text(json.dumps({"status":"accepted","attempt":attempt,"audit":audit}, ensure_ascii=False, indent=2) + "\n")
            return 0
        except Exception as exc:
            audit.append({"attempt": attempt, "error": str(exc)})
    (directory / "harness-audit.json").write_text(json.dumps({"status":"rejected","audit":audit}, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit("GLM candidate rejected by evolution harness")


if __name__ == "__main__":
    main()
