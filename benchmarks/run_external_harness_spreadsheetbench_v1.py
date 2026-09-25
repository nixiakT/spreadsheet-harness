#!/usr/bin/env python3
"""Run SpreadsheetBench-v1 through a real Codex, Claude Code, or DSH CLI.

SpreadsheetBench-v1 is a code-generation benchmark: one solution is produced
from case 1 and that exact solution is replayed on the sibling inputs.  The
v2 CLI runners cannot be used directly because independently solving all
siblings would change the protocol.  This runner reuses their real-harness
launchers only for case-1 generation, freezes ``solution.py``, executes that
same file for every case, and applies the pinned v1 Soft/Hard evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

import run_claude_spreadsheetbench_v2 as claude_runner
import run_codex_spreadsheetbench_v2 as codex_runner
import run_deepseek_harness_spreadsheetbench_v2 as dsh_runner

from spreadsheet_harness.spreadsheetbench_v1 import (
    V1_AVAILABLE_INPUT_CASE_COUNT,
    V1_DATASET_JSON_SHA256,
    V1_INSTRUCTION_COUNT,
    V1_OFFICIAL_EVALUATOR_SHA256,
    V1_RUN_PROTOCOL,
    V1_SOURCE_REPOSITORY,
    V1_SOURCE_REVISION,
    SpreadsheetBenchV1Instruction,
    load_spreadsheetbench_v1,
    score_v1_instruction,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "benchmarks/data/spreadsheetbench_912_v0.1"
DEFAULT_SKILL = ROOT / "skills/spreadsheet-core/SKILL.md"
V2_EVALUATOR = (
    ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
)
SCHEMA_VERSION = "spreadsheetbench-v1-real-cli-harness-v1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def migrate_legacy_skill_manifest(
    run_root: Path, previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Migrate the pre-package xlsx manifest without rerunning completed tasks.

    The first official-xlsx runs were created before the manifest recorded the
    package list for a skill.  The runner now records that list, so a strict
    equality check would make every resume fail before any task is scheduled.
    This narrowly scoped migration accepts only that additive metadata change,
    updates the manifest hash on existing records, and leaves unfinished tasks
    retryable.
    """
    old_skill = previous.get("skill")
    new_skill = current.get("skill")
    if not isinstance(old_skill, dict) or not isinstance(new_skill, dict):
        return False
    if "packages" in old_skill or "packages" not in new_skill:
        return False
    if any(old_skill.get(key) != new_skill.get(key) for key in old_skill):
        return False
    old_without = dict(previous)
    new_without = dict(current)
    old_without.pop("manifest_sha256", None)
    new_without.pop("manifest_sha256", None)
    old_without.pop("skill", None)
    new_without.pop("skill", None)
    if old_without != new_without:
        return False

    old_hash = previous.get("manifest_sha256")
    new_hash = current.get("manifest_sha256")
    if not isinstance(old_hash, str) or not isinstance(new_hash, str):
        return False
    atomic_json(run_root / "manifest.json", current)
    tasks_root = run_root / "tasks"
    for status_path in tasks_root.glob("*/status.json"):
        try:
            row = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("manifest_sha256") == old_hash:
            row["manifest_sha256"] = new_hash
            atomic_json(status_path, row)
    results_path = run_root / "results.json"
    if results_path.is_file():
        try:
            rows = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows = None
        if isinstance(rows, list):
            changed = False
            for row in rows:
                if isinstance(row, dict) and row.get("manifest_sha256") == old_hash:
                    row["manifest_sha256"] = new_hash
                    changed = True
            if changed:
                atomic_json(results_path, rows)
    summary_path = run_root / "summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = None
        if isinstance(summary, dict) and summary.get("manifest_sha256") == old_hash:
            summary["manifest_sha256"] = new_hash
            atomic_json(summary_path, summary)
    return True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def task_dict(task: SpreadsheetBenchV1Instruction) -> dict[str, Any]:
    return {
        "id": task.task_id,
        "instruction": task.instruction,
        "instruction_type": task.instruction_type,
        "spreadsheet_path": f"{task.task_id}/1_{task.task_id}_input.xlsx",
        "_category": "spreadsheet",
        "_task_id": task.task_id,
    }


