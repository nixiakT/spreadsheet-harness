"""Pinned SpreadsheetBench Verified adapter and clean-room value scorer."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
import platform
import re
import shutil
import tarfile
import urllib.request
import uuid
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import fmean
from time import monotonic
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries

from .agent import CONTEXT_POLICY, SpreadsheetAgent
from .config import ProviderConfig
from .errors import AgentTimeoutError, HarnessError, ProviderError
from .pacing import PACING_POLICY, RelayPacer
from .session import WorkbookSession
from .skills import SkillRegistry
from .tools import SpreadsheetToolRegistry

VERIFIED_REVISION = "ab0b742b0fc95b946f212d80ac7771b5531272e4"
VERIFIED_URL = (
    "https://huggingface.co/datasets/KAKA22/SpreadsheetBench/resolve/"
    f"{VERIFIED_REVISION}/spreadsheetbench_verified_400.tar.gz"
)
VERIFIED_SHA256 = "10ef893dd29cb13ab97143ea787e68cdc9574a13873ab9a54e50b31dc03fc949"
VERIFIED_DATASET_JSON_SHA256 = (
    "bcecaa89a005bd4e3bbe98da150a86e8062c27f262e575d5e47bd9861b3525e7"
)
TRACE2SKILL_HELDOUT_START = 200
TRACE2SKILL_HELDOUT_STOP = 400
TRACE2SKILL_HELDOUT_EXCLUDED_INDICES = (337, 338)
TRACE2SKILL_HELDOUT_TASK_COUNT = 198
TRACE2SKILL_HELDOUT_TASK_IDS_SHA256 = (
    "445ceec8e033601a054babf7997e340cf21d1c1d2d54a4aa421a8ba29b189582"
)
TRACE2SKILL_SPLIT_SCHEMA_VERSION = "spreadsheetbench-trace2skill-heldout-v1"
BENCHMARK_MANIFEST_SCHEMA_VERSION = 2
BENCHMARK_PROTOCOL_VERSION = "agent_per_workbook_v2"
_PROCESS_PACERS: dict[str, RelayPacer] = {}


def _process_pacer(scope_id: str, interval_seconds: float) -> RelayPacer:
    """Reuse one pacer across every task handled by a benchmark worker process."""

    pacer = _PROCESS_PACERS.get(scope_id)
    if pacer is None:
        pacer = RelayPacer(interval_seconds)
        _PROCESS_PACERS[scope_id] = pacer
    elif pacer.interval_seconds != interval_seconds:
        raise RuntimeError("benchmark pacing scope was reused with a different interval")
    return pacer


@dataclass(frozen=True)
class SpreadsheetTask:
    task_id: str
    instruction: str
    input_path: Path
    golden_path: Path
    instruction_type: str
    answer_position: str
    answer_sheet: str | None
    protocol: str = "agent_per_workbook"
    excluded: bool = False


def _ordered_task_ids_sha256(task_ids: Iterable[str]) -> str:
    return _text_sha256("".join(f"{task_id}\n" for task_id in task_ids))


@dataclass(frozen=True)
class Comparison:
    passed: bool
    checked_cells: int
    differences: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_cells": self.checked_cells,
            "differences": list(self.differences),
        }


def comparison_evidence(comparison: Comparison) -> dict[str, Any]:
    """Return evaluator evidence without copying golden values into trajectories."""

    categories: Counter[str] = Counter()
    for difference in comparison.differences:
        reasons = difference.get("reasons")
        if isinstance(reasons, list):
            categories.update(str(reason) for reason in reasons)
        elif difference.get("error"):
            categories["metadata_or_structure"] += 1
        else:
            categories["other"] += 1
    return {
        "checked_cells": comparison.checked_cells,
        "difference_count": len(comparison.differences),
        "difference_categories": dict(sorted(categories.items())),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_fingerprint() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    repository_root = package_root.parents[1]
    files = sorted(package_root.glob("*.py"))
    pyproject = repository_root / "pyproject.toml"
    if pyproject.is_file():
        files.append(pyproject)
    entries: list[dict[str, str]] = []
    combined = hashlib.sha256()
    for path in files:
        relative = str(path.relative_to(repository_root))
        digest = _sha256(path)
        entries.append({"path": relative, "sha256": digest})
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    return {"sha256": combined.hexdigest(), "files": entries}


def _runtime_fingerprint() -> dict[str, Any]:
    dependencies: dict[str, str | None] = {}
    for distribution in ("httpx", "openpyxl", "pandas", "Pillow", "PyMuPDF", "PyYAML"):
        try:
            dependencies[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            dependencies[distribution] = None
    from .render import find_libreoffice, libreoffice_version

    libreoffice = find_libreoffice()
    return {
        "python": platform.python_version(),
        "dependencies": dependencies,
        "libreoffice": libreoffice_version(libreoffice) if libreoffice else None,
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Durably replace a JSON file without exposing a partially written document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.is_file():
        return [], 0
    rows: list[dict[str, Any]] = []
    invalid = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            invalid += 1
    return rows, invalid


def _repair_jsonl(path: Path) -> int:
    """Rewrite a damaged journal with every intact row before appending again."""

    if not path.is_file():
        return 0
    raw = path.read_bytes()
    rows, invalid = _valid_jsonl_rows(path)
    if invalid == 0 and (not raw or raw.endswith(b"\n")):
        return 0
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.repair")
    try:
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            for row in rows:
                handle.write(
                    (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return invalid


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"Unsafe path in dataset archive: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Links are not accepted in dataset archive: {member.name}")
        tar.extractall(destination, members=members)  # noqa: S202 - validated above


def download_verified(destination: str | Path) -> Path:
    """Download and checksum the pinned Verified 400 archive."""

    destination_path = Path(destination).expanduser().resolve()
    root = destination_path / "spreadsheetbench_verified_400"
    dataset_json = root / "dataset.json"
    if dataset_json.is_file():
        return root
    destination_path.mkdir(parents=True, exist_ok=True)
    archive = destination_path / "spreadsheetbench_verified_400.tar.gz"
    partial = archive.with_name(archive.name + ".part")
    if not archive.is_file() or _sha256(archive) != VERIFIED_SHA256:
        partial.unlink(missing_ok=True)
        request = urllib.request.Request(VERIFIED_URL, headers={"User-Agent": "sheet-harness/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if _sha256(partial) != VERIFIED_SHA256:
            partial.unlink(missing_ok=True)
            raise ValueError("SpreadsheetBench Verified archive checksum mismatch")
        partial.replace(archive)
    _safe_extract(archive, destination_path)
    if not dataset_json.is_file():
        raise FileNotFoundError(f"Archive did not contain expected dataset file: {dataset_json}")
    return root


def _find_pair(task_dir: Path, task_id: str) -> tuple[Path, Path]:
    candidates = [
        (task_dir / f"1_{task_id}_init.xlsx", task_dir / f"1_{task_id}_golden.xlsx"),
        (task_dir / "initial.xlsx", task_dir / "golden.xlsx"),
    ]
    for initial, golden in candidates:
        if initial.is_file() and golden.is_file():
            return initial, golden
    initial_matches = sorted(task_dir.glob("*_init.xlsx"))
    golden_matches = sorted(task_dir.glob("*_golden.xlsx"))
    if len(initial_matches) == 1 and len(golden_matches) == 1:
        return initial_matches[0], golden_matches[0]
    raise FileNotFoundError(f"Could not resolve init/golden pair in {task_dir}")


def load_verified_tasks(
    dataset_root: str | Path,
    *,
    include_excluded: bool = False,
    original_index_start: int | None = None,
    original_index_stop: int | None = None,
) -> list[SpreadsheetTask]:
    root = Path(dataset_root).expanduser().resolve(strict=True)
    rows = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Verified dataset.json must be a JSON array")
    if original_index_start is not None and original_index_start < 0:
        raise ValueError("original_index_start must be non-negative")
    if original_index_stop is not None and original_index_stop < 0:
        raise ValueError("original_index_stop must be non-negative")
    start = 0 if original_index_start is None else original_index_start
    stop = len(rows) if original_index_stop is None else original_index_stop
    if stop < start:
        raise ValueError("original_index_stop must be at least original_index_start")
    tasks: list[SpreadsheetTask] = []
    for original_index, row in enumerate(rows):
        if not start <= original_index < stop:
            continue
        excluded = bool(row.get("exclude"))
        if excluded and not include_excluded:
            continue
        task_id = str(row["id"])
        raw_path = Path(str(row.get("spreadsheet_path", f"spreadsheet/{task_id}")))
        task_dir = raw_path if raw_path.is_absolute() else root / raw_path
        if not task_dir.is_dir():
            task_dir = root / "spreadsheet" / task_id
        initial, golden = _find_pair(task_dir, task_id)
        tasks.append(
            SpreadsheetTask(
                task_id=task_id,
                instruction=str(row["instruction"]),
                input_path=initial,
                golden_path=golden,
                instruction_type=str(row.get("instruction_type", "")),
                answer_position=str(row["answer_position"]),
                answer_sheet=str(row["answer_sheet"]) if row.get("answer_sheet") else None,
                excluded=excluded,
            )
        )
    return tasks


def trace2skill_heldout_manifest(dataset_root: str | Path) -> dict[str, Any]:
    """Build a frozen split manifest from raw row indices before exclusions."""

    root = Path(dataset_root).expanduser().resolve(strict=True)
    dataset_path = root / "dataset.json"
    raw = dataset_path.read_bytes()
    dataset_hash = hashlib.sha256(raw).hexdigest()
    if dataset_hash != VERIFIED_DATASET_JSON_SHA256:
        raise ValueError(
            "Pinned SpreadsheetBench dataset.json checksum changed: "
            f"expected {VERIFIED_DATASET_JSON_SHA256}, got {dataset_hash}"
        )
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("Verified dataset.json must be a JSON array")
    if len(rows) < TRACE2SKILL_HELDOUT_STOP:
        raise ValueError(
            f"Trace2Skill held-out split requires at least {TRACE2SKILL_HELDOUT_STOP} raw rows"
        )
    selected_rows = list(
        enumerate(
            rows[TRACE2SKILL_HELDOUT_START:TRACE2SKILL_HELDOUT_STOP],
            start=TRACE2SKILL_HELDOUT_START,
        )
    )
    excluded = [
        {
            "original_index": index,
            "task_id": str(row["id"]),
            "reason_sha256": _text_sha256(str(row.get("exclude", ""))),
        }
        for index, row in selected_rows
        if row.get("exclude")
    ]
    task_ids = [str(row["id"]) for _, row in selected_rows if not row.get("exclude")]
    split_hash = _ordered_task_ids_sha256(task_ids)
    if tuple(item["original_index"] for item in excluded) != TRACE2SKILL_HELDOUT_EXCLUDED_INDICES:
        raise ValueError(
            "Trace2Skill held-out exclusions changed; expected raw indices "
            f"{list(TRACE2SKILL_HELDOUT_EXCLUDED_INDICES)}, got "
            f"{[item['original_index'] for item in excluded]}"
        )
    if len(task_ids) != TRACE2SKILL_HELDOUT_TASK_COUNT:
        raise ValueError(
            f"Trace2Skill held-out split expected {TRACE2SKILL_HELDOUT_TASK_COUNT} usable tasks, "
            f"got {len(task_ids)}"
        )
    if split_hash != TRACE2SKILL_HELDOUT_TASK_IDS_SHA256:
        raise ValueError(
            "Trace2Skill held-out task IDs/order changed: "
            f"expected {TRACE2SKILL_HELDOUT_TASK_IDS_SHA256}, got {split_hash}"
        )
    tasks = load_verified_tasks(
        root,
        original_index_start=TRACE2SKILL_HELDOUT_START,
        original_index_stop=TRACE2SKILL_HELDOUT_STOP,
    )
    loaded_ids = [task.task_id for task in tasks]
    if loaded_ids != task_ids:
        raise ValueError("Loaded held-out tasks do not match raw-row split selection")
    return {
        "schema_version": TRACE2SKILL_SPLIT_SCHEMA_VERSION,
        "dataset": {
            "revision": VERIFIED_REVISION,
            "archive_sha256": VERIFIED_SHA256,
            "dataset_json_sha256": dataset_hash,
            "raw_row_count": len(rows),
        },
        "selection": {
            "original_index_start_inclusive": TRACE2SKILL_HELDOUT_START,
            "original_index_stop_exclusive": TRACE2SKILL_HELDOUT_STOP,
            "apply_before_exclusion_filter": True,
            "raw_rows": TRACE2SKILL_HELDOUT_STOP - TRACE2SKILL_HELDOUT_START,
            "usable_tasks": len(task_ids),
            "excluded_tasks": excluded,
        },
        "task_ids": task_ids,
        "task_ids_sha256": split_hash,
    }


def verify_trace2skill_heldout_manifest(
    dataset_root: str | Path, manifest_path: str | Path
) -> dict[str, Any]:
    """Read-only verification of a frozen held-out manifest against a dataset."""

    path = Path(manifest_path).expanduser().resolve(strict=True)
    try:
        frozen = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid split manifest JSON: {path}") from exc
    if not isinstance(frozen, dict):
        raise ValueError("Split manifest must be a JSON object")
    expected = trace2skill_heldout_manifest(dataset_root)
    if frozen != expected:
        mismatches = [key for key in expected if frozen.get(key) != expected[key]]
        raise ValueError(
            "Frozen Trace2Skill held-out manifest does not match dataset; fields: "
            + ", ".join(mismatches)
        )
    return {
        "valid": True,
        "manifest": str(path),
        "schema_version": expected["schema_version"],
        "usable_tasks": expected["selection"]["usable_tasks"],
        "task_ids_sha256": expected["task_ids_sha256"],
        "dataset_json_sha256": expected["dataset"]["dataset_json_sha256"],
    }


def _transform_official(value: Any) -> Any:
    """Match the published SpreadsheetBench value normalization."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return round(float(value), 2)
    if isinstance(value, time):
        return str(value)[:-3]
    if isinstance(value, datetime):
        origin = datetime(1899, 12, 30)
        delta = value - origin
        return round(delta.days + delta.seconds / 86400.0, 0)
    if isinstance(value, date):
        origin_date = date(1899, 12, 30)
        return float((value - origin_date).days)
    if isinstance(value, str):
        try:
            return round(float(value), 2)
        except ValueError:
            return value
    return value


