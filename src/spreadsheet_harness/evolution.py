"""Candidate-only skill evolution from redacted trajectories.

The generation path in this module deliberately cannot write to a production
skill directory.  It publishes an auditable candidate under
``candidates/<candidate-id>/``.  Promotion is a separate, explicit operation
with paired-seed validation gates.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from .agent import ResponsesClient
from .trajectory import read_trajectory

_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token|password|secret)", re.I
)
_SECRET_VALUE = re.compile(
    r"\b(?:cr|sk)-[A-Za-z0-9_-]{12,}\b|\bcr_[A-Za-z0-9_-]{12,}\b|"
    r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
)
_SEVERE_WORD = re.compile(r"\b(?:severe|serious|critical|fatal|blocker)\b", re.I)
_SKILL_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

_LESSON_INSTRUCTIONS = """You analyze one redacted spreadsheet-agent trajectory.
Derive concise, reusable operating lessons grounded only in the supplied evidence.
Separate: what worked, what failed, tool errors and their likely prevention, and
verification steps. The evaluator outcome, when present, is the authoritative task
correctness signal; agent completion and committed file mutations are not proof of
correctness. If no evaluator outcome is present, do not infer that the task passed.
Do not invent events, credentials, benchmark results, or file contents. Return
Markdown only; do not return a SKILL.md yet."""

_CONSOLIDATION_INSTRUCTIONS = """Consolidate trajectory lessons into one candidate
SKILL.md for a spreadsheet editing agent. Resolve contradictions conservatively,
prefer repeated evidence, retain failure-prevention and verification procedures,
and do not claim validation or production status. Return only the complete SKILL.md
with YAML frontmatter containing name and description. Do not wrap it in a code
fence and do not include secrets or provenance metadata."""


class PromotionRejected(ValueError):
    """Raised when a candidate does not satisfy every promotion gate."""


@dataclass(frozen=True)
class TrajectoryEvidence:
    """Evidence extracted from one already-redacted trajectory JSONL file."""

    source: Path
    sha256: str
    event_count: int
    run_ids: tuple[str, ...]
    successes: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]
    tool_errors: tuple[dict[str, Any], ...]
    evaluator_outcome: dict[str, Any] | None

    def for_prompt(self) -> dict[str, Any]:
        """Return the evidence subset sent to the lesson-generation stage."""

        return {
            "trajectory": self.source.name,
            "sha256": self.sha256,
            "event_count": self.event_count,
            "run_ids": list(self.run_ids),
            "success_evidence": list(self.successes),
            "failure_evidence": list(self.failures),
            "tool_error_evidence": list(self.tool_errors),
            "evaluator_outcome": self.evaluator_outcome,
        }


@dataclass(frozen=True)
class Candidate:
    """A generated skill candidate and its audit artifacts."""

    candidate_id: str
    path: Path
    skill_path: Path
    provenance_path: Path
    lessons_path: Path
    sha256: str


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact(value: Any, key: str | None = None) -> Any:
    """Apply a second defensive redaction pass before model prompting."""

    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return repr(value)


def _bounded(value: Any, *, string_limit: int = 4_000, list_limit: int = 50) -> Any:
    """Bound evidence size without changing the original trajectory."""

    value = _redact(value)
    if isinstance(value, dict):
        return {
            key: _bounded(item, string_limit=string_limit, list_limit=list_limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        bounded = [
            _bounded(item, string_limit=string_limit, list_limit=list_limit)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            bounded.append({"truncated_items": len(value) - list_limit})
        return bounded
    if isinstance(value, str) and len(value) > string_limit:
        return value[:string_limit] + f"...[truncated {len(value) - string_limit} chars]"
    return value


def _event_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event": str(row.get("event", "")),
        "run_id": str(row.get("run_id", "")),
        "timestamp": str(row.get("timestamp", "")),
        "payload": _bounded(row.get("payload") if isinstance(row.get("payload"), dict) else {}),
    }


def _status_value(payload: Mapping[str, Any]) -> str:
    for key in ("status", "outcome", "result"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip().lower()
    return ""


def _is_success_event(event: str, payload: Mapping[str, Any]) -> bool:
    lowered = event.lower()
    if lowered.endswith((".succeeded", ".success", ".passed")):
        return True
    if payload.get("success") is True or payload.get("passed") is True:
        return True
    return _status_value(payload) in {"success", "succeeded", "passed", "pass"}


def _is_failure_event(event: str, payload: Mapping[str, Any]) -> bool:
    lowered = event.lower()
    if lowered.startswith("tool."):
        return False
    if lowered.endswith((".failed", ".failure", ".error", ".rolled_back")):
        return True
    if payload.get("success") is False or payload.get("passed") is False:
        return True
    return _status_value(payload) in {"failure", "failed", "error", "rolled_back", "rejected"}


def _evaluator_outcome(event: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return an explicit evaluator verdict without treating agent state as correctness."""

    lowered = event.lower()
    if lowered not in {"benchmark.evaluated", "evaluation.completed", "evaluation.failed"}:
        return None
    passed = payload.get("passed")
    if not isinstance(passed, bool):
        return None
    return {
        "event": event,
        "passed": passed,
        "payload": _bounded(payload),
    }


