#!/usr/bin/env python3
"""Run the closest available SpreadsheetAgent protocol with GLM-5.1.

The released SpreadsheetAgent repository is not directly executable on this Linux
LiteLLM/SpreadsheetBench harness (its Jupyter and Windows Excel-image services are
different).  This launcher therefore uses the frozen ``spreadsheet-agent`` arm, which
preserves the paper's extraction -> visual verification -> LaTeX verification -> solver
staging, while recording the GLM-5.1 and Qwen3-VL substitutions explicitly.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
V1 = ROOT / "benchmarks/data/spreadsheetbench_912_v0.1"
V2 = ROOT / "benchmarks/data/spreadsheetbench-v2"
EVALUATOR = ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
MODEL = "GLM-5.1"
VISION_MODEL = "dashscope/qwen3-vl-235b-a22b-thinking"
BASE_URL = "http://10.130.138.46:8010/v1"
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
PROXY_SCRIPT = ROOT / "tools/dsh_chat_completions_proxy.py"
OFFICIAL_REVISION = "b4ded1ebdb73ab66acfa8439ad2af54470e317e3"


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def common_flags(args: argparse.Namespace, suite: str, base_url: str) -> list[str]:
    # V1's argparse accepts only integer resource caps; V2 additionally accepts
    # the literal ``unlimited``.  Keep the protocol otherwise identical.
    total_tokens = "10000000" if suite == "v1" else "unlimited"
    output_tokens = "32768"
    return [
        "--base-url", base_url,
        "--model", MODEL,
        "--api-key-file", str(args.api_key_file),
        "--api-protocol", "chat-completions",
        "--reasoning-effort", "medium",
        "--enable-thinking",
        "--temperature", "0",
        "--top-p", "1",
        "--max-turns-per-arm", "50",
        "--max-model-calls", "50",
        "--max-total-tokens", total_tokens,
        "--max-output-tokens", output_tokens,
        "--task-timeout", str(args.task_timeout),
        "--request-timeout", str(args.request_timeout),
        "--litellm-timeout", str(args.request_timeout),
        "--request-retries", "5",
        "--request-interval-seconds", str(args.request_interval),
        "--vision-model", VISION_MODEL,
        "--vision-base-url", base_url,
        "--vision-api-key-file", str(args.api_key_file),
        "--vision-api-protocol", "chat-completions",
        "--vision-reasoning-effort", "medium",
        "--vision-enable-thinking",
        "--vision-temperature", "0",
        "--vision-top-p", "1",
        "--vision-request-timeout", str(args.request_timeout),
        "--vision-litellm-timeout", str(args.request_timeout),
        "--vision-request-retries", "5",
        "--vision-request-interval-seconds", str(args.request_interval),
    ]


def v1_complete(output: Path) -> bool:
    try:
        rows = json.loads((output / "results.json").read_text(encoding="utf-8"))
        return isinstance(rows, list) and len(rows) == 1 and rows[0].get("status") == "completed"
    except (OSError, ValueError, TypeError):
        return False


def v2_complete(output: Path) -> bool:
    try:
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        arms = summary.get("arms", {})
        row = arms.get("spreadsheet-agent") or next(iter(arms.values()))
        return bool(row.get("completed") == 1 and row.get("errors", 0) == 0)
    except (OSError, ValueError, TypeError, StopIteration):
        return False


def run_one(job: tuple[str, str, Path, argparse.Namespace]) -> dict[str, object]:
    suite, task_id, output, args = job
    complete = v1_complete if suite == "v1" else v2_complete
    if complete(output):
        return {"suite": suite, "task": task_id, "status": "reused", "output": str(output)}
    # A prior interrupted attempt may leave a manifest/partial directory.
    # The CLI treats any existing leaf as a fresh-run collision, so preserve
    # that incomplete attempt and give the retry a clean leaf.
    if output.exists() and not complete(output):
        prior = output.with_name(output.name + ".prior-incomplete")
        suffix = 1
        while prior.exists():
            prior = output.with_name(output.name + f".prior-incomplete-{suffix}")
            suffix += 1
        output.rename(prior)
    # The benchmark CLI claims a fresh output directory itself; creating the
    # leaf beforehand makes it reject every task as an existing run.
    output.parent.mkdir(parents=True, exist_ok=True)
    relay_base = f"{args.relay_base_url}/task/{slug(task_id)}/v1"
    if suite == "v1":
        command = [
            str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v1-compare",
            "--dataset", str(V1), "--output", str(output), "--task-id", task_id,
            "--arm", "spreadsheet-agent", "--v1-execution-mode", "direct",
            "--arm-order-seed", "20260928", "--seed", "41",
        ] + common_flags(args, "v1", relay_base)
    else:
        category, _ = task_id.split("/", 1)
        command = [
            str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
            "--dataset", str(V2), "--official-evaluator", str(EVALUATOR),
            "--category", category, "--task-id", task_id,
            "--output", str(output), "--arm", "spreadsheet-agent",
            "--arm-order-seed", "20260928", "--seed", "41",
        ] + common_flags(args, "v2", relay_base)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(args.runtime_root)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    log = output.parent / (output.name + ".launcher.log")
    with log.open("a", encoding="utf-8") as stream:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, check=False)
    return {
        "suite": suite,
        "task": task_id,
        "status": "completed" if completed.returncode == 0 and complete(output) else "failed",
        "returncode": completed.returncode,
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("v1", "v2", "both"), default="both")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--task-timeout", type=int, default=21600)
    parser.add_argument("--request-timeout", type=int, default=3600)
    parser.add_argument("--request-interval", type=float, default=1.1)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--api-key-file", type=Path, default=KEY_FILE)
    parser.add_argument("--v1-task", action="append", default=[])
    parser.add_argument("--v2-task", action="append", default=[])
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Freeze the exact harness implementation before launching parallel workers.
    # This keeps the long-running study reproducible even if the working tree changes.
    runtime_root = out / "frozen-source"
    if not runtime_root.exists():
        shutil.copytree(ROOT / "src/spreadsheet_harness", runtime_root / "spreadsheet_harness")
    args.runtime_root = runtime_root
    # The working runners use a local Chat Completions relay.  It avoids the
    # machine-wide HTTP proxy and uses direct http.client connections to the
    # internal LiteLLM gateway, while preserving a separate 50-request counter
    # per benchmark task.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        relay_port = int(sock.getsockname()[1])
    args.relay_base_url = f"http://127.0.0.1:{relay_port}"
    relay_log = out / "chat-relay.log"
    relay_audit = out / "chat-relay-requests.jsonl"
    relay = subprocess.Popen(
        [
            sys.executable, str(PROXY_SCRIPT),
            "--port", str(relay_port),
            "--upstream", args.base_url,
            "--api-key-file", str(args.api_key_file),
            "--audit", str(relay_audit),
            "--max-requests", "50",
            "--timeout", str(args.request_timeout),
            "--interval", str(args.request_interval),
        ],
        cwd=ROOT,
        stdout=relay_log.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        env={**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
    )
    time.sleep(0.5)
    if relay.poll() is not None:
        raise SystemExit(f"local Chat Completions relay failed to start; see {relay_log}")

    jobs: list[tuple[str, str, Path, argparse.Namespace]] = []
    if args.suite in {"v1", "both"}:
        sys.path.insert(0, str(ROOT / "src"))
        from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1
        tasks = load_spreadsheetbench_v1(V1)
        wanted = set(args.v1_task)
        ids = [task.task_id for task in tasks if not wanted or task.task_id in wanted]
        if wanted and set(ids) != wanted:
            raise SystemExit("unknown --v1-task: " + ", ".join(sorted(wanted - set(ids))))
        jobs.extend(("v1", task_id, out / "v1" / slug(task_id), args) for task_id in ids)
    if args.suite in {"v2", "both"}:
        categories = ("Template", "Debugging", "Financial_Model")
        wanted = set(args.v2_task)
        ids: list[str] = []
        for category in categories:
            data = json.loads((V2 / category / "dataset.json").read_text(encoding="utf-8"))
            ids.extend(f"{category}/{item['id']}" for item in data)
        if wanted:
            missing = wanted - set(ids)
            if missing:
                raise SystemExit("unknown --v2-task: " + ", ".join(sorted(missing)))
            ids = [task_id for task_id in ids if task_id in wanted]
        jobs.extend(("v2", task_id, out / "v2" / slug(task_id), args) for task_id in ids)

    metadata = {
        "schema": "spreadsheetagent-glm51-official-compat-20260928",
        "official_repository_revision": OFFICIAL_REVISION,
        "protocol": "clean-room official workflow transfer: extraction -> visual verification -> LaTeX verification -> solver",
        "main_model": MODEL,
        "vision_model": VISION_MODEL,
        "base_url": args.base_url,
        "relay_base_url": args.relay_base_url,
        "skill": None,
        "temperature": 0.0,
        "top_p": 1.0,
        "thinking": True,
        "reasoning_effort": "medium",
        "max_turns_per_arm": 50,
        "task_count": len(jobs),
        "v1_task_count": sum(job[0] == "v1" for job in jobs),
        "v2_task_count": sum(job[0] == "v2" for job in jobs),
        "deviations": [
            "GLM-4.5V is unavailable; Qwen3-VL-235B-A22B-Thinking is used as the visual verifier.",
            "Linux render/LibreOffice proxy replaces the official Windows Excel/WPS visual service.",
            "V2 is a method-transfer evaluation; the paper reports V1.",
        ],
    }
    (out / "experiment.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        rows: list[dict[str, object]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(run_one, job): job[:2] for job in jobs}
            for index, future in enumerate(concurrent.futures.as_completed(pending), 1):
                row = future.result()
                rows.append(row)
                print(json.dumps({"completed": index, "total": len(jobs), **row}, ensure_ascii=False), flush=True)
        (out / "launcher-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0 if all(row["status"] in {"completed", "reused"} for row in rows) else 1
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=10)
        except subprocess.TimeoutExpired:
            relay.kill()
            relay.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
