#!/usr/bin/env python3
"""Finish the formal baseline, build its profile, then launch 500 evolution."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def completed_count(root: Path) -> int:
    count = 0
    for path in (root / "baseline-trajectories").glob("*/cell.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        trajectory = Path(str(row.get("trajectory", "")))
        if row.get("status") == "complete" and trajectory.is_file():
            count += 1
    return count


def wait_pid(pid: int) -> None:
    while pid > 0 and Path(f"/proc/{pid}").exists():
        time.sleep(15)


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print(json.dumps({"event": "command.started", "command": command}), flush=True)
    completed = subprocess.run(command, check=False, env=env)
    print(
        json.dumps(
            {"event": "command.finished", "returncode": completed.returncode},
            ensure_ascii=False,
        ),
        flush=True,
    )
    if completed.returncode:
        raise RuntimeError(f"Command failed with {completed.returncode}: {command}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--template-protocol", type=Path, required=True)
    parser.add_argument("--shared-slots", type=Path, required=True)
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--baseline-parallelism", type=int, default=4)
    parser.add_argument("--evolution-parallelism", type=int, default=3)
    parser.add_argument("--max-baseline-passes", type=int, default=12)
    args = parser.parse_args()
    source = args.source_root.expanduser().resolve()
    output = args.output_root.expanduser().resolve()
    snapshot = args.snapshot_root.expanduser().resolve()
    wait_pid(args.wait_pid)
    environment = dict(os.environ)
    environment["SPREADSHEET_FIN15K_SOLVER_MODEL"] = "DeepSeek-V4-Flash"
    for attempt in range(1, args.max_baseline_passes + 1):
        count = completed_count(source)
        print(
            json.dumps(
                {"event": "baseline.progress", "complete": count, "target": 500},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if count == 500:
            break
        completed = subprocess.run(
            [
                ".venv/bin/python",
                "tools/finish_formal_500_baseline_compat.py",
                "--root",
                str(source),
                "--parallelism",
                str(args.baseline_parallelism),
            ],
            check=False,
            env=environment,
        )
        print(
            json.dumps(
                {
                    "event": "baseline.pass.finished",
                    "attempt": attempt,
                    "returncode": completed.returncode,
                    "complete": completed_count(source),
                }
            ),
            flush=True,
        )
        if completed_count(source) < 500:
            time.sleep(15)
    if completed_count(source) != 500:
        raise RuntimeError(
            f"Formal baseline remains incomplete after {args.max_baseline_passes} passes: "
            f"{completed_count(source)}/500"
        )
    run(
        [
            ".venv/bin/python",
            "tools/prepare_fin15k_formal_500_evolution.py",
            "--source-root",
            str(source),
            "--output-root",
            str(output),
            "--snapshot-root",
            str(snapshot),
            "--template-protocol",
            str(args.template_protocol.expanduser().resolve()),
        ]
    )
    profile = output / "plugin-profile-500" / "plugin-profile.json"
    if not profile.is_file():
        run(
            [
                ".venv/bin/python",
                "tools/report_fin15k_plugin_profile_20260920.py",
                "--root",
                str(output),
                "--output",
                str(output / "plugin-profile-500"),
            ]
        )
    evolution_env = dict(environment)
    evolution_env["SPREADSHEET_EVOLUTION_GLOBAL_SLOTS_DIR"] = str(
        args.shared_slots.expanduser().resolve()
    )
    evolution_env.setdefault("SPREADSHEET_EVOLUTION_GLOBAL_SLOTS", "6")
    # The repaired evolution wrapper is imported from the live checkout, but
    # all candidate/evaluator workers are rooted at the frozen v7 snapshot.
    # Bind the environment to the same source tree so the controller and
    # adapter cannot accidentally load a different local module.
    evolution_env["PYTHONPATH"] = str(snapshot / "src")
    run(
        [
            ".venv/bin/python",
            "tools/run_fin15k_repaired_evolution.py",
            "--root",
            str(output),
            "--snapshot-root",
            str(snapshot),
            "--size",
            "500",
            "--baseline-parallelism",
            "1",
            "--evolution-parallelism",
            str(args.evolution_parallelism),
        ],
        env=evolution_env,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
