"""Bounded local Python execution with an optional strict filesystem boundary.

Ordinary local runs retain the trusted-code behavior. Comparison runs require a
Bubblewrap sandbox that exposes only the current run workspace plus allowlisted
Python/runtime files. Required isolation is fail-closed and never falls back to
an unsandboxed process.
"""

from __future__ import annotations

import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from .errors import CodeIsolationError, ToolInputError

STRICT_ISOLATION_POLICY = "bubblewrap-strict-workspace-v1"
_PROBE_SENTINEL = "SHEET_STRICT_ISOLATION_OK"
_MAX_SANDBOX_PROCESSES = 64
_PROBE_LOCK = threading.Lock()
_PROBE_SUCCESSES: set[tuple[str, ...]] = set()


def _limits(*, include_process_limit: bool = True) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
    if include_process_limit and hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(
            resource.RLIMIT_NPROC,
            (_MAX_SANDBOX_PROCESSES, _MAX_SANDBOX_PROCESSES),
        )
    if platform.system() == "Linux" and hasattr(resource, "RLIMIT_AS"):
        resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))


def _outer_sandbox_limits() -> None:
    """Apply limits that cannot prevent Bubblewrap from creating namespaces.

    RLIMIT_NPROC is charged against every process/thread owned by the host UID,
    not just this child. Applying a fixed value before Bubblewrap starts makes
    namespace creation fail on shared servers whose UID already owns more than
    that many threads. The strict launcher lowers the hard process limit after
    Bubblewrap has established the sandbox and before any model code runs.
    """

    _limits(include_process_limit=False)


def _require_bubblewrap() -> Path:
    if platform.system() != "Linux":
        raise CodeIsolationError(
            "Strict comparison code isolation requires Linux and Bubblewrap"
        )
    discovered = shutil.which("bwrap")
    if not discovered:
        raise CodeIsolationError(
            "Strict comparison code isolation requires the bwrap executable"
        )
    return Path(discovered).absolute()


