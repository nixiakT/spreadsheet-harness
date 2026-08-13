"""Command-line entry point for local runs and SpreadsheetBench."""

from __future__ import annotations

import argparse
import csv
import ctypes
import errno
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from .agent import SpreadsheetAgent, _provider_client
from .audit import audit_comparison
from .benchmark import (
    VerifiedBenchmarkRunner,
    _atomic_write_json,
    download_verified,
    load_and_verify_trace2skill_split_manifest,
    load_verified_tasks,
    require_evaluation_task_authorization,
    summarize_results,
    trace2skill_heldout_manifest,
    trace2skill_split_provenance,
)
from .comparison import (
    AVAILABLE_COMPARISON_ARMS,
    COMPARISON_ARMS,
    RUN_SPEC_ANCHORS,
    RUN_SPEC_COPY_FILENAME,
    ComparisonBenchmarkRunner,
    comparison_execution_contract,
    load_pilot_run_spec,
    protected_run_spec_split_ids,
    require_launchable_run_spec,
    verify_pilot_run_spec_contract,
)
from .config import API_PROTOCOLS, REASONING_ALIASES, REASONING_EFFORTS, ProviderConfig
from .errors import HarnessError
from .evolution import generate_candidate, promote_candidate
from .preprocess import preprocess_workbook
from .provider_compat import check_tool_compatibility
from .render import (
    convert_spreadsheet_copy,
    find_libreoffice,
    libreoffice_version,
    render_workbook,
)
from .session import SUPPORTED_EDIT_FORMATS, WorkbookSession
from .skills import SkillRegistry
from .tools import SpreadsheetToolRegistry


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def _json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _run_version(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (completed.stdout or completed.stderr).strip() or None


def _provider(args: argparse.Namespace) -> ProviderConfig:
    return ProviderConfig.discover(
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
        api_key_file=getattr(args, "api_key_file", None),
        model=getattr(args, "model", None),
        api_protocol=getattr(args, "api_protocol", None),
        reasoning_effort=getattr(args, "reasoning_effort", None),
        timeout_seconds=getattr(args, "request_timeout", None),
        max_retries=getattr(args, "request_retries", None),
        request_interval_seconds=getattr(args, "request_interval_seconds", None),
        temperature=getattr(args, "temperature", None),
        top_p=getattr(args, "top_p", None),
        seed=getattr(args, "seed", None),
        presence_penalty=getattr(args, "presence_penalty", None),
        top_k=getattr(args, "top_k", None),
        min_p=getattr(args, "min_p", None),
        repetition_penalty=getattr(args, "repetition_penalty", None),
        enable_thinking=getattr(args, "enable_thinking", None),
        litellm_timeout_seconds=getattr(args, "litellm_timeout", None),
    )


def _default_skill_root() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def _skills(args: argparse.Namespace) -> SkillRegistry:
    roots = [_default_skill_root()]
    roots.extend(Path(item).expanduser().resolve() for item in getattr(args, "skills", []) or [])
    return SkillRegistry(roots)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _lexical_absolute(path: str | Path) -> Path:
    """Make a path absolute without following symlinks."""

    return Path(os.path.abspath(Path(path).expanduser()))


def _reject_repository_path_symlinks(
    repository_root: Path, target: Path, *, label: str
) -> None:
    try:
        relative = target.relative_to(repository_root)
    except ValueError as exc:
        raise HarnessError(f"Pilot {label} path escapes the repository") from exc

    current = repository_root
    missing_component = False
    for component in relative.parts:
        current /= component
        if missing_component:
            continue
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing_component = True
            continue
        except OSError as exc:
            raise HarnessError(f"Unable to inspect pilot {label} path: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HarnessError(f"Pilot {label} path must not contain symlinks: {current}")


def _repository_relative_pilot_path(
    repository_root: Path, value: Any, *, label: str
) -> Path:
    if not isinstance(value, str) or not value:
        raise HarnessError(f"Pilot run spec {label} path is invalid")
    relative = Path(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HarnessError(f"Pilot run spec {label} path must be repository-relative")
    target = _lexical_absolute(repository_root / relative)
    if target == repository_root or repository_root not in target.parents:
        raise HarnessError(f"Pilot run spec {label} path escapes the repository")
    _reject_repository_path_symlinks(repository_root, target, label=label)
    return target


def _require_exact_pilot_cli_path(
    supplied: str | Path, expected: Path, *, repository_root: Path, label: str
) -> None:
    actual = _lexical_absolute(supplied)
    if actual != expected:
        raise HarnessError(f"Pilot {label} path differs from the run spec")
    _reject_repository_path_symlinks(repository_root, actual, label=label)


def _pilot_repository_paths(
    args: argparse.Namespace,
    document: dict[str, Any],
    *,
    output_argument: str | Path,
) -> tuple[Path, Path, Path]:
    repository_root = _repository_root()
    repository_paths = document.get("repository_relative_paths")
    required = {"dataset", "split_manifest", "output"}
    if not isinstance(repository_paths, dict) or set(repository_paths) != required:
        raise HarnessError("Pilot run spec repository paths are invalid")

    dataset = _repository_relative_pilot_path(
        repository_root, repository_paths["dataset"], label="dataset"
    )
    split = _repository_relative_pilot_path(
        repository_root, repository_paths["split_manifest"], label="split manifest"
    )
    output = _repository_relative_pilot_path(
        repository_root, repository_paths["output"], label="output"
    )
    _require_exact_pilot_cli_path(
        args.dataset, dataset, repository_root=repository_root, label="dataset"
    )
    _require_exact_pilot_cli_path(
        args.split_manifest,
        split,
        repository_root=repository_root,
        label="split manifest",
    )
    _require_exact_pilot_cli_path(
        output_argument, output, repository_root=repository_root, label="output"
    )
    return dataset, split, output


def _load_pilot_run_spec_from_repository(
    path: str | Path,
) -> tuple[dict[str, Any], dict[str, str], bytes]:
    repository_root = _repository_root()
    actual = _lexical_absolute(path)
    expected_paths = {
        _repository_relative_pilot_path(
            repository_root,
            f"benchmarks/protocols/{anchor.filename}",
            label="run spec",
        ): anchor
        for anchor in RUN_SPEC_ANCHORS
    }
    if actual not in expected_paths:
        raise HarnessError("Run spec path is not a registered repository protocol")
    _require_exact_pilot_cli_path(
        actual, actual, repository_root=repository_root, label="run spec"
    )
    return load_pilot_run_spec(actual)


def _write_private_bytes(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically claim an absent destination without replacing another run."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise HarnessError("Atomic no-replace directory rename is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    if (
        renameat2(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(destination),
            rename_noreplace,
        )
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise HarnessError("Fresh pilot output path must not already exist")
    raise OSError(error_number, os.strerror(error_number), destination)


def _claim_directory_with_mkdir(source: Path, destination: Path) -> None:
    """Fallback for filesystems that reject renameat2(RENAME_NOREPLACE)."""

    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise HarnessError("Fresh pilot output path must not already exist") from exc
    try:
        children = list(source.iterdir())
        if any(child.is_symlink() or not child.is_file() for child in children):
            raise HarnessError("Fresh pilot claim contains an unexpected entry")
        for child in children:
            child.rename(destination / child.name)
        _fsync_directory(destination)
        source.rmdir()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _claim_fresh_pilot_output(output: Path, run_spec_bytes: bytes) -> None:
    """Publish a complete copy-only pilot directory in one no-replace rename."""

    parent = output.parent
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.claim-", dir=parent)
    )
    claimed = False
    try:
        _write_private_bytes(temporary / RUN_SPEC_COPY_FILENAME, run_spec_bytes)
        _fsync_directory(temporary)
        try:
            _rename_directory_noreplace(temporary, output)
        except OSError as exc:
            unsupported = {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
                getattr(errno, "ENOTSUP", errno.EINVAL),
            }
            if exc.errno not in unsupported:
                raise
            _claim_directory_with_mkdir(temporary, output)
        claimed = True
        _fsync_directory(parent)
    except Exception:
        cleanup = output if claimed else temporary
        shutil.rmtree(cleanup, ignore_errors=True)
        try:
            _fsync_directory(parent)
        except OSError:
            pass
        raise


@contextmanager
def _editable_source(source: Path) -> Iterator[tuple[Path, bool]]:
    """Yield an editable OOXML path and whether normalization occurred."""

    source = source.expanduser().resolve(strict=True)
    if source.suffix.lower() in SUPPORTED_EDIT_FORMATS:
        yield source, False
        return
    with tempfile.TemporaryDirectory(prefix="sheet-harness-normalize-") as raw:
        temporary = Path(raw)
        if source.suffix.lower() == ".csv":
            target = temporary / f"{source.stem}.xlsx"
            try:
                text = source.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = source.read_text(encoding="latin-1")
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = source.stem[:31] or "CSV"
            for row in csv.reader(text.splitlines(), dialect):
                worksheet.append(row)
            workbook.save(target)
            workbook.close()
        else:
            target = convert_spreadsheet_copy(source, temporary, target_format="xlsx")
        yield target, True


def cmd_doctor(args: argparse.Namespace) -> int:
    tools_requested = bool(getattr(args, "tools", False))
    if tools_requested and not args.online:
        raise HarnessError("doctor --tools requires --online because it sends two model requests")
    libreoffice = find_libreoffice()
    report: dict[str, Any] = {
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "libreoffice": {
            "found": bool(libreoffice),
            "path": libreoffice,
            "version": libreoffice_version(libreoffice) if libreoffice else None,
        },
        "bubblewrap": shutil.which("bwrap"),
        "provider": None,
        "online": None,
        "tools": None,
    }
    ok = bool(libreoffice)
    try:
        config = _provider(args)
        report["provider"] = config.public_dict()
        if args.online:
            if tools_requested:
                report["tools"] = check_tool_compatibility(config)
                report["online"] = {"ok": report["tools"]["ok"], "covered_by": "tools"}
                ok = ok and bool(report["tools"]["ok"])
            else:
                payload = {
                    "model": config.model,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Reply exactly HARNESS_OK."}
                            ],
                        }
                    ],
                    "max_output_tokens": 32,
                }
                with _provider_client(config) as client:
                    turn = client.create(payload)
                report["online"] = {"ok": "HARNESS_OK" in turn.text, "text": turn.text[:200]}
                ok = ok and bool(report["online"]["ok"])
    except Exception as exc:
        report["provider"] = {"ok": False, "error": str(exc)}
        ok = False
    report["ok"] = ok
    _json_print(report)
    return 0 if ok else 1


def cmd_preprocess(args: argparse.Namespace) -> int:
    result = preprocess_workbook(
        args.source,
        args.output,
        max_cells_per_sheet=args.max_cells,
        timeout_seconds=args.timeout,
    )
    _json_print(result.to_dict())
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    result = render_workbook(
        args.source,
        args.output,
        dpi=args.dpi,
        per_sheet=not args.whole_workbook,
        timeout_seconds=args.timeout,
    )
    _json_print(result.to_dict())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    instruction = args.instruction
    if args.instruction_file:
        instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    if not instruction:
        raise HarnessError("Provide --instruction or --instruction-file")
    source = Path(args.source)
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir = runs_dir / _now_id()
    config = _provider(args)

    with _editable_source(source) as (editable, normalized):
        session = WorkbookSession.create(
            editable,
            run_dir,
            recorder_secrets=(config.api_key,),
        )
        if normalized:
            shutil.copy2(
                source.resolve(), session.paths.input.parent / ("original" + source.suffix)
            )
        tools = SpreadsheetToolRegistry(
            session,
            enable_code=not args.no_code,
            redaction_secrets=(config.api_key,),
        )
        agent = SpreadsheetAgent(
            config,
            tools,
            skills=_skills(args),
            max_turns=args.max_turns,
            max_output_tokens=args.max_output_tokens,
        )

        def stream_text(delta: str) -> None:
            if not args.quiet:
                print(delta, end="", flush=True)

        result = agent.run(instruction, on_text=stream_text)
        if not args.quiet:
            print()
        manifest = {
            "schema_version": 1,
            "run_id": session.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": str(source.expanduser().resolve()),
            "normalized": normalized,
            "provider": config.public_dict(),
            "calculation_backend": {
                "name": "LibreOffice",
                "version": libreoffice_version(find_libreoffice() or "libreoffice"),
            },
            "agent": result.to_dict(),
            "output_workbook": str(session.workbook_path),
        }
        session.write_manifest(manifest)
    _json_print(
        {
            "ok": True,
            "run_dir": str(run_dir),
            "output_workbook": str(session.workbook_path),
            "agent": result.to_dict(),
        }
    )
    return 0


def cmd_benchmark_download(args: argparse.Namespace) -> int:
    root = download_verified(args.output)
    tasks = load_verified_tasks(root)
    _json_print({"root": str(root), "tasks": len(tasks), "excluded_by_default": 2})
    return 0


def cmd_benchmark_split(args: argparse.Namespace) -> int:
    root = (
        Path(args.dataset).expanduser().resolve() if args.dataset else download_verified(args.cache)
    )
    if args.write:
        manifest = trace2skill_heldout_manifest(root)
        destination = Path(args.write).expanduser().resolve()
        if destination.exists():
            raise HarnessError(f"Refusing to overwrite frozen split manifest: {destination}")
        _atomic_write_json(destination, manifest)
        _json_print(
            {
                "written": str(destination),
                "usable_tasks": manifest["selection"]["usable_tasks"],
                "task_ids_sha256": manifest["task_ids_sha256"],
                "dataset_json_sha256": manifest["dataset"]["dataset_json_sha256"],
            }
        )
        return 0
    report = load_and_verify_trace2skill_split_manifest(root, args.verify)
    _json_print(report)
    return 0


def cmd_benchmark_run(args: argparse.Namespace) -> int:
    root = (
        Path(args.dataset).expanduser().resolve() if args.dataset else download_verified(args.cache)
    )
    tasks = load_verified_tasks(root, include_excluded=args.include_excluded)
    if args.task_id:
        selected = set(args.task_id)
        tasks = [task for task in tasks if task.task_id in selected]
    tasks = tasks[args.offset :]
    if args.limit is not None:
        tasks = tasks[: args.limit]
    if not tasks:
        raise HarnessError("No benchmark tasks selected")
    require_evaluation_task_authorization(task.task_id for task in tasks)
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path("benchmarks/results") / _now_id()
    )
    runner = VerifiedBenchmarkRunner(
        _provider(args),
        output,
        skill_registry=_skills(args),
        max_turns=args.max_turns,
        max_output_tokens=args.max_output_tokens,
        enable_code=not args.no_code,
        recalculate=not args.no_recalculate,
        workers=args.workers,
        task_timeout_seconds=args.task_timeout,
        task_retries=args.task_retries,
        circuit_breaker_threshold=args.circuit_breaker,
    )
    summary = runner.run(tasks, resume=not args.no_resume)
    _json_print(summary)
    return 0 if summary["errors"] == 0 and summary["missing"] == 0 else 2


