#!/usr/bin/env python3
"""Run SpreadsheetBench-v2 with the official DeepSeek Harness ``dsh`` CLI.

Each task receives an isolated DSH home and workspace.  The official headless
profile owns the model/tool loop and session log; this runner only prepares the
task, supervises the CLI, recalculates the resulting workbook, and invokes the
official SpreadsheetBench evaluator.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from run_codex_spreadsheetbench_v2 import (
    EXPECTED_COUNTS,
    load_tasks,
    recalculate_workbook,
    sha256,
    skill_names,
    skill_package_manifest,
    skill_packages,
    skill_sha256,
    workbook_is_valid,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DSH_BIN = ROOT / "tmp/dsh-install/node_modules/.bin/dsh"
DEFAULT_SKILL = ROOT / "skills/spreadsheet-core/SKILL.md"
PROXY_SCRIPT = ROOT / "tools/dsh_chat_completions_proxy.py"
EVALUATOR = (
    ROOT
    / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
)


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def dsh_prompt(
    task: dict[str, Any], workspace: Path, skill_name: str | None
) -> str:
    skill_note = (
        "Before doing spreadsheet work, call the `skill` tool once for each of the following "
        f"financial skills: {skill_name}. Follow all of them throughout the task.\n"
        "These are the only benchmark skills available in the DeepSeek Harness skill registry."
        if skill_name is not None
        else "No benchmark skill is installed or loaded for this run. Use the DeepSeek Harness's native capabilities."
    )
    return f"""Complete one isolated SpreadsheetBench-v2 spreadsheet editing task.

{skill_note}

Category: {task['_category']}
Task id: {task['_task_id']}
Task instruction:
{task['instruction']}

Workspace contract:
- Work only inside {workspace}.
- The input workbook is {workspace / 'input.xlsx'}.
- Save the finished workbook to exactly {workspace / 'output.xlsx'}.
- Never search for, open, or inspect any golden/reference/answer workbook.
- Perform the edits; do not stop at a plan or explanation.
- Use Python with openpyxl and LibreOffice Calc when appropriate. Preserve unrelated values,
  formulas, styles, merged cells, dimensions, and workbook structure.
- You are running alongside other independent benchmark workers. Never use `kill`, `pkill`,
  `killall`, service-management commands, or any other command that terminates or changes
  processes outside this task. Do not inspect or modify any other worker's files.
- If LibreOffice is needed, use only a private profile at
  `{workspace / '.libreoffice-profile'}` (for example,
  `-env:UserInstallation=file://{workspace / '.libreoffice-profile'}`); never attach to,
  terminate, or reuse a system LibreOffice process. If recalculation fails, leave the output
  intact for the outer runner to recalculate with its own isolated profile.
- Reopen output.xlsx and verify the requested result before finishing.
- Also create a reusable `{workspace / 'solution.py'}` containing the complete workbook
  transformation, then run it (or otherwise use it) to produce output.xlsx. This file is
  required by the v1 sibling-replay adapter.
- Keep all inspection output bounded; do not dump whole workbooks.