def generation_prompt(
    task: dict[str, Any], workspace: Path, *, harness: str, skill_name: str | None
) -> str:
    skill_loading = ({
        "codex": (
            f"The `{skill_name}` skill is already loaded through AGENTS.md and the "
            "isolated Codex skill registry. Follow it throughout."
        ),
        "claude": (
            f"The `{skill_name}` skill is already loaded through CLAUDE.md. "
            "Follow it throughout."
        ),
        "dsh": (
            f"Before spreadsheet work, call the skill tool once for each loaded financial skill: `{skill_name}`. "
            "These are the only benchmark skills in this DSH registry."
        ),
    }[harness] if skill_name is not None else
        "No benchmark skill is installed or loaded for this run. Use only the harness's native capabilities."
    )
    final_instruction = (
        f"In the final response state that `{skill_name}` was loaded and both artifacts were verified."
        if skill_name is not None
        else "In the final response state that both artifacts were verified."
    )
    return f"""You are solving one SpreadsheetBench-v1 instruction with its official sibling-replay protocol.

{skill_loading}

Task id: {task['_task_id']}
Instruction type: {task.get('instruction_type', '')}
Instruction:
{task['instruction']}

The visible case-1 input is: {workspace / 'input.xlsx'}

Required artifacts and replay contract:
- Work only inside {workspace}. Never search for or inspect golden/reference/answer workbooks.
- Create exactly {workspace / 'solution.py'} containing the complete reusable solution.
- The identical script will be run on unseen sibling workbooks as:
    python solution.py INPUT.xlsx OUTPUT.xlsx
- The script must read argv[1], write argv[2], and must not depend on case-1 values, absolute
  paths, hidden files, network access, or files other than the supplied workbook.
- Preserve unrelated workbook content, formulas, styles, merged cells, dimensions, and structure.
- Then run the script on input.xlsx and save a valid {workspace / 'output.xlsx'}.
- Reopen output.xlsx and verify the requested result. Keep inspection output bounded.

Do the work now; do not stop at a plan. Finish only when both solution.py and output.xlsx exist.
{final_instruction}
"""


def install_prompts(harness: str, skill_name: str | None) -> None:
    if harness == "codex":
        codex_runner.prompt_for = lambda task, workspace, **_kwargs: generation_prompt(
            task, workspace, harness="codex", skill_name=skill_name
        )
    elif harness == "claude":
        claude_runner.claude_prompt = lambda task, workspace, **_kwargs: generation_prompt(
            task, workspace, harness="claude", skill_name=skill_name
        )
    else:
        dsh_runner.dsh_prompt = lambda task, workspace, requested_skill: generation_prompt(
            task, workspace, harness="dsh", skill_name=skill_name
        )


def start_proxy(args: argparse.Namespace, port: int) -> tuple[subprocess.Popen[str], Any]:
    audit = args.run_root / "proxy-requests.jsonl"
    log_handle = (args.run_root / "proxy.log").open("a", encoding="utf-8")
    if args.harness == "codex":
        command = [
            sys.executable,
            str(codex_runner.PROXY_SCRIPT),
            "--port", str(port),
            "--upstream", args.base_url,
            "--audit", str(audit),
            "--max-requests", str(args.max_turns),
            "--max-output-tokens", str(args.max_output_tokens),
        ]
    elif args.harness == "claude":
        command = [
            sys.executable,
            str(claude_runner.CLAUDE_PROXY_SCRIPT),
            "--port", str(port),
            "--upstream", args.base_url,
            "--api-key-file", str(args.api_key_file),
            "--audit", str(audit),
            "--max-requests", str(args.max_turns),
            "--max-output-tokens", str(args.max_output_tokens),
        ]
    else:
        command = [
            sys.executable,
            str(dsh_runner.PROXY_SCRIPT),
            "--port", str(port),
            "--upstream", args.base_url,
            "--api-key-file", str(args.api_key_file),
            "--audit", str(audit),
            "--max-requests", str(args.max_turns),
        ]
    proxy = subprocess.Popen(
        command, stdout=log_handle, stderr=subprocess.STDOUT, text=True
    )
    time.sleep(0.6)
    if proxy.poll() is not None:
        log_handle.close()
        raise RuntimeError(f"{args.harness} proxy failed to start")
    return proxy, log_handle