def cmd_benchmark_summary(args: argparse.Namespace) -> int:
    _json_print(summarize_results(args.results))
    return 0


def cmd_benchmark_compare(args: argparse.Namespace) -> int:
    run_spec_document: dict[str, Any] | None = None
    run_spec_provenance: dict[str, str] | None = None
    run_spec_bytes: bytes | None = None
    config: ProviderConfig | None = None
    skills: SkillRegistry | None = None
    arms = tuple(args.arm or COMPARISON_ARMS)
    if args.run_spec:
        if args.api_key is not None or not args.api_key_file:
            raise HarnessError(
                "Pilot run spec requires only --api-key-file for credential injection"
            )
        if not args.split_manifest or not args.dataset or not args.output:
            raise HarnessError(
                "Pilot run spec requires explicit --dataset, --split-manifest, and --output"
            )
        run_spec_document, run_spec_provenance, run_spec_bytes = (
            _load_pilot_run_spec_from_repository(args.run_spec)
        )
        require_launchable_run_spec(run_spec_provenance, resume=args.resume)
        pilot_root, pilot_split_path, pilot_output = _pilot_repository_paths(
            args, run_spec_document, output_argument=args.output
        )
        config = _provider(args)
        skills = _skills(args).freeze()
    requested_ids = list(args.task_id or [])
    if args.split_manifest and (
        args.offset != 0
        or args.limit is not None
        or requested_ids
        or args.task_id_file
    ):
        raise HarnessError(
            "--split-manifest selects a frozen task set; use a derivative manifest "
            "instead of additional task selectors"
        )
    root = (
        pilot_root
        if args.run_spec
        else Path(args.dataset).expanduser().resolve()
        if args.dataset
        else download_verified(args.cache)
    )
    tasks = load_verified_tasks(root)
    frozen_ids: list[str] = []
    split_provenance: dict[str, Any] | None = None
    if args.split_manifest:
        split_path = (
            pilot_split_path
            if args.run_spec
            else Path(args.split_manifest).expanduser().resolve()
        )
        split_report = load_and_verify_trace2skill_split_manifest(root, split_path)
        frozen_ids = [str(task_id) for task_id in split_report["task_ids"]]
        split_provenance = trace2skill_split_provenance(split_report)
    if args.task_id_file:
        requested_ids.extend(
            line.strip()
            for line in Path(args.task_id_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if frozen_ids:
        tasks_by_id = {task.task_id: task for task in tasks}
        missing = [task_id for task_id in frozen_ids if task_id not in tasks_by_id]
        if missing:
            raise HarnessError(
                "Verified split references unavailable task IDs: " + ", ".join(missing)
            )
        tasks = [tasks_by_id[task_id] for task_id in frozen_ids]
    elif requested_ids:
        if len(set(requested_ids)) != len(requested_ids):
            raise HarnessError("Comparison task IDs must be unique")
        selected = set(requested_ids)
        missing = sorted(selected - {task.task_id for task in tasks})
        if missing:
            raise HarnessError("Unknown comparison task IDs: " + ", ".join(missing))
        tasks = [task for task in tasks if task.task_id in selected]
    tasks = tasks[args.offset :]
    if args.limit is not None:
        tasks = tasks[: args.limit]
    if not tasks:
        raise HarnessError("No comparison tasks selected")
    require_evaluation_task_authorization(
        (task.task_id for task in tasks),
        authorized_manifest_id=(
            split_provenance.get("manifest_id")
            if run_spec_document is not None
            and isinstance(split_provenance, dict)
            else None
        ),
    )
    if config is None:
        config = _provider(args)
    if skills is None:
        skills = _skills(args).freeze()
    if args.run_spec:
        actual_contract = comparison_execution_contract(
            config,
            arms=arms,
            max_model_calls=args.max_model_calls,
            max_turns_per_arm=args.max_turns_per_arm,
            max_total_tokens=args.max_total_tokens,
            max_output_tokens=args.max_output_tokens,
            task_timeout_seconds=args.task_timeout,
            recalculate=not args.no_recalculate,
            arm_order_seed=args.arm_order_seed,
            circuit_breaker_threshold=args.circuit_breaker,
            split_provenance=split_provenance,
            skills=skills,
        )
        verify_pilot_run_spec_contract(run_spec_document, actual_contract)
    elif args.resume:
        raise HarnessError("--resume is supported only with --run-spec")
    elif (
        split_provenance
        and split_provenance.get("manifest_id") in protected_run_spec_split_ids()
    ):
        raise HarnessError("The frozen split requires its registered run spec")
    output = (
        pilot_output
        if args.run_spec
        else Path(args.output).expanduser().resolve()
        if args.output
        else Path("benchmarks/results") / ("comparison-" + _now_id())
    )
    if args.run_spec:
        if args.resume:
            if not output.is_dir():
                raise HarnessError("--resume requires an existing comparison directory")
        elif output.exists():
            raise HarnessError("Fresh pilot output path must not already exist")
        copy_path = output / RUN_SPEC_COPY_FILENAME
        if args.resume:
            if (
                not copy_path.is_file()
                or copy_path.is_symlink()
                or copy_path.read_bytes() != run_spec_bytes
            ):
                raise HarnessError("Existing comparison has a missing or different run spec")
    runner = ComparisonBenchmarkRunner(
        config,
        output,
        skill_registry=skills,
        arms=arms,
        max_model_calls=args.max_model_calls,
        max_turns_per_arm=args.max_turns_per_arm,
        max_total_tokens=args.max_total_tokens,
        max_output_tokens=args.max_output_tokens,
        task_timeout_seconds=args.task_timeout,
        recalculate=not args.no_recalculate,
        arm_order_seed=args.arm_order_seed,
        circuit_breaker_threshold=args.circuit_breaker,
        split_provenance=split_provenance,
        run_spec_document=run_spec_document,
        run_spec_provenance=run_spec_provenance,
        run_spec_bytes=run_spec_bytes,
    )
    if args.run_spec:
        runner.preflight(tasks)
        if not args.resume:
            _claim_fresh_pilot_output(output, run_spec_bytes)
    summary = (
        runner.run(tasks, resume=args.resume)
        if args.run_spec
        else runner.run(tasks)
    )
    _json_print(summary)
    errors = sum(int(item["errors"]) for item in summary["arms"].values())
    return 0 if summary["missing_arm_tasks"] == 0 and errors == 0 else 2


def cmd_benchmark_seal_interrupted(args: argparse.Namespace) -> int:
    if args.api_key is not None or not args.api_key_file:
        raise HarnessError(
            "Pilot interruption sealing requires only --api-key-file for credentials"
        )
    run_spec_document, run_spec_provenance, run_spec_bytes = (
        _load_pilot_run_spec_from_repository(args.run_spec)
    )
    require_launchable_run_spec(
        run_spec_provenance,
        operation="seal interrupted state for",
    )
    root, split_path, output = _pilot_repository_paths(
        args, run_spec_document, output_argument=args.results
    )
    if not output.is_dir():
        raise HarnessError("Pilot interruption sealing requires the existing output directory")
    split_report = load_and_verify_trace2skill_split_manifest(root, split_path)
    split_provenance = trace2skill_split_provenance(split_report)
    tasks_by_id = {task.task_id: task for task in load_verified_tasks(root)}
    missing = [
        str(task_id)
        for task_id in split_report["task_ids"]
        if str(task_id) not in tasks_by_id
    ]
    if missing:
        raise HarnessError(
            "Verified split references unavailable task IDs: " + ", ".join(missing)
        )
    tasks = [tasks_by_id[str(task_id)] for task_id in split_report["task_ids"]]
    config = _provider(args)
    skills = _skills(args).freeze()
    execution = run_spec_document["execution"]
    resources = execution["resources"]
    verify_pilot_run_spec_contract(
        run_spec_document,
        comparison_execution_contract(
            config,
            arms=tuple(execution["arms"]),
            max_model_calls=resources["max_model_calls"],
            max_turns_per_arm=resources["max_turns_per_arm"],
            max_total_tokens=resources["max_total_tokens"],
            max_output_tokens=resources["max_output_tokens_per_call"],
            task_timeout_seconds=resources["task_timeout_seconds"],
            recalculate=resources["recalculate"],
            arm_order_seed=resources["arm_order_seed"],
            circuit_breaker_threshold=resources["circuit_breaker_threshold"],
            split_provenance=split_provenance,
            skills=skills,
        ),
    )
    runner = ComparisonBenchmarkRunner(
        config,
        output,
        skill_registry=skills,
        arms=tuple(execution["arms"]),
        max_model_calls=resources["max_model_calls"],
        max_turns_per_arm=resources["max_turns_per_arm"],
        max_total_tokens=resources["max_total_tokens"],
        max_output_tokens=resources["max_output_tokens_per_call"],
        task_timeout_seconds=resources["task_timeout_seconds"],
        recalculate=resources["recalculate"],
        arm_order_seed=resources["arm_order_seed"],
        circuit_breaker_threshold=resources["circuit_breaker_threshold"],
        split_provenance=split_provenance,
        run_spec_document=run_spec_document,
        run_spec_provenance=run_spec_provenance,
        run_spec_bytes=run_spec_bytes,
    )
    seal = runner.seal_interrupted_inflight(tasks)
    _json_print({"sealed": True, "seal": seal})
    return 0


def cmd_benchmark_audit(args: argparse.Namespace) -> int:
    root = (
        Path(args.dataset).expanduser().resolve() if args.dataset else download_verified(args.cache)
    )
    tasks = load_verified_tasks(root)
    manifest_path = Path(args.results).expanduser().resolve() / "comparison-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Unable to read comparison manifest: {manifest_path}") from exc
    task_ids = manifest.get("task_ids") if isinstance(manifest, dict) else None
    if not isinstance(task_ids, list) or not all(isinstance(item, str) for item in task_ids):
        raise HarnessError("Comparison manifest does not contain a valid task_ids list")
    selected = set(task_ids)
    tasks_by_id = {task.task_id: task for task in tasks}
    missing = sorted(selected - set(tasks_by_id))
    if missing:
        raise HarnessError("Comparison manifest references unknown task IDs: " + ", ".join(missing))
    selected_tasks = [tasks_by_id[task_id] for task_id in task_ids]
    summary = audit_comparison(args.results, selected_tasks)
    _json_print(summary)
    return 0 if summary["audit_valid"] else 2


def cmd_evolve_generate(args: argparse.Namespace) -> int:
    config = _provider(args)
    with _provider_client(config) as client:
        candidate = generate_candidate(
            args.trajectory,
            args.output,
            client,
            model=config.model,
            candidate_id=args.candidate_id,
            skill_name=args.skill_name,
        )
    _json_print(
        {
            "candidate_id": candidate.candidate_id,
            "path": str(candidate.path),
            "skill_path": str(candidate.skill_path),
            "sha256": candidate.sha256,
            "promoted": False,
        }
    )
    return 0


def cmd_evolve_promote(args: argparse.Namespace) -> int:
    destination = promote_candidate(
        args.candidate,
        args.skill_root,
        args.validation_report,
        min_delta=args.min_delta,
    )
    _json_print({"ok": True, "promoted_to": str(destination)})
    return 0


def _add_provider_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", help="Provider API base URL ending in /v1")
    parser.add_argument("--api-key", help=argparse.SUPPRESS)
    parser.add_argument(
        "--api-key-file",
        type=Path,
        metavar="PATH",
        help=(
            "Read one API key from an owner-only file; alternatively set "
            "SHEET_AGENT_API_KEY_FILE"
        ),
    )
    parser.add_argument("--model")
    parser.add_argument(
        "--api-protocol",
        choices=API_PROTOCOLS,
        help="Provider protocol: responses or chat-completions",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=[*REASONING_EFFORTS, *REASONING_ALIASES],
        help="Reasoning effort; ultra is recorded and sent as the API maximum, max",
    )
    parser.add_argument("--request-timeout", type=float, help="Seconds per provider request")
    parser.add_argument(
        "--request-retries", type=int, choices=range(0, 6), help="Retries for transient failures"
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        help="Minimum start-to-start interval between Relay HTTP attempts (default: 0)",
    )
    parser.add_argument(
        "--litellm-timeout",
        type=float,
        help="Optional LiteLLM upstream timeout in seconds via x-litellm-timeout",
    )
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int, help="Signed 64-bit generation seed")
    parser.add_argument("--presence-penalty", type=float)
    parser.add_argument("--top-k", type=int, help="vLLM sampling extension")
    parser.add_argument("--min-p", type=float, help="vLLM sampling extension")
    parser.add_argument("--repetition-penalty", type=float, help="vLLM sampling extension")
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument(
        "--enable-thinking",
        dest="enable_thinking",
        action="store_true",
        default=None,
        help="Set vLLM chat_template_kwargs.enable_thinking=true",
    )
    thinking.add_argument(
        "--disable-thinking",
        dest="enable_thinking",
        action="store_false",
        help="Set vLLM chat_template_kwargs.enable_thinking=false",
    )