def _equal_values(expected: Any, actual: Any) -> bool:
    expected = _transform_official(expected)
    actual = _transform_official(actual)
    if expected in (None, "") and actual in (None, ""):
        return True
    if isinstance(expected, float) and isinstance(actual, float):
        if math.isnan(expected) and math.isnan(actual):
            return True
    return type(expected) is type(actual) and expected == actual


_RANGE_PATTERN = r"(?:\$?[A-Za-z]+\$?\d+(?::(?:\$?[A-Za-z]*\$?\d+))?|\$?[A-Za-z]+:\$?[A-Za-z]+)"


def _answer_sheet_names(answer_sheet: str | None, sheetnames: list[str]) -> list[str]:
    if not answer_sheet:
        return []
    cleaned = answer_sheet.strip().strip('"')
    if cleaned.strip("'") in sheetnames:
        return [cleaned.strip("'")]
    requested = [piece.strip().strip("'") for piece in cleaned.split(",")]
    return [name for name in requested if name in sheetnames]


def _split_answer_positions(
    answer_position: str,
    *,
    sheetnames: list[str],
    answer_sheet: str | None,
) -> list[tuple[str, str]]:
    """Parse dataset ranges using known sheet names to survive malformed quoting."""

    answer_position = re.sub(r"('!)'(?=\s*\$?[A-Za-z])", r"\1", answer_position)
    answer_position = re.sub(r"!'(?=\s*\$?[A-Za-z])", "'!", answer_position)
    positions: list[tuple[str, str]] = []
    masked = list(answer_position)
    alternatives = "|".join(re.escape(name) for name in sorted(sheetnames, key=len, reverse=True))
    if alternatives:
        explicit = re.compile(
            rf"'?\s*(?P<sheet>{alternatives})'?\s*!\s*(?P<range>{_RANGE_PATTERN})",
            re.IGNORECASE,
        )
        canonical = {name.casefold(): name for name in sheetnames}
        for match in explicit.finditer(answer_position):
            sheet = canonical[match.group("sheet").casefold()]
            positions.append((sheet, match.group("range")))
            for index in range(match.start(), match.end()):
                masked[index] = " "

    remaining = "".join(masked)
    unqualified = [match.group(0) for match in re.finditer(_RANGE_PATTERN, remaining)]
    targets = _answer_sheet_names(answer_sheet, sheetnames) or [sheetnames[0]]
    for cell_range in unqualified:
        positions.extend((sheet, cell_range) for sheet in targets)
    return positions


