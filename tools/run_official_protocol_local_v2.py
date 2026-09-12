#!/usr/bin/env python3
"""Run SpreadsheetBench-v2 with the official tool/prompt protocol locally.

This deliberately does not use the official SWE-agent Docker deployment.  It
uses the official prompt and tool contracts, while executing bash in the
repository's bwrap workspace sandbox.  Results are labelled as a local
deployment protocol-compatible run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_ROOT = Path("/tmp/SpreadsheetBench-2-full")
SWE_ROOT = OFFICIAL_ROOT / "SWE-agent"
VIEW_XLSX = SWE_ROOT / "tools/view_xlsx/bin/view_xlsx"
SUBMIT = SWE_ROOT / "tools/submit/bin/submit"
CONFIGS = {
    "Visualization": SWE_ROOT / "config/visualisation.yaml",
    "default": SWE_ROOT / "config/spreadsheet.yaml",
}
ARMS = ("bash", "view_xlsx", "submit")
RESULT_LOCK = threading.Lock()
REQUEST_LOCK = threading.Lock()
NEXT_REQUEST_AT = 0.0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_prompt(category: str) -> tuple[str, str]:
    cfg = yaml.safe_load(CONFIGS.get(category, CONFIGS["default"]).read_text())
    templates = cfg["agent"]["templates"]
    return str(templates["system_template"]), str(templates["instance_template"])


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run one shell command in the spreadsheet workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_xlsx",
            "description": "View an xlsx workbook. Use mode list or content and optional sheet/row range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "mode": {"type": "string", "enum": ["list", "content"]},
                    "sheet": {"type": "string"},
                    "start_row": {"type": "integer"},
                    "end_row": {"type": "integer"},
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": "Submit the current workbook after verification.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _workspace_path(workspace: Path, value: str) -> Path:
    p = Path(value)
    if p.is_absolute() and str(p).startswith("/workspace/"):
        return workspace / str(p.relative_to("/workspace"))
    if p.is_absolute():
        return p
    return workspace / p


def run_bash(workspace: Path, command: str, timeout: int = 60) -> dict[str, Any]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bwrap is required for official-protocol/local-deployment")
    cmd = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-net", "--unshare-pid",
        "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc", "/etc", "--ro-bind", str(ROOT / ".venv"), "/opt/venv",
        "--bind", str(workspace), "/workspace", "--tmpfs", "/tmp",
        "--proc", "/proc", "--dev", "/dev", "--chdir", "/workspace",
        "--setenv", "PATH", "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
        "--", "bash", "-lc", command,
    ]
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    return {"returncode": p.returncode, "stdout": p.stdout[-20000:], "stderr": p.stderr[-10000:]}


def run_view_xlsx(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(workspace, str(args["file_path"]))
    command = [str(ROOT / ".venv/bin/python"), str(VIEW_XLSX), str(path)]
    for key in ("mode", "sheet", "start_row", "end_row"):
        if args.get(key) is not None:
            command.append(str(args[key]))
    p = subprocess.run(command, text=True, capture_output=True, timeout=60, cwd=workspace)
    return {"returncode": p.returncode, "stdout": p.stdout[-24000:], "stderr": p.stderr[-4000:]}


def completion(client: OpenAI, *, model: str, messages: list[dict[str, Any]]) -> Any:
    """Respect the relay's one-request-per-second model-group limit."""
    global NEXT_REQUEST_AT
    for attempt in range(6):
        with REQUEST_LOCK:
            wait = NEXT_REQUEST_AT - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            NEXT_REQUEST_AT = time.monotonic() + 1.1
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0,
                top_p=1,
                max_tokens=8192,
                timeout=700,
                extra_body={"chat_template_kwargs": {"enable_thinking": True}},
            )
        except Exception as exc:
            text = str(exc).lower()
            if not any(
                token in text
                for token in (
                    "429",
                    "rate limit",
                    "503",
                    "overloaded",
                    "connection error",
                    "connect timeout",
                    "read timeout",
                    "temporarily unavailable",
                )
            ) or attempt == 5:
                raise
            time.sleep(min(60.0, 2.0 ** attempt * 2.0))
    raise RuntimeError("unreachable completion retry state")


