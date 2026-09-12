#!/usr/bin/env python3
"""Run SpreadsheetBench-v2 tasks with the real Codex CLI.

Each task gets an isolated workspace containing a copied input workbook.  The
Codex CLI performs the agent loop and edits the workbook directly.  This file
does not implement a replacement model/tool loop; it only launches Codex,
captures its JSONL events, enforces the turn ceiling, and runs the official
value-only evaluator for non-visual tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CODEX_BIN = Path("/home/tongzeyuan/.nvm/versions/node/v24.19.0/bin/codex")
CLAUDE_BIN = Path("/home/tongzeyuan/.nvm/versions/node/v24.19.0/bin/claude")
PROXY_SCRIPT = ROOT / "tools/codex_responses_proxy.py"
CLAUDE_PROXY_SCRIPT = ROOT / "tools/claude_messages_proxy.py"
DEFAULT_SKILL = ROOT / "skills/spreadsheet-manipulation/SKILL.md"
EXPECTED_COUNTS = {"Debugging": 100, "Financial_Model": 100, "Template": 97, "Visualization": 24}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workbook_is_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=False)
        workbook.close()
    except Exception:
        return False
    return True


def recalculate_workbook(source: Path, destination: Path, timeout: float = 300.0) -> tuple[bool, str]:
    """Recalculate a workbook with LibreOffice without touching the Codex artifact.

    openpyxl writes formulas but does not populate Excel's cached formula values.  The
    official SpreadsheetBench evaluator reads most workbooks with ``data_only=True``,
    so a formula that is present can otherwise appear as ``None``.  Recalculation is
    performed into a per-call temporary directory and the destination is replaced only
    after the converted workbook passes a validity check.  A failed LibreOffice run
    therefore never destroys the original output.
    """
    if not source.is_file():
        return False, "source workbook does not exist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="spreadsheet-lo-") as temp:
            temp_root = Path(temp)
            out_dir = temp_root / "out"
            out_dir.mkdir()
            profile = temp_root / "profile"
            command = [
                "libreoffice",
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                "--norestore",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(out_dir),
                str(source),
            ]
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            converted = out_dir / source.name
            if completed.returncode != 0 or not workbook_is_valid(converted):
                detail = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "conversion failed"
                return False, detail
            # Keep an .xlsx suffix so openpyxl validates the staging file too.
            temp_destination = destination.with_name(destination.stem + ".recalc.tmp" + destination.suffix)
            shutil.copy2(converted, temp_destination)
            if not workbook_is_valid(temp_destination):
                temp_destination.unlink(missing_ok=True)
                return False, "converted workbook failed validation"
            temp_destination.replace(destination)
            return True, completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "ok"
    except subprocess.TimeoutExpired:
        return False, f"LibreOffice timed out after {timeout:g}s"
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, f"{type(exc).__name__}: {exc}"


def load_tasks(dataset: Path, categories: list[str] | None, task_ids: set[str]) -> list[dict[str, Any]]:
    selected = categories or list(EXPECTED_COUNTS)
    tasks: list[dict[str, Any]] = []
    for category in selected:
        if category not in EXPECTED_COUNTS:
            raise ValueError(f"unknown category: {category}")
        path = dataset / category / "dataset.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if len(data) != EXPECTED_COUNTS[category]:
            raise ValueError(f"{category}: expected {EXPECTED_COUNTS[category]}, found {len(data)}")
        for item in data:
            task_id = f"{category}/{item['id']}"
            if task_ids and task_id not in task_ids:
                continue
            item = dict(item)
            item["_category"] = category
            item["_task_id"] = task_id
            tasks.append(item)
    if task_ids:
        found = {item["_task_id"] for item in tasks}
        missing = sorted(task_ids - found)
        if missing:
            raise ValueError("unknown task ids: " + ", ".join(missing))
    return tasks


def make_codex_home(base_dir: Path, port: int, api_key: str, skill: Path, task_key: str) -> Path:
    home = base_dir / "codex-home"
    home.mkdir(mode=0o700, parents=True)
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": api_key}), encoding="utf-8")
    (home / "auth.json").chmod(0o600)
    (home / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "litellm"',
                'model = "DeepSeek-V4-Flash"',
                'model_reasoning_effort = "medium"',
                "disable_response_storage = true",
                'preferred_auth_method = "apikey"',
                "",
                "[model_providers.litellm]",
                'name = "litellm"',
                f'base_url = "http://127.0.0.1:{port}/task/{task_key}/v1"',
                'wire_api = "responses"',
                "requires_openai_auth = true",
                "",
                "[features]",
                "apps = false",
                "plugins = false",
                "remote_plugin = false",
                "recommended_plugins = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (home / "config.toml").chmod(0o600)
    # Keep a Codex-discoverable copy as well as the explicit prompt reference.
    skill_target = home / "skills" / skill.parent.name
    skill_target.mkdir(parents=True)
    shutil.copy2(skill, skill_target / skill.name)
    return home


def make_claude_home(base_dir: Path) -> Path:
    """Create an isolated Claude Code config directory for one task."""
    home = base_dir / "claude-config"
    home.mkdir(mode=0o700, parents=True)
    return home


def prompt_for(task: dict[str, Any], workspace: Path) -> str:
    category = task["_category"]
    return f"""You are the spreadsheet editing agent for one isolated SpreadsheetBench-v2 task.

