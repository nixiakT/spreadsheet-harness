from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from spreadsheet_harness import code_interpreter
from spreadsheet_harness.code_interpreter import LocalCodeInterpreter
from spreadsheet_harness.errors import CodeIsolationError, ToolInputError
from spreadsheet_harness.session import WorkbookSession
from spreadsheet_harness.tools import SpreadsheetToolRegistry


def test_outer_sandbox_limits_defer_host_uid_process_limit(monkeypatch: Any) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        code_interpreter.resource,
        "setrlimit",
        lambda kind, value: calls.append((kind, value)),
    )
    monkeypatch.setattr(code_interpreter.platform, "system", lambda: "Linux")

    code_interpreter._outer_sandbox_limits()

    assert all(
        kind != code_interpreter.resource.RLIMIT_NPROC for kind, _ in calls
    )
    calls.clear()
    code_interpreter._limits()
    assert (
        code_interpreter.resource.RLIMIT_NPROC,
        (
            code_interpreter._MAX_SANDBOX_PROCESSES,
            code_interpreter._MAX_SANDBOX_PROCESSES,
        ),
    ) in calls


def test_strict_command_mounts_only_workspace_and_runtime_allowlist(tmp_path: Path) -> None:
    workspace = (tmp_path / "runs" / "task" / "bare").resolve()
    workspace.mkdir(parents=True)
    script = workspace / "probe.py"
    script.write_text("print('ok')", encoding="utf-8")

    command = code_interpreter._strict_command(
        workspace,
        [sys.executable, "-I", str(script)],
        bubblewrap=Path("/usr/bin/bwrap"),
    )

    triples = [command[index : index + 3] for index in range(len(command) - 2)]
    assert ["--ro-bind", "/", "/"] not in triples
    assert ["--bind", str(workspace), str(workspace)] in triples
    writable_binds = [
        triple for triple in triples if triple[0] == "--bind"
    ]
    assert writable_binds == [["--bind", str(workspace), str(workspace)]]
    assert str(tmp_path / "golden.xlsx") not in command
    assert "--unshare-net" in command
    assert "--unshare-pid" in command
    assert command.index("--tmpfs") < command.index("--bind")


def test_venv_mount_is_exact_and_does_not_expose_its_repository(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repository = tmp_path / "repository"
    venv = repository / ".venv"
    executable = venv / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    workspace = repository / "benchmarks" / "results" / "task" / "bare"
    workspace.mkdir(parents=True)
    script = workspace / "script.py"
    script.touch()
    monkeypatch.setattr(code_interpreter.sys, "prefix", str(venv))
    monkeypatch.setattr(code_interpreter.sys, "base_prefix", "/usr")
    monkeypatch.setattr(code_interpreter.sys, "executable", str(executable))

    command = code_interpreter._strict_command(
        workspace,
        [str(executable), "-I", str(script)],
        bubblewrap=Path("/usr/bin/bwrap"),
    )
    triples = [command[index : index + 3] for index in range(len(command) - 2)]

    assert ["--ro-bind", str(venv), str(venv)] in triples
    assert ["--ro-bind", str(repository), str(repository)] not in triples
    assert ["--bind", str(workspace), str(workspace)] in triples


def test_required_isolation_fails_when_bubblewrap_is_missing(monkeypatch: Any) -> None:
    code_interpreter._reset_isolation_probe_cache()
    monkeypatch.setattr(code_interpreter.platform, "system", lambda: "Linux")
    monkeypatch.setattr(code_interpreter.shutil, "which", lambda _: None)

    with pytest.raises(CodeIsolationError, match="bwrap"):
        code_interpreter.ensure_strict_code_isolation()


def test_required_isolation_fails_when_bubblewrap_probe_cannot_start(
    monkeypatch: Any,
) -> None:
    code_interpreter._reset_isolation_probe_cache()
    monkeypatch.setattr(code_interpreter.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        code_interpreter.shutil, "which", lambda _: "/usr/bin/bwrap"
    )

    def failed_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["bwrap"],
            1,
            stdout="",
            stderr="bwrap: Creating new namespace failed: Operation not permitted",
        )

    monkeypatch.setattr(code_interpreter.subprocess, "run", failed_run)

    with pytest.raises(CodeIsolationError, match="probe failed"):
        code_interpreter.ensure_strict_code_isolation()


