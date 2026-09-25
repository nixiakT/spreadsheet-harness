#!/usr/bin/env python3
"""Freeze a development-only Method operator ablation.

The three arms share the same solver, proposer, family-disjoint contexts,
artifact utility and evidence.  Only the admissible operator set differs.
Held-out task IDs are hashed into every config but are never executed here.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SEARCH_ROOT = REPO / "benchmarks/results/paper36-search-alternating-qwen36plus-20260917"
DEFAULT_ROOT = REPO / "benchmarks/results/method-operator-ablation-v3-qwen36plus-glm52-20260918"
METHOD = Path(
    "/home/tongzeyuan/.codex/attachments/a1cc4bec-bb48-4972-ae68-b648a2ad026f/pasted-text.txt"
)
ARMS = {
    "revision-only": ["revision"],
    "revision-recomposition": ["revision", "recomposition"],
    "full": ["revision", "recomposition", "synthesis"],
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path, pattern: str) -> str:
    rows = [
        (path.relative_to(root).as_posix(), digest(path))
        for path in sorted(root.rglob(pattern))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def canonical_trajectory(task: dict[str, Any]) -> Path:
    slug = str(task["task_id"]).replace("/", "_")
    base = (
        SEARCH_ROOT / "runs/evolution/incumbent-r00" / str(task["dataset"]) / "development" / slug
    )
    paths = [
        path
        for path in base.glob("runs/**/trajectory.jsonl")
        if ".incomplete." not in path.as_posix()
    ]
    if len(paths) != 1:
        raise RuntimeError(f"Expected one canonical development trajectory for {task['task_id']}")
    return paths[0].resolve()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("Refusing to overwrite a non-empty ablation root")

    manifest_path = SEARCH_ROOT / "split-manifest.json"
    endpoint_path = SEARCH_ROOT / "search-final.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
    if endpoint.get("heldout_opened") is not False:
        raise RuntimeError("Source search held-out state is not sealed")
    tasks = manifest["tasks"]
    role_map = {"development": "replay", "transfer": "transfer", "regression": "regression"}
    contexts = []
    for source_role, kind in role_map.items():
        selected = [task for task in tasks if task["role"] == source_role]
        if len(selected) < 2:
            raise RuntimeError(f"Context {kind} has fewer than two workbook families")
        contexts.append(
            {
                "name": kind,
                "kind": kind,
                "task_ids": [task["task_id"] for task in selected],
                "workbook_families": [task["source_workbook"] for task in selected],
                "weight": 1.0,
                "runner": {
                    "task_datasets": {
                        task["task_id"]: str((REPO / task["dataset_root"]).resolve())
                        for task in selected
                    }
                },
            }
        )
    context_families = [family for context in contexts for family in context["workbook_families"]]
    if len(context_families) != len(set(context_families)):
        raise RuntimeError("Replay, transfer and regression are not family-disjoint")

    development = [task for task in tasks if task["role"] == "development"]
    evidence = [
        {
            "path": str(canonical_trajectory(task)),
            "task_id": task["task_id"],
            "task_type": "Financial_Model",
            "workbook_family": task["source_workbook"],
        }
        for task in development
    ]
    heldout = [task["task_id"] for task in tasks if task["role"] == "heldout"]
    proposer = REPO / "tools/propose_method_candidate.py"
    evaluator = REPO / "tools/evaluate_continuous_financial_20260911.py"
    base_config = {
        "schema_version": "continuous-plugin-evolution-config-v1",
        "repository_root": str(REPO),
        "composition": "spreadsheet-harness-financial",
        "groups": {
            "harness": [
                "control-ours",
                "observe-profile-compact",
                "observe-profile-full",
                "knowledge-coordination",
            ],
            "domain": ["knowledge-financial-model", "knowledge-formula"],
        },
        "contexts": contexts,
        "heldout_task_ids": heldout,
        "initial_evidence": evidence,
        "evaluation_binding": {
            "protocol": "paired-official-family-bootstrap-method-v1",
            "solver_model": "qwen3.6-plus",
            "provider": "http://10.130.138.46:8010/v1",
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "max_model_calls": 50,
            "max_turns": 50,
            "max_total_tokens": None,
            "max_output_tokens": None,
            "task_timeout_seconds": 7200,
            "seed": 41,
            "incumbent_cache_root": str(root / "incumbent-cache"),
            "score_weights": {
                "accuracy": 1.0,
                "modification_accuracy": 0.25,
                "regression_accuracy": 0.10,
            },
            "family_aggregation": "mean-tasks-then-uniform-families",
            "evaluator_sha256": digest(
                REPO
                / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
            ),
        },
        "promotion": {
            "delta": 0.000001,
            "replay_delta": 0.0,
            "transfer_delta": 0.0,
            "epsilon": 0.0,
            "confidence": 0.90,
            "bootstrap_samples": 4000,
            "min_pairs_per_context": 2,
        },
        "first_group": "harness",
        "max_rounds": 1,
        "max_candidates_per_round": 1,
        "proposer_command": [
            str(REPO / ".venv/bin/python"),
            str(proposer),
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
        ],
        "evaluator_command": [
            str(REPO / ".venv/bin/python"),
            str(evaluator),
            "{request}",
            "{response}",
        ],
        "command_timeout_seconds": 43200,
        "static_checks": [[str(REPO / ".venv/bin/python"), "-m", "compileall", "-q", "{source}"]],
    }
    root.mkdir(parents=True)
    configs = {}
    for arm, operators in ARMS.items():
        config = {**base_config, "allowed_operators": operators}
        config_path = root / "configs" / f"{arm}.json"
        atomic_json(config_path, config)
        configs[arm] = {
            "path": str(config_path.relative_to(REPO)),
            "sha256": digest(config_path),
            "allowed_operators": operators,
        }
    protocol = {
        "schema_version": "method-operator-ablation-v1",
        "purpose": "development-only operator ablation; no held-out feedback",
        "source_search": str(SEARCH_ROOT.relative_to(REPO)),
        "source_heldout_opened": False,
        "split_manifest_sha256": digest(manifest_path),
        "source_endpoint_sha256": digest(endpoint_path),
        "method_sha256": digest(METHOD),
        "proposer_sha256": digest(proposer),
        "evaluator_adapter_sha256": digest(evaluator),
        "continuous_controller_sha256": digest(
            REPO / "src/spreadsheet_harness/continuous_evolution.py"
        ),
        "source_tree_sha256": tree_digest(REPO / "src/spreadsheet_harness", "*.py"),
        "skill_tree_sha256": tree_digest(REPO / "skills", "SKILL.md"),
        "solver": "qwen3.6-plus",
        "proposer": "dashscope/glm-5.2",
        "temperature": 0.0,
        "top_p": 1.0,
        "thinking": True,
        "max_calls_and_turns": 50,
        "token_limits": None,
        "contexts": {context["name"]: len(context["task_ids"]) for context in contexts},
        "artifact_score": "(accuracy + 0.25*modification_accuracy + 0.10*regression_accuracy) / 1.35",
        "confidence": 0.90,
        "configs": configs,
    }
    atomic_json(root / "protocol.json", protocol)
    print(json.dumps({"root": str(root), **protocol["contexts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