Finish only after a valid output.xlsx exists. Report the output path and verification result.
"""


def audit_state(audit_path: Path, slug: str) -> tuple[int, bool]:
    # A DSH HTTP request that receives a LiteLLM 429 did not consume a model
    # turn.  Count only requests with a successful upstream response.  This
    # also lets a restarted runner recover tasks that were interrupted while
    # the gateway was in cooldown.
    accepted = 0
    exceeded = False
    if not audit_path.is_file():
        return accepted, exceeded
    successful = 0
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("task_key") != slug:
            continue
        if event.get("event") == "request":
            if event.get("limit_exceeded") is True:
                exceeded = True
        elif event.get("event") == "response" and isinstance(
            event.get("upstream_status"), int
        ) and 200 <= event["upstream_status"] < 300:
            successful += 1
    accepted = successful
    return accepted, exceeded


def session_skill_loaded(dsh_home: Path, skill_name: str | list[str]) -> bool | dict[str, bool]:
    names = [skill_name] if isinstance(skill_name, str) else list(skill_name)
    loaded = {name: False for name in names}
    sessions = dsh_home / "sessions"
    if not sessions.exists():
        return loaded if len(names) > 1 else False
    # Headless DSH stores sessions as zstd-compressed JSONL.  Keep support for
    # plain JSONL too, since older/package-local profiles may use that format.
    for path in sessions.rglob("*.jsonl*"):
        try:
            if path.name.endswith(".zstd"):
                completed = subprocess.run(
                    ["zstdcat", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if completed.returncode != 0:
                    continue
                text = completed.stdout
            else:
                text = path.read_text(encoding="utf-8")
        except (OSError, subprocess.SubprocessError):
            continue
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict):
                data = event if isinstance(event, dict) else {}
            if event.get("type") == "tool/call" and data.get("name") == "skill":
                arguments = data.get("arguments", "")
                arguments_text = str(arguments)
                for name in names:
                    if name in arguments_text:
                        loaded[name] = True
    return loaded if len(names) > 1 else loaded[names[0]]


def prepare_dsh_home(
    dsh_home: Path,
    *,
    proxy_port: int,
    slug: str,
    skill: Path | None,
    model: str,
) -> None:
    profile = dsh_home / "profiles/headless"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if skill is not None:
        for package in skill_packages(skill):
            skill_dir = dsh_home / "skills" / package.parent.name
            shutil.copytree(package.parent, skill_dir, dirs_exist_ok=True)
    else:
        # Prevent a reused workspace from inheriting a previously installed skill.
        shutil.rmtree(dsh_home / "skills", ignore_errors=True)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "name": "dsh-profile-headless",
                "private": True,
                "dependencies": {},
                "dsh": {
                    "profile": {
                        "bundles": [
                            "@deepseek-ai/dsh-base",
                            "@deepseek-ai/dsh-headless",
                        ],
                        "patchReload": "startup",
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (profile / "pnpm-workspace.yaml").write_text("packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n", encoding="utf-8")
    (profile / "cordis.yml").write_text("[]\n", encoding="utf-8")
    # A custom pi-ai route preserves the user's exact LiteLLM model name and
    # exposes medium reasoning to DSH.  The transparent proxy pins top_p, which
    # is not part of DSH's provider-neutral call-config vocabulary.
    patch = f"""- id: session-title-llm
  disabled: true

- id: llm-deepseek
  disabled: true

- id: llm-pi-ai
  config:
    providers:
      litellm:
        displayName: LiteLLM
        apiKeyEnv: DSH_BENCHMARK_PROXY_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:{proxy_port}/task/{slug}/v1
        defaultContextWindow: 262144
        defaultMaxTokens: 32768
        # Keep DSH's stream idle limit aligned with the outer per-task budget.
        # The old fixed 30-minute value caused long tasks to fail around 1800s
        # even when the runner was configured for a six-hour task timeout.
        streamIdleTimeoutMs: 21600000
        retryPolicy:
          mode: normal
          maxRetries: 5
        compat:
          supportsStore: false
          supportsDeveloperRole: false
          supportsReasoningEffort: false
          supportsUsageInStreaming: true
          supportsFinishReason: true
          maxTokensField: max_tokens
          requiresReasoningContentOnAssistantMessages: true
          thinkingFormat: chat-template
          chatTemplateKwargs:
            enable_thinking:
              $var: thinking.enabled
        models:
          - id: {model}
            name: {model}
            contextWindow: 262144
            input: [text]
            reasoningEfforts:
              off:
              medium: medium

{f'''- id: skill-filesystem
  config:
    includeDefaultRoots: false
    customSkillDirs:
      - {dsh_home / 'skills'}
    watch: false''' if skill is not None else '''- id: skill-filesystem
  disabled: true'''}