def _runtime_roots(workspace: Path) -> list[Path]:
    """Return the minimum runtime directories needed by the active Python."""

    candidates = [Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")]
    candidates.extend([Path(sys.prefix), Path(sys.base_prefix)])
    roots: list[Path] = []
    resolved_workspace = workspace.resolve()
    for candidate in candidates:
        absolute = candidate.absolute()
        if not absolute.exists() or absolute in roots:
            continue
        resolved = absolute.resolve()
        if (
            resolved == Path("/")
            or resolved_workspace == resolved
            or resolved in resolved_workspace.parents
        ):
            raise CodeIsolationError(
                f"Refusing unsafe runtime mount that contains the task workspace: {absolute}"
            )
        # /usr already includes common /usr/local base prefixes. Keep /lib and
        # /lib64 mount points themselves because dynamic loaders use those paths.
        if absolute not in {Path("/bin"), Path("/lib"), Path("/lib64")} and any(
            resolved == root.resolve() or root.resolve() in resolved.parents
            for root in roots
        ):
            continue
        roots.append(absolute)
    executable = Path(sys.executable).absolute()
    if not any(executable == root or root in executable.parents for root in roots):
        raise CodeIsolationError(
            f"Active Python executable is outside the allowlisted runtime roots: {executable}"
        )
    return roots


def _parent_directories(paths: list[Path]) -> list[Path]:
    parents: set[Path] = set()
    for path in paths:
        current = path.absolute().parent
        while current != Path("/"):
            parents.add(current)
            current = current.parent
    return sorted(parents, key=lambda item: (len(item.parts), str(item)))


def _strict_command(
    workspace: Path,
    argv: list[str],
    *,
    bubblewrap: Path | None = None,
) -> list[str]:
    """Build an empty-root Bubblewrap command with explicit runtime mounts."""

    resolved_workspace = workspace.resolve()
    bwrap = bubblewrap or _require_bubblewrap()
    runtime_roots = _runtime_roots(resolved_workspace)
    runtime_files = [
        path
        for path in (
            Path("/etc/ld.so.cache"),
            Path("/etc/localtime"),
        )
        if path.is_file()
    ]
    runtime_parent_paths = _parent_directories([*runtime_roots, *runtime_files])
    workspace_parent_paths = _parent_directories([resolved_workspace])
    command = [
        str(bwrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--cap-drop",
        "ALL",
    ]
    for parent in runtime_parent_paths:
        command.extend(["--dir", str(parent)])
    for root in runtime_roots:
        command.extend(["--ro-bind", str(root), str(root)])
    for runtime_file in runtime_files:
        command.extend(["--ro-bind", str(runtime_file), str(runtime_file)])
    command.extend(
        [
            "--tmpfs",
            "/tmp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
    )
    # A task workspace is commonly below /tmp during the startup probe. Create
    # its mount-point hierarchy only after /tmp is replaced, then bind the
    # exact workspace. Binding an ancestor would expose sibling arms or goldens.
    for parent in workspace_parent_paths:
        if parent != Path("/tmp"):
            command.extend(["--dir", str(parent)])
    command.extend(
        [
            "--bind",
            str(resolved_workspace),
            str(resolved_workspace),
            "--chdir",
            str(resolved_workspace),
            "--",
            *argv,
        ]
    )
    return command


def _environment(workspace: Path, workbook: Path) -> dict[str, str]:
    executable_dir = str(Path(sys.executable).absolute().parent)
    return {
        "PATH": f"{executable_dir}:/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "SHEET_WORKSPACE": str(workspace),
        "SHEET_WORKBOOK": str(workbook),
    }


def _diagnostic(stderr: str) -> str:
    cleaned = " ".join(stderr.strip().split())[:1_000]
    for name in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned or "no diagnostic output"


def _run_strict_probe(bubblewrap: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="sheet-code-isolation-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        outside = root / "must-not-be-readable.txt"
        outside.write_text("isolation-probe-secret", encoding="utf-8")
        script = workspace / "probe.py"
        script.write_text(
            f"""from pathlib import Path
import os
import resource
from openpyxl import Workbook

if hasattr(resource, "RLIMIT_NPROC"):
    resource.setrlimit(
        resource.RLIMIT_NPROC,
        ({_MAX_SANDBOX_PROCESSES}, {_MAX_SANDBOX_PROCESSES}),
    )
    if resource.getrlimit(resource.RLIMIT_NPROC) != (
        {_MAX_SANDBOX_PROCESSES},
        {_MAX_SANDBOX_PROCESSES},
    ):
        raise RuntimeError("strict sandbox process limit was not applied")
outside = Path(os.environ["SHEET_PROBE_OUTSIDE"])
try:
    outside.read_bytes()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise RuntimeError("strict sandbox exposed a file outside the workspace")
book = Workbook()
book.active["A1"] = "probe"
book.save(os.environ["SHEET_WORKBOOK"])
book.close()
print("SHEET_STRICT_ISOLATION_OK")
""",
            encoding="utf-8",
        )
        workbook = workspace / "probe.xlsx"
        environment = _environment(workspace, workbook)
        environment["SHEET_PROBE_OUTSIDE"] = str(outside)
        command = _strict_command(
            workspace,
            [sys.executable, "-I", str(script)],
            bubblewrap=bubblewrap,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
                preexec_fn=_outer_sandbox_limits if os.name == "posix" else None,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodeIsolationError(
                f"Strict comparison sandbox probe could not start: {type(exc).__name__}: {exc}"
            ) from exc
        if (
            completed.returncode != 0
            or _PROBE_SENTINEL not in completed.stdout
            or not workbook.is_file()
        ):
            raise CodeIsolationError(
                "Strict comparison sandbox probe failed "
                f"(exit {completed.returncode}): {_diagnostic(completed.stderr)}"
            )


def ensure_strict_code_isolation() -> dict[str, str]:
    """Prove that strict isolation and the active venv's openpyxl work."""

    bubblewrap = _require_bubblewrap()
    key = (
        str(bubblewrap),
        sys.executable,
        sys.prefix,
        sys.base_prefix,
        STRICT_ISOLATION_POLICY,
    )
    with _PROBE_LOCK:
        if key not in _PROBE_SUCCESSES:
            _run_strict_probe(bubblewrap)
            _PROBE_SUCCESSES.add(key)
    return {
        "policy": STRICT_ISOLATION_POLICY,
        "bubblewrap": str(bubblewrap),
        "python": sys.executable,
    }


def _reset_isolation_probe_cache() -> None:
    """Clear successful probes for deterministic tests."""

    with _PROBE_LOCK:
        _PROBE_SUCCESSES.clear()


class LocalCodeInterpreter:
    def __init__(
        self,
        workspace: Path,
        workbook: Path,
        *,
        default_timeout: int = 30,
        max_output_chars: int = 20_000,
        require_isolation: bool = False,
    ) -> None:
        self.workspace = workspace.resolve()
        self.workbook = workbook.resolve()
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        self.require_isolation = require_isolation
        if self.require_isolation:
            if (
                self.workspace != self.workbook
                and self.workspace not in self.workbook.parents
            ):
                raise CodeIsolationError("SHEET_WORKBOOK must be inside the isolated workspace")
            ensure_strict_code_isolation()
        self.code_dir = self.workspace / "code"
        self.code_dir.mkdir(exist_ok=True)

    def _limits(self) -> None:
        _limits()

    def _command(
        self,
        script: Path,
        *,
        launcher: Path | None = None,
        marker: Path | None = None,
    ) -> tuple[list[str], str]:
        if self.require_isolation:
            if launcher is None or marker is None:
                raise CodeIsolationError("Strict sandbox launcher was not prepared")
            return (
                _strict_command(
                    self.workspace,
                    [
                        sys.executable,
                        "-I",
                        str(launcher),
                        str(script),
                        str(marker),
                    ],
                ),
                f"{STRICT_ISOLATION_POLICY}: writable workspace, runtime allowlist, no network",
            )

        base = [sys.executable, "-I", str(script)]
        bubblewrap = shutil.which("bwrap") if platform.system() == "Linux" else None
        if not bubblewrap:
            return base, "cwd+rlimit (trusted code only)"
        return (
            [
                bubblewrap,
                "--die-with-parent",
                "--new-session",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--bind",
                str(self.workspace),
                str(self.workspace),
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                str(self.workspace),
                *base,
            ],
            "bubblewrap: read-only host, writable workspace, network disabled",
        )

    def _execute(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        preexec_fn = _outer_sandbox_limits if self.require_isolation else self._limits
        return subprocess.run(
            command,
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            preexec_fn=preexec_fn if os.name == "posix" else None,
        )

    def run(self, code: str, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        if not code.strip():
            raise ToolInputError("code must not be empty")
        if len(code) > 30_000:
            raise ToolInputError("code exceeds the 30,000 character limit")
        timeout = timeout_seconds or self.default_timeout
        if timeout < 1 or timeout > 60:
            raise ToolInputError("timeout_seconds must be between 1 and 60")
        identifier = uuid.uuid4().hex
        script = self.code_dir / f"snippet_{identifier}.py"
        script.write_text(code, encoding="utf-8")
        launcher: Path | None = None
        marker: Path | None = None
        if self.require_isolation:
            launcher = self.code_dir / f".launcher_{identifier}.py"
            marker = self.code_dir / f".sandbox_started_{identifier}"
            launcher.write_text(
                f"""import resource
import runpy
import sys

script_path, marker_path = sys.argv[1:3]
if hasattr(resource, "RLIMIT_NPROC"):
    resource.setrlimit(
        resource.RLIMIT_NPROC,
        ({_MAX_SANDBOX_PROCESSES}, {_MAX_SANDBOX_PROCESSES}),
    )
with open(marker_path, "xb"):
    pass
sys.argv = [script_path]
runpy.run_path(script_path, run_name="__main__")
""",
                encoding="utf-8",
            )
        command, sandbox = self._command(script, launcher=launcher, marker=marker)
        environment = _environment(self.workspace, self.workbook)
        try:
            completed = self._execute(command, environment=environment, timeout=timeout)
            if self.require_isolation and (marker is None or not marker.is_file()):
                raise CodeIsolationError(
                    "Strict comparison sandbox did not start; refusing unsandboxed fallback: "
                    + _diagnostic(completed.stderr)
                )
            bubblewrap_error: str | None = None
            namespace_failure = (
                not self.require_isolation
                and sandbox.startswith("bubblewrap:")
                and completed.returncode != 0
                and any(
                    marker_text in completed.stderr.lower()
                    for marker_text in (
                        "creating new namespace failed",
                        "operation not permitted",
                        "permission denied",
                    )
                )
            )
            if namespace_failure:
                bubblewrap_error = completed.stderr.strip()[:1000]
                completed = self._execute(
                    [sys.executable, "-I", str(script)],
                    environment=environment,
                    timeout=timeout,
                )
                sandbox = "cwd+rlimit fallback (bubblewrap unavailable; trusted code only)"
            stdout = completed.stdout
            stderr = completed.stderr
            truncated = len(stdout) > self.max_output_chars or len(stderr) > self.max_output_chars
            return {
                "ok": completed.returncode == 0,
                "exit_code": completed.returncode,
                "stdout": stdout[: self.max_output_chars],
                "stderr": stderr[: self.max_output_chars],
                "truncated": truncated,
                "sandbox": sandbox,
                "bubblewrap_error": bubblewrap_error,
                "script": str(script.relative_to(self.workspace)),
            }
        except subprocess.TimeoutExpired as exc:
            if self.require_isolation and (marker is None or not marker.is_file()):
                raise CodeIsolationError(
                    "Strict comparison sandbox timed out before its launcher started"
                ) from exc
            return {
                "ok": False,
                "error": f"Code execution timed out after {timeout} seconds",
                "stdout": (exc.stdout or "")[: self.max_output_chars]
                if isinstance(exc.stdout, str)
                else "",
                "stderr": (exc.stderr or "")[: self.max_output_chars]
                if isinstance(exc.stderr, str)
                else "",
                "sandbox": sandbox,
                "script": str(script.relative_to(self.workspace)),
            }
        except OSError as exc:
            if self.require_isolation:
                raise CodeIsolationError(
                    f"Strict comparison sandbox could not start: {type(exc).__name__}: {exc}"
                ) from exc
            raise
        finally:
            if launcher is not None:
                launcher.unlink(missing_ok=True)
            if marker is not None:
                marker.unlink(missing_ok=True)
