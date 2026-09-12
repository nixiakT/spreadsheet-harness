#!/usr/bin/env python3
"""Run a bounded, resumable H/D co-evolution study on the two Harbor datasets.

The controller keeps workbook families disjoint across development, transfer,
regression, and held-out splits.  It alternates accepted mutations of the
spreadsheet-structure (H) and spreadsheet-financial-model (D) skills, evaluates
each candidate against the current joint incumbent, and only exposes the
held-out set after four accepted coordinate updates.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from spreadsheet_harness.capability_evolution import four_arm_metrics

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv/bin/python"
HARNESS = REPO / ".venv/bin/sheet-harness"
DEFAULT_RESULT_ROOT = REPO / "benchmarks/results/true-coevolution-deepseekpro-fast-20260911"
MODEL = "DeepSeek-V4-Pro"
SEED = 20260911
COORDINATES = (
    ("harness", "spreadsheet-structure"),
    ("domain", "spreadsheet-financial-model"),
    ("harness", "spreadsheet-structure"),
    ("domain", "spreadsheet-financial-model"),
)
DATASETS = {
    "v06": REPO / "benchmarks/data/normalized-harbor/v06-financial-269",
    "enhanced-v2": REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565",
}

# One task is selected per source workbook.  The validation sample emphasizes
# hard cases; the held-out sample remains balanced across both supplied sets.
SPLIT_QUOTAS: Mapping[str, Mapping[str, Mapping[str, int]]] = {
    "development": {
        "v06": {"C3": 1, "C2": 1},
        "enhanced-v2": {"C3": 1, "C2": 1},
    },
    "transfer": {
        "v06": {"C3": 1, "C2": 1},
        "enhanced-v2": {"C3": 1, "C2": 1},
    },
    "regression": {
        "v06": {"C1": 1},
        "enhanced-v2": {"C1": 1},
    },
    "heldout": {
        "v06": {"C3": 3, "C2": 2, "C1": 1},
        "enhanced-v2": {"C3": 3, "C2": 2, "C1": 1},
    },
}
EVOLUTION_ROLES = ("development", "transfer", "regression")
FINAL_ARMS = ("h0d0", "h1d0", "h0d1", "h1d1")

# These two C3 cases repeatedly consume nearly all 50 allowed model calls.  They
# remain in the first full replay and in the final held-out/challenge analysis,
# but can be omitted from subsequent coordinate gates when --fast-gate is used.
RECURRENT_GATE_EXCLUSIONS = {
    "Financial_Model/fina_Debu_01_c0",
    "Financial_Model/fina_Fina_02_PP_c0",
}


@dataclass(frozen=True)
class Task:
    dataset: str
    dataset_root: Path
    task_id: str
    complexity: str
    source_workbook: str
    role: str

    @property
    def slug(self) -> str:
        return self.task_id.replace("/", "_")

    def to_dict(self) -> dict[str, str]:
        return {
            "dataset": self.dataset,
            "dataset_root": str(self.dataset_root.relative_to(REPO)),
            "task_id": self.task_id,
            "complexity": self.complexity,
            "source_workbook": self.source_workbook,
            "role": self.role,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, document: Mapping[str, Any] | Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_rows(dataset_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    path = dataset_root / "Financial_Model/dataset.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError(f"Dataset metadata is not a list: {path}")
    return path, [dict(row) for row in raw]


def selection_key(dataset: str, role: str, complexity: str, source: str) -> str:
    material = f"{SEED}\0{dataset}\0{role}\0{complexity}\0{source}".encode()
    return hashlib.sha256(material).hexdigest()


def input_size(dataset_root: Path, row: Mapping[str, Any]) -> int:
    path = dataset_root / "Financial_Model/spreadsheet" / f"{row['id']}_input.xlsx"
    return path.stat().st_size


def create_split_manifest(path: Path) -> dict[str, Any]:
    dataset_rows: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, Any] = {}
    for label, root in DATASETS.items():
        dataset_json, rows = load_rows(root)
        dataset_rows[label] = rows
        metadata[label] = {
            "root": str(root.relative_to(REPO)),
            "task_count": len(rows),
            "source_workbook_count": len({str(row["source_workbook"]) for row in rows}),
            "dataset_json_sha256": sha256_file(dataset_json),
        }

    selected: list[Task] = []
    used_sources: dict[str, set[str]] = defaultdict(set)
    for role, dataset_quotas in SPLIT_QUOTAS.items():
        for dataset, complexity_quotas in dataset_quotas.items():
            rows = dataset_rows[dataset]
            by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                by_source[str(row["source_workbook"])].append(row)
            for complexity, count in complexity_quotas.items():
                eligible = [
                    source
                    for source, source_rows in by_source.items()
                    if source not in used_sources[dataset]
                    and any(str(row.get("complexity")) == complexity for row in source_rows)
                ]
                # Evolution contexts are replayed after every coordinate update.
                # Prefer compact workbooks within each official complexity stratum
                # so hard-case coverage does not turn four rounds into a multi-day
                # workbook-preprocessing workload. Hash ordering keeps ties frozen.
                eligible.sort(
                    key=lambda source: (
                        min(
                            input_size(DATASETS[dataset], row)
                            for row in by_source[source]
                            if str(row.get("complexity")) == complexity
                        ),
                        selection_key(dataset, role, complexity, source),
                    )
                )
                if len(eligible) < count:
                    raise RuntimeError(
                        f"Insufficient {dataset} {complexity} source workbooks for {role}: "
                        f"need {count}, found {len(eligible)}"
                    )
                for source in eligible[:count]:
                    candidates = sorted(
                        (
                            row
                            for row in by_source[source]
                            if str(row.get("complexity")) == complexity
                        ),
                        key=lambda row: str(row["id"]),
                    )
                    row = candidates[0]
                    used_sources[dataset].add(source)
                    selected.append(
                        Task(
                            dataset=dataset,
                            dataset_root=DATASETS[dataset],
                            task_id=f"Financial_Model/{row['id']}",
                            complexity=complexity,
                            source_workbook=source,
                            role=role,
                        )
                    )

    manifest: dict[str, Any] = {
        "schema_version": "true-hd-coevolution-split-v1",
        "created_at": utc_now(),
        "seed": SEED,
        "split_unit": "source_workbook",
        "dataset_role": "calibration_only",
        "datasets": metadata,
        "tasks": [task.to_dict() for task in selected],
        "counts": {
            role: sum(task.role == role for task in selected) for role in SPLIT_QUOTAS
        },
        "policy": {
            "development": "candidate generation and replay validation",
            "transfer": "candidate selection only; never candidate generation",
            "regression": "candidate selection only; unchanged-cell regression guard",
            "heldout": "sealed until four accepted coordinate updates",
        },
    }
    atomic_json(path, manifest)
    return manifest


def validate_manifest(manifest: Mapping[str, Any]) -> list[Task]:
    if manifest.get("schema_version") != "true-hd-coevolution-split-v1":
        raise RuntimeError("Unexpected split manifest schema")
    for label, root in DATASETS.items():
        dataset_json, _ = load_rows(root)
        expected = ((manifest.get("datasets") or {}).get(label) or {}).get(
            "dataset_json_sha256"
        )
        if expected != sha256_file(dataset_json):
            raise RuntimeError(f"Frozen dataset metadata changed for {label}")
    tasks: list[Task] = []
    seen: dict[tuple[str, str], str] = {}
    for raw in manifest.get("tasks") or []:
        task = Task(
            dataset=str(raw["dataset"]),
            dataset_root=REPO / str(raw["dataset_root"]),
            task_id=str(raw["task_id"]),
            complexity=str(raw["complexity"]),
            source_workbook=str(raw["source_workbook"]),
            role=str(raw["role"]),
        )
        key = (task.dataset, task.source_workbook)
        previous = seen.setdefault(key, task.role)
        if previous != task.role:
            raise RuntimeError(f"Source-workbook leakage across {previous}/{task.role}: {key}")
        tasks.append(task)
    expected_counts = {
        role: sum(sum(values.values()) for values in quotas.values())
        for role, quotas in SPLIT_QUOTAS.items()
    }
    actual_counts = Counter(task.role for task in tasks)
    if any(actual_counts[role] != count for role, count in expected_counts.items()):
        raise RuntimeError(f"Frozen split has unexpected role counts: {dict(actual_counts)}")
    return tasks


def load_or_create_manifest(root: Path) -> tuple[dict[str, Any], list[Task]]:
    path = root / "split-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else create_split_manifest(path)
    return manifest, validate_manifest(manifest)


def prepare_baseline_root(root: Path) -> Path:
    destination = root / "skill-roots/round-00-h0d0"
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / "skills", destination)
    required = [
        destination / "spreadsheet-structure/SKILL.md",
        destination / "spreadsheet-financial-model/SKILL.md",
    ]
    if not all(path.is_file() for path in required):
        raise RuntimeError(f"Baseline skill root is incomplete: {destination}")
    return destination


def is_scored_summary(path: Path) -> bool:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        arm = next(iter(document.get("arms", {}).values()))
        score = arm.get("official_score") or arm
        return (
            bool(document.get("study_complete"))
            and int(arm.get("completed") or 0) == int(arm.get("expected") or 1)
            and isinstance(score.get("accuracy"), (int, float))
            and isinstance(score.get("modification_accuracy"), (int, float))
            and isinstance(score.get("regression_accuracy"), (int, float))
        )
    except (OSError, ValueError, StopIteration, TypeError):
        return False


def summary_payload(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "summary.json"
    if not is_scored_summary(path):
        raise RuntimeError(f"Task has no scored summary: {run_dir}")
    document = json.loads(path.read_text(encoding="utf-8"))
    arm = next(iter(document["arms"].values()))
    score = dict(arm.get("official_score") or arm)
    error_message = str(score.get("error_message") or "")
    results_path = run_dir / "results.json"
    if not error_message and results_path.is_file():
        try:
            result_rows = json.loads(results_path.read_text(encoding="utf-8"))
            if isinstance(result_rows, list) and result_rows:
                official = result_rows[0].get("official_score") or {}
                error_message = str(official.get("error_message") or "")
        except (OSError, ValueError, TypeError):
            pass
    return {
        "accuracy": float(score["accuracy"]),
        "modification_accuracy": float(score.get("modification_accuracy", 0.0)),
        "regression_accuracy": float(score.get("regression_accuracy", 0.0)),
        "model_calls": int(arm.get("model_calls") or score.get("interaction_turns") or 0),
        "errors": int(arm.get("errors") or 0),
        "error_message": error_message,
    }


def trajectory_path(run_dir: Path) -> Path:
    paths = sorted(run_dir.rglob("trajectory.jsonl"))
    if len(paths) != 1:
        raise RuntimeError(f"Expected one trajectory beneath {run_dir}, found {len(paths)}")
    return paths[0]


class Experiment:
    def __init__(self, args: argparse.Namespace, tasks: Sequence[Task]) -> None:
        self.args = args
        self.root = args.result_root.resolve()
        self.tasks = tuple(tasks)
        self.status_path = self.root / "run-status.tsv"
        self._status_lock = threading.Lock()

    def record_status(
        self, phase: str, label: str, task: Task, status: str, code: int, attempt: int
    ) -> None:
        line = "\t".join(
            (
                utc_now(),
                phase,
                label,
                task.dataset,
                task.role,
                task.complexity,
                task.task_id,
                status,
                str(code),
                str(attempt),
            )
        )
        with self._status_lock:
            if not self.status_path.exists():
                self.status_path.write_text(
                    "timestamp\tphase\tlabel\tdataset\trole\tcomplexity\ttask\tstatus\t"
                    "exit_code\tattempt\n",
                    encoding="utf-8",
                )
            with self.status_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def task_dir(self, phase: str, label: str, task: Task) -> Path:
        return self.root / "runs" / phase / label / task.dataset / task.role / task.slug

    def recover_scored_archive(self, output: Path) -> bool:
        """Restore a scored run that an older completion check misclassified."""

        if is_scored_summary(output / "summary.json"):
            return True
        scored_archives = [
            path
            for path in sorted(output.parent.glob(f"{output.name}.incomplete.*"))
            if is_scored_summary(path / "summary.json")
        ]
        if not scored_archives:
            return False
        if output.exists():
            interrupted = output.with_name(
                f"{output.name}.superseded.{datetime.now().strftime('%Y%m%dT%H%M%S')}"
            )
            output.rename(interrupted)
        scored_archives[-1].rename(output)
        return True

    def run_one(
        self,
        phase: str,
        label: str,
        skill_root: Path,
        task: Task,
        max_model_calls: int | None = None,
    ) -> None:
        output = self.task_dir(phase, label, task)
        log = output.with_suffix(".log")
        if self.recover_scored_archive(output):
            self.record_status(phase, label, task, "skipped-scored", 0, 0)
            return
        for attempt in range(1, self.args.task_attempts + 1):
            if output.exists():
                archived = output.with_name(
                    f"{output.name}.incomplete.{datetime.now().strftime('%Y%m%dT%H%M%S')}.a{attempt}"
                )
                output.rename(archived)
            if log.exists():
                log.rename(log.with_name(f"{log.name}.previous.a{attempt}"))
            output.parent.mkdir(parents=True, exist_ok=True)
            command = [
                str(PYTHON),
                "-m",
                "spreadsheet_harness.cli",
                "benchmark",
                "v2-compare",
                "--dataset",
                str(task.dataset_root),
                "--category",
                "Financial_Model",
                "--task-id",
                task.task_id,
                "--arm",
                "spreadsheet-harness-financial",
                "--skill-root",
                str(skill_root),
                "--output",
                str(output),
                "--max-model-calls",
                str(max_model_calls or 50),
                "--max-turns-per-arm",
                "50",
                "--max-total-tokens",
                "unlimited",
                "--max-output-tokens",
                "unlimited",
                "--task-timeout",
                str(self.args.task_timeout),
                "--request-timeout",
                "1800",
                "--litellm-timeout",
                "1800",
                "--request-retries",
                "5",
                "--request-interval-seconds",
                str(self.args.request_interval),
                "--arm-order-seed",
                str(SEED),
                "--base-url",
                self.args.base_url,
                "--api-key-file",
                str(self.args.api_key_file),
                "--model",
                self.args.model,
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
            with log.open("w", encoding="utf-8") as handle:
                completed = subprocess.run(
                    command,
                    cwd=REPO,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            if completed.returncode == 0 and is_scored_summary(output / "summary.json"):
                self.record_status(phase, label, task, "scored", 0, attempt)
                return
            code = completed.returncode or 2
            self.record_status(phase, label, task, "failed-attempt", code, attempt)
        raise RuntimeError(f"Task exhausted retries: {label} {task.task_id}")

    def run_batch(
        self,
        phase: str,
        label: str,
        skill_root: Path,
        tasks: Sequence[Task],
        max_model_calls: int | None = None,
    ) -> None:
        print(f"[{utc_now()}] start {phase}/{label}: {len(tasks)} tasks", flush=True)
        failures: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.parallelism) as executor:
            future_tasks = {
                executor.submit(
                    self.run_one, phase, label, skill_root, task, max_model_calls
                ): task
                for task in tasks
            }
            for future in concurrent.futures.as_completed(future_tasks):
                task = future_tasks[future]
                try:
                    future.result()
                except Exception as exc:  # preserve other workers and report all failures
                    failures.append(f"{task.task_id}: {exc}")
        if failures:
            raise RuntimeError("Batch failures:\n" + "\n".join(failures))
        print(f"[{utc_now()}] complete {phase}/{label}", flush=True)

    def run_matrix(
        self,
        phase: str,
        arms: Mapping[str, Path],
        tasks: Sequence[Task],
    ) -> None:
        total = len(arms) * len(tasks)
        print(f"[{utc_now()}] start {phase} matrix: {total} runs", flush=True)
        failures: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.parallelism) as executor:
            futures = {
                executor.submit(self.run_one, phase, label, skill_root, task): (label, task)
                for label, skill_root in arms.items()
                for task in tasks
            }
            for future in concurrent.futures.as_completed(futures):
                label, task = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append(f"{label} {task.task_id}: {exc}")
        if failures:
            raise RuntimeError("Matrix failures:\n" + "\n".join(failures))
        print(f"[{utc_now()}] complete {phase} matrix", flush=True)

    def result_rows(self, phase: str, label: str, tasks: Sequence[Task]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in tasks:
            rows.append({**task.to_dict(), **summary_payload(self.task_dir(phase, label, task))})
        return rows

    def attribute_failures(
        self, phase: str, label: str, tasks: Sequence[Task], output: Path
    ) -> list[dict[str, Any]]:
        attributions: list[dict[str, Any]] = []
        for task in tasks:
            run_dir = self.task_dir(phase, label, task)
            score = summary_payload(run_dir)
            events: list[dict[str, Any]] = []
            for line in trajectory_path(run_dir).read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            names = [str(event.get("event") or "").casefold() for event in events]
            agent_events = [
                event for event in events if str(event.get("event") or "").endswith("agent.completed")
            ]
            tool_errors = max(
                (
                    int((event.get("payload") or {}).get("tool_errors") or 0)
                    for event in agent_events
                ),
                default=0,
            )
            if any(name == "model.failed" or "provider.failed" in name for name in names):
                route, reason = "infrastructure", "provider/model failure event"
            elif tool_errors or any("planner.failed" in name for name in names):
                route, reason = "interface", f"tool_errors={tool_errors} or planner failure"
            elif score["regression_accuracy"] < 1.0:
                route, reason = "harness", "unchanged-cell regression"
            elif score["modification_accuracy"] < 1.0:
                route, reason = "domain", "incorrect or incomplete target-cell modification"
            else:
                route, reason = "none", "official evaluator passed"
            attributions.append(
                {
                    **task.to_dict(),
                    "route": route,
                    "reason": reason,
                    "score": score,
                    "trajectory": str(trajectory_path(run_dir).relative_to(self.root)),
                    "trajectory_sha256": sha256_file(trajectory_path(run_dir)),
                }
            )
        atomic_json(output, attributions)
        return attributions

    def generate_candidate(
        self,
        *,
        round_number: int,
        attempt: int,
        coordinate: str,
        skill_name: str,
        incumbent_root: Path,
        evidence_phase: str,
        evidence_label: str,
        evidence_tasks: Sequence[Task],
    ) -> tuple[Path, Path]:
        candidate_id = f"r{round_number:02d}-{coordinate}-a{attempt}"
        generation_root = self.root / "generation" / candidate_id
        generated_skill = generation_root / "candidates" / candidate_id / "SKILL.md"
        candidate_root = self.root / "skill-roots" / candidate_id
        if generated_skill.is_file() and candidate_root.is_dir():
            return generated_skill, candidate_root

        attribution_path = self.root / "attribution" / f"{evidence_label}.json"
        attributions = self.attribute_failures(
            evidence_phase, evidence_label, evidence_tasks, attribution_path
        )
        desired_routes = {"harness", "interface"} if coordinate == "harness" else {"domain"}
        selected = [
            task
            for task, attribution in zip(evidence_tasks, attributions, strict=True)
            if task.role == "development" and attribution["route"] in desired_routes
        ]
        selection_policy = "coordinate-attributed-development-failures"
        if not selected:
            selected = [
                task
                for task, attribution in zip(evidence_tasks, attributions, strict=True)
                if task.role == "development" and attribution["route"] != "infrastructure"
            ]
            selection_policy = "fallback-all-scored-development-trajectories"
        if not selected:
            raise RuntimeError(f"No eligible development trajectories for {candidate_id}")
        selection_document = {
            "candidate_id": candidate_id,
            "coordinate": coordinate,
            "policy": selection_policy,
            "desired_routes": sorted(desired_routes),
            "selected_tasks": [task.to_dict() for task in selected],
        }
        atomic_json(generation_root / "evidence-selection.json", selection_document)
        trajectories = [
            trajectory_path(self.task_dir(evidence_phase, evidence_label, task)) for task in selected
        ]
        command = [
            str(HARNESS),
            "evolve",
            "generate",
            *(str(path) for path in trajectories),
            "--output",
            str(generation_root),
            "--candidate-id",
            candidate_id,
            "--skill-name",
            skill_name,
            "--base-skill",
            str(incumbent_root / skill_name / "SKILL.md"),
            "--lesson-max-output-tokens",
            "unlimited",
            "--consolidation-max-output-tokens",
            "unlimited",
            "--base-url",
            self.args.base_url,
            "--api-key-file",
            str(self.args.api_key_file),
            "--model",
            self.args.model,
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
        generation_root.mkdir(parents=True, exist_ok=True)
        log = generation_root / "generation.log"
        with log.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if completed.returncode or not generated_skill.is_file():
            raise RuntimeError(f"Candidate generation failed ({completed.returncode}): {candidate_id}")
        if candidate_root.exists():
            raise RuntimeError(f"Partial candidate skill root already exists: {candidate_root}")
        shutil.copytree(incumbent_root, candidate_root)
        shutil.copy2(generated_skill, candidate_root / skill_name / "SKILL.md")
        return generated_skill, candidate_root


def mean_metric(rows: Sequence[Mapping[str, Any]], metric: str) -> float:
    values = [float(row[metric]) for row in rows]
    return fmean(values) if values else math.nan


def grouped_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[Mapping[str, Any]]] = {"all": list(rows)}
    for row in rows:
        groups.setdefault(f"role:{row['role']}", []).append(row)
        groups.setdefault(f"dataset:{row['dataset']}", []).append(row)
        groups.setdefault(f"complexity:{row['complexity']}", []).append(row)
    return {
        group: {
            "n": len(items),
            "accuracy": mean_metric(items, "accuracy"),
            "modification_accuracy": mean_metric(items, "modification_accuracy"),
            "regression_accuracy": mean_metric(items, "regression_accuracy"),
            "model_calls": sum(int(item["model_calls"]) for item in items),
        }
        for group, items in sorted(groups.items())
    }


def candidate_decision(
    incumbent: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    incumbent_groups = grouped_metrics(incumbent)
    candidate_groups = grouped_metrics(candidate)
    deltas: dict[str, dict[str, float]] = {}
    for group in incumbent_groups:
        deltas[group] = {
            metric: float(candidate_groups[group][metric]) - float(incumbent_groups[group][metric])
            for metric in ("accuracy", "modification_accuracy", "regression_accuracy")
        }
    quality_delta = (
        deltas["all"]["accuracy"]
        + 0.25 * deltas["all"]["modification_accuracy"]
        + 0.10 * deltas["all"]["regression_accuracy"]
    )
    blockers: list[str] = []
    if deltas["role:development"]["accuracy"] < -1e-12:
        blockers.append("development-exact-pass-regression")
    if deltas["role:transfer"]["accuracy"] < -1e-12:
        blockers.append("transfer-exact-pass-regression")
    if deltas["role:regression"]["regression_accuracy"] < -1e-12:
        blockers.append("unchanged-cell-regression")
    if deltas["all"]["modification_accuracy"] < -0.06:
        blockers.append("target-cell-accuracy-drop-over-0.06")
    if quality_delta < -1e-12:
        blockers.append("negative-weighted-quality-delta")
    return {
        "schema_version": "true-hd-candidate-decision-v1",
        "accepted": not blockers,
        "blockers": blockers,
        "quality_delta": quality_delta,
        "incumbent": incumbent_groups,
        "candidate": candidate_groups,
        "deltas": deltas,
        "gate": {
            "development_accuracy_min_delta": 0.0,
            "transfer_accuracy_min_delta": 0.0,
            "regression_unchanged_cell_min_delta": 0.0,
            "overall_modification_accuracy_min_delta": -0.06,
            "weighted_quality_min_delta": 0.0,
            "weighted_quality": "accuracy + 0.25*modification_accuracy + 0.10*regression_accuracy",
        },
    }


def skill_sha(root: Path, skill_name: str) -> str:
    return sha256_file(root / skill_name / "SKILL.md")


def materialize_final_roots(result_root: Path, baseline: Path, joint: Path) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for arm in FINAL_ARMS:
        destination = result_root / "skill-roots/final" / arm
        if not destination.exists():
            shutil.copytree(baseline, destination)
            if arm in {"h1d0", "h1d1"}:
                shutil.copy2(
                    joint / "spreadsheet-structure/SKILL.md",
                    destination / "spreadsheet-structure/SKILL.md",
                )
            if arm in {"h0d1", "h1d1"}:
                shutil.copy2(
                    joint / "spreadsheet-financial-model/SKILL.md",
                    destination / "spreadsheet-financial-model/SKILL.md",
                )
        roots[arm] = destination
    return roots


def write_final_report(
    experiment: Experiment,
    tasks: Sequence[Task],
    round_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    arm_rows = {
        arm: experiment.result_rows("heldout", arm, tasks) for arm in FINAL_ARMS
    }
    metrics = {arm: grouped_metrics(rows) for arm, rows in arm_rows.items()}
    interaction_groups: dict[str, Any] = {}
    for group in metrics["h0d0"]:
        interaction_groups[group] = four_arm_metrics(
            {arm: float(metrics[arm][group]["accuracy"]) for arm in FINAL_ARMS}
        )
    document: dict[str, Any] = {
        "schema_version": "true-hd-coevolution-report-v1",
        "completed_at": utc_now(),
        "model": experiment.args.model,
        "settings": {
            "max_model_calls_per_task": 50,
            "max_total_tokens": None,
            "max_output_tokens": None,
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "target_rounds": len(round_records),
            "heldout_limit": len(tasks),
            "accelerated_modification_gate": -0.06,
        },
        "accepted_rounds": list(round_records),
        "heldout": {"metrics": metrics, "interactions": interaction_groups, "rows": arm_rows},
    }
    atomic_json(experiment.root / "final-report.json", document)
    lines = [
        "# True H/D co-evolution held-out report",
        "",
        f"Model: `{experiment.args.model}`. Held-out tasks: {len(tasks)}. Accepted rounds: {len(round_records)}.",
        "",
        "| Arm | Exact pass | Modification | Regression | Model calls |",
        "|---|---:|---:|---:|---:|",
    ]
    for arm in FINAL_ARMS:
        row = metrics[arm]["all"]
        lines.append(
            f"| {arm} | {row['accuracy']:.4f} | {row['modification_accuracy']:.4f} | "
            f"{row['regression_accuracy']:.4f} | {row['model_calls']} |"
        )
    overall = interaction_groups["all"]
    lines.extend(
        [
            "",
            f"Interaction gain `I`: {overall['interaction_gain']:+.4f}",
            f"Joint gain: {overall['joint_gain']:+.4f}",
            "",
            f"Accelerated protocol: {len(round_records)} accepted evolution round(s); "
            f"{len(tasks)} held-out task(s); modification gate -0.06.",
            "",
            "The held-out sample is intentionally hard-case enriched and should be reported with "
            "dataset/complexity strata rather than as a population-weighted benchmark estimate.",
        ]
    )
    (experiment.root / "final-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return document


def load_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", type=Path, default=DEFAULT_RESULT_ROOT)
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--parallelism", type=int, default=8)
    parser.add_argument("--task-attempts", type=int, default=2)
    parser.add_argument("--candidate-attempts", type=int, default=2)
    parser.add_argument("--gate-model-calls", type=int, default=50)
    parser.add_argument("--target-rounds", type=int, default=len(COORDINATES))
    parser.add_argument("--heldout-limit", type=int, default=0)
    parser.add_argument("--task-timeout", type=int, default=3600)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--fast-gate", action="store_true")
    parser.add_argument("--prewarm-baseline-heldout", action="store_true")
    args = parser.parse_args(argv)
    if (
        args.parallelism < 1
        or args.task_attempts < 1
        or args.candidate_attempts < 1
        or args.gate_model_calls < 1
        or args.target_rounds < 1
        or args.target_rounds > len(COORDINATES)
        or args.heldout_limit < 0
    ):
        parser.error("parallelism, attempt limits, gate-model-calls, target-rounds, and heldout-limit are invalid")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result_root = args.result_root.resolve()
    result_root.mkdir(parents=True, exist_ok=True)
    manifest, tasks = load_or_create_manifest(result_root)
    baseline = prepare_baseline_root(result_root)
    evolution_tasks = [task for task in tasks if task.role in EVOLUTION_ROLES]
    heldout_tasks = [task for task in tasks if task.role == "heldout"]
    if args.heldout_limit:
        # Keep one C2 and one C3 per dataset for a bounded, stratified challenge
        # sample when a full sealed matrix would exceed the available window.
        selected: list[Task] = []
        # Prefer the already-completed cross-arm intersection when resuming a
        # partially interrupted broad matrix, then fill remaining slots by the
        # frozen dataset/complexity order.
        preferred = (("enhanced-v2", "C1"), ("v06", "C3"), ("enhanced-v2", "C3"), ("v06", "C2"))
        ordered = list(preferred) + [
            (dataset, complexity)
            for dataset in sorted({task.dataset for task in heldout_tasks})
            for complexity in ("C3", "C2", "C1")
        ]
        for dataset, complexity in ordered:
            match = next(
                (
                    task
                    for task in heldout_tasks
                    if task.dataset == dataset and task.complexity == complexity
                ),
                None,
            )
            if match is not None and match not in selected:
                selected.append(match)
            if len(selected) >= args.heldout_limit:
                break
        heldout_tasks = selected
    recurrent_tasks = [
        task for task in evolution_tasks if task.task_id not in RECURRENT_GATE_EXCLUSIONS
    ]
    if not args.fast_gate:
        recurrent_tasks = evolution_tasks
    print(
        json.dumps(
            {
                "result_root": str(result_root),
                "evolution_tasks": len(evolution_tasks),
                "heldout_tasks": len(heldout_tasks),
                "split_counts": manifest["counts"],
                "model": args.model,
                "parallelism": args.parallelism,
                "recurrent_gate_tasks": len(recurrent_tasks),
            },
            indent=2,
        ),
        flush=True,
    )
    if args.prepare_only:
        return 0
    if not args.api_key_file.is_file():
        raise RuntimeError(f"Missing API key file: {args.api_key_file}")
    if args.model != MODEL:
        print(f"warning: model override in use: {args.model}", file=sys.stderr, flush=True)

    experiment = Experiment(args, tasks)
    if args.prewarm_baseline_heldout:
        # Compute only the invariant baseline arm.  Its scores are deliberately
        # not loaded or reported until all four coordinate updates are accepted.
        experiment.run_batch("heldout", "h0d0", baseline, heldout_tasks)
        atomic_json(
            result_root / "heldout-h0d0-prewarm.json",
            {
                "schema_version": "sealed-heldout-prewarm-v1",
                "arm": "h0d0",
                "tasks": len(heldout_tasks),
                "scores_exposed": False,
                "completed_at": utc_now(),
            },
        )
        return 0

    if args.fast_gate:
        atomic_json(
            result_root / "recurrent-gate-protocol.json",
            {
                "schema_version": "true-hd-recurrent-gate-v1",
                "first_candidate_replay": "all development, transfer, and regression tasks",
                "subsequent_candidate_replay": "recurrent gate subset",
                "recurrent_gate_task_ids": [task.task_id for task in recurrent_tasks],
                "challenge_task_ids": sorted(RECURRENT_GATE_EXCLUSIONS),
                "rationale": (
                    "Preserve full first-round validation while moving two 50-call C3 "
                    "long-tail cases out of the serial coordinate-update path."
                ),
            },
        )
    state_path = result_root / "state.json"
    state = load_json(state_path) if state_path.exists() else {
        "schema_version": "true-hd-coevolution-state-v1",
        "accepted_rounds": [],
        "current_skill_root": str(baseline),
        "status": "evolving",
    }
    current_root = Path(str(state["current_skill_root"]))
    accepted_records = list(state.get("accepted_rounds") or [])

    experiment.run_batch("evolution", "incumbent-r00", baseline, evolution_tasks)
    incumbent_phase = "evolution"
    incumbent_label = (
        str(accepted_records[-1]["candidate_id"])
        if accepted_records
        else "incumbent-r00"
    )
    evidence_phase = incumbent_phase
    evidence_label = incumbent_label
    evidence_tasks = evolution_tasks

    while len(accepted_records) < args.target_rounds:
        round_number = len(accepted_records) + 1
        coordinate, skill_name = COORDINATES[round_number - 1]
        accepted = False
        for attempt in range(1, args.candidate_attempts + 1):
            candidate_tasks = (
                evolution_tasks if round_number == 1 and attempt == 1 else recurrent_tasks
            )
            candidate_id = f"r{round_number:02d}-{coordinate}-a{attempt}"
            decision_path = result_root / "decisions" / f"{candidate_id}.json"
            _, candidate_root = experiment.generate_candidate(
                round_number=round_number,
                attempt=attempt,
                coordinate=coordinate,
                skill_name=skill_name,
                incumbent_root=current_root,
                evidence_phase=evidence_phase,
                evidence_label=evidence_label,
                evidence_tasks=evidence_tasks,
            )
            candidate_max_calls = (
                50 if round_number == 1 and attempt == 1 else args.gate_model_calls
            )
            experiment.run_batch(
                "evolution",
                candidate_id,
                candidate_root,
                candidate_tasks,
                max_model_calls=candidate_max_calls,
            )
            incumbent_rows = experiment.result_rows(
                incumbent_phase, incumbent_label, candidate_tasks
            )
            candidate_rows = experiment.result_rows("evolution", candidate_id, candidate_tasks)
            decision = candidate_decision(incumbent_rows, candidate_rows)
            decision.update(
                {
                    "candidate_id": candidate_id,
                    "round": round_number,
                    "coordinate": coordinate,
                    "skill_name": skill_name,
                    "created_at": utc_now(),
                    "incumbent_skill_sha256": skill_sha(current_root, skill_name),
                    "candidate_skill_sha256": skill_sha(candidate_root, skill_name),
                }
            )
            if decision["incumbent_skill_sha256"] == decision["candidate_skill_sha256"]:
                decision["accepted"] = False
                decision["blockers"] = [*decision["blockers"], "unchanged-skill"]
            atomic_json(decision_path, decision)
            if decision["accepted"]:
                record = {
                    "round": round_number,
                    "coordinate": coordinate,
                    "candidate_id": candidate_id,
                    "skill_root": str(candidate_root),
                    "quality_delta": decision["quality_delta"],
                    "decision": str(decision_path.relative_to(result_root)),
                }
                accepted_records.append(record)
                current_root = candidate_root
                incumbent_phase = "evolution"
                incumbent_label = candidate_id
                evidence_phase = "evolution"
                evidence_label = candidate_id
                evidence_tasks = candidate_tasks
                state.update(
                    {
                        "accepted_rounds": accepted_records,
                        "current_skill_root": str(current_root),
                        "status": "evolving",
                        "updated_at": utc_now(),
                    }
                )
                atomic_json(state_path, state)
                accepted = True
                print(f"[{utc_now()}] accepted {candidate_id}", flush=True)
                break
            evidence_phase = "evolution"
            evidence_label = candidate_id
            evidence_tasks = candidate_tasks
            print(
                f"[{utc_now()}] retired {candidate_id}: {', '.join(decision['blockers'])}",
                flush=True,
            )
        if not accepted:
            state.update(
                {
                    "status": "candidate-limit-reached",
                    "blocked_coordinate": coordinate,
                    "updated_at": utc_now(),
                }
            )
            atomic_json(state_path, state)
            raise RuntimeError(
                f"No acceptable {coordinate} candidate after {args.candidate_attempts} attempts; "
                "the coordinate was not advanced"
            )

    final_roots = materialize_final_roots(result_root, baseline, current_root)
    experiment.run_matrix("heldout", final_roots, heldout_tasks)
    report = write_final_report(experiment, heldout_tasks, accepted_records)
    state.update({"status": "complete", "completed_at": utc_now()})
    atomic_json(state_path, state)
    overall = report["heldout"]["interactions"]["all"]
    print(json.dumps({"status": "complete", "overall": overall}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