"""
    (profile / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    (dsh_home / "settings.yaml").write_text(
        "agent-default-model:\n"
        "  provider: litellm\n"
        f"  model: {model}\n"
        "  reasoningEffort: medium\n",
        encoding="utf-8",
    )


def adopt_output(task_root: Path, output_path: Path, task_id: str) -> None:
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


def run_one(
    task: dict[str, Any],
    *,
    dataset: Path,
    run_root: Path,
    dsh_bin: Path,
    proxy_port: int,
    skill: Path | None,
    model: str,
    max_turns: int,
    task_timeout: float,
    recalculate_before_evaluation: bool,
    defer_evaluation: bool = False,
) -> dict[str, Any]:
    category = str(task["_category"])
    task_id = str(task["id"])
    slug = f"{category}__{task_id}"
    task_root = run_root / "tasks" / slug
    status_path = task_root / "status.json"
    output_path = task_root / "output.xlsx"
    input_path = task_root / "input.xlsx"
    audit_path = run_root / "proxy-requests.jsonl"
    task_root.mkdir(parents=True, exist_ok=True)

    old: dict[str, Any] = {}
    if status_path.is_file():
        try:
            old = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
    ready = workbook_is_valid(output_path)
    requests_before, _ = audit_state(audit_path, slug)
    if old.get("status") == "completed" and ready:
        return old
    if old.get("status") == "turn_limit" and requests_before >= max_turns and not ready:
        return old

    input_source = (dataset / category / str(task["spreadsheet_path"])).resolve(strict=True)
    if not input_path.is_file():
        shutil.copy2(input_source, input_path)
    dsh_home = task_root / "dsh-home"
    prepare_dsh_home(
        dsh_home,
        proxy_port=proxy_port,
        slug=slug,
        skill=skill,
        model=model,
    )
    final_path = task_root / "dsh.final.txt"
    stderr_path = task_root / "dsh.stderr.log"
    started = time.time()
    timed_out = False
    return_code = 0

    if not ready and requests_before < max_turns:
        env = dict(os.environ)
        env.update(
            {
                "DSH_HOME": str(dsh_home),
                "DSH_BENCHMARK_PROXY_KEY": "local-proxy-only",
                # Restrict each DSH child to its task workspace.  This is
                # important because several independent benchmark suites run
                # concurrently on the same host.
                "DSH_PERMISSION_MODE": "workspace-write",
                "DSH_TELEMETRY_DISABLED": "1",
                "DSH_TOOLS_MODE": "native",
                "SHEET_AGENT_TASK_ID": f"{category}/{task_id}",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        command = [
            str(dsh_bin),
            "--profile",
            "headless",
            dsh_prompt(
                task, task_root, ", ".join(skill_names(skill)) if skill is not None else None
            ),
        ]
        try:
            with final_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                "a", encoding="utf-8"
            ) as stderr:
                completed = subprocess.run(
                    command,
                    cwd=task_root,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    timeout=task_timeout,
                    check=False,
                )
            return_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = 124

    adopt_output(task_root, output_path, task_id)
    ready = workbook_is_valid(output_path)
    recalculated = False
    recalculation_error: str | None = None
    if ready and recalculate_before_evaluation and not defer_evaluation and category != "Visualization":
        backup = task_root / "output.pre-recalc.xlsx"
        if not backup.exists():
            shutil.copy2(output_path, backup)
        recalculated, recalculation_error = recalculate_workbook(output_path, output_path)
        ready = workbook_is_valid(output_path)

    model_requests, limit_exceeded = audit_state(audit_path, slug)
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
        "harness": "deepseek-ai/deepseek-harness",
        "harness_cli": "dsh --profile headless",
        "dsh_version": "0.1.5-rc.1",
        "task_id": f"{category}/{task_id}",
        "category": category,
        "status": status,
        "exit_code": return_code,
        "model": model,
        "provider": "litellm",
        "reasoning_effort": "medium",
        "temperature": 0.0,
        "top_p": 1.0,
        "enable_thinking": True,
        "max_turns": max_turns,
        "turn_limit_unit": "model_requests",
        "model_requests": model_requests,
        "turns": model_requests,
        "started_at": started,
        "elapsed_seconds": round(time.time() - started, 3),
        "skill_enabled": skill is not None,
        "skill_name": ", ".join(skill_names(skill)) if skill is not None else None,
        "skill_names": skill_names(skill),
        "skill_packages": skill_package_manifest(skill),
        "skill_path": str(skill) if skill is not None else None,
        "skill_sha256": skill_sha256(skill),
        "skill_load_mode": (
            "official-dsh-skill-filesystem-and-skill-tool"
            if skill is not None
            else "none"
        ),
        "skill_loaded_in_session": (
            session_skill_loaded(dsh_home, skill_names(skill))
            if skill is not None
            else False
        ),
        "dsh_home": str(dsh_home),
        "dsh_sessions": str(dsh_home / "sessions"),
        "output": str(output_path),
        "output_exists": output_path.is_file(),
        "output_valid": ready,
        "output_changed_from_input": (
            sha256(output_path) != sha256(input_path) if output_path.is_file() else False
        ),
        "final_message": str(final_path),
        "stderr_log": str(stderr_path),
        "proxy_audit": str(audit_path),
        "recalculated_before_evaluation": recalculated,
        "evaluation_deferred": bool(ready and defer_evaluation),
    }
    if recalculation_error and not recalculated:
        record["recalculation_error"] = recalculation_error

    if ready and category != "Visualization" and not defer_evaluation:
        try:
            evaluator_output = task_root / f"{task_id}_output.xlsx"
            shutil.copy2(output_path, evaluator_output)
            if str(EVALUATOR.parent) not in sys.path:
                sys.path.insert(0, str(EVALUATOR.parent))
            import evaluation  # type: ignore

            score, _missing = evaluation.process_single_item(
                task,
                str(dataset / category),
                str(task_root),
                {task_id: model_requests},
                category,
            )
            record["official_score"] = score
        except Exception as exc:  # pragma: no cover - evaluator dependent
            record["evaluation_error"] = f"{type(exc).__name__}: {exc}"
    write_json(status_path, record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "benchmarks/data/spreadsheetbench-v2"
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--category", action="append")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--parallelism", type=int, default=6)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--task-timeout", type=float, default=5400.0)
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--no-skill", action="store_true")
    parser.add_argument("--dsh-bin", type=Path, default=DEFAULT_DSH_BIN)
    parser.add_argument("--recalculate-before-evaluation", action="store_true")
    args = parser.parse_args()
    args.dataset = args.dataset.resolve()
    args.run_root = args.run_root.resolve()
    args.api_key_file = args.api_key_file.resolve()
    args.skill = None if args.no_skill else args.skill.resolve()
    args.dsh_bin = args.dsh_bin.resolve()
    if args.parallelism < 1 or args.max_turns < 1 or args.task_timeout <= 0:
        parser.error("parallelism, max-turns, and task-timeout must be positive")
    if not args.dsh_bin.is_file() or not os.access(args.dsh_bin, os.X_OK):
        parser.error(f"official dsh binary is not executable: {args.dsh_bin}")
    if args.skill is not None and not args.skill.exists():
        parser.error(f"skill not found: {args.skill}")
    if args.skill is not None:
        try:
            skill_packages(args.skill)
        except ValueError as exc:
            parser.error(str(exc))
    if not args.api_key_file.is_file() or not args.api_key_file.read_text(
        encoding="utf-8"
    ).strip():
        parser.error("API key file is missing or empty")

    tasks = load_tasks(args.dataset, args.category, set(args.task_id))
    expected_total = sum(EXPECTED_COUNTS.values())
    if not args.category and not args.task_id and len(tasks) != expected_total:
        parser.error(f"expected all {expected_total} tasks, found {len(tasks)}")
    args.run_root.mkdir(parents=True, exist_ok=True)
    write_json(
        args.run_root / "manifest.json",
        {
            "schema_version": 1,
            "harness": "deepseek-ai/deepseek-harness",
            "harness_cli": "dsh --profile headless",
            "dsh_bin": str(args.dsh_bin),
            "dsh_version": "0.1.5-rc.1",
            "dataset": str(args.dataset),
            "selected": len(tasks),
            "expected_counts": EXPECTED_COUNTS,
            "model": args.model,
            "reasoning_effort": "medium",
            "temperature": 0.0,
            "top_p": 1.0,
            "enable_thinking": True,
            "max_turns": args.max_turns,
            "turn_limit_unit": "model_requests",
            "parallelism": args.parallelism,
            "skill_enabled": args.skill is not None,
            "skill": str(args.skill) if args.skill is not None else None,
            "skill_sha256": skill_sha256(args.skill),
            "base_url": args.base_url,
            "task_plan": [task["_task_id"] for task in tasks],
        },
    )
    if args.skill is not None and args.skill.is_dir():
        manifest = json.loads((args.run_root / "manifest.json").read_text(encoding="utf-8"))
        manifest["skill_packages"] = skill_package_manifest(args.skill)
        write_json(args.run_root / "manifest.json", manifest)
    port = free_local_port()
    audit_path = args.run_root / "proxy-requests.jsonl"
    proxy_log = args.run_root / "proxy.log"
    with proxy_log.open("a", encoding="utf-8") as proxy_output:
        proxy = subprocess.Popen(
            [
                sys.executable,
                str(PROXY_SCRIPT),
                "--port",
                str(port),
                "--upstream",
                args.base_url,
                "--api-key-file",
                str(args.api_key_file),
                "--audit",
                str(audit_path),
                "--max-requests",
                str(args.max_turns),
                "--timeout",
                str(args.task_timeout),
            ],
            stdout=proxy_output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(0.5)
            if proxy.poll() is not None:
                raise RuntimeError("DSH Chat Completions proxy failed to start")
            results: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
                futures = {
                    pool.submit(
                        run_one,
                        task,
                        dataset=args.dataset,
                        run_root=args.run_root,
                        dsh_bin=args.dsh_bin,
                        proxy_port=port,
                        skill=args.skill,
                        model=args.model,
                        max_turns=args.max_turns,
                        task_timeout=args.task_timeout,
                        recalculate_before_evaluation=args.recalculate_before_evaluation,
                    ): task["_task_id"]
                    for task in tasks
                }
                for future in as_completed(futures):
                    task_key = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        result = {
                            "schema_version": 1,
                            "task_id": task_key,
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                "task_id": result["task_id"],
                                "status": result["status"],
                                "turns": result.get("turns", 0),
                                "skill_loaded": result.get("skill_loaded_in_session"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            results.sort(key=lambda item: item["task_id"])
            write_json(args.run_root / "results.json", {"results": results})
            print(
                json.dumps(
                    {
                        "selected": len(results),
                        "completed": sum(
                            result.get("status") == "completed" for result in results
                        ),
                        "run_root": str(args.run_root),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
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