def _tool_error(
    row: Mapping[str, Any], pending_calls: dict[tuple[str, str], list[dict[str, Any]]]
) -> dict[str, Any] | None:
    event = str(row.get("event", ""))
    lowered = event.lower()
    if not lowered.startswith("tool."):
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    run_id = str(row.get("run_id", ""))
    name = str(payload.get("name", ""))

    if lowered == "tool.called":
        pending_calls.setdefault((run_id, name), []).append(
            _bounded(payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {})
        )
        return None

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    calls = pending_calls.get((run_id, name), [])
    arguments = calls.pop() if calls else {}
    has_error = (
        result.get("ok") is False
        or result.get("success") is False
        or bool(result.get("error"))
        or _status_value(result) in {"failure", "failed", "error", "rejected"}
        or lowered.endswith((".failed", ".failure", ".error"))
    )
    if not has_error:
        return None

    return {
        "event": event,
        "run_id": run_id,
        "timestamp": str(row.get("timestamp", "")),
        "tool": name,
        "arguments": arguments,
        "error": _bounded(result.get("error", "unknown tool error")),
        "error_type": _bounded(result.get("type", "")),
    }


def extract_trajectory_evidence(
    trajectory: str | Path, *, max_items_per_category: int = 50
) -> TrajectoryEvidence:
    """Extract success, failure, and tool-error evidence from redacted JSONL.

    The exact file bytes are hashed for provenance.  Evidence is defensively
    redacted and bounded before it can be used in a model prompt.
    """

    if max_items_per_category < 1:
        raise ValueError("max_items_per_category must be at least 1")
    source = Path(trajectory).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    input_hash = _sha256_path(source)
    rows = read_trajectory(source)
    if _sha256_path(source) != input_hash:
        raise ValueError(f"Trajectory changed while it was being read: {source}")
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    tool_errors: list[dict[str, Any]] = []
    pending_calls: dict[tuple[str, str], list[dict[str, Any]]] = {}
    run_ids: set[str] = set()
    evaluator_outcome: dict[str, Any] | None = None

    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = row.get("run_id")
        if run_id is not None:
            run_ids.add(str(run_id))
        tool_error = _tool_error(row, pending_calls)
        if tool_error is not None and len(tool_errors) < max_items_per_category:
            tool_errors.append(tool_error)

        event = str(row.get("event", ""))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        outcome = _evaluator_outcome(event, payload)
        if outcome is not None:
            if (
                evaluator_outcome is not None
                and evaluator_outcome["passed"] != outcome["passed"]
            ):
                raise ValueError(f"Conflicting evaluator outcomes in trajectory: {source}")
            evaluator_outcome = outcome
        is_failure = _is_failure_event(event, payload)
        if is_failure and len(failures) < max_items_per_category:
            failures.append(_event_item(row))
        elif _is_success_event(event, payload) and len(successes) < max_items_per_category:
            successes.append(_event_item(row))

    return TrajectoryEvidence(
        source=source,
        sha256=input_hash,
        event_count=len(rows),
        run_ids=tuple(sorted(run_ids)),
        successes=tuple(successes),
        failures=tuple(failures),
        tool_errors=tuple(tool_errors),
        evaluator_outcome=evaluator_outcome,
    )