def _coordinates(range_ref: str, *, max_row_hint: int, max_column_hint: int) -> Iterable[str]:
    cleaned = range_ref.replace("$", "")
    malformed_end = re.fullmatch(r"([A-Za-z]+)(\d+):(\d+)", cleaned)
    if malformed_end:
        column, start_row, end_row = malformed_end.groups()
        cleaned = f"{column}{start_row}:{column}{end_row}"
    if ":" not in cleaned and any(char.isdigit() for char in cleaned):
        yield cleaned
        return
    min_col, min_row, max_col, max_row = range_boundaries(cleaned)
    from openpyxl.utils import get_column_letter

    if min_col is None and max_col is None:
        min_col, max_col = 1, max_column_hint
    else:
        min_col = min_col or max_col or 1
        max_col = max_col or min_col
    if min_row is None and max_row is None:
        min_row, max_row = 1, max_row_hint
    else:
        min_row = min_row or max_row or 1
        max_row = max_row or min_row
    for column in range(min_col, max_col + 1):
        for row in range(min_row, max_row + 1):
            yield f"{get_column_letter(column)}{row}"


def compare_workbooks(
    golden_path: str | Path,
    candidate_path: str | Path,
    answer_position: str,
    *,
    answer_sheet: str | None = None,
    compare_styles: bool = False,
    max_differences: int = 100,
) -> Comparison:
    """Compare answer cells using published value semantics plus optional styles."""

    golden_file = Path(golden_path)
    candidate_file = Path(candidate_path)
    if not candidate_file.is_file():
        return Comparison(False, 0, ({"error": "candidate file does not exist"},))
    expected_book = load_workbook(golden_file, data_only=True, read_only=False)
    actual_book = load_workbook(candidate_file, data_only=True, read_only=False)
    differences: list[dict[str, Any]] = []
    checked = 0
    try:
        positions = _split_answer_positions(
            answer_position,
            sheetnames=expected_book.sheetnames,
            answer_sheet=answer_sheet,
        )
        if not positions:
            differences.append(
                {"range": answer_position, "error": "answer range could not be parsed"}
            )
        for sheet, range_ref in positions:
            if sheet not in expected_book.sheetnames or sheet not in actual_book.sheetnames:
                differences.append(
                    {"sheet": sheet, "range": range_ref, "error": "worksheet missing"}
                )
                continue
            expected_sheet = expected_book[sheet]
            actual_sheet = actual_book[sheet]
            for coordinate in _coordinates(
                range_ref,
                max_row_hint=max(expected_sheet.max_row, actual_sheet.max_row),
                max_column_hint=max(expected_sheet.max_column, actual_sheet.max_column),
            ):
                checked += 1
                expected_cell = expected_sheet[coordinate]
                actual_cell = actual_sheet[coordinate]
                reasons: list[str] = []
                if not _equal_values(expected_cell.value, actual_cell.value):
                    reasons.append("value")
                if compare_styles:
                    if expected_cell.number_format != actual_cell.number_format:
                        reasons.append("number_format")
                    if expected_cell._style != actual_cell._style:
                        reasons.append("style")
                if reasons and len(differences) < max_differences:
                    differences.append(
                        {
                            "sheet": sheet,
                            "cell": coordinate,
                            "expected": repr(expected_cell.value),
                            "actual": repr(actual_cell.value),
                            "reasons": reasons,
                        }
                    )
    finally:
        expected_book.close()
        actual_book.close()
    return Comparison(not differences, checked, tuple(differences))