def run_harness_generation(
    task: SpreadsheetBenchV1Instruction,
    *,
    args: argparse.Namespace,
    port: int,
    runtime_root: Path,
    api_key: str,
) -> tuple[dict[str, Any], Path]:
    converted = task_dict(task)
    generation_root = args.run_root / "tasks" / task.task_id / "generation"
    generation_root.mkdir(parents=True, exist_ok=True)
    # The reused harness adapters count requests relative to their run root.
    # Point that expected path at this experiment's single proxy audit so the
    # recorded turns and resume ceiling remain correct without duplicating it.
    generation_audit = generation_root / "proxy-requests.jsonl"
    # ``Path.exists`` is false for a dangling symlink.  Before the first proxy
    # request the shared audit target may not exist yet, but the link itself
    # already does and must not be recreated on resume.
    if not os.path.lexists(generation_audit):
        generation_audit.symlink_to(
            os.path.relpath(args.run_root / "proxy-requests.jsonl", generation_root)
        )
    common = {
        "task": converted,
        "dataset": args.dataset,
        "run_root": generation_root,
        "skill": args.skill,
        "model": args.model,
        "max_turns": args.max_turns,
        "task_timeout": args.task_timeout,
        "recalculate_before_evaluation": False,
    }
    if args.harness == "codex":
        result = codex_runner.run_one(
            **common,
            runtime_root=runtime_root,
            proxy_port=port,
            api_key=api_key,
            max_output_tokens=args.max_output_tokens,
            evaluator=V2_EVALUATOR,
            defer_evaluation=True,
        )
    elif args.harness == "claude":
        result = claude_runner.run_one(
            **common,
            runtime_root=runtime_root,
            proxy_port=port,
            max_output_tokens=args.max_output_tokens,
            evaluator=V2_EVALUATOR,
            api_key_file=args.api_key_file,
            defer_evaluation=True,
        )
    else:
        result = dsh_runner.run_one(
            **common,
            dsh_bin=args.dsh_bin,
            proxy_port=port,
            defer_evaluation=True,
        )
    workspace = generation_root / "tasks" / f"spreadsheet__{task.task_id}"
    return result, workspace


def replay_solution(
    task: SpreadsheetBenchV1Instruction,
    script: Path,
    *,
    task_root: Path,
    replay_timeout: float,
) -> tuple[dict[int, Path], list[dict[str, Any]]]:
    outputs: dict[int, Path] = {}
    rows: list[dict[str, Any]] = []
    script_digest = sha256(script)
    for case in task.cases:
        if case.input_path is None or case.golden_path is None:
            rows.append({"case_index": case.index, "status": "dataset_missing"})
            continue
        case_root = task_root / f"case-{case.index}"
        case_root.mkdir(parents=True, exist_ok=True)
        input_path = case_root / "input.xlsx"
        output_path = case_root / "output.xlsx"
        frozen_script = case_root / "solution.py"
        shutil.copy2(case.input_path, input_path)
        shutil.copy2(script, frozen_script)
        output_path.unlink(missing_ok=True)
        stdout_path = case_root / "solution.stdout.log"
        stderr_path = case_root / "solution.stderr.log"
        private_home = case_root / ".home"
        private_tmp = case_root / ".tmp"
        private_home.mkdir(exist_ok=True)
        private_tmp.mkdir(exist_ok=True)
        started = time.monotonic()
        timed_out = False
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            try:
                completed = subprocess.run(
                    [sys.executable, str(frozen_script), str(input_path), str(output_path)],
                    cwd=case_root,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    timeout=replay_timeout,
                    check=False,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "LANG": os.environ.get("LANG", "C.UTF-8"),
                        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                        "HOME": str(private_home),
                        "TMPDIR": str(private_tmp),
                        "PYTHONPATH": "",
                    },
                )
                return_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                return_code = 124
        valid = codex_runner.workbook_is_valid(output_path)
        recalculated = False
        recalculation_error: str | None = None
        if valid:
            backup = case_root / "output.pre-recalc.xlsx"
            shutil.copy2(output_path, backup)
            recalculated, recalculation_error = codex_runner.recalculate_workbook(
                output_path, output_path, timeout=300.0
            )
            valid = codex_runner.workbook_is_valid(output_path)
        status = "scored" if valid else ("timeout" if timed_out else "replay_failed")
        row: dict[str, Any] = {
            "case_index": case.index,
            "status": status,
            "exit_code": return_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "input_sha256": sha256(input_path),
            "solution_sha256": sha256(frozen_script),
            "solution_identity_verified": sha256(frozen_script) == script_digest,
            "output_valid": valid,
            "recalculated": recalculated,
        }
        if recalculation_error and not recalculated:
            row["recalculation_error"] = recalculation_error
        if valid:
            row["output_sha256"] = sha256(output_path)
            outputs[case.index] = output_path
        rows.append(row)
        if not valid:
            break
    return outputs, rows