def task_run(task: dict[str, Any], dataset_root: Path, output_root: Path, client_args: dict[str, Any]) -> dict[str, Any]:
    category = str(task["_category"])
    task_id = str(task["id"])
    task_key = f"{category}/{task_id}"
    task_dir = output_root / "work" / category / task_id
    final_path = output_root / "outputs" / category / f"{task_id}_output.xlsx"
    status_path = output_root / "tasks" / category / f"{task_id}.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    if status_path.exists():
        try:
            old = json.loads(status_path.read_text())
            if old.get("status") == "completed" and final_path.is_file():
                return old
        except Exception:
            pass
    task_dir.mkdir(parents=True, exist_ok=True)
    input_path = (dataset_root / category / str(task["spreadsheet_path"])).resolve(strict=True)
    shutil.copy2(input_path, task_dir / "input.xlsx")
    (task_dir / "output.xlsx").unlink(missing_ok=True)
    system_template, instance_template = load_prompt(category)
    instance = instance_template.replace("{{instruction}}", str(task["instruction"]))
    instance = instance.replace("{{spreadsheet_path}}", "/workspace/input.xlsx")
    instance = instance.replace("{{output_path}}", "/workspace/output.xlsx")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_template},
        {"role": "user", "content": instance},
    ]
    submitted = False
    calls = 0
    started = time.time()
    error: str | None = None
    try:
        while calls < 50 and not submitted:
            response = completion(
                client_args["client"], model=client_args["model"], messages=messages
            )
            calls += 1
            message = response.choices[0].message
            assistant = message.model_dump(exclude_none=True)
            messages.append(assistant)
            tool_calls = message.tool_calls or []
            if not tool_calls:
                messages.append({"role": "user", "content": "Continue using exactly one tool; call submit when finished."})
                continue
            if len(tool_calls) > 1:
                tool_calls = tool_calls[:1]
            call = tool_calls[0]
            name = call.function.name
            raw_arguments = call.function.arguments or "{}"
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else dict(raw_arguments)
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                # SWE-agent keeps the trajectory alive after malformed function
                # arguments; return the parser error as the tool observation.
                result = {
                    "error": "invalid JSON tool arguments; retry this tool call",
                    "exception": str(exc),
                    "raw_arguments_tail": str(raw_arguments)[-2000:],
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                continue
            if name == "bash":
                result = run_bash(task_dir, str(arguments.get("command", "")))
            elif name == "view_xlsx":
                result = run_view_xlsx(task_dir, arguments)
            elif name == "submit":
                submitted = True
                result = {"submitted": True, "output_exists": (task_dir / "output.xlsx").is_file()}
            else:
                result = {"error": f"unknown official tool: {name}"}
            messages.append({"role": "tool", "tool_call_id": call.id, "name": name, "content": json.dumps(result, ensure_ascii=False)})
        if not submitted:
            raise RuntimeError("50-call limit reached without submit")
        source = task_dir / "output.xlsx"
        if not source.is_file():
            raise RuntimeError("submit called but /workspace/output.xlsx does not exist")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, final_path)
        row = {"task_id": task_key, "category": category, "status": "completed", "calls": calls, "output": str(final_path), "protocol": "official-protocol/local-deployment", "tools": list(ARMS), "elapsed_seconds": round(time.time() - started, 3)}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        row = {"task_id": task_key, "category": category, "status": "error", "calls": calls, "error": error, "protocol": "official-protocol/local-deployment", "tools": list(ARMS), "elapsed_seconds": round(time.time() - started, 3)}
    status_path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=ROOT / "benchmarks/data/spreadsheetbench-v2")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--model", default="DeepSeek-V4-Flash")
    ap.add_argument("--base-url", default="http://47.96.153.159:8010/v1")
    ap.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="Limit tasks for a canary; 0 means all")
    args = ap.parse_args()
    if not VIEW_XLSX.is_file() or not SUBMIT.is_file():
        raise SystemExit("official SWE-agent tool bundle is missing")
    key = args.api_key_file.read_text().strip()
    client = OpenAI(base_url=args.base_url, api_key=key, max_retries=0)
    args.output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for category in ("Debugging", "Financial_Model", "Template", "Visualization"):
        payload = json.loads((args.dataset / category / "dataset.json").read_text())
        for task in payload:
            task["_category"] = category
            rows.append(task)
    if args.limit > 0:
        rows = rows[: args.limit]
    manifest = {
        "protocol": "official-protocol/local-deployment",
        "model": args.model,
        "max_model_calls": 50,
        "temperature": 0,
        "top_p": 1,
        "enable_thinking": True,
        "thinking_wire_parameter": "chat_template_kwargs.enable_thinking",
        "deployment": "bwrap-local",
        "official_prompt_sha256": sha256(CONFIGS["default"]),
        "official_view_xlsx_sha256": sha256(VIEW_XLSX),
        "official_submit_sha256": sha256(SUBMIT),
        "tasks": len(rows),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(task_run, task, args.dataset, args.output, {"client": client, "model": args.model}) for task in rows]
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            print(row["task_id"], row["status"], row.get("calls"), flush=True)
    statuses = []
    for path in (args.output / "tasks").glob("**/*.json"):
        statuses.append(json.loads(path.read_text()))
    (args.output / "results.json").write_text(json.dumps(statuses, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"completed": sum(x.get("status") == "completed" for x in statuses), "errors": sum(x.get("status") == "error" for x in statuses), "total": len(statuses)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