class VerifiedBenchmarkRunner:
    """Run isolated agents with bounded concurrency and resumable durable results."""

    def __init__(
        self,
        config: ProviderConfig,
        output_dir: Path,
        *,
        skill_registry: SkillRegistry | None = None,
        max_turns: int = 30,
        max_output_tokens: int = 16_000,
        enable_code: bool = True,
        recalculate: bool = True,
        workers: int = 1,
        task_timeout_seconds: float | None = 7200.0,
        task_retries: int = 1,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be between 1 and 16")
        if config.request_interval_seconds > 0 and workers != 1:
            raise ValueError(
                "Non-zero request pacing requires benchmark workers=1 because pacing is "
                "process-local"
            )
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if task_timeout_seconds is not None and task_timeout_seconds <= 0:
            raise ValueError("task_timeout_seconds must be positive")
        if task_retries < 0 or task_retries > 5:
            raise ValueError("task_retries must be between 0 and 5")
        if circuit_breaker_threshold < 1:
            raise ValueError("circuit_breaker_threshold must be positive")
        self.config = config
        self.output_dir = output_dir.resolve()
        self.skill_registry = skill_registry.freeze() if skill_registry is not None else None
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        self.enable_code = enable_code
        self.recalculate = recalculate
        self.workers = workers
        self.task_timeout_seconds = task_timeout_seconds
        self.task_retries = task_retries
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self._pacing_scope_id = uuid.uuid4().hex
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = self.output_dir / "results.jsonl"
        self.manifest_path = self.output_dir / "benchmark-manifest.json"
        self.lock_path = self.output_dir / ".benchmark.lock"
        self.recovered_invalid_rows = 0

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError(
                    f"Another benchmark process is already using {self.output_dir}"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _manifest(self, tasks: list[SpreadsheetTask]) -> dict[str, Any]:
        skills = []
        if self.skill_registry is not None:
            skills = [
                {"name": skill.name, "sha256": skill.sha256}
                for skill in self.skill_registry.discover()
            ]
        return {
            "schema_version": BENCHMARK_MANIFEST_SCHEMA_VERSION,
            "benchmark_protocol_version": BENCHMARK_PROTOCOL_VERSION,
            "dataset_revision": f"KAKA22/SpreadsheetBench@{VERIFIED_REVISION}",
            "dataset_archive_sha256": VERIFIED_SHA256,
            "protocol": "agent_per_workbook",
            "task_count": len(tasks),
            "task_ids": [task.task_id for task in tasks],
            "tasks": [
                {
                    "task_id": task.task_id,
                    "instruction_sha256": _text_sha256(task.instruction),
                    "instruction_type": task.instruction_type,
                    "answer_position": task.answer_position,
                    "answer_sheet": task.answer_sheet,
                    "input_sha256": _sha256(task.input_path),
                    "golden_sha256": _sha256(task.golden_path),
                }
                for task in tasks
            ],
            "harness_source": _source_fingerprint(),
            "runtime": _runtime_fingerprint(),
            "configuration": {
                "model": self.config.model,
                "requested_reasoning_effort": (
                    self.config.requested_reasoning_effort or self.config.reasoning_effort
                ),
                "reasoning_effort": self.config.reasoning_effort,
                "provider_base_url": self.config.base_url,
                "request_timeout_seconds": self.config.timeout_seconds,
                "request_retries": self.config.max_retries,
                "request_interval_seconds": self.config.request_interval_seconds,
                "request_pacing_policy": PACING_POLICY,
                "request_pacing_scope": "single_worker_process",
                "request_pacing_retries_included": True,
                "request_pacing_first_attempt_immediate": True,
                "store_responses": self.config.store_responses,
                "max_turns": self.max_turns,
                "max_output_tokens": self.max_output_tokens,
                "context_policy": CONTEXT_POLICY,
                "workers": self.workers,
                "task_timeout_seconds": self.task_timeout_seconds,
                "task_retries": self.task_retries,
                "circuit_breaker_threshold": self.circuit_breaker_threshold,
                "enable_code": self.enable_code,
                "recalculate": self.recalculate,
                "skills": skills,
            },
        }

    def _prepare_manifest(self, tasks: list[SpreadsheetTask]) -> None:
        expected = self._manifest(tasks)
        if self.manifest_path.is_file():
            try:
                actual = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise HarnessError(f"Invalid benchmark manifest: {self.manifest_path}") from exc
            if actual != expected:
                raise HarnessError(
                    "Refusing to resume with a different model, effort, task set, or run config"
                )
            return
        if self.results_path.is_file() and self.results_path.stat().st_size:
            raise HarnessError(
                f"Results exist without a compatibility manifest: {self.results_path}"
            )
        _atomic_write_json(self.manifest_path, expected)

    def _rows(self) -> list[dict[str, Any]]:
        return _valid_jsonl_rows(self.results_path)[0]

    def _latest_rows(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self._rows():
            if row.get("task_id") is not None:
                latest[str(row["task_id"])] = row
        return latest

    def _attempt_counts(self) -> Counter[str]:
        return Counter(str(row.get("task_id")) for row in self._rows() if row.get("task_id"))

    def _final_ids(self) -> set[str]:
        attempts = self._attempt_counts()
        final: set[str] = set()
        for task_id, row in self._latest_rows().items():
            if row.get("status") == "completed":
                final.add(task_id)
            elif row.get("error_retryable") is not True:
                final.add(task_id)
            elif attempts[task_id] >= self.task_retries + 1:
                final.add(task_id)
        return final

    def _append_result(self, row: dict[str, Any]) -> None:
        encoded = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        descriptor = os.open(
            self.results_path,
            os.O_CREAT | os.O_WRONLY | os.O_APPEND,
            0o600,
        )
        try:
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _task_directory(self, task_id: str, attempt: int) -> Path:
        name = task_id if attempt == 1 else f"{task_id}-retry-{attempt}"
        path = self.output_dir / "runs" / name
        if path.exists():
            path = path.with_name(f"{name}-{uuid.uuid4().hex[:8]}")
        return path

    def _run_task(self, task: SpreadsheetTask, attempt: int) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started_clock = monotonic()
        task_dir = self._task_directory(task.task_id, attempt)
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "task_attempt": attempt,
            "instruction_type": task.instruction_type,
            "answer_position": task.answer_position,
            "protocol": task.protocol,
            "benchmark_protocol_version": BENCHMARK_PROTOCOL_VERSION,
            "model": self.config.model,
            "requested_reasoning_effort": (
                self.config.requested_reasoning_effort or self.config.reasoning_effort
            ),
            "reasoning_effort": self.config.reasoning_effort,
            "provider_base_url": self.config.base_url,
            "request_timeout_seconds": self.config.timeout_seconds,
            "request_retries": self.config.max_retries,
            "request_interval_seconds": self.config.request_interval_seconds,
            "request_pacing_scope": "single_worker_process",
            "max_turns": self.max_turns,
            "max_output_tokens": self.max_output_tokens,
            "task_timeout_seconds": self.task_timeout_seconds,
            "calculation_backend": "libreoffice" if self.recalculate else "not_recalculated",
            "run_dir": str(task_dir),
            "started_at": started_at.isoformat(),
        }
        session: WorkbookSession | None = None
        try:
            session = WorkbookSession.create(task.input_path, task_dir, run_id=task.task_id)
            tools = SpreadsheetToolRegistry(session, enable_code=self.enable_code)
            pacer = _process_pacer(
                self._pacing_scope_id, self.config.request_interval_seconds
            )
            session.recorder.record(
                "benchmark.configured",
                {
                    "schema_version": BENCHMARK_MANIFEST_SCHEMA_VERSION,
                    "benchmark_protocol_version": BENCHMARK_PROTOCOL_VERSION,
                    "request_interval_seconds": self.config.request_interval_seconds,
                    "request_pacing_policy": PACING_POLICY,
                    "request_pacing_scope": "single_worker_process",
                    "max_turns": self.max_turns,
                    "max_output_tokens_per_call": self.max_output_tokens,
                    "task_timeout_seconds": self.task_timeout_seconds,
                },
            )
            agent = SpreadsheetAgent(
                self.config,
                tools,
                skills=self.skill_registry,
                max_turns=self.max_turns,
                max_output_tokens=self.max_output_tokens,
                max_elapsed_seconds=self.task_timeout_seconds,
                pacer=pacer,
            )
            agent_result = agent.run(task.instruction)
            recalc_metadata: dict[str, Any] | None = None
            if self.recalculate:
                from .render import recalculate_workbook

                recalc_metadata = recalculate_workbook(session.workbook_path, session.workbook_path)
            comparison = compare_workbooks(
                task.golden_path,
                session.workbook_path,
                task.answer_position,
                answer_sheet=task.answer_sheet,
            )
            row.update(
                {
                    "status": "completed",
                    "passed": comparison.passed,
                    "comparison": comparison.to_dict(),
                    "agent": agent_result.to_dict(),
                    "recalculation": recalc_metadata,
                    "output_workbook": str(session.workbook_path),
                    "output_sha256": _sha256(session.workbook_path),
                }
            )
            session.recorder.record(
                "benchmark.evaluated",
                {
                    "task_id": task.task_id,
                    "passed": comparison.passed,
                    "status": "completed",
                    "scorer": "cleanroom-corrected-value-v1",
                    "style_checked": False,
                    "calculation_backend": row["calculation_backend"],
                    **comparison_evidence(comparison),
                    "scoring_metadata_sha256": _text_sha256(
                        json.dumps(
                            {
                                "answer_position": task.answer_position,
                                "answer_sheet": task.answer_sheet,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                },
            )
        except Exception as exc:
            safe_error = str(exc).replace(self.config.api_key, "[REDACTED]")
            row.update(
                {
                    "status": "error",
                    "passed": False,
                    "error": safe_error,
                    "error_type": type(exc).__name__,
                    "error_retryable": False,
                    "error_category": "harness",
                }
            )
            if isinstance(exc, ProviderError):
                row.update(
                    {
                        "error_retryable": bool(exc.safe_to_retry),
                        "error_category": (
                            "provider_transient"
                            if exc.retryable
                            else "provider_fatal"
                            if exc.global_fatal
                            else "provider_task"
                        ),
                        "provider_error": exc.public_dict(secrets=(self.config.api_key,)),
                    }
                )
            elif isinstance(exc, AgentTimeoutError):
                row["error_category"] = "task_timeout"
        finally:
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            row["elapsed_seconds"] = round(monotonic() - started_clock, 3)
            if session is not None:
                if row.get("status") != "completed":
                    try:
                        session.recorder.record(
                            "benchmark.not_evaluated",
                            {
                                "task_id": task.task_id,
                                "status": str(row.get("status", "error")),
                                "scorer": "cleanroom-corrected-value-v1",
                                "style_checked": False,
                                "calculation_backend": row["calculation_backend"],
                                "error_category": row.get("error_category"),
                            },
                        )
                    except Exception:
                        pass
                task_manifest = asdict(task)
                task_manifest["input_path"] = str(task.input_path)
                task_manifest["golden_path"] = str(task.golden_path)
                try:
                    session.write_manifest({"task": task_manifest, "result": row})
                except Exception:
                    pass
        return row

    def run(self, tasks: Iterable[SpreadsheetTask], *, resume: bool = True) -> dict[str, Any]:
        task_list = list(tasks)
        task_ids = [task.task_id for task in task_list]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Benchmark task IDs must be unique")
        with self._exclusive_lock():
            self.recovered_invalid_rows = _repair_jsonl(self.results_path)
            self._prepare_manifest(task_list)
            if not resume and self.results_path.is_file() and self.results_path.stat().st_size:
                raise HarnessError("--no-resume requires an empty output directory")
            attempts = self._attempt_counts()
            final_ids = self._final_ids() if resume else set()
            pending: deque[tuple[SpreadsheetTask, int]] = deque(
                (task, attempts[task.task_id] + 1)
                for task in task_list
                if task.task_id not in final_ids
            )
            finalized = set(final_ids)
            exhausted_transient_tasks = 0
            circuit_breaker_tripped = False
            futures: dict[Future[dict[str, Any]], tuple[SpreadsheetTask, int]] = {}

            # Process isolation keeps LibreOffice and the local code interpreter
            # independent. In particular, code execution uses POSIX resource
            # limits in a child process, which is unsafe to fork from worker threads.
            with ProcessPoolExecutor(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
            ) as executor:
                while pending or futures:
                    while (
                        pending
                        and len(futures) < self.workers
                        and not circuit_breaker_tripped
                    ):
                        task, attempt = pending.popleft()
                        future = executor.submit(self._run_task, task, attempt)
                        futures[future] = (task, attempt)
                    if not futures:
                        break
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        task, attempt = futures.pop(future)
                        try:
                            row = future.result()
                        except Exception as exc:
                            safe_error = str(exc).replace(self.config.api_key, "[REDACTED]")
                            now = datetime.now(timezone.utc).isoformat()
                            row = {
                                "task_id": task.task_id,
                                "task_attempt": attempt,
                                "instruction_type": task.instruction_type,
                                "answer_position": task.answer_position,
                                "protocol": task.protocol,
                                "benchmark_protocol_version": BENCHMARK_PROTOCOL_VERSION,
                                "model": self.config.model,
                                "requested_reasoning_effort": (
                                    self.config.requested_reasoning_effort
                                    or self.config.reasoning_effort
                                ),
                                "reasoning_effort": self.config.reasoning_effort,
                                "provider_base_url": self.config.base_url,
                                "request_timeout_seconds": self.config.timeout_seconds,
                                "request_retries": self.config.max_retries,
                                "request_interval_seconds": (
                                    self.config.request_interval_seconds
                                ),
                                "request_pacing_scope": "single_worker_process",
                                "max_turns": self.max_turns,
                                "max_output_tokens": self.max_output_tokens,
                                "task_timeout_seconds": self.task_timeout_seconds,
                                "calculation_backend": (
                                    "libreoffice"
                                    if self.recalculate
                                    else "not_recalculated"
                                ),
                                "status": "error",
                                "passed": False,
                                "error": f"Worker process failed: {safe_error}",
                                "error_type": type(exc).__name__,
                                "error_retryable": False,
                                "error_category": "worker_process",
                                "started_at": now,
                                "finished_at": now,
                                "elapsed_seconds": 0.0,
                            }
                        self._append_result(row)

                        category = row.get("error_category")
                        retry_eligible = (
                            row.get("status") == "error"
                            and row.get("error_retryable") is True
                            and attempt < self.task_retries + 1
                        )
                        if category == "provider_transient" and not retry_eligible:
                            exhausted_transient_tasks += 1
                        if category == "provider_fatal" or (
                            exhausted_transient_tasks >= self.circuit_breaker_threshold
                        ):
                            circuit_breaker_tripped = True
                        should_retry = retry_eligible and not circuit_breaker_tripped
                        if should_retry:
                            pending.appendleft((task, attempt + 1))
                        else:
                            finalized.add(task.task_id)

                        progress = {
                            "event": "benchmark.task_finished",
                            "task_id": task.task_id,
                            "task_attempt": attempt,
                            "status": row.get("status"),
                            "passed": row.get("passed"),
                            "error_category": category,
                            "elapsed_seconds": row.get("elapsed_seconds"),
                            "finalized": len(finalized),
                            "expected": len(task_list),
                            "retry_eligible": retry_eligible,
                            "retry_queued": should_retry,
                            "circuit_breaker_tripped": circuit_breaker_tripped,
                        }
                        print(json.dumps(progress, ensure_ascii=False), flush=True)

            summary = summarize_results(
                self.results_path,
                expected_task_ids=task_ids,
                write_summary=False,
            )
            summary.update(
                {
                    "dataset_revision": f"KAKA22/SpreadsheetBench@{VERIFIED_REVISION}",
                    "selected_task_count": len(task_list),
                    "circuit_breaker_tripped": circuit_breaker_tripped,
                    "exhausted_transient_tasks": exhausted_transient_tasks,
                    "recovered_invalid_result_rows": self.recovered_invalid_rows,
                }
            )
            _atomic_write_json(self.output_dir / "summary.json", summary)
            return summary


def summarize_results(
    results_path: str | Path,
    *,
    expected_task_ids: Iterable[str] | None = None,
    write_summary: bool = True,
) -> dict[str, Any]:
    path = Path(results_path)
    comparison_manifest_path = path.with_name("comparison-manifest.json")
    if comparison_manifest_path.is_file():
        raise HarnessError(
            "Refusing to summarize comparison results as a single-arm benchmark: "
            f"{comparison_manifest_path}"
        )
    rows_from_disk, invalid_rows = _valid_jsonl_rows(path)
    latest: dict[str, dict[str, Any]] = {}
    anonymous = 0
    for row in rows_from_disk:
        task_id = row.get("task_id")
        if task_id is None:
            anonymous += 1
            task_id = f"__anonymous_{anonymous}"
        latest[str(task_id)] = row
    if expected_task_ids is None:
        manifest_path = path.with_name("benchmark-manifest.json")
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                expected_task_ids = [str(item) for item in manifest.get("task_ids", [])]
            except (json.JSONDecodeError, TypeError):
                expected_task_ids = None
    expected = list(dict.fromkeys(str(item) for item in (expected_task_ids or latest.keys())))
    rows = [latest[task_id] for task_id in expected if task_id in latest]
    completed = [row for row in rows if row.get("status") == "completed"]
    scores = [1.0 if row.get("passed") else 0.0 for row in completed]
    passed = int(sum(scores))
    denominator = len(expected)
    attempted_score = passed / denominator if denominator else 0.0
    error_categories = Counter(
        str(row.get("error_category") or "unspecified")
        for row in rows
        if row.get("status") != "completed"
    )
    known_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    successful_request_retries = 0
    for row in completed:
        agent = row.get("agent") or {}
        usage = agent.get("usage") or {}
        for key in known_usage:
            known_usage[key] += int(usage.get(key, 0) or 0)
        for timing in agent.get("request_timings") or []:
            successful_request_retries += max(int(timing.get("attempts", 1) or 1) - 1, 0)
    summary = {
        "protocol": "agent_per_workbook",
        "scorer": "cleanroom-corrected-value-v1",
        "style_checked": False,
        "calculation_backend": sorted({row.get("calculation_backend") for row in rows}),
        "attempted": len(rows),
        "expected": denominator,
        "missing": denominator - len(rows),
        "missing_task_ids": [task_id for task_id in expected if task_id not in latest],
        "completed": len(completed),
        "errors": len(rows) - len(completed),
        "error_categories": dict(sorted(error_categories.items())),
        "known_completed_usage_lower_bound": known_usage,
        "successful_request_retries": successful_request_retries,
        "invalid_result_rows_ignored": invalid_rows,
        "completion_rate": len(completed) / denominator if denominator else 0.0,
        "passed": passed,
        "verified_accuracy": attempted_score,
        "completed_accuracy": fmean(scores) if scores else 0.0,
        "soft": attempted_score,
        "hard": attempted_score,
    }
    if write_summary:
        _atomic_write_json(path.with_name("summary.json"), summary)
    return summary
