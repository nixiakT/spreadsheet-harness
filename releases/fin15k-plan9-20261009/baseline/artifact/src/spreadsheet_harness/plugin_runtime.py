"""Execute additive plugin hooks without importing candidate code in the host.

The controller owns plugin declarations and hook ordering.  This adapter reads
only the declared implementation, executes it with the workbook interpreter's
filesystem/network isolation, and commits workbook writes only when the whole
hook chain succeeds and each writer has the appropriate contract permission.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .code_interpreter import LocalCodeInterpreter
from .errors import CodeIsolationError, HarnessError
from .plugins import CANDIDATE_PLUGIN_HOOKS, PluginExecutionPlan, PluginInstance
from .session import WorkbookSession

_PACKAGE_ROOT = Path(__file__).resolve().parent
_ENTRYPOINT = re.compile(r"^generated_plugins\.([a-z][a-z0-9_]*):on_hook$")
_MAX_SOURCE_BYTES = 256_000
_MAX_RESULT_BYTES = 16_000
_MAX_CONTEXT_ITEMS = 16
_MAX_CONTEXT_ITEM_CHARS = 4_000
_MAX_CHAIN_CONTEXT_CHARS = 16_000


def _digest(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _implementation_source(plugin: PluginInstance) -> tuple[str, str]:
    """Resolve a controller-derived entrypoint and read data, never import it."""

    match = _ENTRYPOINT.fullmatch(plugin.contract.runtime_entrypoint or "")
    if match is None:
        raise HarnessError("Candidate plugin has an invalid runtime entrypoint")
    module_root = _PACKAGE_ROOT / "generated_plugins"
    source = module_root / f"{match.group(1)}.py"
    if module_root.is_symlink() or source.is_symlink():
        raise HarnessError("Candidate plugin implementation may not contain a symlink")
    if not source.is_file() or source.stat().st_size > _MAX_SOURCE_BYTES:
        raise HarnessError(
            "Candidate plugin implementation is missing or exceeds its source budget"
        )
    try:
        content = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError("Candidate plugin implementation is not readable UTF-8") from exc
    return content, hashlib.sha256(content.encode("utf-8")).hexdigest()


def _restore_snapshot(snapshot: Path, workbook: Path) -> None:
    """Restore atomically, including when candidate code deleted or symlinked output."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=".plugin-rollback-", dir=workbook.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(snapshot, temporary)
        os.replace(temporary, workbook)
    finally:
        temporary.unlink(missing_ok=True)


def _hook_script(source_path: Path, context: dict[str, Any], marker: str) -> str:
    # Source is read/compiled only inside the sandbox.  The result channel is
    # bounded JSON; neither a Python object nor an import is returned to host.
    return (
        "import json as _plugin_json\n"
        "from pathlib import Path as _PluginPath\n"
        f"_plugin_context = _plugin_json.loads({json.dumps(context, ensure_ascii=False)!r})\n"
        "_plugin_globals = {'__name__': '__candidate_plugin__', "
        f"'__file__': {str(source_path)!r}" + "}\n"
        f"_plugin_source = _PluginPath({str(source_path)!r}).read_text(encoding='utf-8')\n"
        f"exec(compile(_plugin_source, {str(source_path)!r}, 'exec'), _plugin_globals)\n"
        "_plugin_entry = _plugin_globals.get('on_hook')\n"
        "if not callable(_plugin_entry):\n"
        "    raise RuntimeError('Candidate implementation has no callable on_hook')\n"
        "_plugin_result = _plugin_entry(_plugin_context)\n"
        "if not isinstance(_plugin_result, dict):\n"
        "    raise RuntimeError('Candidate on_hook must return a JSON object')\n"
        "_plugin_encoded = _plugin_json.dumps(_plugin_result, ensure_ascii=False, allow_nan=False)\n"
        f"if len(_plugin_encoded.encode('utf-8')) > {_MAX_RESULT_BYTES}:\n"
        "    raise RuntimeError('Candidate hook result exceeds its output budget')\n"
        f"print({marker!r} + _plugin_encoded)\n"
    )


