#!/usr/bin/env python3
"""Generate one contract-bound candidate from a distilled evidence packet.

The controller, not the model, fixes the coordinate and Method operator.  This
adapter only proposes content inside that scope.  Recomposition is entirely
deterministic and therefore does not spend a model call.
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any

import httpx


def _compact_evidence_for_model(value: Any) -> Any:
    """Remove audit-only bulk from the proposer view without changing evidence.

    The controller keeps the complete packet on disk and uses its hashes for
    routing.  Sending every trace hash, phase event, and repeated representative
    to a thinking model can push a single proposal request beyond the provider's
    practical context/latency limit.  These fields are provenance, not repair
    content, so the model receives a deterministic bounded view while the full
    packet remains immutable in ``evidence-packet.json``.
    """
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if "sha256" in lowered or lowered.endswith("_hash"):
                continue
            compacted[str(key)] = _compact_evidence_for_model(item)
        return compacted
    if isinstance(value, list):
        items = [_compact_evidence_for_model(item) for item in value]
        if len(items) > 12:
            return [*items[:10], {"_omitted_items": len(items) - 12}, *items[-2:]]
        return items
    if isinstance(value, str) and len(value) > 12_000:
        return value[:6_000] + "\n[...omitted audit bulk...]\n" + value[-6_000:]
    return value


def _compact_editable_files_for_model(files: Any) -> Any:
    """Make a bounded source excerpt for large implementation proposals.

    The controller still supplies and validates the full frozen file.  For a
    very large Python implementation, the proposer only needs definitions,
    target-column helpers, and nearby code to formulate a small contextual
    hunk.  Omitted-line markers are deliberately not valid patch context; the
    prompt asks the model to emit minimal hunks, and the canonicalizer matches
    them against the complete source after the call.
    """
    if not isinstance(files, list):
        return files
    result: list[Any] = []
    # Keep the proposer request comfortably below the provider's practical
    # thinking latency limit.  ``financial_model_repairs.py`` is a generated
    # implementation file (roughly 4.8k lines); the old excerpt rule retained
    # a neighbourhood around every ``def`` and could therefore send almost
    # the entire 216KB file.  A proposer only needs the public outline and a
    # small amount of exact context to emit a unified hunk—the controller
    # canonicalizes that hunk against the complete frozen source afterwards.
    max_excerpt_chars = 72_000
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            result.append(item)
            continue
        source = item["content"]
        lines = source.splitlines()
        if len(source) <= 80_000:
            result.append(item)
            continue
        keep: set[int] = set(range(min(60, len(lines))))
        needles = (
            "def ",
            "class ",
            "_instruction_target_columns",
            "_instruction_year_columns",
            "year_columns",
            "period_columns",
        )
        for index, line in enumerate(lines):
            if any(needle in line for needle in needles):
                # Signatures plus a few exact body lines make a useful patch
                # anchor without copying every implementation detail.
                keep.update(range(max(0, index - 2), min(len(lines), index + 4)))
        keep.update(range(max(0, len(lines) - 40), len(lines)))
        excerpt: list[str] = []
        previous: int | None = None
        for index in sorted(keep):
            if previous is not None and index > previous + 1:
                excerpt.append(f"# [omitted source lines {previous + 2}-{index}]")
            excerpt.append(lines[index])
            previous = index
        rendered = "\n".join(excerpt) + ("\n" if source.endswith("\n") else "")
        # The first pass above is deterministic, but a file with many short
        # helpers can still exceed the bound.  Preserve the beginning/end and
        # exact line anchors in that case; omitted markers are never used as
        # patch context by the canonicalizer.
        if len(rendered) > max_excerpt_chars:
            rendered = (
                rendered[: max_excerpt_chars // 2]
                + "\n# [omitted source excerpt for latency bound]\n"
                + rendered[-max_excerpt_chars // 2 :]
            )
        replacement = dict(item)
        replacement["content"] = rendered
        replacement["source_excerpt"] = True
        replacement["source_line_count"] = len(lines)
        result.append(replacement)
    return result


def _canonicalize_implementation_patch(
    patch: str, files: list[dict[str, Any]]
) -> str:
    """Turn a model diff with placeholder hunk offsets into a real git diff.

    Some OpenAI-compatible models emit otherwise useful patches with ``X,Y``
    hunk offsets or omit the ``diff --git`` header. The controller correctly
    rejects those patches. Before handing a proposal to the controller, make
    such output concrete only when every old hunk has one exact match in the
    controller-supplied source. Ambiguous or invented context still fails.
    """

    if len(files) != 1:
        raise ValueError("Implementation proposal needs exactly one editable file")
    relative = str(files[0].get("path", ""))
    source = files[0].get("content")
    if not relative or not isinstance(source, str):
        raise ValueError("Implementation proposal is missing editable source")

    normalized = patch.strip()
    if normalized.startswith("```"):
        normalized = normalized.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if normalized.startswith("diff\n"):
            normalized = normalized[5:]

    lines = normalized.splitlines()
    expected_header = f"diff --git a/{relative} b/{relative}"
    hunk_headers = [line for line in lines if line.startswith("@@")]
    # Never trust model-supplied hunk offsets, even when they are numeric.
    # Providers frequently copy line numbers from a stale excerpt.  Rebuild
    # every patch from old-context matches in the frozen source below.

    old_marker = f"--- a/{relative}"
    new_marker = f"+++ b/{relative}"
    if old_marker not in lines or new_marker not in lines or not hunk_headers:
        raise ValueError("Implementation patch does not target the editable file")

    original_lines = source.splitlines()
    revised_lines = list(original_lines)
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("@@"):
            current = []
            hunks.append(current)
        elif current is not None:
            if line.startswith("diff --git ") or line.startswith("--- ") or line.startswith("+++ "):
                current = None
            elif line.startswith("\\ No newline at end of file"):
                continue
            elif line[:1] in {" ", "+", "-"}:
                current.append(line)
            elif line:
                raise ValueError("Implementation patch contains invalid hunk content")

    for hunk in hunks:
        old = [line[1:] for line in hunk if line[:1] in {" ", "-"}]
        new = [line[1:] for line in hunk if line[:1] in {" ", "+"}]
        if not old or old == new:
            raise ValueError("Implementation patch hunk has no concrete edit")
        width = len(old)
        matches = [
            index
            for index in range(len(revised_lines) - width + 1)
            if revised_lines[index : index + width] == old
        ]
        if len(matches) != 1:
            raise ValueError(
                "Implementation patch context must match exactly one source location"
            )
        start = matches[0]
        revised_lines[start : start + width] = new

    if revised_lines == original_lines:
        raise ValueError("Implementation patch made no source change")
    unified = list(
        difflib.unified_diff(
            original_lines,
            revised_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
    )
    return expected_header + "\n" + "\n".join(unified) + "\n"


def _json_text(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Thinking-capable LiteLLM routes occasionally place a short rationale
        # or a closing markdown fence around the JSON despite the instruction
        # to return JSON only. Recover only the first complete object; never
        # guess fields or repair malformed JSON.
        decoder = json.JSONDecoder()
        result = None
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                candidate, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and text[index + end :].strip():
                # Trailing prose is acceptable only when the decoded object
                # itself is complete; the caller still validates its schema.
                result = candidate
                break
            if isinstance(candidate, dict):
                result = candidate
                break
        if result is None:
            raise
    if not isinstance(result, dict):
        raise ValueError("Proposer response must be a JSON object")
    return result


def _content(document: dict[str, Any]) -> str:
    """Extract final text, falling back to reasoning text for thinking routes.

    Some LiteLLM thinking adapters put the JSON in ``reasoning_content`` and
    return ``content: null``.  We only use the reasoning field when the final
    content is empty; :func:`_json_text` still requires a complete JSON object
    and performs no schema repair.
    """
    message = document["choices"][0]["message"]
    content = message.get("content", "")
    if isinstance(content, str):
        if content.strip():
            return content
    if isinstance(content, list):
        rendered = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
        if rendered.strip():
            return rendered
    reasoning = message.get("reasoning_content", "")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    if isinstance(reasoning, list):
        rendered = "".join(
            str(item.get("text", "")) for item in reasoning if isinstance(item, dict)
        )
        if rendered.strip():
            return rendered
    raise ValueError("Proposer returned no text content")


def _post_model_json(
    args: argparse.Namespace, body: dict[str, Any], *, attempts: int = 3
) -> dict[str, Any]:
    """Call the proposer with bounded retries for empty/truncated responses."""
    key = args.api_key_file.read_text(encoding="utf-8").strip()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=args.timeout, trust_env=False) as client:
                response = client.post(
                    args.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json=body,
                )
            response.raise_for_status()
            return _json_text(_content(response.json()))
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == attempts:
                raise
    assert last_error is not None
    raise last_error


def _model_proposal(args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    route = request["route"]
    route_items = route.get("mutations") or [route]
    if not isinstance(route_items, list) or not route_items:
        raise ValueError("Deterministic route has no mutation targets")
    # A joint route is sent to the proposer as one request.  The controller
    # still owns target/operator/surface selection; the model supplies only
    # bounded content for each already-authorized coordinate.
    if len(route_items) > 1 or route.get("scope") == "joint":
        mutation_schemas: list[dict[str, Any]] = []
        for item in route_items:
            surface = item.get("surface")
            schema: dict[str, Any] = {
                "target_plugin": item.get("target_plugin"),
                "operation": item.get("operation"),
                "surface": surface,
                "operator": "exact controller-approved operator",
            }
            if surface in {"prompt", "description"}:
                schema["content"] = "complete replacement content for this route target"
            elif surface == "implementation":
                schema["patch"] = "git unified diff constrained to this target contract"
            elif surface == "config":
                schema["config_patch"] = {"declared-field": "bounded value"}
            mutation_schemas.append(schema)
        visible = {
            "route": route,
            "operator_policy": request["operator_policy"],
            "plugin_contracts": request.get("plugin_contracts", {}),
            "editable_files_by_plugin": {
                name: _compact_editable_files_for_model(files)
                for name, files in request.get("editable_files_by_plugin", {}).items()
            },
            "evidence_packet": _compact_evidence_for_model(request["evidence_packet"]),
            "rejected_candidate_sha256": request.get("rejected_candidate_sha256", []),
            "response_schema": {
                "rationale": "short evidence-grounded explanation",
                "mutations": mutation_schemas,
            },
        }
        body = {
            "model": args.model,
            "temperature": 0,
            "top_p": 1,
            # Candidate patches are deliberately small.  Keep the proposer
            # response bounded so a thinking route cannot spend the entire
            # request deadline on unbounded internal deliberation; solver
            # evaluation budgets remain unchanged and unlimited.
            "max_tokens": args.max_tokens,
            "chat_template_kwargs": {"enable_thinking": True},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return JSON only. You are a plugin evolution proposer, not the router. "
                        "The deterministic joint route is immutable. Return exactly one mutation "
                        "for every route target, preserving target_plugin, operation, surface and "
                        "controller-approved operator. Infer a reusable cross-plugin mechanism "
                        "from the redacted failure prototypes; do not fit task IDs, workbook names, "
                        "cell coordinates, evaluator answers, or constants. Preserve contracts and "
                        "make the smallest jointly coherent change. For implementation patches, "
                        "emit a complete git unified diff with exactly one 'diff --git a/... b/...' "
                        "header and numeric hunk offsets; never use X,Y or other placeholders. "
                        "For a source excerpt, use minimal hunks with only the exact changed "
                        "old/new lines and do not copy omitted-line markers as context."
                    ),
                },
                {"role": "user", "content": json.dumps(visible, ensure_ascii=False)},
            ],
        }
        return _post_model_json(args, body)
    surface = route.get("surface")
    schema: dict[str, Any] = {"rationale": "short evidence-grounded explanation"}
    if surface in {"prompt", "description"}:
        schema["content"] = "complete replacement content for editable_files[0]"
    elif surface == "implementation":
        schema["patch"] = "git unified diff constrained to plugin_contract.edit_policies"
    elif surface == "config":
        schema["config_patch"] = {"declared-field": "bounded value"}
    else:
        raise ValueError(f"Unsupported model-generated surface: {surface!r}")
    visible = {
        "route": route,
        "operator_policy": request["operator_policy"],
        "plugin_contract": request["plugin_contract"],
        "editable_files": _compact_editable_files_for_model(
            request.get("editable_files", [])
        ),
        "evidence_packet": _compact_evidence_for_model(request["evidence_packet"]),
        "rejected_candidate_sha256": request.get("rejected_candidate_sha256", []),
        "response_schema": schema,
    }
    body = {
        "model": args.model,
        "temperature": 0,
        "top_p": 1,
        "max_tokens": args.max_tokens,
        "chat_template_kwargs": {"enable_thinking": True},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return JSON only. You are a plugin evolution proposer, not the router. "
                    "The deterministic route and operator are immutable. Infer a reusable "
                    "mechanism from failure prototypes and causal sketches; do not fit task IDs, "
                    "workbook names, cell coordinates, evaluator answers, or constants. Preserve "
                    "the external contract and permissions. Make the smallest change that should "
                    "improve replay and transfer without harming no-regression anchors. For a "
                    "replacement content response, retain valid skill frontmatter. For an "
                    "implementation response, emit a complete git unified diff with exactly one "
                    "'diff --git a/... b/...' header and numeric hunk offsets; never use X,Y or "
                    "other placeholders. For a source excerpt, use minimal hunks with only the "
                    "exact changed old/new lines and do not copy omitted-line markers as context."
                ),
            },
            {"role": "user", "content": json.dumps(visible, ensure_ascii=False)},
        ],
    }
    return _post_model_json(args, body)


def _content_mutation(
    item: dict[str, Any], route: dict[str, Any], files: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project model content onto one immutable deterministic route target."""

    if not isinstance(item, dict):
        raise ValueError("Each joint mutation must be an object")
    expected = {
        "target_plugin": route.get("target_plugin"),
        "operation": route.get("operation"),
        "surface": route.get("surface"),
    }
    for key, value in expected.items():
        if item.get(key) != value:
            raise ValueError(f"Joint mutation changed deterministic {key}")
    result: dict[str, Any] = dict(expected)
    replacement = route.get("replacement_plugin")
    if replacement is not None:
        if item.get("replacement_plugin") != replacement:
            raise ValueError("Joint mutation changed deterministic replacement_plugin")
        result["replacement_plugin"] = replacement
    elif item.get("replacement_plugin") is not None:
        raise ValueError("Unexpected replacement_plugin in joint mutation")
    operation = route.get("operation")
    if operation in {"enable", "disable", "replace"}:
        # Composition-only operations are controller materialized and cannot
        # smuggle file/config content through the model response.
        if any(item.get(key) not in (None, {}, [], "") for key in ("files", "patch", "config_patch")):
            raise ValueError("Composition mutation contains unauthorized content")
        return result
    surface = route.get("surface")
    if surface in {"prompt", "description"}:
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Joint replacement mutation omitted content")
        if len(files) != 1:
            raise ValueError("Joint replacement route needs exactly one editable file")
        result.update(operator="replace-file", files=[{"path": files[0]["path"], "content": content}])
    elif surface == "implementation":
        patch = item.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError("Joint implementation mutation omitted patch")
        result.update(
            operator="unified-diff",
            patch=_canonicalize_implementation_patch(patch, files),
        )
    elif surface == "config":
        config_patch = item.get("config_patch")
        if not isinstance(config_patch, dict):
            raise ValueError("Joint config mutation omitted config_patch")
        result.update(operator="bounded-config", config_patch=config_patch)
    else:
        raise ValueError(f"Unsupported joint mutation surface: {surface!r}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("response", type=Path)
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    parser.add_argument("--model", default="dashscope/glm-5.2")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4_096,
        help="Bound proposer response (including thinking) while leaving solver budgets unchanged",
    )
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    route = request["route"]
    operation = route["operation"]
    route_items = route.get("mutations") or [route]
    if not isinstance(route_items, list) or not route_items:
        raise ValueError("Route mutations must be a non-empty list")
    joint = len(route_items) > 1 or route.get("scope") == "joint"
    candidate: dict[str, Any] = {
        "candidate_id": f"r{int(request['round']):03d}-{operation}",
        "base_revision_sha256": request["base_revision_sha256"],
        "operation": operation,
        "target_plugin": route["target_plugin"],
        "surface": route.get("surface"),
        "rationale": "Deterministic evidence route and contract-bound Method operator.",
    }
    if joint:
        generated = _model_proposal(args, request)
        raw_mutations = generated.get("mutations")
        if not isinstance(raw_mutations, list):
            raise ValueError("Joint proposer response must contain a mutations list")
        by_target: dict[str, dict[str, Any]] = {}
        for item in raw_mutations:
            if not isinstance(item, dict):
                raise ValueError("Joint proposer mutation must be an object")
            target = str(item.get("target_plugin", ""))
            if not target or target in by_target:
                raise ValueError("Joint proposer returned duplicate/empty target")
            by_target[target] = item
        if set(by_target) != {str(item.get("target_plugin")) for item in route_items}:
            raise ValueError("Joint proposer mutation targets do not match deterministic route")
        mutations = [
            _content_mutation(
                by_target[str(item["target_plugin"])],
                item,
                request.get("editable_files_by_plugin", {}).get(str(item["target_plugin"]), []),
            )
            for item in route_items
        ]
        candidate.update(
            scope="joint",
            rationale=str(generated.get("rationale", ""))[:4000],
            mutations=mutations,
        )
    elif operation == "replace":
        candidate["replacement_plugin"] = route["replacement_plugin"]
    elif operation == "enable" or operation == "disable":
        pass
    else:
        generated = _model_proposal(args, request)
        candidate["rationale"] = str(generated.get("rationale", ""))[:4000]
        surface = route.get("surface")
        if surface in {"prompt", "description"}:
            files = request.get("editable_files") or []
            if len(files) != 1:
                raise ValueError("Replacement proposal needs exactly one editable file")
            content = generated.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Replacement proposal omitted content")
            candidate.update(
                operator="replace-file",
                files=[{"path": files[0]["path"], "content": content}],
            )
        elif surface == "implementation":
            patch = generated.get("patch")
            if not isinstance(patch, str) or not patch.strip():
                raise ValueError("Implementation proposal omitted patch")
            candidate.update(
                operator="unified-diff",
                patch=_canonicalize_implementation_patch(
                    patch, request.get("editable_files") or []
                ),
            )
        elif surface == "config":
            config_patch = generated.get("config_patch")
            if not isinstance(config_patch, dict):
                raise ValueError("Config proposal omitted config_patch")
            candidate.update(operator="bounded-config", config_patch=config_patch)
    args.response.parent.mkdir(parents=True, exist_ok=True)
    args.response.write_text(
        json.dumps({"candidates": [candidate]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