def run_one(
    task: SpreadsheetBenchV1Instruction,
    *,
    args: argparse.Namespace,
    port: int,
    runtime_root: Path,
    api_key: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    task_root = args.run_root / "tasks" / task.task_id
    status_path = task_root / "status.json"
    if status_path.is_file():
        try:
            previous = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        if previous.get("status") == "completed" and previous.get(
            "manifest_sha256"
        ) == manifest_sha256:
            return previous
    task_root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "protocol": V1_RUN_PROTOCOL,
        "task_id": task.task_id,
        "harness": args.harness,
        "model": args.model,
        "manifest_sha256": manifest_sha256,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        generation, workspace = run_harness_generation(
            task,
            args=args,
            port=port,
            runtime_root=runtime_root,
            api_key=api_key,
        )
        script = workspace / "solution.py"
        record["generation"] = generation
        record["generation_workspace"] = str(workspace)
        record["solution_exists"] = script.is_file()
        if not script.is_file():
            raise RuntimeError("real harness did not create solution.py")
        record["solution_sha256"] = sha256(script)
        if args.harness == "dsh" and args.skill is not None:
            loaded = generation.get("skill_loaded_in_session")
            expected = codex_runner.skill_names(args.skill)
            if isinstance(loaded, dict):
                missing = [name for name in expected if loaded.get(name) is not True]
            else:
                missing = expected if loaded is not True else []
            if missing:
                raise RuntimeError(
                    "DSH session did not record loading financial skills: "
                    + ", ".join(missing)
                )
        outputs, replay_rows = replay_solution(
            task,
            script,
            task_root=task_root,
            replay_timeout=args.replay_timeout,
        )
        score = score_v1_instruction(task, outputs)
        record.update(score)
        record["replays"] = replay_rows
        record["outcome_kind"] = "scored" if score["status"] == "completed" else "not_scored"
    except Exception as exc:
        record.update(
            {
                "status": "not_scored",
                "outcome_kind": "not_scored",
                "soft": None,
                "hard": None,
                "error_type": type(exc).__name__,
                "error": str(exc).replace(api_key, "[REDACTED]"),
            }
        )
    record["elapsed_seconds"] = round(time.time() - started, 3)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(status_path, record)
    return record


def make_manifest(args: argparse.Namespace, tasks: list[SpreadsheetBenchV1Instruction]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "protocol": V1_RUN_PROTOCOL,
        "dataset": {
            "repository": V1_SOURCE_REPOSITORY,
            "revision": V1_SOURCE_REVISION,
            "dataset_json_sha256": V1_DATASET_JSON_SHA256,
            "instruction_count": V1_INSTRUCTION_COUNT,
            "available_input_cases": V1_AVAILABLE_INPUT_CASE_COUNT,
            "official_denominator_cases_per_instruction": 3,
        },
        "evaluator": {
            "sha256": V1_OFFICIAL_EVALUATOR_SHA256,
            "semantics": "pinned clean-room exact value-only v1 port",
        },
        "harness": args.harness,
        "model": args.model,
        "provider": "internal-litellm",
        "base_url": args.base_url,
        "generation": {
            "reasoning_effort": "medium",
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking_enabled": True,
            "max_turns": args.max_turns,
            "max_output_tokens": args.max_output_tokens,
            "task_timeout_seconds": args.task_timeout,
            "replay_timeout_seconds": args.replay_timeout,
            "parallelism": args.parallelism,
        },
        "skill": (
            {
                "enabled": True,
                "name": ", ".join(codex_runner.skill_names(args.skill)),
                "path": str(args.skill),
                "sha256": codex_runner.skill_sha256(args.skill),
                "packages": codex_runner.skill_package_manifest(args.skill),
            }
            if args.skill is not None
            else {"enabled": False, "name": None, "path": None, "sha256": None}
        ),
        "task_ids": [task.task_id for task in tasks],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


def summarize(rows: list[dict[str, Any]], expected: int) -> dict[str, Any]:
    completed = [row for row in rows if row.get("status") == "completed"]
    study_complete = (
        len(rows) == expected
        and len({row.get("task_id") for row in rows}) == expected
        and len(completed) == expected
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "study_complete": study_complete,
        "expected_instructions": expected,
        "recorded_instructions": len(rows),
        "completed_instructions": len(completed),
        "not_scored_instructions": len(rows) - len(completed),
        "soft": fmean(float(row["soft"]) for row in completed) if study_complete else None,
        "hard": fmean(float(row["hard"]) for row in completed) if study_complete else None,
        "scored_soft_mean": (
            fmean(float(row["soft"]) for row in completed) if completed else None
        ),
        "scored_hard_mean": (
            fmean(float(row["hard"]) for row in completed) if completed else None
        ),
        "model_requests": sum(
            int((row.get("generation") or {}).get("model_requests", 0) or 0)
            for row in rows
        ),
        "elapsed_seconds_sum": sum(float(row.get("elapsed_seconds", 0) or 0) for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=("codex", "claude", "dsh"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--no-skill", action="store_true")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--parallelism", type=int, default=6)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--task-timeout", type=float, default=21600.0)
    parser.add_argument("--replay-timeout", type=float, default=1800.0)
    parser.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--dsh-bin", type=Path, default=dsh_runner.DEFAULT_DSH_BIN)
    args = parser.parse_args()
    for name in ("dataset", "api_key_file", "dsh_bin"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    args.skill = None if args.no_skill else args.skill.expanduser().resolve()
    args.run_root = args.run_root.expanduser().resolve()
    if args.parallelism < 1 or args.max_turns < 1 or args.task_timeout <= 0:
        parser.error("parallelism, max-turns, and timeouts must be positive")
    if args.skill is not None and not args.skill.exists():
        parser.error("--skill must point to an existing SKILL.md or skill directory")
    if args.skill is not None:
        try:
            codex_runner.skill_packages(args.skill)
        except ValueError as exc:
            parser.error(str(exc))
    if not args.api_key_file.is_file():
        parser.error("API key file is missing")
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        parser.error("API key file is empty")
    all_tasks = load_spreadsheetbench_v1(args.dataset)
    selected = set(args.task_id)
    tasks = [task for task in all_tasks if not selected or task.task_id in selected]
    missing = selected - {task.task_id for task in tasks}
    if missing:
        parser.error("unknown task ids: " + ", ".join(sorted(missing)))
    args.run_root.mkdir(parents=True, exist_ok=True)
    manifest = make_manifest(args, tasks)
    manifest_path = args.run_root / "manifest.json"
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous != manifest:
            if not migrate_legacy_skill_manifest(args.run_root, previous, manifest):
                parser.error("existing run manifest differs from requested configuration")
    else:
        atomic_json(manifest_path, manifest)
    install_prompts(
        args.harness,
        ", ".join(codex_runner.skill_names(args.skill)) if args.skill is not None else None,
    )
    port = free_port()
    proxy, proxy_log = start_proxy(args, port)
    runtime_parent = ROOT / "tmp"
    runtime_parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"spreadsheetbench-v1-{args.harness}-",
            dir=runtime_parent,
            ignore_cleanup_errors=True,
        ) as temporary:
            runtime_root = Path(temporary)
            existing: dict[str, dict[str, Any]] = {}
            for task in tasks:
                path = args.run_root / "tasks" / task.task_id / "status.json"
                if path.is_file():
                    try:
                        row = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if row.get("status") == "completed" and row.get(
                        "manifest_sha256"
                    ) == manifest["manifest_sha256"]:
                        existing[task.task_id] = row
            pending = [task for task in tasks if task.task_id not in existing]
            rows = list(existing.values())
            with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
                futures = {
                    pool.submit(
                        run_one,
                        task,
                        args=args,
                        port=port,
                        runtime_root=runtime_root,
                        api_key=api_key,
                        manifest_sha256=manifest["manifest_sha256"],
                    ): task.task_id
                    for task in pending
                }
                for future in as_completed(futures):
                    task_id = futures[future]
                    try:
                        row = future.result()
                    except Exception as exc:
                        row = {
                            "schema_version": SCHEMA_VERSION,
                            "task_id": task_id,
                            "status": "not_scored",
                            "soft": None,
                            "hard": None,
                            "manifest_sha256": manifest["manifest_sha256"],
                            "error_type": type(exc).__name__,
                            "error": str(exc).replace(api_key, "[REDACTED]"),
                        }
                    rows.append(row)
                    rows.sort(key=lambda item: str(item.get("task_id")))
                    atomic_json(args.run_root / "results.json", rows)
                    atomic_json(args.run_root / "summary.json", summarize(rows, len(tasks)))
                    print(
                        json.dumps(
                            {
                                "task_id": task_id,
                                "status": row.get("status"),
                                "soft": row.get("soft"),
                                "hard": row.get("hard"),
                                "finished": len(rows),
                                "expected": len(tasks),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            rows.sort(key=lambda item: str(item.get("task_id")))
            atomic_json(args.run_root / "results.json", rows)
            summary = summarize(rows, len(tasks))
            summary["manifest_sha256"] = manifest["manifest_sha256"]
            atomic_json(args.run_root / "summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=10)
        proxy_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
