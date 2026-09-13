#!/usr/bin/env python3
"""Run a resumable multi-plugin co-evolution probe.

The earlier H/D runner only changed two SKILL.md files and could label a
partial run as a four-arm factorial result.  This controller keeps the same
four arms, but also evaluates two composition mutations and a newly generated
coordination plugin.  Every generated artifact is isolated, and each arm is
run with the same task, model, seed, and unbounded output-token policy.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spreadsheet_harness.plugins import (
    PLUGEOLVE_SEED_COMPOSITION,
    CompositionSpec,
    default_plugin_registry,
    enumerate_single_plugin_candidates,
)

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv/bin/python"
HARNESS = REPO / ".venv/bin/sheet-harness"
BASELINE = (
    REPO
    / "benchmarks/results/true-coevolution-deepseekpro-fast-20260911/skill-roots/round-00-h0d0"
)
OLD_ROOTS = REPO / "tmp/coevolution-roots-deepseek-20260910"
DEFAULT_ROOT = REPO / "benchmarks/results/multi-plugin-coevolution-deepseekflash-20260913"
DEFAULT_MODEL = "DeepSeek-V4-Flash"
DATASETS = {
    "v06": REPO / "benchmarks/data/normalized-harbor/v06-financial-269",
    "enhanced-v2": REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565",
}
TASKS = (
    ("v06", "Financial_Model/fina_Fina_18_RoadAssets_c0"),
    ("enhanced-v2", "Financial_Model/fina_Fina_she_76be0f6c16_118661d1313_c0"),
)


@dataclass(frozen=True)
class Arm:
    name: str
    skill_root: Path
    composition_file: Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def composition_document(spec: CompositionSpec) -> dict[str, Any]:
    return {"schema_version": "plugevolve-composition-v1", **spec.to_dict()}


def prepare_generated_skill(
    root: Path,
    *,
    label: str,
    skill_name: str,
    trajectories: list[Path],
    model: str,
    base_url: str,
    api_key_file: Path,
    fallback: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Generate one candidate SKILL.md, falling back only to a known artifact."""

    generation = root / "generation" / label
    candidate = generation / "candidates" / label / "SKILL.md"
    base_skill = BASELINE / skill_name / "SKILL.md"
    if not base_skill.is_file():
        base_skill = REPO / "skills" / skill_name / "SKILL.md"
    record: dict[str, Any] = {
        "label": label,
        "skill_name": skill_name,
        "trajectories": [str(path) for path in trajectories],
        "model": model,
        "status": "pending",
    }
    if not candidate.is_file():
        command = [
            str(HARNESS),
            "evolve",
            "generate",
            *(str(path) for path in trajectories),
            "--output",
            str(generation),
            "--candidate-id",
            label,
            "--skill-name",
            skill_name,
            "--base-skill",
            str(base_skill),
            "--lesson-max-output-tokens",
            "unlimited",
            "--consolidation-max-output-tokens",
            "unlimited",
            "--base-url",
            base_url,
            "--api-key-file",
            str(api_key_file),
            "--model",
            model,
            "--api-protocol",
            "chat-completions",
            "--reasoning-effort",
            "medium",
            "--seed",
            "41",
            "--temperature",
            "0",
            "--top-p",
            "1",
            "--enable-thinking",
        ]
        generation.mkdir(parents=True, exist_ok=True)
        log = generation / "generation.log"
        completed = subprocess.run(command, cwd=REPO, stdout=log.open("w"), stderr=subprocess.STDOUT, check=False)
        record["returncode"] = completed.returncode
    if candidate.is_file():
        record["status"] = "generated"
    elif fallback is not None and fallback.is_file():
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fallback, candidate)
        record["status"] = "fallback-known-candidate"
        record["fallback"] = str(fallback)
    else:
        raise RuntimeError(f"No candidate artifact for {label}: generation failed")
    record["sha256"] = sha256(candidate)
    write_json(generation / "record.json", record)
    return candidate, record


