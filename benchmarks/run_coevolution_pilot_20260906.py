#!/usr/bin/env python3
"""Prepare a reproducible H/D co-evolution pilot.

Benchmark execution and candidate generation default to MiniMax-M2.7-highspeed.  The two
model settings remain independently overrideable for controlled ablations.
This script only prepares candidates and frozen manifests; benchmark execution
is launched separately so candidate generation cannot alter evaluation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from spreadsheet_harness.capability_evolution import alternating_coordinate_schedule

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / os.environ.get(
    "COEVOLUTION_OUT",
    "benchmarks/results/coevolution-pilot-minimax-m27-highspeed-thinking-20260909",
)
TRACE_ROOT = ROOT / "benchmarks/results/spreadsheetbench-v2-qwen36-pilot30-5arm-v32-20260902/runs"
MODEL = os.environ.get("BENCH_MODEL", "MiniMax-M2.7-highspeed")
EVOLUTION_MODEL = os.environ.get("EVOLUTION_MODEL", "MiniMax-M2.7-highspeed")
BASE_URL = os.environ.get("BASE_URL", "http://47.96.153.159:8010/v1")
TEMPERATURE = os.environ.get("TEMPERATURE", "0")
TOP_P = os.environ.get("TOP_P", "1")
SEED = os.environ.get("SEED", "41")

DEV = ["01_01", "01_02", "02_02", "04_01", "05_04", "06_02", "07_01", "09_01", "13_05", "16_04"]
TRANSFER = ["02_01", "03_01", "04_02", "05_02", "06_01"]
HELDOUT = ["03_02", "04_03", "05_01", "06_03", "20_02", "01_03", "02_03", "03_03", "04_04", "05_03"]


def traces(ids: list[str]) -> list[str]:
    result = []
    for item in ids:
        p = (
            TRACE_ROOT
            / "Financial_Model"
            / item
            / "spreadsheet-harness-financial"
            / "trajectory.jsonl"
        )
        if p.is_file():
            try:
                events = [
                    json.loads(line).get("event", "").lower()
                    for line in p.read_text().splitlines()
                    if line.strip()
                ]
            except Exception:
                continue
            if any(
                event
                in {
                    "spreadsheetbench_v2.evaluated",
                    "benchmark.evaluated",
                    "evaluation.completed",
                    "evaluation.failed",
                }
                for event in events
            ):
                result.append(str(p))
    return result


def run(args: list[str]) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    dev = traces(DEV)
    if len(dev) < 5:
        raise SystemExit(f"insufficient development trajectories: {len(dev)}")
    (OUT / "development_trajectories.json").write_text(json.dumps(dev, indent=2) + "\n")
    (OUT / "split_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "coevolution-split-v1",
                "created": "2026-09-06",
                "model": MODEL,
                "benchmark_model": MODEL,
                "evolution_model": EVOLUTION_MODEL,
                "generation": {
                    "temperature": float(TEMPERATURE),
                    "top_p": float(TOP_P),
                    "seed": int(SEED),
                    "enable_thinking": True,
                },
                "evolution": {
                    "policy": "alternating-one-coordinate-v1",
                    "schedule": [
                        step.to_dict() for step in alternating_coordinate_schedule(4)
                    ],
                    "candidate_validation": ["replay", "transfer", "regression"],
                },
                "four_arm_protocol": ["h0d0", "h1d0", "h0d1", "h1d1"],
                "development": DEV,
                "transfer": TRANSFER,
                "heldout": HELDOUT,
                "development_trajectory_sha256": {
                    p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in dev
                },
                "policy": "heldout tasks are not used for generation or candidate selection",
            },
            indent=2,
        )
        + "\n"
    )
    for kind, skill in [
        ("harness", "spreadsheet-structure"),
        ("financial", "spreadsheet-financial-model"),
    ]:
        out = OUT / kind
        if out.exists():
            shutil.rmtree(out)
        run(
            [
                str(ROOT / ".venv/bin/sheet-harness"),
                "evolve",
                "generate",
                *dev,
                "--output",
                str(out),
                "--candidate-id",
                f"{kind}-evolution-r1",
                "--skill-name",
                skill,
                "--base-skill",
                str(ROOT / "skills" / skill / "SKILL.md"),
                "--lesson-max-output-tokens",
                "unlimited",
                "--consolidation-max-output-tokens",
                "unlimited",
                "--base-url",
                BASE_URL,
                "--api-key-file",
                "/tmp/spreadsheet-harness-litellm.key",
                "--model",
                EVOLUTION_MODEL,
                "--api-protocol",
                "chat-completions",
                "--reasoning-effort",
                "medium",
                "--seed",
                SEED,
                "--temperature",
                TEMPERATURE,
                "--top-p",
                TOP_P,
                "--enable-thinking",
            ]
        )
    print(
        json.dumps(
            {"status": "candidates_ready", "output": str(OUT), "development": len(dev)}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
