#!/usr/bin/env python3
"""Run SpreadsheetBench-v2 with the Claude Code CLI and the real task tools.

This is the Claude Code counterpart of ``run_codex_spreadsheetbench_v2.py``.
The model/API/skill/evaluation settings are kept the same; only the CLI and
Anthropic Messages transport differ.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from run_codex_spreadsheetbench_v2 import (
    DEFAULT_SKILL,
    EXPECTED_COUNTS,
    load_tasks,
    prompt_for,
    recalculate_workbook,
    sha256,
    workbook_is_valid,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
CLAUDE_BIN = Path("/home/tongzeyuan/.nvm/versions/node/v24.19.0/bin/claude")
CLAUDE_PROXY_SCRIPT = ROOT / "tools/claude_messages_proxy.py"


def claude_prompt(task: dict[str, Any], workspace: Path) -> str:
    return prompt_for(task, workspace).replace(
        "Codex skill registry", "Claude Code skill instructions"
    ).replace(
        "The required spreadsheet-manipulation skill is already loaded through this workspace's AGENTS.md\n"
        "and the Claude Code skill instructions. Follow it throughout the task. Do not read SKILL.md again; begin by",
        "The required spreadsheet-manipulation skill is already loaded through this workspace's CLAUDE.md.\n"
        "Follow it throughout the task. Do not read SKILL.md again; begin by",
    )


def run_one(
    task: dict[str, Any], *, dataset: Path, run_root: Path, runtime_root: Path,
    proxy_port: int, skill: Path, model: str, max_turns: int, max_output_tokens: int,
    task_timeout: float, evaluator: Path, recalculate_before_evaluation: bool,
    api_key_file: Path,
) -> dict[str, Any]:
    category = task["_category"]
    task_id = str(task["id"])
    slug = f"{category}__{task_id}"
    task_root = run_root / "tasks" / slug
    task_root.mkdir(parents=True, exist_ok=True)
    status_path = task_root / "status.json"
    output_path = task_root / "output.xlsx"
    old: dict[str, Any] = {}
    if status_path.is_file():
        try:
            old = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    reuse_output = workbook_is_valid(output_path)
    # On a resume, preserve a task that already has a complete artifact and
    # official score.  Re-running LibreOffice/evaluation for every old task is
    # both unnecessary and can leave a shared soffice process wedged; only
    # tasks without a completed status are sent back through the model loop.
    if (
        old.get("status") == "completed"
        and isinstance(old.get("official_score"), dict)
        and reuse_output
    ):
        return old
    input_source = (dataset / category / str(task["spreadsheet_path"])).resolve(strict=True)
    input_path = task_root / "input.xlsx"
    if not input_path.exists():
        shutil.copy2(input_source, input_path)
    skill_text = skill.read_text(encoding="utf-8")
    (task_root / "CLAUDE.md").write_text(
        "# Loaded spreadsheet-manipulation skill\n\n"
        "The following skill is binding. It is already loaded; do not spend a tool call reading it.\n\n"
        + skill_text,
        encoding="utf-8",
    )
    # Keep AGENTS.md too, so the artifact has an identical skill provenance to
    # the Codex run and other workspace-aware tools can discover it.
    (task_root / "AGENTS.md").write_text((task_root / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")
    trajectory_path = task_root / "trajectory.jsonl"
    stderr_path = task_root / "claude.stderr.log"
    final_path = task_root / "claude.final.txt"
    config_root = make_runtime_config(runtime_root / slug)
    env = dict(os.environ)
    env.update({
        "CLAUDE_CONFIG_DIR": str(config_root),
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}/task/{slug}",
        "ANTHROPIC_AUTH_TOKEN": "local-proxy",
        "ANTHROPIC_API_KEY": "local-proxy",
        "DISABLE_AUTOUPDATER": "1",
    })
    started = float(old.get("started_at", time.time()))
    deadline = time.monotonic() + task_timeout
    session_id: str | None = old.get("session_id")
    calls = 0
    timed_out = False
    trajectory_path.touch()

    def request_state() -> tuple[int, bool]:
        audit = run_root / "proxy-requests.jsonl"
        count = 0
        exceeded = False
        if not audit.exists():
            return 0, False
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("task_key") != slug:
                continue
            if event.get("limit_exceeded"):
                exceeded = True
            elif (
                event.get("request_index")
                and "upstream_attempt" not in event
                and "response_bytes" not in event
                and "error" not in event
            ):
                # Count accepted requests across all proxy lifetimes instead
                # of taking max(request_index), which resets after a restart.
                count += 1
        return count, exceeded

    def invoke(prompt: str, resume_id: str | None = None) -> int:
        nonlocal session_id, calls, timed_out
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            return 124
        command = [
            str(CLAUDE_BIN), "-p", "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions", "--model", model, "--effort", "medium",
            "--tools", "Bash,Read,Write,Edit,Skill",
        ]
        if resume_id:
            command += ["--resume", resume_id]
        process = subprocess.Popen(
            command, cwd=task_root, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(prompt)
        process.stdin.close()
        with trajectory_path.open("a", encoding="utf-8") as trace:
            for line in process.stdout:
                trace.write(line)
                trace.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("session_id"):
                    session_id = event["session_id"]
                if event.get("type") == "result":
                    calls += int(event.get("num_turns", 1) or 1)
                    final = event.get("result")
                    if isinstance(final, str):
                        final_path.write_text(final, encoding="utf-8")
        try:
            return process.wait(timeout=max(1.0, remaining))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                return process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait(timeout=15)

    return_code = 0
    if not reuse_output:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            return_code = invoke(claude_prompt(task, task_root))
            for _ in range(max_turns):
                ready = workbook_is_valid(output_path)
                requests, exceeded = request_state()
                if ready or timed_out or exceeded or requests >= max_turns or not session_id:
                    break
                before = requests
                return_code = invoke(
                    "Continue the same task now. Use one focused Python/openpyxl edit script, "
                    "save output.xlsx, reopen it to verify, and do not stop with a description of future work.",
                    session_id,
                )
                after, _ = request_state()
                if after == before and return_code != 0:
                    break

    ready = workbook_is_valid(output_path)
    recalculated = False
    recalculation_error: str | None = None
    if ready and recalculate_before_evaluation and category != "Visualization":
        backup = task_root / "output.pre-recalc.xlsx"
        if not backup.exists():
            shutil.copy2(output_path, backup)
        recalculated, recalculation_error = recalculate_workbook(output_path, output_path)
        ready = workbook_is_valid(output_path)
    requests, exceeded = request_state()
    if ready:
        status = "completed"
    elif timed_out:
        status = "timeout"
    elif exceeded or requests >= max_turns:
        status = "turn_limit"
    else:
        status = "failed"
    record: dict[str, Any] = {
        "schema_version": 1, "task_id": f"{category}/{task_id}", "category": category,
        "status": status, "exit_code": return_code, "max_turns": max_turns,
        "max_output_tokens": max_output_tokens, "session_id": session_id, "model": model,
        "started_at": started, "elapsed_seconds": round(time.time() - started, 3),
        "skill_path": str(skill), "skill_sha256": sha256(skill), "temperature": 0.0,
        "top_p": 1.0, "enable_thinking": True, "thinking_budget_tokens": 50,
        "output": str(output_path), "trajectory": str(trajectory_path),
        "final_message": str(final_path), "output_exists": output_path.is_file(),
        "output_valid": ready, "output_changed_from_input": sha256(output_path) != sha256(input_path) if output_path.exists() else False,
        "skill_load_mode": "claude-claude-md-and-agents", "reused_existing_output": reuse_output,
        "recalculated_before_evaluation": recalculated, "turns": requests, "model_requests": requests,
    }
    if recalculation_error and not recalculated:
        record["recalculation_error"] = recalculation_error
    if ready and category != "Visualization":
        try:
            evaluator_output = task_root / f"{task_id}_output.xlsx"
            shutil.copy2(output_path, evaluator_output)
            sys.path.insert(0, str(evaluator.parent))
            import evaluation  # type: ignore
            score, _missing = evaluation.process_single_item(task, str(dataset / category), str(task_root), {task_id: requests}, category)
            record["official_score"] = score
        except Exception as exc:
            record["evaluation_error"] = f"{type(exc).__name__}: {exc}"
    write_json(status_path, record)
    return record


def make_runtime_config(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "benchmarks/data/spreadsheetbench-v2")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--category", action="append")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--parallelism", type=int, default=8)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--task-timeout", type=float, default=5400.0)
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--recalculate-before-evaluation", action="store_true")
    args = parser.parse_args()
    args.dataset = args.dataset.resolve(); args.run_root = args.run_root.resolve(); args.skill = args.skill.resolve(); args.api_key_file = args.api_key_file.resolve()
    if not CLAUDE_BIN.is_file() or not os.access(CLAUDE_BIN, os.X_OK):
        parser.error(f"Claude binary is not executable: {CLAUDE_BIN}")
    tasks = load_tasks(args.dataset, args.category, set(args.task_id))
    args.run_root.mkdir(parents=True, exist_ok=True)
    (args.run_root / "task-plan.json").write_text(json.dumps([t["_task_id"] for t in tasks], indent=2), encoding="utf-8")
    port = free_local_port()
    audit = args.run_root / "proxy-requests.jsonl"
    proxy = subprocess.Popen([
        sys.executable, str(CLAUDE_PROXY_SCRIPT), "--port", str(port), "--upstream", args.base_url,
        "--api-key-file", str(args.api_key_file), "--audit", str(audit), "--max-requests", str(args.max_turns),
        "--max-output-tokens", str(args.max_output_tokens),
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(0.4)
        if proxy.poll() is not None:
            raise RuntimeError("Claude Messages proxy failed to start")
        runtime_tmp = ROOT / "tmp"; runtime_tmp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="spreadsheetbench-claude-", dir=runtime_tmp) as temp:
            results: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
                futures = {pool.submit(run_one, t, dataset=args.dataset, run_root=args.run_root, runtime_root=Path(temp), proxy_port=port, skill=args.skill, model=args.model, max_turns=args.max_turns, max_output_tokens=args.max_output_tokens, task_timeout=args.task_timeout, evaluator=ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py", recalculate_before_evaluation=args.recalculate_before_evaluation, api_key_file=args.api_key_file): t["_task_id"] for t in tasks}
                for future in as_completed(futures):
                    # Keep the batch alive if an individual task hits an
                    # unexpected worker/evaluator exception.  The task can be
                    # resumed safely from its workspace on a later pass.
                    task_key = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        result = {
                            "schema_version": 1,
                            "task_id": task_key.replace("__", "/", 1),
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    results.append(result)
                    print(json.dumps({"task_id": result["task_id"], "status": result["status"], "turns": result.get("turns", 0)}, ensure_ascii=False), flush=True)
            results.sort(key=lambda item: item["task_id"])
            (args.run_root / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"selected": len(results), "completed": sum(r["status"] == "completed" for r in results), "run_root": str(args.run_root)}), flush=True)
    finally:
        proxy.terminate()
        try: proxy.wait(timeout=10)
        except subprocess.TimeoutExpired: proxy.kill(); proxy.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