def build_arms(
    root: Path,
    *,
    h_candidate: Path,
    d_candidate: Path,
    coordination_candidate: Path,
) -> tuple[Arm, ...]:
    if not BASELINE.is_dir():
        raise RuntimeError(f"Frozen baseline skill root is missing: {BASELINE}")
    roots = root / "skill-roots"
    root_by_name: dict[str, Path] = {}
    for name, edits in {
        "h0d0": (),
        "h1d0": (("spreadsheet-structure", h_candidate),),
        "h0d1": (("spreadsheet-financial-model", d_candidate),),
        "h1d1": (
            ("spreadsheet-structure", h_candidate),
            ("spreadsheet-financial-model", d_candidate),
        ),
        "h1d1-coordination": (
            ("spreadsheet-structure", h_candidate),
            ("spreadsheet-financial-model", d_candidate),
            ("spreadsheet-coordination", coordination_candidate),
        ),
        "h1d1-core-enable": (
            ("spreadsheet-structure", h_candidate),
            ("spreadsheet-financial-model", d_candidate),
        ),
        "h1d1-profile4": (
            ("spreadsheet-structure", h_candidate),
            ("spreadsheet-financial-model", d_candidate),
        ),
    }.items():
        destination = roots / name
        if not destination.exists():
            shutil.copytree(BASELINE, destination)
        for directory, source in edits:
            (destination / directory).mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination / directory / "SKILL.md")
        root_by_name[name] = destination

    registry = default_plugin_registry()
    base = PLUGEOLVE_SEED_COMPOSITION
    candidates = enumerate_single_plugin_candidates(
        registry,
        base,
        config_variants={"profile-deterministic-compact": [{"max-regions-per-sheet": 4}]},
    )
    coordination = next(
        item for item in candidates
        if item.operation == "enable" and item.target == "skill-spreadsheet-coordination"
    ).composition.spec
    core = next(
        item for item in candidates
        if item.operation == "enable" and item.target == "skill-spreadsheet-core"
    ).composition.spec
    profile4 = next(
        item for item in candidates
        if item.operation == "configure" and item.target == "profile-deterministic-compact"
    ).composition.spec
    specs = {
        "h0d0": base,
        "h1d0": base,
        "h0d1": base,
        "h1d1": base,
        "h1d1-coordination": coordination,
        "h1d1-core-enable": core,
        "h1d1-profile4": profile4,
    }
    arms: list[Arm] = []
    for name, spec in specs.items():
        path = root / "compositions" / f"{name}.json"
        if not path.exists():
            write_json(path, composition_document(spec))
        arms.append(Arm(name, root_by_name[name], path))
    return tuple(arms)


def run_one(
    root: Path,
    arm: Arm,
    dataset: str,
    task_id: str,
    *,
    model: str,
    base_url: str,
    api_key_file: Path,
    max_calls: int,
    timeout: float,
) -> dict[str, Any]:
    output = root / "runs" / arm.name / dataset / task_id.replace("/", "_")
    summary = output / "summary.json"
    if summary.is_file():
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
            if payload.get("study_complete"):
                return {"arm": arm.name, "dataset": dataset, "task_id": task_id, "status": "skipped", "summary": payload}
        except (OSError, json.JSONDecodeError):
            pass
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON),
        "-m",
        "spreadsheet_harness.cli",
        "benchmark",
        "v2-compare",
        "--dataset",
        str(DATASETS[dataset]),
        "--category",
        "Financial_Model",
        "--task-id",
        task_id,
        "--arm",
        "ours",
        "--composition-file",
        f"ours={arm.composition_file}",
        "--skill-root",
        str(arm.skill_root),
        "--output",
        str(output),
        "--max-model-calls",
        str(max_calls),
        "--max-turns-per-arm",
        str(max_calls),
        "--max-total-tokens",
        "unlimited",
        "--max-output-tokens",
        "unlimited",
        "--task-timeout",
        str(timeout),
        "--arm-order-seed",
        "20260913",
        "--base-url",
        base_url,
        "--api-key-file",
        str(api_key_file),
        "--model",
        model,
        "--api-protocol",
        "chat-completions",
        "--reasoning-effort",
        "medium",
        "--seed",
        "41",
        "--temperature",
        "0",
        "--top-p",
        "1",
        "--enable-thinking",
    ]
    log = output.with_suffix(".log")
    with log.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=REPO, stdout=handle, stderr=subprocess.STDOUT, check=False)
    payload: dict[str, Any] = {}
    if summary.is_file():
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    return {
        "arm": arm.name,
        "dataset": dataset,
        "task_id": task_id,
        "status": "scored" if payload.get("study_complete") else "failed",
        "returncode": completed.returncode,
        "summary": payload,
    }