def test_strict_execution_never_falls_back_when_launcher_did_not_start(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workbook = workspace / "book.xlsx"
    workbook.touch()
    monkeypatch.setattr(code_interpreter, "ensure_strict_code_isolation", lambda: {})
    monkeypatch.setattr(
        code_interpreter,
        "_strict_command",
        lambda *_args, **_kwargs: ["bwrap", "strict"],
    )
    interpreter = LocalCodeInterpreter(workspace, workbook, require_isolation=True)
    calls = 0

    def failed_execute(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            ["bwrap"],
            1,
            stdout="",
            stderr="bwrap: Permission denied",
        )

    monkeypatch.setattr(interpreter, "_execute", failed_execute)

    with pytest.raises(CodeIsolationError, match="refusing unsandboxed fallback"):
        interpreter.run("print('must not run')")
    assert calls == 1


def test_code_interpreter_preloads_openpyxl_helper_and_reports_workbook_change(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "helper-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
from openpyxl import load_workbook

wb = sheet_harness.load_workbook()
ws = wb["Sales"]
print(sheet_harness.workbook_overview(wb)[0]["name"])
print(sheet_harness.table_refs(ws))
print(sheet_harness.defined_name_refs(wb))
ws["E1"] = "helper wrote"
sheet_harness.save_workbook(wb)
wb.close()
"""
    )

    assert result["ok"] is True, result
    assert result["workbook_changed"] is True
    assert result["helper_module"] == "sheet_harness.py"
    assert "Sales" in result["stdout"]


def test_code_interpreter_rolls_back_new_invalid_formula_references(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "formula-gate-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
import sheet_harness

wb = sheet_harness.load_workbook()
ws = wb["Sales"]
ws["E2"] = "=SUMIFS(B:B,A:A,$55)"
sheet_harness.save_workbook(wb)
wb.close()
"""
    )

    assert result["ok"] is False
    assert result["workbook_changed"] is False
    assert result["workbook_rolled_back"] is True
    assert result["formula_validation"]["introduced_invalid_reference_count"] == 1
    assert result["formula_validation"]["examples"] == [
        {
            "sheet": "Sales",
            "cell": "E2",
            "invalid_reference": "$55",
            "formula": "=SUMIFS(B:B,A:A,$55)",
        }
    ]
    assert "E$5" in result["error"]
    workbook = load_workbook(session.workbook_path, data_only=False)
    try:
        assert workbook["Sales"]["E2"].value is None
    finally:
        workbook.close()


def test_code_interpreter_preserves_preexisting_invalid_formula_reference(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    workbook = load_workbook(sample_workbook)
    workbook["Sales"]["E2"] = "=SUMIFS(B:B,A:A,$55)"
    workbook.save(sample_workbook)
    workbook.close()
    session = WorkbookSession.create(sample_workbook, tmp_path / "formula-gate-existing-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
import sheet_harness

wb = sheet_harness.load_workbook()
wb["Sales"]["F2"] = 42
sheet_harness.save_workbook(wb)
wb.close()
"""
    )

    assert result["ok"] is True
    assert result["workbook_changed"] is True
    workbook = load_workbook(session.workbook_path, data_only=False)
    try:
        assert workbook["Sales"]["E2"].value == "=SUMIFS(B:B,A:A,$55)"
        assert workbook["Sales"]["F2"].value == 42
    finally:
        workbook.close()


def test_code_interpreter_helper_fills_formula(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "fill-helper-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
import sheet_harness

wb = sheet_harness.load_workbook()
ws = wb["Sales"]
ws["H6"] = "=SUM($E6:$G6)"
result = sheet_harness.fill_formula(ws, "H6", "H6:J7")
sheet_harness.save_workbook(wb)
wb.close()
print(result)
"""
    )

    assert result["ok"] is True, result
    assert result["workbook_changed"] is True
    assert "'cells_filled': 6" in result["stdout"]
    assert "'warnings': []" in result["stdout"]
    workbook = load_workbook(session.workbook_path, data_only=False)
    try:
        assert workbook["Sales"]["I6"].value == "=SUM($E6:$G6)"
        assert workbook["Sales"]["H7"].value == "=SUM($E7:$G7)"
    finally:
        workbook.close()


def test_code_interpreter_helper_warns_on_drifting_formula_fill(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "fill-helper-warning-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
import sheet_harness

wb = sheet_harness.load_workbook()
ws = wb["Sales"]
ws["H6"] = "=SUM($E6:G6)"
print(sheet_harness.fill_formula(ws, "H6", "H6:J7"))
wb.close()
"""
    )

    assert result["ok"] is True, result
    assert "'possible_expanding_or_drifting_range'" in result["stdout"]
    assert "'translated_range': '$E6:H6'" in result["stdout"]


def test_code_interpreter_helper_expands_endpoint_and_warns_on_relative_drift(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "fill-helper-endpoint-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
import sheet_harness

wb = sheet_harness.load_workbook()
ws = wb["Sales"]
ws["H6"] = "=SUM(E6:G6)"
print(sheet_harness.fill_formula(ws, "H6", "J6"))
sheet_harness.save_workbook(wb)
wb.close()
"""
    )

    assert result["ok"] is True, result
    assert result["workbook_changed"] is True
    assert "'range': 'H6:J6'" in result["stdout"]
    assert "'target_range_expanded_from_endpoint': True" in result["stdout"]
    assert "'translated_range': 'F6:H6'" in result["stdout"]


def test_code_interpreter_rejects_compressed_code_placeholder(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "placeholder-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    with pytest.raises(ToolInputError, match="complete runnable Python"):
        interpreter.run("print('start')\n# ...[compressed]\n")


def test_code_interpreter_openpyxl_compat_shim_supports_legacy_table_access(
    tmp_path: Path,
) -> None:
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table, TableStyleInfo

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workbook_path = workspace / "table.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["A", "B"])
    sheet.append([1, 2])
    table = Table(displayName="DataTable", ref="A1:B2")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9")
    sheet.add_table(table)
    workbook.save(workbook_path)
    workbook.close()

    interpreter = LocalCodeInterpreter(workspace, workbook_path, require_isolation=False)
    result = interpreter.run(
        """
from openpyxl import load_workbook
wb = load_workbook(sheet_harness.workbook_path())
ws = wb["Data"]
print([table.name for table in ws.tables])
print([table.ref for table in ws._tableparts])
print([(name, table.ref) for name, table in ws.tables.items()])
wb.close()
"""
    )

    assert result["ok"] is True, result
    assert "DataTable" in result["stdout"]
    assert "A1:B2" in result["stdout"]


def test_code_interpreter_helper_accepts_path_and_cell_dtype_alias(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "path-helper-run")
    interpreter = LocalCodeInterpreter(
        session.workspace,
        session.workbook_path,
        require_isolation=False,
    )

    result = interpreter.run(
        """
import os
from openpyxl import load_workbook

path = os.environ["SHEET_WORKBOOK"]
print(sheet_harness.workbook_overview(path)[0]["name"])
wb = load_workbook(path)
cell = wb["Sales"]["D2"]
print(cell.dtype)
wb.close()
"""
    )

    assert result["ok"] is True, result
    assert "Sales" in result["stdout"]
    assert "formula" in result["stdout"]


def test_tool_registry_propagates_required_isolation_failure(
    sample_workbook: Path,
    tmp_path: Path,
) -> None:
    session = WorkbookSession.create(sample_workbook, tmp_path / "tool-run")
    tools = SpreadsheetToolRegistry(session, allowed_tools={"code_interpreter"})

    class FailingInterpreter:
        def run(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise CodeIsolationError("sandbox disappeared")

    tools.interpreter = FailingInterpreter()  # type: ignore[assignment]

    with pytest.raises(CodeIsolationError, match="sandbox disappeared"):
        tools.invoke("code_interpreter", {"code": "print('x')"})


@pytest.mark.skipif(
    platform.system() != "Linux" or shutil.which("bwrap") is None,
    reason="strict Bubblewrap integration requires Linux and bwrap",
)
def test_strict_integration_imports_openpyxl_and_hides_sibling(tmp_path: Path) -> None:
    code_interpreter._reset_isolation_probe_cache()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workbook = workspace / "book.xlsx"
    outside = tmp_path / "golden.xlsx"
    outside.write_text("must remain hidden", encoding="utf-8")
    try:
        interpreter = LocalCodeInterpreter(
            workspace,
            workbook,
            require_isolation=True,
        )
    except CodeIsolationError as exc:
        pytest.skip(f"Bubblewrap installed but namespaces unavailable: {exc}")

    result = interpreter.run(
        f"""from pathlib import Path
import resource
from openpyxl import Workbook
assert resource.getrlimit(resource.RLIMIT_NPROC) == (64, 64)
try:
    Path({str(outside)!r}).read_text()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise RuntimeError("sibling was readable")
book = Workbook()
book.active["A1"] = "isolated"
book.save(Path({str(workbook)!r}))
book.close()
print("openpyxl-isolated-ok")
"""
    )

    assert result["ok"] is True, result
    assert result["stdout"].strip() == "openpyxl-isolated-ok"
    assert result["sandbox"].startswith(code_interpreter.STRICT_ISOLATION_POLICY)
    assert workbook.is_file()