def _parse_hook_result(execution: dict[str, Any], marker: str) -> dict[str, Any]:
    if execution.get("ok") is not True:
        raise HarnessError(
            "Candidate hook execution failed: "
            + str(execution.get("error") or execution.get("stderr") or "interpreter failure")[
                :1_000
            ]
        )
    if execution.get("truncated"):
        raise HarnessError("Candidate hook output was truncated")
    matches = [
        line[len(marker) :]
        for line in str(execution.get("stdout", "")).splitlines()
        if line.startswith(marker)
    ]
    if len(matches) != 1 or len(matches[0].encode("utf-8")) > _MAX_RESULT_BYTES:
        raise HarnessError("Candidate hook did not return exactly one bounded JSON result")
    try:
        result = json.loads(
            matches[0],
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Non-finite candidate result: {value}")
            ),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise HarnessError("Candidate hook returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise HarnessError("Candidate on_hook must return a JSON object")
    if result.get("ok") is False:
        raise HarnessError(
            "Candidate hook rejected the workbook: "
            + str(result.get("error") or result.get("message") or "ok=false")[:1_000]
        )
    if "context" in result:
        context = result["context"]
        if (
            not isinstance(context, list)
            or len(context) > _MAX_CONTEXT_ITEMS
            or any(
                not isinstance(item, str) or len(item) > _MAX_CONTEXT_ITEM_CHARS for item in context
            )
        ):
            raise HarnessError("Candidate context must be a bounded list of strings")
    return result


def execute_candidate_hooks(
    plan: PluginExecutionPlan,
    hook: str,
    session: WorkbookSession,
    instruction: str,
    task_category: str,
    timeout_seconds: float = 60,
    require_isolation: bool = True,
) -> list[dict[str, Any]]:
    """Execute the active hook chain transactionally, before evaluator access.

    A result may optionally return ``context: list[str]``.  Only ``before_task``
    context is consumed by the solver, as untrusted plugin observations, and
    text authority never replaces the controller-owned task instruction.

    No provider, scorer, reference-workbook, or raw session object is exposed
    to generated code.  Required isolation fails closed; it is never retried
    with a trusted/unsandboxed interpreter.  Explicit ``require_isolation=False``
    is provided solely for trusted local tests and development protocols.
    """

    if hook not in CANDIDATE_PLUGIN_HOOKS:
        raise HarnessError("Unsupported candidate implementation hook")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or (not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 60)
    ):
        raise HarnessError("Candidate hook timeout must be between 1 and 60 seconds")
    active = tuple(plugin for plugin in plan.candidate_hooks if hook in plugin.contract.hooks)
    if not active:
        return []
    workbook = session.workbook_path
    records: list[dict[str, Any]] = []
    # The session and original workbook remain outside the writable sandbox.
    with (
        session._write_lock,
        tempfile.TemporaryDirectory(prefix="spreadsheet-plugin-workspace-") as workspace,
    ):
        if _digest(workbook) is None:
            raise HarnessError("Candidate hooks require a regular workbook artifact")
        sandbox_root = Path(workspace)
        sandbox_workbook = sandbox_root / f"output{workbook.suffix}"
        shutil.copy2(workbook, sandbox_workbook)
        before_chain = _digest(workbook)
        deadline = time.monotonic() + timeout_seconds
        current: dict[str, Any] = {"hook": hook}
        context_chars = 0
        try:
            interpreter = LocalCodeInterpreter(
                sandbox_root,
                sandbox_workbook,
                default_timeout=math.ceil(timeout_seconds),
                max_output_chars=40_000,
                require_isolation=require_isolation,
            )
            for plugin in active:
                current = {
                    "plugin": plugin.contract.name,
                    "hook": hook,
                    "entrypoint": plugin.contract.runtime_entrypoint,
                }
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    raise HarnessError("Candidate hook chain exhausted its timeout budget")
                content, source_digest = _implementation_source(plugin)
                current["implementation_sha256"] = source_digest
                session.recorder.record("candidate.plugin.called", current)
                before_hook = _digest(sandbox_workbook)
                identifier = uuid.uuid4().hex
                source_copy = interpreter.code_dir / f".candidate_plugin_{identifier}.py"
                source_copy.write_text(content, encoding="utf-8")
                marker = f"__CANDIDATE_PLUGIN_RESULT_{identifier}__"
                context = {
                    "hook": hook,
                    "instruction": str(instruction),
                    "task_category": str(task_category),
                    "workbook_path": str(sandbox_workbook),
                    "config": dict(plugin.config),
                }
                try:
                    execution = interpreter.run(
                        _hook_script(source_copy, context, marker),
                        timeout_seconds=max(1, math.floor(remaining)),
                    )
                finally:
                    source_copy.unlink(missing_ok=True)
                result = _parse_hook_result(execution, marker)
                context_chars += sum(len(item) for item in result.get("context", []))
                if context_chars > _MAX_CHAIN_CONTEXT_CHARS:
                    raise HarnessError("Candidate hook context exceeds its chain character budget")
                after_hook = _digest(sandbox_workbook)
                if (
                    before_hook != after_hook
                    and "workbook.write" not in plugin.contract.permissions
                ):
                    raise HarnessError(
                        "Candidate hook changed the workbook without workbook.write permission"
                    )
                if after_hook is None:
                    raise HarnessError(
                        "Candidate hook removed or replaced the workbook with a non-regular file"
                    )
                session._validate(sandbox_workbook)
                record = {
                    **current,
                    "result": result,
                    "workbook_changed": before_hook != after_hook,
                    "workbook_sha256_before": before_hook,
                    "workbook_sha256_after": after_hook,
                }
                records.append(record)
                session.recorder.record("candidate.plugin.returned", record)
            # Only the workbook is a publishable artifact.  Logs, input,
            # provider configuration, scores and earlier runtime snippets were
            # never mounted, and no arbitrary files from the plugin sandbox
            # are copied back.  Atomic publication happens after the ENTIRE
            # hook chain has validated successfully.
            if _digest(workbook) != before_chain:
                raise HarnessError("Workbook changed concurrently during the candidate hook chain")
            if time.monotonic() > deadline:
                raise HarnessError("Candidate hook chain exhausted its timeout budget")
            if _digest(sandbox_workbook) != before_chain:
                _restore_snapshot(sandbox_workbook, workbook)
            return records
        except Exception as exc:
            rejected_digest = _digest(sandbox_workbook)
            rolled_back = rejected_digest != before_chain
            session.recorder.record(
                "candidate.plugin.failed",
                {
                    **current,
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_category": (
                        "code-isolation-infrastructure"
                        if isinstance(exc, CodeIsolationError)
                        else "candidate-plugin-runtime"
                    ),
                    "workbook_rolled_back": rolled_back,
                    "workbook_sha256_rejected": rejected_digest,
                    "workbook_sha256_after": _digest(workbook),
                },
            )
            raise


__all__ = ["execute_candidate_hooks"]
