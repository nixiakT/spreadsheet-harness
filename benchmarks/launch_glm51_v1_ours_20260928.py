from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
DATASET = ROOT / "benchmarks/data/spreadsheetbench_912_v0.1"
OUT = ROOT / "benchmarks/results/v1-glm51-ours-20260928"
BASE_URL = "http://47.96.153.159:8010/v1"
KEY = "/tmp/spreadsheet-harness-litellm.key"
# The pinned V1 CLI exposes Basic/Financial; Bare uses a separate runner and is
# intentionally not mislabeled as a V1 CLI arm here.
ARMS = ("spreadsheet-harness-basic", "spreadsheet-harness-financial")


def complete(path: Path) -> bool:
    try:
        rows = json.loads((path / "results.json").read_text())
        return isinstance(rows, list) and rows and rows[0].get("status") == "completed"
    except Exception:
        return False


def run(job: tuple[str, str]) -> dict[str, object]:
    arm, task_id = job
    leaf = OUT / arm / task_id
    if complete(leaf):
        return {"arm": arm, "task_id": task_id, "status": "reused"}
    if leaf.exists():
        n = 2
        while (OUT / arm / f"{task_id}__retry{n}").exists():
            n += 1
        leaf = OUT / arm / f"{task_id}__retry{n}"
    leaf.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v1-compare",
        "--dataset", str(DATASET), "--task-id", task_id, "--arm", arm,
        "--output", str(leaf), "--max-model-calls", "50", "--max-turns-per-arm", "50",
        "--max-total-tokens", "10000000", "--max-output-tokens", "32768",
        "--task-timeout", "21600", "--request-timeout", "1800", "--litellm-timeout", "1800",
        "--request-retries", "5", "--request-interval-seconds", "1.1",
        "--arm-order-seed", "20260928", "--base-url", BASE_URL,
        "--api-key-file", KEY, "--model", "dashscope/glm-5.1",
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--seed", "41", "--temperature", "0", "--top-p", "1", "--enable-thinking",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    log = leaf.parent / f"{task_id}.launcher.log"
    with log.open("a") as stream:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"arm": arm, "task_id": task_id, "status": "completed" if proc.returncode == 0 else "failed", "returncode": proc.returncode}


def main() -> int:
    tasks = json.loads((DATASET / "dataset.json").read_text())
    task_ids = [str(x.get("id") or x.get("task_id")) for x in tasks]
    jobs = [(arm, task_id) for arm in ARMS for task_id in task_ids]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result(); rows.append(row)
            print(json.dumps({"completed": i, "total": len(jobs), **row}, ensure_ascii=False), flush=True)
    (OUT / "launcher-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return 0 if all(r["status"] in {"completed", "reused"} for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