def _add_agent_flags(parser: argparse.ArgumentParser) -> None:
    _add_provider_flags(parser)
    parser.add_argument("--skills", action="append", default=[], help="Additional skills root")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-output-tokens", type=int, default=16_000)
    parser.add_argument("--no-code", action="store_true", help="Disable local code interpreter")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sheet-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local dependencies and provider config")
    _add_provider_flags(doctor)
    doctor.add_argument("--online", action="store_true", help="Send one minimal model request")
    doctor.add_argument(
        "--tools",
        action="store_true",
        help="With --online, check a synthetic function-call round trip",
    )
    doctor.set_defaults(handler=cmd_doctor)

    preprocess = subparsers.add_parser("preprocess", help="Create JSON/YAML/Markdown views")
    preprocess.add_argument("source", type=Path)
    preprocess.add_argument("--output", type=Path, required=True)
    preprocess.add_argument("--max-cells", type=int, default=5000)
    preprocess.add_argument("--timeout", type=float, default=120)
    preprocess.set_defaults(handler=cmd_preprocess)

    render = subparsers.add_parser("render", help="Render workbook pages to original PNGs")
    render.add_argument("source", type=Path)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--dpi", type=int, default=144)
    render.add_argument("--whole-workbook", action="store_true")
    render.add_argument("--timeout", type=float, default=120)
    render.set_defaults(handler=cmd_render)

    run = subparsers.add_parser("run", help="Run the spreadsheet agent once")
    run.add_argument("source", type=Path)
    instruction = run.add_mutually_exclusive_group(required=True)
    instruction.add_argument("--instruction")
    instruction.add_argument("--instruction-file", type=Path)
    run.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run.add_argument("--quiet", action="store_true")
    _add_agent_flags(run)
    run.set_defaults(handler=cmd_run)

    benchmark = subparsers.add_parser("benchmark", help="SpreadsheetBench Verified operations")
    benchmark_commands = benchmark.add_subparsers(dest="benchmark_command", required=True)
    download = benchmark_commands.add_parser("download", help="Download pinned Verified 400")
    download.add_argument("--output", type=Path, default=Path("benchmarks/data"))
    download.set_defaults(handler=cmd_benchmark_download)

    split = benchmark_commands.add_parser(
        "split", help="Generate or verify a frozen Trace2Skill split manifest"
    )
    split.add_argument("--dataset", type=Path, help="Extracted dataset root")
    split.add_argument("--cache", type=Path, default=Path("benchmarks/data"))
    split_action = split.add_mutually_exclusive_group(required=True)
    split_action.add_argument("--write", type=Path, help="Create a new frozen JSON manifest")
    split_action.add_argument("--verify", type=Path, help="Verify an existing frozen JSON manifest")
    split.set_defaults(handler=cmd_benchmark_split)

    benchmark_run = benchmark_commands.add_parser("run", help="Run Verified tasks")
    benchmark_run.add_argument("--dataset", type=Path, help="Extracted dataset root")
    benchmark_run.add_argument("--cache", type=Path, default=Path("benchmarks/data"))
    benchmark_run.add_argument("--output", type=Path)
    benchmark_run.add_argument("--task-id", action="append")
    benchmark_run.add_argument("--offset", type=int, default=0)
    benchmark_run.add_argument("--limit", type=int)
    benchmark_run.add_argument("--include-excluded", action="store_true")
    benchmark_run.add_argument("--no-recalculate", action="store_true")
    benchmark_run.add_argument("--no-resume", action="store_true")
    benchmark_run.add_argument("--workers", type=int, choices=range(1, 17), default=1)
    benchmark_run.add_argument(
        "--task-timeout",
        type=float,
        default=7200,
        help="Approximate wall-clock limit per task, checked between requests and tools",
    )
    benchmark_run.add_argument(
        "--task-retries",
        type=int,
        choices=range(0, 6),
        default=1,
        help="Fresh-workbook retries after transient provider failures",
    )
    benchmark_run.add_argument(
        "--circuit-breaker",
        type=int,
        default=3,
        help="Stop scheduling after this many tasks exhaust transient-provider retries",
    )
    _add_agent_flags(benchmark_run)
    benchmark_run.set_defaults(handler=cmd_benchmark_run)

    summary = benchmark_commands.add_parser("summary", help="Recompute a result summary")
    summary.add_argument("results", type=Path)
    summary.set_defaults(handler=cmd_benchmark_summary)

    compare = benchmark_commands.add_parser(
        "compare", help="Run a resource-matched bare/paper/ours comparison"
    )
    compare.add_argument("--dataset", type=Path, help="Extracted dataset root")
    compare.add_argument("--cache", type=Path, default=Path("benchmarks/data"))
    compare.add_argument("--output", type=Path)
    compare.add_argument(
        "--run-spec",
        type=Path,
        help="Enforce the code-anchored local pilot execution contract",
    )
    compare.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing run-spec comparison after exact preflight validation",
    )
    compare.add_argument("--task-id", action="append")
    compare.add_argument(
        "--split-manifest",
        type=Path,
        help="Verify and select frozen Trace2Skill task IDs in manifest order",
    )
    compare.add_argument(
        "--task-id-file",
        type=Path,
        help="UTF-8 file containing one task ID per line; blank/comment lines are ignored",
    )
    compare.add_argument("--offset", type=int, default=0)
    compare.add_argument("--limit", type=int)
    compare.add_argument("--arm", action="append", choices=AVAILABLE_COMPARISON_ARMS)
    compare.add_argument("--no-recalculate", action="store_true")
    compare.add_argument("--skills", action="append", default=[], help="Additional skills root")
    compare.add_argument("--max-model-calls", type=int, default=20)
    compare.add_argument(
        "--max-turns-per-arm",
        type=int,
        default=20,
        help="Maximum model-response turns across all stages of each arm",
    )
    compare.add_argument("--max-total-tokens", type=int, default=100_000)
    compare.add_argument("--max-output-tokens", type=int, default=4_096)
    compare.add_argument("--task-timeout", type=float, default=900)
    compare.add_argument("--arm-order-seed", type=int, default=20_260_811)
    compare.add_argument("--circuit-breaker", type=int, default=3)
    _add_provider_flags(compare)
    compare.set_defaults(handler=cmd_benchmark_compare)

    seal_interrupted = benchmark_commands.add_parser(
        "seal-interrupted",
        help="Seal one ambiguous pilot arm-task without replaying it",
    )
    seal_interrupted.add_argument("results", type=Path)
    seal_interrupted.add_argument("--dataset", type=Path, required=True)
    seal_interrupted.add_argument("--split-manifest", type=Path, required=True)
    seal_interrupted.add_argument("--run-spec", type=Path, required=True)
    seal_interrupted.add_argument("--skills", action="append", default=[])
    _add_provider_flags(seal_interrupted)
    seal_interrupted.set_defaults(handler=cmd_benchmark_seal_interrupted)

    audit = benchmark_commands.add_parser(
        "audit", help="Read-only fresh-rescore audit of a comparison directory"
    )
    audit.add_argument("results", type=Path)
    audit.add_argument("--dataset", type=Path, help="Extracted dataset root")
    audit.add_argument("--cache", type=Path, default=Path("benchmarks/data"))
    audit.set_defaults(handler=cmd_benchmark_audit)

    evolve = subparsers.add_parser("evolve", help="Generate or validate skill candidates")
    evolve_commands = evolve.add_subparsers(dest="evolve_command", required=True)
    generate = evolve_commands.add_parser(
        "generate", help="Generate a candidate from redacted trajectories"
    )
    generate.add_argument("trajectory", nargs="+", type=Path)
    generate.add_argument("--output", type=Path, default=Path("evolution"))
    generate.add_argument("--candidate-id")
    generate.add_argument("--skill-name", default="spreadsheet-core")
    _add_provider_flags(generate)
    generate.set_defaults(handler=cmd_evolve_generate)

    promote = evolve_commands.add_parser(
        "promote", help="Promote only after paired-seed validation gates pass"
    )
    promote.add_argument("candidate", type=Path)
    promote.add_argument("--skill-root", type=Path, required=True)
    promote.add_argument("--validation-report", type=Path, required=True)
    promote.add_argument("--min-delta", type=float, default=0.0)
    promote.set_defaults(handler=cmd_evolve_promote)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (HarnessError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
