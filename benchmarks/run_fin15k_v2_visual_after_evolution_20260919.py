#!/usr/bin/env python3
"""Generate and score all V2 Visualization cases for frozen Fin-1.5K variants.

The nine evolved revisions and their common baseline are loaded from their
frozen artifacts. Generation is kept separate from the Linux VLM proxy: the
same XLSX bundle can later be scored by the pinned official Windows COM
evaluator without rerunning the solver.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "benchmarks/results/fin15k-scaling-coevolution-20260919"
DATASET = REPO / "benchmarks/data/spreadsheetbench-v2"
VISUAL_EVALUATOR = Path(
    "/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py"
)
VISUAL_EVALUATOR_SHA256 = (
    "8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703"
)
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
BASE_URL = "http://10.130.138.46:8010/v1"
SOLVER_MODEL = "qwen3.6-plus"
SIZES = (50, 200, 500)
SCOPES = ("general-only", "domain-only", "coevolution")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def frozen_variants(root: Path) -> dict[str, Path]:
    variants: dict[str, Path] = {}
    baseline: Path | None = None
    baseline_revision: str | None = None
    for size in SIZES:
        for scope in SCOPES:
            cell_path = root / "cells" / f"fin15k-{size}-{scope}.json"
            if not cell_path.is_file():
                raise RuntimeError(f"Missing completed evolution cell: {size}/{scope}")
            cell = load(cell_path)
            workspace = Path(str(cell["workspace"]))
            initial = str(cell["initial_revision_sha256"])
            frozen = str(cell["frozen_revision_sha256"])
            if baseline_revision is None:
                baseline_revision = initial
                baseline = workspace / "revisions" / initial
            elif initial != baseline_revision:
                raise RuntimeError("Evolution cells do not share one baseline revision")
            variants[f"fin15k-{size}-{scope}"] = workspace / "revisions" / frozen
    if baseline is None:
        raise RuntimeError("No baseline revision was found")
    return {"baseline": baseline, **variants}


def worker(root: Path, variant: str, revision_dir: Path, output: Path) -> int:
    # Imports deliberately occur after the parent has selected a frozen
    # artifact and set PYTHONPATH to that artifact's source directory.
    from spreadsheet_harness.config import ProviderConfig
    from spreadsheet_harness.plugins import CompositionSpec
    from spreadsheet_harness.skills import SkillRegistry
    from spreadsheet_harness.spreadsheetbench_v2 import (
        load_spreadsheetbench_v2_tasks,
        run_spreadsheetbench_v2_comparison,
    )

    document = load(revision_dir / "composition.json")
    if isinstance(document.get("composition"), dict):
        document = document["composition"]
    composition = CompositionSpec.create(
        str(document.get("name", variant)),
        list(document["plugins"]),
        dict(document.get("overrides") or {}),
    )
    config = ProviderConfig(
        BASE_URL,
        KEY_FILE.read_text(encoding="utf-8").strip(),
        SOLVER_MODEL,
        reasoning_effort="medium",
        timeout_seconds=600,
        max_retries=2,
        request_interval_seconds=0.8,
        temperature=0.0,
        top_p=1.0,
        seed=41,
        enable_thinking=True,
        api_protocol="chat-completions",
        litellm_timeout_seconds=600,
    )
    tasks = load_spreadsheetbench_v2_tasks(
        DATASET, categories=("Visualization",)
    )
    summary = run_spreadsheetbench_v2_comparison(
        config=config,
        dataset_root=DATASET,
        evaluator_path=VISUAL_EVALUATOR,
        output_dir=output,
        skill_registry=SkillRegistry([revision_dir / "artifact" / "skills"]),
        tasks=tasks,
        arms=("ours",),
        composition_overrides={"ours": composition},
        max_model_calls=50,
        max_turns_per_arm=50,
        max_total_tokens=None,
        max_output_tokens=None,
        task_timeout_seconds=7200,
        arm_order_seed=20260919,
        visual_generation_only=True,
    )
    atomic_json(
        output / "frozen-variant.json",
        {
            "variant": variant,
            "revision_sha256": revision_dir.name,
            "artifact": str(revision_dir / "artifact"),
            "summary": summary,
        },
    )
    return 0 if summary.get("generation_complete") else 2


def child_environment(revision_dir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(revision_dir / "artifact" / "src")
    for name in (
        "OPENAI_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    return environment


def generate_variant(
    root: Path, variant: str, revision_dir: Path, attempts: int
) -> dict[str, Any]:
    attempts_root = root / "v2-visualization" / "attempts" / variant
    complete_root = root / "v2-visualization" / "complete"
    complete_root.mkdir(parents=True, exist_ok=True)
    link = complete_root / variant
    if link.is_dir() and (link / "summary.json").is_file():
        summary = load(link / "summary.json")
        if summary.get("generation_complete"):
            return {"variant": variant, "status": "complete", "output": str(link)}
    for attempt in range(1, attempts + 1):
        output = attempts_root / f"attempt-{attempt}"
        if output.exists():
            summary_path = output / "summary.json"
            if summary_path.is_file() and load(summary_path).get("generation_complete"):
                code = 0
            else:
                continue
        else:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--root",
                str(root),
                "worker",
                "--variant",
                variant,
                "--revision-dir",
                str(revision_dir),
                "--output",
                str(output),
            ]
            log = attempts_root / f"attempt-{attempt}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                completed = subprocess.run(
                    command,
                    cwd=revision_dir / "artifact",
                    env=child_environment(revision_dir),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=180_000,
                    check=False,
                )
            code = completed.returncode
        if code == 0:
            if link.exists() or link.is_symlink():
                if link.resolve() != output.resolve():
                    raise RuntimeError(f"Completed visual link changed for {variant}")
            else:
                link.symlink_to(output.resolve(), target_is_directory=True)
            return {"variant": variant, "status": "complete", "output": str(link)}
    return {"variant": variant, "status": "infrastructure", "output": None}


def run_proxy(root: Path) -> int:
    command = [
        "/usr/bin/python3",
        str(REPO / "tools/run_visual_linux_proxy.py"),
        "--results-root",
        str(root / "v2-visualization" / "complete"),
        "--output-root",
        str(root / "v2-visualization" / "linux-proxy"),
        "--api-key-file",
        str(KEY_FILE),
        "--base-url",
        BASE_URL,
    ]
    with (root / "logs" / "v2-visual-linux-proxy.log").open(
        "a", encoding="utf-8"
    ) as handle:
        return subprocess.run(
            command,
            cwd=REPO,
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=86_400,
            check=False,
        ).returncode


def orchestrate(root: Path, *, parallelism: int, attempts: int) -> int:
    if sha256(VISUAL_EVALUATOR) != VISUAL_EVALUATOR_SHA256:
        raise RuntimeError("Pinned V2 visual evaluator changed")
    variants = frozen_variants(root)
    rows: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
        jobs = {
            pool.submit(generate_variant, root, name, revision, attempts): name
            for name, revision in variants.items()
        }
        for job in futures.as_completed(jobs):
            rows.append(job.result())
            atomic_json(
                root / "v2-visualization" / "generation-status.json",
                {"expected_variants": len(variants), "rows": sorted(rows, key=lambda x: x["variant"])},
            )
    if any(row["status"] != "complete" for row in rows):
        return 2
    bundle = {
        "schema_version": "fin15k-v2-visual-windows-bundle-v1",
        "tasks_per_variant": 24,
        "variants": sorted(variants),
        "official_evaluator": {
            "path": str(VISUAL_EVALUATOR),
            "sha256": VISUAL_EVALUATOR_SHA256,
            "requirement": "Windows Excel/WPS COM plus glm-4.6v",
        },
        "linux_proxy_is_official": False,
        "generated_outputs": str(root / "v2-visualization" / "complete"),
    }
    atomic_json(root / "v2-visualization" / "windows-official-bundle.json", bundle)
    return run_proxy(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--parallelism", type=int, default=4)
    run.add_argument("--attempts", type=int, default=3)
    worker_parser = commands.add_parser("worker")
    worker_parser.add_argument("--variant", required=True)
    worker_parser.add_argument("--revision-dir", type=Path, required=True)
    worker_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.command == "worker":
        return worker(root, args.variant, args.revision_dir.resolve(), args.output.resolve())
    return orchestrate(root, parallelism=args.parallelism, attempts=args.attempts)


if __name__ == "__main__":
    raise SystemExit(main())
