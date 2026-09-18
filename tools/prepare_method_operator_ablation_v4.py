#!/usr/bin/env python3
"""Prepare an adaptive-scope co-evolution protocol (v4).

This script only freezes a new development workspace/configuration.  It does
not start benchmark jobs.  The v4 controller routes each round to harness,
domain, joint, or abstain from replay attribution; the kernel manifest and
all evaluation splits are committed before any candidate is generated.

The small pilot default keeps the turnaround useful (two families per
context); ``--full-development`` restores the complete Fin-269 + Fin-1.5K
development split from the already sealed v3 manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "benchmarks/results/method-operator-ablation-v3-qwen36plus-glm52-20260918"
SPLIT_MANIFEST = REPO / "benchmarks/results/paper36-search-alternating-qwen36plus-20260917/split-manifest.json"
DEFAULT_ROOT = REPO / "benchmarks/results/method-adaptive-scope-v4-qwen36plus-glm52-20260918"
METHOD = Path("/home/tongzeyuan/.codex/attachments/a1cc4bec-bb48-4972-ae68-b648a2ad026f/pasted-text.txt")
PYTHON = REPO / ".venv/bin/python"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def tree_digest(root: Path, suffix: str = ".py") -> str:
    rows = [
        (path.relative_to(root).as_posix(), digest(path))
        for path in sorted(root.rglob(f"*{suffix}"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _kernel_hash(groups: dict[str, list[str]]) -> str:
    # Use the same controller helper as initialization, so the commitment
    # cannot drift from the invariant enforced during materialization.
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from spreadsheet_harness.continuous_evolution import _kernel_manifest_sha256
    from spreadsheet_harness.plugins import default_plugin_registry

    registry = default_plugin_registry()
    artifact = REPO
    # The helper expects an artifact containing src/ and skills/.  The repo is
    # laid out identically for these paths.
    return _kernel_manifest_sha256(artifact, registry)


def _select_contexts(config: dict[str, Any], *, full_development: bool) -> list[dict[str, Any]]:
    contexts = []
    for raw in config["contexts"]:
        item = json.loads(json.dumps(raw))
        if not full_development and item["kind"] != "regression":
            # The pilot uses two families per adaptation context.  Regression
            # already has two families in the sealed split.
            item["task_ids"] = item["task_ids"][:2]
            item["workbook_families"] = item["workbook_families"][:2]
            item["runner"]["task_datasets"] = {
                task_id: dataset
                for task_id, dataset in item["runner"]["task_datasets"].items()
                if task_id in item["task_ids"]
            }
        contexts.append(item)
    return contexts


def prepare(
    root: Path,
    *,
    max_rounds: int = 3,
    full_development: bool = False,
    max_turns: int | None = None,
    max_output_tokens: int | None = None,
    max_total_tokens: int | None = None,
    task_timeout_seconds: int | None = None,
    parallelism: int = 1,
) -> dict[str, Any]:
    if max_rounds not in {3, 4}:
        raise ValueError("v4 max_rounds must be 3 or 4")
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"Refusing to overwrite non-empty v4 root: {root}")
    source_config = json.loads(
        (SOURCE_ROOT / "configs/full.json").read_text(encoding="utf-8")
    )
    source_protocol = json.loads((SOURCE_ROOT / "protocol.json").read_text(encoding="utf-8"))
    if not SPLIT_MANIFEST.is_file():
        raise RuntimeError(f"Sealed split manifest is missing: {SPLIT_MANIFEST}")
    split_hash = digest(SPLIT_MANIFEST)
    if source_protocol.get("split_manifest_sha256") not in {None, split_hash}:
        raise RuntimeError("v3 protocol split hash does not match the sealed manifest")
    contexts = _select_contexts(source_config, full_development=full_development)
    groups = {
        "harness": list(source_config["groups"]["harness"]),
        "domain": list(source_config["groups"]["domain"]),
    }
    kernel_hash = _kernel_hash(groups)
    config = json.loads(json.dumps(source_config))
    config["contexts"] = contexts
    declared_ids = {task_id for item in contexts for task_id in item["task_ids"]}
    config["initial_evidence"] = [
        item for item in config.get("initial_evidence", []) if item["task_id"] in declared_ids
    ]
    config["max_rounds"] = max_rounds
    config["max_candidates_per_round"] = 1
    config["allowed_operators"] = ["revision", "recomposition", "synthesis"]
    config["kernel_manifest_sha256"] = kernel_hash
    if max_turns is not None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        config["evaluation_binding"]["max_turns"] = max_turns
        config["evaluation_binding"]["max_model_calls"] = max_turns
    if max_output_tokens is not None and max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive when provided")
    if max_total_tokens is not None and max_total_tokens < 1:
        raise ValueError("max_total_tokens must be positive when provided")
    if task_timeout_seconds is not None:
        if task_timeout_seconds < 1:
            raise ValueError("task_timeout_seconds must be positive")
        config["evaluation_binding"]["task_timeout_seconds"] = task_timeout_seconds
    if parallelism < 1:
        raise ValueError("parallelism must be positive")
    # The v4 protocol owns a new cache namespace; no old candidate result is
    # silently reused as adaptation evidence.
    binding = config["evaluation_binding"]
    binding["incumbent_cache_root"] = str(root / "incumbent-cache")
    binding["max_total_tokens"] = max_total_tokens
    binding["max_output_tokens"] = max_output_tokens
    binding["parallelism"] = parallelism
    config["proposer_command"] = [
        str(PYTHON),
        str(REPO / "tools/propose_method_candidate.py"),
        "{request}",
        "{response}",
        "--base-url",
        "http://10.130.138.46:8010/v1",
        "--api-key-file",
        "/tmp/spreadsheet-harness-litellm.key",
        "--model",
        "dashscope/glm-5.2",
        "--timeout",
        "1800",
    ]
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "configs/adaptive.json"
    atomic_json(config_path, config)
    # Fail before any run is started if the new controller cannot consume its
    # frozen config (especially important for the reduced pilot contexts).
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from spreadsheet_harness.continuous_evolution import ContinuousEvolutionConfig

    loaded_config = ContinuousEvolutionConfig.load(config_path)
    protocol = {
        "schema_version": "method-adaptive-scope-v4",
        "purpose": "development pilot for evidence-driven sparse co-evolution",
        "source_v3_root": str(SOURCE_ROOT.relative_to(REPO)),
        "source_protocol_sha256": digest(SOURCE_ROOT / "protocol.json"),
        "source_split_manifest_sha256": source_protocol.get("split_manifest_sha256"),
        "split_manifest_sha256": split_hash,
        "split_sha256": split_hash,
        "method_sha256": digest(METHOD),
        "controller_sha256": digest(REPO / "src/spreadsheet_harness/continuous_evolution.py"),
        "continuous_controller_sha256": digest(REPO / "src/spreadsheet_harness/continuous_evolution.py"),
        "proposer_sha256": digest(REPO / "tools/propose_method_candidate.py"),
        "evaluator_sha256": digest(REPO / "tools/evaluate_continuous_financial_20260911.py"),
        "evaluator_adapter_sha256": digest(REPO / "tools/evaluate_continuous_financial_20260911.py"),
        "runner_sha256": digest(REPO / "src/spreadsheet_harness/spreadsheetbench_v2.py"),
        "kernel_manifest_sha256": kernel_hash,
        "kernel_policy": "all artifact files outside registered plugin edit policies are immutable",
        "solver": "qwen3.6-plus",
        "proposer": "dashscope/glm-5.2",
        "temperature": 0.0,
        "top_p": 1.0,
        "thinking": True,
        "max_calls": 50,
        "max_turns": int(binding.get("max_turns", 50)),
        "max_total_tokens": binding.get("max_total_tokens"),
        "max_output_tokens": binding.get("max_output_tokens"),
        "parallelism": parallelism,
        "max_rounds": max_rounds,
        "allowed_max_rounds": [3, 4],
        "max_candidates_per_round": 1,
        "scope_policy": {
            "routes": ["harness", "domain", "joint", "abstain"],
            "joint_atomic": True,
            "kernel_frozen": True,
            "selected_by": "replay attribution with interface/cross-group evidence",
        },
        "contexts": {
            item["name"]: {
                "kind": item["kind"],
                "task_count": len(item["task_ids"]),
                # Match EvolutionContext.task_set_sha256 exactly: task IDs,
                # family bindings, kind and context name are all committed.
                "task_set_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "name": item["name"],
                            "kind": item["kind"],
                            "task_ids": item["task_ids"],
                            "workbook_families": item["workbook_families"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
            for item in contexts
        },
        "heldout_policy": "heldout IDs are sealed hashes only; no scores/read paths used",
        "heldout_task_ids_sha256": hashlib.sha256(
            json.dumps(config["heldout_task_ids"], ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
        "heldout_sealed": True,
        "config_sha256": loaded_config.sha256,
        "config_file_sha256": digest(config_path),
        "config_canonical_sha256": loaded_config.sha256,
        "pilot": not full_development,
    }
    atomic_json(root / "protocol.json", protocol)
    atomic_json(root / "configs/adaptive.protocol.json", {
        "protocol_sha256": digest(root / "protocol.json"),
        "config_sha256": loaded_config.sha256,
        "config_file_sha256": digest(config_path),
    })
    return {"root": str(root), "config": str(config_path), "protocol": protocol}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--max-rounds", type=int, default=3, choices=(3, 4))
    parser.add_argument("--full-development", action="store_true")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--max-total-tokens", type=int)
    parser.add_argument("--task-timeout-seconds", type=int)
    parser.add_argument("--parallelism", type=int, default=1)
    args = parser.parse_args()
    if args.max_rounds < 1:
        parser.error("--max-rounds must be positive")
    result = prepare(
        args.root,
        max_rounds=args.max_rounds,
        full_development=args.full_development,
        max_turns=args.max_turns,
        max_output_tokens=args.max_output_tokens,
        max_total_tokens=args.max_total_tokens,
        task_timeout_seconds=args.task_timeout_seconds,
        parallelism=args.parallelism,
    )
    print(json.dumps({"root": result["root"], "config": result["config"], "pilot": result["protocol"]["pilot"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
