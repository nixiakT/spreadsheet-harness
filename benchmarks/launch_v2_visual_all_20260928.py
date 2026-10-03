from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
DATASET = ROOT / "benchmarks/data/spreadsheetbench-v2"
EVALUATOR = "/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py"
BASE_URL = "http://47.96.153.159:8010/v1"
KEY = "/tmp/spreadsheet-harness-litellm.key"
OUT = ROOT / "benchmarks/results/v2-visual-all-20260928"
JOBS = [
    ("deepseek", "spreadsheet-harness-basic", "dashscope/deepseek-v4-flash-0731"),
    ("deepseek", "spreadsheet-harness-financial", "dashscope/deepseek-v4-flash-0731"),
    ("glm51", "bare", "dashscope/glm-5.1"),
    ("glm51", "spreadsheet-harness-basic", "dashscope/glm-5.1"),
    ("glm51", "spreadsheet-harness-financial", "dashscope/glm-5.1"),
]


def run(job: tuple[str, str, str, str]) -> dict[str, object]:
    label, arm, model, task_id = job
    leaf = OUT / label / arm / task_id.replace("/", "__")
    if (leaf / "results.json").is_file():
        return {"label": label, "arm": arm, "task_id": task_id, "status": "reused"}
    if leaf.exists():
        n = 2
        while (OUT / label / arm / f"{task_id.replace('/', '__')}__retry{n}").exists():
            n += 1
        leaf = OUT / label / arm / f"{task_id.replace('/', '__')}__retry{n}"
    leaf.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v2-visual-generate",
        "--dataset", str(DATASET), "--visual-evaluator", EVALUATOR,
        "--task-id", task_id, "--arm", arm, "--output", str(leaf),
        "--max-model-calls", "50", "--max-turns-per-arm", "50",
        "--max-total-tokens", "unlimited", "--max-output-tokens", "unlimited",
        "--task-timeout", "3600", "--request-timeout", "1800", "--litellm-timeout", "1800",
        "--request-retries", "5", "--request-interval-seconds", "1.1",
        "--arm-order-seed", "20260928", "--base-url", BASE_URL,
        "--api-key-file", KEY, "--model", model, "--api-protocol", "chat-completions",
        "--reasoning-effort", "medium", "--seed", "41", "--temperature", "0", "--top-p", "1",
        "--enable-thinking",
    ]
    env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
    with (leaf.parent / f"{leaf.name}.log").open("a") as stream:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"label": label, "arm": arm, "task_id": task_id, "status": "completed" if proc.returncode == 0 else "failed", "returncode": proc.returncode}


def main() -> int:
    payloads = json.loads((DATASET / "Visualization/dataset.json").read_text())
    jobs = [(label, arm, model, f"Visualization/{item['id']}") for label, arm, model in JOBS for item in payloads]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result(); rows.append(row)
            print(json.dumps({"completed": i, "total": len(jobs), **row}, ensure_ascii=False), flush=True)
    (OUT / "launcher-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return 0 if all(r["status"] in {"completed", "reused"} for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