The required spreadsheet-manipulation skill is already loaded through this workspace's AGENTS.md
and the Codex skill registry. Follow it throughout the task. Do not read SKILL.md again; begin by
inspecting the workbook. In your final response explicitly state that the skill was loaded.

Category: {category}
Task id: {task['_task_id']}
Task instruction:
{task['instruction']}

Workspace rules:
- Work only inside {workspace}.
- Input workbook (read-only source copy): {workspace / 'input.xlsx'}
- You must save the finished workbook to exactly: {workspace / 'output.xlsx'}
- Do not use or inspect the golden workbook. Do not change files outside this workspace.
- Use Python/openpyxl and LibreOffice Calc as appropriate. Inspect workbook structure and relevant
  ranges before editing, preserve unrelated formulas/styles/merged cells, and verify the saved output.
- For visual/layout tasks, render or inspect the workbook when useful, while still saving output.xlsx.

Do not stop at an explanation: perform the edits, save output.xlsx, reopen it, and report the output
path and verification result only after the artifact exists.
"""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def run_one(
    task: dict[str, Any],
    *,
    dataset: Path,
    run_root: Path,
    runtime_root: Path,
    proxy_port: int,
    api_key: str,
    skill: Path,
    model: str,
    max_turns: int,
    max_output_tokens: int,
    task_timeout: float,
    evaluator: Path,
    defer_evaluation: bool,
    recalculate_before_evaluation: bool,
) -> dict[str, Any]:
    category = task["_category"]
    task_id = str(task["id"])
    slug = f"{category}__{task_id}"
    task_root = run_root / "tasks" / slug
    status_path = task_root / "status.json"
    output_path = task_root / "output.xlsx"
    old: dict[str, Any] = {}
    if status_path.is_file() and output_path.is_file():
        try:
            old = json.loads(status_path.read_text(encoding="utf-8"))
            if old.get("status") == "completed" and not recalculate_before_evaluation:
                return old
        except Exception:
            pass
    reuse_output = workbook_is_valid(output_path)
    task_root.mkdir(parents=True, exist_ok=True)
    skill_text = skill.read_text(encoding="utf-8")
    (task_root / "AGENTS.md").write_text(
        "# Loaded spreadsheet-manipulation skill\n\n"
        "The following skill is binding. It is already loaded; do not spend a tool call reading it.\n\n"
        + skill_text,
        encoding="utf-8",
    )
    codex_home = make_codex_home(runtime_root / slug, proxy_port, api_key, skill, slug)
    input_source = (dataset / category / str(task["spreadsheet_path"])).resolve(strict=True)
    input_path = task_root / "input.xlsx"
    if not input_path.is_file():
        shutil.copy2(input_source, input_path)
    if not reuse_output:
        output_path.unlink(missing_ok=True)
        (task_root / f"{task_id}_output.xlsx").unlink(missing_ok=True)
    trajectory_path = task_root / "trajectory.jsonl"
    stderr_path = task_root / "codex.stderr.log"
    final_path = task_root / "codex.final.txt"
    if not reuse_output:
        trajectory_path.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "OPENAI_API_KEY": "",
            "SHEET_AGENT_TASK_ID": f"{category}/{task_id}",
        }
    )
    command = [
        str(CODEX_BIN),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(task_root),
        "-m",
        model,
        "-o",
        str(final_path),
        "-",
    ]
    started = float(old.get("started_at", time.time()))
    deadline = time.monotonic() + task_timeout
    outer_turns = 0
    thread_id: str | None = None
    timed_out = False
    if reuse_output and trajectory_path.is_file():
        for line in trajectory_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
            if event.get("type") == "turn.started":
                outer_turns += 1

    audit_path = run_root / "proxy-requests.jsonl"

    def request_state() -> tuple[int, bool]:
        count = 0
        limit = False
        if not audit_path.is_file():
            return count, limit
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("task_key") != slug or "path" not in event:
                continue
            if event.get("limit_exceeded"):
                limit = True
            else:
                count += 1
        return count, limit

    def adopt_output() -> None:
        if output_path.is_file():
            return
        candidates = sorted(
            (
                path
                for path in task_root.glob("*.xlsx")
                if path.name not in {"input.xlsx", f"{task_id}_output.xlsx"}
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            shutil.copy2(candidates[0], output_path)

    def artifact_ready() -> bool:
        adopt_output()
        return workbook_is_valid(output_path)

    def invoke(invocation: list[str], prompt: str) -> int:
        nonlocal outer_turns, thread_id, timed_out
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            return 124
        process = subprocess.Popen(
            invocation,
            cwd=task_root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(prompt)
        process.stdin.close()

        def consume() -> None:
            nonlocal outer_turns, thread_id
            for line in process.stdout:
                trajectory.write(line)
                trajectory.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                if event.get("type") == "turn.started":
                    outer_turns += 1

        reader = threading.Thread(target=consume, name=f"codex-{slug}", daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=15)
        reader.join(timeout=15)
        if process.poll() is None:
            process.kill()
            code = process.wait(timeout=15)
        return code

    return_code = int(old.get("exit_code", 0))
    if not reuse_output:
        with trajectory_path.open("w", encoding="utf-8") as trajectory, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            return_code = invoke(command, prompt_for(task, task_root))
            for _ in range(max_turns):
                if artifact_ready():
                    break
                model_requests, limit_exceeded = request_state()
                if timed_out or limit_exceeded or model_requests >= max_turns or not thread_id:
                    break
                before = model_requests
                resume_command = [
                    str(CODEX_BIN),
                    "exec",
                    "resume",
                    "--json",
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-m",
                    model,
                    "-o",
                    str(final_path),
                    thread_id,
                    "-",
                ]
                return_code = invoke(
                    resume_command,
                    f"You stopped before creating a valid {output_path}. Continue the same task now. "
                    "Use one focused Python/openpyxl edit script, save output.xlsx, reopen it to verify, "
                    "and do not stop with a description of future work.",
                )
                after, _ = request_state()
                if after == before and return_code != 0:
                    break

    ready = artifact_ready()
    recalculated = False
    recalculation_error: str | None = None
    # Formula caches are not populated by openpyxl.  Recalculate existing/generated
    # non-visual outputs immediately before the official evaluator so data_only=True
    # comparisons see the computed values.  Visualization is intentionally excluded:
    # LibreOffice can rewrite chart XML and its COM/VLM score is not run on Linux.
    if ready and recalculate_before_evaluation and not defer_evaluation and category != "Visualization":
        backup_path = task_root / "output.pre-recalc.xlsx"
        if not backup_path.exists():
            shutil.copy2(output_path, backup_path)
        recalculated, recalculation_error = recalculate_workbook(output_path, output_path)
        ready = artifact_ready()
    model_requests, limit_exceeded = request_state()
    if ready and defer_evaluation:
        status = "generated"
    elif ready:
        status = "completed"
    elif timed_out:
        status = "timeout"
    elif limit_exceeded or model_requests >= max_turns:
        status = "turn_limit"
    else:
        status = "failed"

    record: dict[str, Any] = {
        "schema_version": 1,
        "task_id": f"{category}/{task_id}",
        "category": category,
        "status": status,
        "exit_code": return_code,
        "codex_outer_turns": outer_turns,
        "max_turns": max_turns,
        "max_output_tokens": max_output_tokens,
        "thread_id": thread_id,
        "model": model,
        "started_at": started,
        "elapsed_seconds": round(time.time() - started, 3),
        "skill_path": str(skill),
        "skill_sha256": sha256(skill),
        "temperature": 0.0,
        "top_p": 1.0,
        "enable_thinking": True,
        "output": str(output_path),
        "trajectory": str(trajectory_path),
        "final_message": str(final_path),
        "output_exists": output_path.is_file(),
        "output_valid": ready,
        "output_changed_from_input": (
            sha256(output_path) != sha256(input_path) if output_path.is_file() else False
        ),
        "skill_load_mode": "codex-agents-and-skill-registry",
        "reused_existing_output": reuse_output,
        "evaluation_deferred": bool(ready and defer_evaluation),
        "recalculated_before_evaluation": recalculated,
    }
    if recalculation_error and not recalculated:
        record["recalculation_error"] = recalculation_error
    record["turns"] = model_requests
    record["model_requests"] = model_requests
    # Official evaluator is intentionally not run for Visualization: its COM/VLM step requires
    # Windows/Excel. The workbook is still generated and retained for that separate stage.
    if ready and category != "Visualization" and not defer_evaluation:
        try:
            evaluator_output = task_root / f"{task_id}_output.xlsx"
            shutil.copy2(output_path, evaluator_output)
            sys.path.insert(0, str(evaluator.parent))
            import evaluation  # type: ignore

            score, _missing = evaluation.process_single_item(
                task,
                str(dataset / category),
                str(task_root),
                {task_id: model_requests},
                category,
            )
            record["official_score"] = score
        except Exception as exc:  # pragma: no cover - evaluator import/runtime dependent
            record["evaluation_error"] = f"{type(exc).__name__}: {exc}"
    write_json(status_path, record)
    return record


def main() -> int:
    global CODEX_BIN
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "benchmarks/data/spreadsheetbench-v2")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--category", action="append")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--task-timeout", type=float, default=3600.0)
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--codex-bin", type=Path, default=CODEX_BIN)
    parser.add_argument("--defer-evaluation", action="store_true")
    parser.add_argument(
        "--recalculate-before-evaluation",
        action="store_true",
        help="recalculate existing outputs with LibreOffice before official scoring",
    )
    args = parser.parse_args()
    CODEX_BIN = args.codex_bin
    args.dataset = args.dataset.resolve()
    args.run_root = args.run_root.resolve()
    args.skill = args.skill.resolve()
    args.api_key_file = args.api_key_file.resolve()
    if args.parallelism < 1 or args.max_turns < 1 or args.max_output_tokens < 1:
        parser.error("parallelism, max-turns, and max-output-tokens must be positive")
    if not CODEX_BIN.is_file() or not os.access(CODEX_BIN, os.X_OK):
        parser.error(f"Codex binary is not executable: {CODEX_BIN}")
    if not args.skill.is_file():
        parser.error(f"skill not found: {args.skill}")
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        parser.error("API key file is empty")
    evaluator = ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
    tasks = load_tasks(args.dataset, args.category, set(args.task_id))
    args.run_root.mkdir(parents=True, exist_ok=True)
    plan_path = args.run_root / "task-plan.json"
    plan_path.write_text(json.dumps([item["_task_id"] for item in tasks], indent=2), encoding="utf-8")

    proxy_port = 39000 + (os.getpid() % 1000)
    audit_path = args.run_root / "proxy-requests.jsonl"
    proxy = subprocess.Popen(
        [
            sys.executable,
            str(PROXY_SCRIPT),
            "--port",
            str(proxy_port),
            "--upstream",
            args.base_url,
            "--audit",
            str(audit_path),
            "--max-requests",
            str(args.max_turns),
            "--max-output-tokens",
            str(args.max_output_tokens),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(0.4)
        if proxy.poll() is not None:
            raise RuntimeError("Responses proxy failed to start")
        # Codex refuses to create its helper aliases when CODEX_HOME itself is
        # below /tmp. Keep the short-lived secret-bearing home under the
        # repository's private tmp directory instead, and remove it on exit.
        runtime_tmp = ROOT / "tmp"
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="spreadsheetbench-codex-", dir=runtime_tmp, ignore_cleanup_errors=True
        ) as temp:
            runtime_root = Path(temp)
            results: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
                futures = {
                    pool.submit(
                        run_one,
                        task,
                        dataset=args.dataset,
                        run_root=args.run_root,
                        runtime_root=runtime_root,
                        proxy_port=proxy_port,
                        api_key=api_key,
                        skill=args.skill,
                        model=args.model,
                        max_turns=args.max_turns,
                        max_output_tokens=args.max_output_tokens,
                        task_timeout=args.task_timeout,
                        evaluator=evaluator,
                        defer_evaluation=args.defer_evaluation,
                        recalculate_before_evaluation=args.recalculate_before_evaluation,
                    ): task["_task_id"]
                    for task in tasks
                }
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    print(json.dumps({"task_id": result["task_id"], "status": result["status"], "turns": result["turns"]}, ensure_ascii=False), flush=True)
            results.sort(key=lambda item: item["task_id"])
            (args.run_root / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            completed = sum(item["status"] == "completed" for item in results)
            print(json.dumps({"selected": len(results), "completed": completed, "run_root": str(args.run_root)}), flush=True)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
