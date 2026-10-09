"""Bounded case-1 code evidence for V1 review; never access sibling or gold files."""
from __future__ import annotations

import ast
import json
from pathlib import Path


def code_warnings(code: str) -> list[str]:
    """Advisory warnings, not rejection rules: explicit fixed ranges can be legitimate."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ["Code could not be parsed; inspect the original call before changing it."]
    warnings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and any(
            isinstance(n, (ast.List, ast.Tuple, ast.Constant)) for n in ast.walk(node.test)
        ):
            warnings.add("Check assertions for case-1 literal expectations that block other data.")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"delete_rows", "delete_cols", "insert_rows", "insert_cols"}:
                values = [*node.args, *(k.value for k in node.keywords)]
                if any(isinstance(n, ast.Constant) for n in values):
                    warnings.add("Structural edit uses literal indices/counts; verify they follow the task, not observed data.")
        if isinstance(node, ast.Compare) and any(
            isinstance(n, ast.Constant) and n.value in (None, "") for n in node.comparators
        ):
            warnings.add("Blank-only targeting: does the task request filling blanks or recomputing ALL target records?")
    return sorted(warnings)


def review_evidence(trajectory: Path, *, max_chars: int = 14000) -> str:
    """Expose successful edits (not just already-correct case-1 output) to the reviewer."""
    calls: list[dict] = []
    failures: list[str] = []
    pending = None
    if trajectory.exists():
        for raw in trajectory.read_text(encoding="utf-8").splitlines():
            event = json.loads(raw)
            name, payload = event.get("event"), event.get("payload", {})
            if name == "tool.called":
                pending = payload
            elif name in {"tool.returned", "tool.failed"}:
                result = payload.get("result", {})
                if (pending and pending.get("name") == payload.get("name")
                        and name == "tool.returned" and result.get("ok") is True
                        and result.get("workbook_changed") is True):
                    args = pending.get("arguments", {})
                    code = args.get("code", "")
                    calls.append({"tool": pending["name"], "arguments": args,
                                  "warnings": code_warnings(code) if isinstance(code, str) else []})
                pending = None
            elif name == "agent.execution_failed":
                failures.append(str(payload.get("reason", "unknown")))
    document = {"edits_total": len(calls), "execution_failures": failures,
                "edits": calls, "warnings_are_advisory": True}
    rendered = json.dumps(document, ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered
    # Keep valid JSON and signal omission; never present a silently truncated program as complete.
    document["edits"] = []
    document["code_omitted"] = True
    for call in reversed(calls):
        candidate = {**document, "edits": [call, *document["edits"]]}
        if len(json.dumps(candidate, ensure_ascii=False)) <= max_chars:
            document = candidate
    return json.dumps(document, ensure_ascii=False)
