"""Command-line entry point for local runs and SpreadsheetBench."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import shutil
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

from .agent import ResponsesClient, SpreadsheetAgent
from .audit import audit_comparison
from .benchmark import (
    VerifiedBenchmarkRunner,
    _atomic_write_json,
    download_verified,
    load_verified_tasks,
    summarize_results,
    trace2skill_heldout_manifest,
    verify_trace2skill_heldout_manifest,
)
from .comparison import AVAILABLE_COMPARISON_ARMS, COMPARISON_ARMS, ComparisonBenchmarkRunner
from .config import REASONING_ALIASES, REASONING_EFFORTS, ProviderConfig
from .errors import HarnessError
from .evolution import generate_candidate, promote_candidate
from .preprocess import preprocess_workbook
from .provider_compat import check_responses_tool_compatibility
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
    )


def _default_skill_root() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def _skills(args: argparse.Namespace) -> SkillRegistry:
    roots = [_default_skill_root()]
    roots.extend(Path(item).expanduser().resolve() for item in getattr(args, "skills", []) or [])
    return SkillRegistry(roots)


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
                report["tools"] = check_responses_tool_compatibility(config)
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
                with ResponsesClient(config) as client:
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
        session = WorkbookSession.create(editable, run_dir)
        if normalized:
            shutil.copy2(
                source.resolve(), session.paths.input.parent / ("original" + source.suffix)
            )
        tools = SpreadsheetToolRegistry(session, enable_code=not args.no_code)
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
    report = verify_trace2skill_heldout_manifest(root, args.verify)
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
    root = (
        Path(args.dataset).expanduser().resolve() if args.dataset else download_verified(args.cache)
    )
    tasks = load_verified_tasks(root)
    requested_ids = list(args.task_id or [])
    frozen_ids: list[str] = []
    if args.split_manifest:
        split_path = Path(args.split_manifest).expanduser().resolve()
        verify_trace2skill_heldout_manifest(root, split_path)
        frozen = json.loads(split_path.read_text(encoding="utf-8"))
        frozen_ids = [str(task_id) for task_id in frozen["task_ids"]]
    if args.task_id_file:
        requested_ids.extend(
            line.strip()
            for line in Path(args.task_id_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if frozen_ids and requested_ids:
        raise HarnessError("--split-manifest cannot be combined with task ID selectors")
    if frozen_ids:
        tasks_by_id = {task.task_id: task for task in tasks}
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
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path("benchmarks/results") / ("comparison-" + _now_id())
    )
    runner = ComparisonBenchmarkRunner(
        _provider(args),
        output,
        skill_registry=_skills(args),
        arms=tuple(args.arm or COMPARISON_ARMS),
        max_model_calls=args.max_model_calls,
        max_turns_per_arm=args.max_turns_per_arm,
        max_total_tokens=args.max_total_tokens,
        max_output_tokens=args.max_output_tokens,
        task_timeout_seconds=args.task_timeout,
        recalculate=not args.no_recalculate,
        arm_order_seed=args.arm_order_seed,
        circuit_breaker_threshold=args.circuit_breaker,
    )
    summary = runner.run(tasks)
    _json_print(summary)
    errors = sum(int(item["errors"]) for item in summary["arms"].values())
    return 0 if summary["missing_arm_tasks"] == 0 and errors == 0 else 2


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
    with ResponsesClient(config) as client:
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
    parser.add_argument("--base-url", help="Responses API base URL ending in /v1")
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
        "--reasoning-effort",
        choices=[*REASONING_EFFORTS, *REASONING_ALIASES],
        help="Responses reasoning effort; ultra is recorded and sent as the API maximum, max",
    )
    parser.add_argument("--request-timeout", type=float, help="Seconds per Responses request")
    parser.add_argument(
        "--request-retries", type=int, choices=range(0, 6), help="Retries for transient failures"
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        help="Minimum start-to-start interval between Relay HTTP attempts (default: 0)",
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
        help="With --online, check a synthetic Responses function-call round trip",
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
        "split", help="Generate or verify a frozen Trace2Skill held-out split manifest"
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
    compare.add_argument("--task-id", action="append")
    compare.add_argument(
        "--split-manifest",
        type=Path,
        help="Verify and select the frozen Trace2Skill held-out task IDs in manifest order",
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