def extract_evidence(
    trajectories: Iterable[str | Path], *, max_items_per_category: int = 50
) -> list[TrajectoryEvidence]:
    """Extract evidence for multiple trajectories in caller-provided order."""

    return [
        extract_trajectory_evidence(path, max_items_per_category=max_items_per_category)
        for path in trajectories
    ]


def _response_text(response: Any, stage: str) -> tuple[str, str | None]:
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"ResponsesClient returned empty text during {stage}")
    response_id = getattr(response, "response_id", None)
    return _SECRET_VALUE.sub("[REDACTED]", text.strip()), (
        str(response_id) if response_id is not None else None
    )


def _model_payload(
    model: str, instructions: str, text: str, max_output_tokens: int
) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        "max_output_tokens": max_output_tokens,
        "store": False,
    }


def _normalize_skill(text: str) -> str:
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", stripped, re.DOTALL | re.I)
    if fence:
        stripped = fence.group(1).strip()
    stripped = _SECRET_VALUE.sub("[REDACTED]", stripped)
    if not _SKILL_FRONTMATTER.match(stripped):
        raise ValueError("Candidate response must be a complete SKILL.md with YAML frontmatter")
    return stripped + "\n"


def _safe_candidate_id(
    candidate_id: str | None, evidences: Sequence[TrajectoryEvidence], model: str
) -> str:
    if candidate_id is None:
        digest = hashlib.sha256(
            (model + "\n" + "\n".join(item.sha256 for item in evidences)).encode("utf-8")
        ).hexdigest()[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate_id = f"{stamp}-{digest}"
    if not _CANDIDATE_ID.fullmatch(candidate_id) or candidate_id in {".", ".."}:
        raise ValueError("candidate_id must be a safe path component")
    return candidate_id


def _candidate_parent(output_root: str | Path) -> Path:
    root = Path(output_root).expanduser().resolve()
    return root if root.name == "candidates" else root / "candidates"


def generate_candidate(
    trajectories: Iterable[str | Path],
    output_root: str | Path,
    client: ResponsesClient,
    *,
    model: str | None = None,
    candidate_id: str | None = None,
    skill_name: str = "spreadsheet-core",
    lesson_max_output_tokens: int = 4_000,
    consolidation_max_output_tokens: int = 8_000,
) -> Candidate:
    """Generate per-trajectory lessons, then consolidate a candidate SKILL.md.

    ``output_root`` may be either a workspace root or its ``candidates``
    directory.  This function has no production-skill argument and writes only
    beneath ``candidates/<candidate-id>/``.
    """

    evidences = extract_evidence(trajectories)
    if not evidences:
        raise ValueError("At least one trajectory is required")
    unevaluated = [item.source.name for item in evidences if item.evaluator_outcome is None]
    if unevaluated:
        raise ValueError(
            "Every trajectory must contain one explicit evaluator outcome; missing from: "
            + ", ".join(unevaluated)
        )
    resolved_model = model or getattr(getattr(client, "config", None), "model", None)
    if not isinstance(resolved_model, str) or not resolved_model.strip():
        raise ValueError("model is required for generation and provenance")
    resolved_model = resolved_model.strip()
    client_config = getattr(client, "config", None)
    apply_generation = getattr(client_config, "apply_generation", None)
    generation_dict = getattr(client_config, "generation_dict", None)
    generation = generation_dict() if callable(generation_dict) else {}

    def model_payload(instructions: str, text: str, max_output_tokens: int) -> dict[str, Any]:
        payload = _model_payload(resolved_model, instructions, text, max_output_tokens)
        return apply_generation(payload) if callable(apply_generation) else payload

    if not skill_name.strip():
        raise ValueError("skill_name must not be empty")
    if lesson_max_output_tokens < 1 or consolidation_max_output_tokens < 1:
        raise ValueError("max output token limits must be positive")
    resolved_id = _safe_candidate_id(candidate_id, evidences, resolved_model)
    candidate_parent = _candidate_parent(output_root)
    candidate_path = candidate_parent / resolved_id
    if candidate_path.exists():
        raise FileExistsError(f"Candidate already exists: {candidate_path}")

    lessons: list[dict[str, Any]] = []
    for evidence in evidences:
        prompt = json.dumps(evidence.for_prompt(), ensure_ascii=False, sort_keys=True)
        response = client.create(
            model_payload(_LESSON_INSTRUCTIONS, prompt, lesson_max_output_tokens)
        )
        lesson, response_id = _response_text(response, "lesson generation")
        lessons.append(
            {
                "trajectory": evidence.source.name,
                "input_sha256": evidence.sha256,
                "response_id": response_id,
                "lesson": lesson,
            }
        )

    consolidation_input = json.dumps(
        {
            "skill_name": skill_name.strip(),
            "trajectory_lessons": [
                {"input_sha256": item["input_sha256"], "lesson": item["lesson"]} for item in lessons
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    consolidation = client.create(
        model_payload(
            _CONSOLIDATION_INSTRUCTIONS,
            consolidation_input,
            consolidation_max_output_tokens,
        )
    )
    skill_text_raw, consolidation_response_id = _response_text(
        consolidation, "lesson consolidation"
    )
    skill_text = _normalize_skill(skill_text_raw)
    skill_hash = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()
    if candidate_path.exists():
        raise FileExistsError(f"Candidate already exists: {candidate_path}")

    provenance = {
        "schema_version": 1,
        "candidate_id": resolved_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": resolved_model,
        "generation": generation,
        "skill_name": skill_name.strip(),
        "inputs": [{"trajectory": item.source.name, "sha256": item.sha256} for item in evidences],
        "input_hashes": [item.sha256 for item in evidences],
        "lesson_response_ids": [item["response_id"] for item in lessons],
        "consolidation_response_id": consolidation_response_id,
        "candidate_sha256": skill_hash,
    }

    candidate_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{resolved_id}.", dir=candidate_parent))
    try:
        (staging / "SKILL.md").write_text(skill_text, encoding="utf-8")
        (staging / "lessons.json").write_text(
            json.dumps(lessons, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, candidate_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return Candidate(
        candidate_id=resolved_id,
        path=candidate_path,
        skill_path=candidate_path / "SKILL.md",
        provenance_path=candidate_path / "provenance.json",
        lessons_path=candidate_path / "lessons.json",
        sha256=skill_hash,
    )


def _load_report(validation_report: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(validation_report, Mapping):
        return dict(validation_report)
    report_path = Path(validation_report).expanduser().resolve()
    try:
        loaded = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromotionRejected(f"Invalid validation report JSON: {report_path}") from exc
    if not isinstance(loaded, dict):
        raise PromotionRejected("Validation report must be a JSON object")
    return loaded


def _flagged(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"", "false", "no", "none", "0", "clear", "ok"}:
            return False
        return lowered in {"true", "yes", "1"} or bool(_SEVERE_WORD.search(lowered))
    if isinstance(value, list | tuple | set | dict):
        return bool(value)
    return bool(value)


def _has_severe_regression(report: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> bool:
    direct_keys = (
        "severe_regression",
        "serious_regression",
        "critical_regression",
        "has_severe_regression",
        "has_serious_regression",
        "severe_regressions",
        "serious_regressions",
        "critical_regressions",
    )
    if any(_flagged(report.get(key)) for key in direct_keys):
        return True

    def flags_in(container: Mapping[str, Any]) -> bool:
        if any(_flagged(container.get(key)) for key in direct_keys):
            return True
        for key, value in container.items():
            normalized_key = str(key).replace("-", "_").lower()
            if "regress" in normalized_key and _SEVERE_WORD.search(normalized_key):
                if _flagged(value):
                    return True
        severity = container.get("severity") or container.get("regression_severity")
        if isinstance(severity, str) and _SEVERE_WORD.search(severity):
            return True
        for key in ("flags", "regression_flags"):
            flags = container.get(key)
            if isinstance(flags, str) and _SEVERE_WORD.search(flags):
                return True
            if isinstance(flags, Sequence) and not isinstance(flags, str | bytes):
                if any(isinstance(flag, str) and _SEVERE_WORD.search(flag) for flag in flags):
                    return True
        regressions = container.get("regressions")
        if isinstance(regressions, Sequence) and not isinstance(regressions, str | bytes):
            return any(isinstance(item, Mapping) and flags_in(item) for item in regressions)
        if isinstance(regressions, Mapping):
            return flags_in(regressions)
        return False

    return flags_in(report) or any(flags_in(row) for row in rows)


def _score(value: Any, label: str) -> float:
    if isinstance(value, Mapping):
        for key in ("score", "value", "mean"):
            if key in value:
                value = value[key]
                break
    if isinstance(value, bool):
        raise PromotionRejected(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PromotionRejected(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise PromotionRejected(f"{label} must be finite")
    return number


def _rows_from_score_collections(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    seeds = report.get("seeds") or report.get("paired_seeds")
    baseline = report.get("baseline_scores", report.get("baseline"))
    candidate = report.get("candidate_scores", report.get("candidate"))
    if isinstance(baseline, Mapping) and "scores" in baseline:
        baseline = baseline["scores"]
    if isinstance(candidate, Mapping) and "scores" in candidate:
        candidate = candidate["scores"]

    if isinstance(baseline, Mapping) and isinstance(candidate, Mapping):
        common = [key for key in baseline if key in candidate]
        return [
            {"seed": key, "baseline": baseline[key], "candidate": candidate[key]} for key in common
        ]
    if (
        isinstance(seeds, Sequence)
        and not isinstance(seeds, str | bytes)
        and isinstance(baseline, Sequence)
        and not isinstance(baseline, str | bytes)
        and isinstance(candidate, Sequence)
        and not isinstance(candidate, str | bytes)
        and len(seeds) == len(baseline) == len(candidate)
    ):
        return [
            {"seed": seed, "baseline": baseline_score, "candidate": candidate_score}
            for seed, baseline_score, candidate_score in zip(
                seeds, baseline, candidate, strict=True
            )
        ]
    return []


def _paired_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("paired_results", "paired", "pairs", "results", "paired_seeds"):
        value = report.get(key)
        if (
            isinstance(value, Sequence)
            and not isinstance(value, str | bytes)
            and value
            and all(isinstance(row, Mapping) for row in value)
        ):
            return [dict(row) for row in value]
    return _rows_from_score_collections(report)


def _validate_pairs(
    report: Mapping[str, Any], min_delta: float
) -> tuple[list[dict[str, Any]], float, float]:
    rows = _paired_rows(report)
    if len(rows) < 3:
        raise PromotionRejected("At least 3 paired seed results are required")
    seen: set[str] = set()
    baseline_scores: list[float] = []
    candidate_scores: list[float] = []
    for index, row in enumerate(rows):
        seed = row.get("seed", row.get("seed_id", row.get("id")))
        if seed is None:
            raise PromotionRejected(f"Paired result {index + 1} is missing a seed")
        seed_key = json.dumps(seed, ensure_ascii=False, sort_keys=True, default=repr)
        if seed_key in seen:
            raise PromotionRejected(f"Duplicate paired seed: {seed}")
        seen.add(seed_key)
        baseline_value = row.get("baseline", row.get("baseline_score"))
        candidate_value = row.get("candidate", row.get("candidate_score"))
        baseline_scores.append(_score(baseline_value, f"baseline score for seed {seed}"))
        candidate_scores.append(_score(candidate_value, f"candidate score for seed {seed}"))

    if len(seen) < 3:
        raise PromotionRejected("At least 3 distinct paired seeds are required")
    if _has_severe_regression(report, rows):
        raise PromotionRejected("Validation report contains a severe regression marker")
    baseline_mean = fmean(baseline_scores)
    candidate_mean = fmean(candidate_scores)
    if not candidate_mean > baseline_mean + min_delta:
        raise PromotionRejected(
            "Candidate mean must be strictly greater than baseline mean + min_delta "
            f"({candidate_mean:.12g} <= {baseline_mean + min_delta:.12g})"
        )
    return rows, baseline_mean, candidate_mean


def _candidate_directory(candidate: Candidate | str | Path) -> Path:
    source = candidate.path if isinstance(candidate, Candidate) else Path(candidate)
    source = source.expanduser().resolve()
    if source.is_file() and source.name == "SKILL.md":
        source = source.parent
    if source.parent.name != "candidates":
        raise PromotionRejected("Candidate must be located at candidates/<id>/")
    if not source.is_dir():
        raise PromotionRejected(f"Candidate directory does not exist: {source}")
    return source


def _verify_candidate_hash(candidate_dir: Path, skill_hash: str, report: Mapping[str, Any]) -> None:
    provenance_path = candidate_dir / "provenance.json"
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PromotionRejected("Candidate provenance is invalid JSON") from exc
        expected = provenance.get("candidate_sha256") if isinstance(provenance, dict) else None
        if expected and expected != skill_hash:
            raise PromotionRejected("Candidate SKILL.md does not match its provenance hash")
    report_hash = report.get("candidate_sha256")
    if report_hash and report_hash != skill_hash:
        raise PromotionRejected("Validation report targets a different candidate hash")


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists():
            os.chmod(temporary, destination.stat().st_mode & 0o777)
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def promote_candidate(
    candidate: Candidate | str | Path,
    skill_root: str | Path,
    validation_report: Mapping[str, Any] | str | Path,
    min_delta: float = 0,
) -> Path:
    """Atomically promote a validated candidate to ``skill_root/SKILL.md``.

    The report must contain at least three distinct paired seeds.  Supported
    canonical rows are ``{"seed": ..., "baseline": ..., "candidate": ...}``
    under ``paired_results`` (``pairs`` and ``results`` are accepted aliases).
    Candidate mean must be strictly greater than baseline mean plus
    ``min_delta``, and neither the report nor a pair may carry a severe/critical
    regression marker.  All gates run before the production file is touched.
    """

    if isinstance(min_delta, bool):
        raise PromotionRejected("min_delta must be a non-negative finite number")
    try:
        delta = float(min_delta)
    except (TypeError, ValueError) as exc:
        raise PromotionRejected("min_delta must be a non-negative finite number") from exc
    if not math.isfinite(delta) or delta < 0:
        raise PromotionRejected("min_delta must be a non-negative finite number")

    candidate_dir = _candidate_directory(candidate)
    skill_path = candidate_dir / "SKILL.md"
    if not skill_path.is_file():
        raise PromotionRejected(f"Candidate SKILL.md is missing: {skill_path}")
    content = skill_path.read_bytes()
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromotionRejected("Candidate SKILL.md must be UTF-8") from exc
    if not _SKILL_FRONTMATTER.match(decoded):
        raise PromotionRejected("Candidate SKILL.md is missing YAML frontmatter")

    report = _load_report(validation_report)
    skill_hash = hashlib.sha256(content).hexdigest()
    _verify_candidate_hash(candidate_dir, skill_hash, report)
    _validate_pairs(report, delta)

    destination = Path(skill_root).expanduser().resolve() / "SKILL.md"
    _atomic_write(destination, content)
    return destination


__all__ = [
    "Candidate",
    "PromotionRejected",
    "TrajectoryEvidence",
    "extract_evidence",
    "extract_trajectory_evidence",
    "generate_candidate",
    "promote_candidate",
]