def summarize(root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, list[dict[str, float]]] = {}
    for row in rows:
        arm = row["arm"]
        payload = row.get("summary") or {}
        first = next(iter((payload.get("arms") or {}).values()), {})
        metrics.setdefault(arm, []).append(
            {
                "accuracy": float(first.get("accuracy", float("nan"))),
                "modification_accuracy": float(first.get("modification_accuracy", float("nan"))),
                "regression_accuracy": float(first.get("regression_accuracy", float("nan"))),
                "model_calls": float(first.get("model_calls", 0)),
            }
        )
    aggregate: dict[str, Any] = {}
    for arm, values in sorted(metrics.items()):
        aggregate[arm] = {
            key: sum(item[key] for item in values) / len(values)
            for key in ("accuracy", "modification_accuracy", "regression_accuracy", "model_calls")
        }
        aggregate[arm]["n"] = len(values)
    document = {
        "schema_version": "multi-plugin-coevolution-report-v1",
        "model": None,
        "factorial": {
            "h0d0": "baseline",
            "h1d0": "H content candidate + D0",
            "h0d1": "H0 + D content candidate",
            "h1d1": "H content candidate + D content candidate",
            "h1d1-coordination": "H1+D1+new generated coordination plugin",
            "h1d1-core-enable": "H1+D1+registry-approved core plugin enable",
            "h1d1-profile4": "H1+D1+profile composition configuration",
        },
        "metrics": aggregate,
        "rows": rows,
    }
    write_json(root / "report.json", document)
    lines = ["# Multi-plugin co-evolution report", "", "| Arm | Exact | Modification | Regression | Calls |", "|---|---:|---:|---:|---:|"]
    for arm, values in aggregate.items():
        lines.append(
            f"| {arm} | {values['accuracy']:.4f} | {values['modification_accuracy']:.4f} | "
            f"{values['regression_accuracy']:.4f} | {values['model_calls']:.0f} |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--parallelism", type=int, default=6)
    parser.add_argument("--max-model-calls", type=int, default=50)
    parser.add_argument("--task-timeout", type=float, default=3600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.result_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not args.api_key_file.is_file():
        raise RuntimeError(f"Missing API key file: {args.api_key_file}")
    trajectories = [
        root / ".." / "true-coevolution-deepseekpro-fast-20260911" / "runs" / "heldout" / "h0d0" / "v06" / "heldout" / "Financial_Model_fina_Fina_18_RoadAssets_c0" / "runs" / "Financial_Model" / "fina_Fina_18_RoadAssets_c0" / "spreadsheet-harness-financial" / "trajectory.jsonl",
        root / ".." / "true-coevolution-deepseekpro-fast-20260911" / "runs" / "heldout" / "h0d0" / "enhanced-v2" / "heldout" / "Financial_Model_fina_Fina_she_76be0f6c16_118661d1313_c0" / "runs" / "Financial_Model" / "fina_Fina_she_76be0f6c16_118661d1313_c0" / "spreadsheet-harness-financial" / "trajectory.jsonl",
    ]
    trajectories = [path.resolve() for path in trajectories if path.resolve().is_file()]
    if not trajectories:
        raise RuntimeError("No prior failed trajectories were found for candidate generation")
    baseline_coordination = REPO / "skills/spreadsheet-coordination/SKILL.md"
    h_candidate, h_record = prepare_generated_skill(
        root,
        label="h-structure-from-failures",
        skill_name="spreadsheet-structure",
        trajectories=trajectories,
        model=args.model,
        base_url=args.base_url,
        api_key_file=args.api_key_file,
        fallback=OLD_ROOTS / "h1d0/spreadsheet-structure/SKILL.md",
    )
    d_candidate, d_record = prepare_generated_skill(
        root,
        label="d-financial-from-failures",
        skill_name="spreadsheet-financial-model",
        trajectories=trajectories,
        model=args.model,
        base_url=args.base_url,
        api_key_file=args.api_key_file,
        fallback=OLD_ROOTS / "h0d1/spreadsheet-financial-model/SKILL.md",
    )
    coordination_candidate, coordination_record = prepare_generated_skill(
        root,
        label="new-coordination-plugin-from-failures",
        skill_name="spreadsheet-coordination",
        trajectories=trajectories,
        model=args.model,
        base_url=args.base_url,
        api_key_file=args.api_key_file,
        fallback=baseline_coordination,
    )
    write_json(root / "candidate-manifest.json", {"model": args.model, "h": h_record, "d": d_record, "new_plugin": coordination_record})
    arms = build_arms(root, h_candidate=h_candidate, d_candidate=d_candidate, coordination_candidate=coordination_candidate)
    write_json(root / "arms.json", [{"name": arm.name, "skill_root": str(arm.skill_root), "composition_file": str(arm.composition_file)} for arm in arms])
    jobs = [(arm, dataset, task_id) for arm in arms for dataset, task_id in TASKS]
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallelism) as executor:
        futures = {
            executor.submit(
                run_one,
                root,
                arm,
                dataset,
                task_id,
                model=args.model,
                base_url=args.base_url,
                api_key_file=args.api_key_file,
                max_calls=args.max_model_calls,
                timeout=args.task_timeout,
            ): (arm.name, dataset, task_id)
            for arm, dataset, task_id in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    report = summarize(root, rows)
    report["model"] = args.model
    write_json(root / "report.json", report)
    print(json.dumps({"status": "complete", "report": str(root / "report.md")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
