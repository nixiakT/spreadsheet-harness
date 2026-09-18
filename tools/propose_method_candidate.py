#!/usr/bin/env python3
"""Generate one contract-bound candidate from a distilled evidence packet.

The controller, not the model, fixes the coordinate and Method operator.  This
adapter only proposes content inside that scope.  Recomposition is entirely
deterministic and therefore does not spend a model call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx


def _json_text(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Proposer response must be a JSON object")
    return result


def _content(document: dict[str, Any]) -> str:
    content = document["choices"][0]["message"].get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    raise ValueError("Proposer returned no text content")


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
            "editable_files_by_plugin": request.get("editable_files_by_plugin", {}),
            "evidence_packet": request["evidence_packet"],
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
                        "make the smallest jointly coherent change."
                    ),
                },
                {"role": "user", "content": json.dumps(visible, ensure_ascii=False)},
            ],
        }
        key = args.api_key_file.read_text(encoding="utf-8").strip()
        with httpx.Client(timeout=args.timeout, trust_env=False) as client:
            response = client.post(
                args.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            )
        response.raise_for_status()
        return _json_text(_content(response.json()))
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
        "editable_files": request.get("editable_files", []),
        "evidence_packet": request["evidence_packet"],
        "rejected_candidate_sha256": request.get("rejected_candidate_sha256", []),
        "response_schema": schema,
    }
    body = {
        "model": args.model,
        "temperature": 0,
        "top_p": 1,
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
                    "replacement content response, retain valid skill frontmatter."
                ),
            },
            {"role": "user", "content": json.dumps(visible, ensure_ascii=False)},
        ],
    }
    key = args.api_key_file.read_text(encoding="utf-8").strip()
    with httpx.Client(timeout=args.timeout, trust_env=False) as client:
        response = client.post(
            args.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json=body,
        )
    response.raise_for_status()
    return _json_text(_content(response.json()))


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
        result.update(operator="unified-diff", patch=patch)
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
            candidate.update(operator="unified-diff", patch=patch)
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
