#!/usr/bin/env python3
"""Keep a small additional pool on pending SpreadsheetAgent supervisor jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def task_dir(root: Path, job: dict[str, object]) -> Path:
    slug = str(job.get("task_slug", ""))
    if not slug:
        task_id = str(job["task_id"])
        suffix = hashlib.sha256(f"{job['group']}\0{task_id}".encode()).hexdigest()[:12]
        slug = f"{task_id.replace('/', '_')}-{suffix}"
    return root / "tasks" / str(job["group"]) / slug


def choose(root: Path, group: str) -> tuple[str, str, Path] | None:
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    for job in plan["jobs"]:
        if str(job["group"]) != group or job.get("version") != "v1":
            continue
        directory = task_dir(root, job)
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "result.json").is_file():
            continue
        state_path = directory / "state.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("status") == "running":
                    continue
            except (OSError, ValueError):
                pass
        claim = directory / ".pending-pool-claim"
        try:
            if claim.exists() and time.time() - claim.stat().st_mtime < 21_600:
                continue
            fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            continue
        return str(job["group"]), str(job["task_id"]), claim
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    root = args.root.resolve()
    runner = root / "frozen-source" / "run_spreadsheetagent_three.py"
    print(f"pending_pool group={args.group} workers={args.workers}", flush=True)

    def loop() -> None:
        while True:
            pair = choose(root, args.group)
            if pair is None:
                time.sleep(15)
                # Re-check indefinitely so this pool also handles interrupted
                # supervisor tasks that become available later.
                continue
            group, task_id, claim = pair
            print(f"dispatch group={group} task={task_id}", flush=True)
            try:
                subprocess.run(
                    [
                        sys.executable,
                        str(runner),
                        "worker",
                        "--root",
                        str(root),
                        "--group",
                        group,
                        "--task-id",
                        task_id,
                    ],
                    cwd=root.parents[2],
                    check=False,
                )
            finally:
                claim.unlink(missing_ok=True)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(loop) for _ in range(args.workers)]
        for future in futures:
            future.result()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
