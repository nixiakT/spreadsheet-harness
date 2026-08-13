"""Resource-matched, resumable three-arm SpreadsheetBench comparison."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any

from .agent import (
    CONNECT_RETRY_MIN_SECONDS,
    OVERLOAD_RETRY_MIN_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
    SAFE_AUTOMATIC_RETRY_REASONS,
    SAFE_RETRY_HTTP_STATUSES,
    TERMINAL_TOOL_NAME,
)
from .arms import (
    COMPARISON_FORCED_TOOL_PREFIX_POLICY,
    COMPARISON_TURN_CAP_POLICY_VERSION,
    PAPER_TURN_CAP_SCALING_VERSION,
    PaperStageValidationError,
    comparison_stage_turn_caps,
    run_arm,
)
from .benchmark import (
    VERIFIED_REVISION,
    VERIFIED_SHA256,
    SpreadsheetTask,
    _atomic_write_json,
    _repair_jsonl,
    _runtime_fingerprint,
    _sha256,
    _source_fingerprint,
    _text_sha256,
    _valid_jsonl_rows,
    compare_workbooks,
    comparison_evidence,
)
from .budget import RunBudget
from .code_interpreter import STRICT_ISOLATION_POLICY, ensure_strict_code_isolation
from .config import ProviderConfig
from .errors import (
    AgentBudgetError,
    AgentRoutingError,
    AgentTimeoutError,
    CodeIsolationError,
    HarnessError,
    ProviderError,
)
from .pacing import PACING_POLICY, RelayPacer
from .preprocess import (
    DETERMINISTIC_PROFILE_BOUNDS,
    DETERMINISTIC_PROFILE_SCHEMA_VERSION,
    build_deterministic_profile,
)
from .session import WorkbookSession
from .skills import SkillRegistry

DEFAULT_COMPARISON_ARMS = ("bare", "paper", "ours")
AVAILABLE_COMPARISON_ARMS = ("bare", "profile", "native", "paper", "ours")
# Backwards-compatible name for the historical default three-arm protocol.
COMPARISON_ARMS = DEFAULT_COMPARISON_ARMS
COMPARISON_ARM_DISPLAY_NAMES = {
    "bare": "bare",
    "profile": "bare + deterministic profile",
    "native": "native harness without skills",
    "paper": "paper-inspired",
    "ours": "ours",
}
COMPARISON_PROTOCOL_VERSION = "resource_matched_multi_arm_v15"


def _run_key(task_id: str, arm: str) -> str:
    return f"{task_id}::{arm}"


def _arm_order(task_id: str, seed: int, arms: tuple[str, ...]) -> list[str]:
    digest = hashlib.sha256(f"{seed}:{task_id}".encode()).digest()
    offset = int.from_bytes(digest[:4], "big") % len(arms)
    return [*arms[offset:], *arms[:offset]]


def _balanced_arm_orders(
    task_ids: list[str], seed: int, arms: tuple[str, ...]
) -> dict[str, list[str]]:
    """Assign cyclic arm orders with exact or near-exact position balance.

    Hash-ranking makes the assignment independent of dataset row order while the
    cyclic offsets ensure every arm appears in every position equally often (the
    unavoidable difference is at most one when the task count is not divisible
    by the arm count).
    """

    if not arms:
        raise ValueError("arms must not be empty")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task IDs must be unique")
    ranked = sorted(
        task_ids,
        key=lambda task_id: hashlib.sha256(f"{seed}:{task_id}".encode()).digest(),
    )
    orders: dict[str, list[str]] = {}
    for index, task_id in enumerate(ranked):
        offset = index % len(arms)
        orders[task_id] = [*arms[offset:], *arms[:offset]]
    return orders


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return round(float(ordered[index]), 3)


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "median": round(float(median(values)), 3) if values else None,
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3) if values else None,
    }


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return [max(center - margin, 0.0), min(center + margin, 1.0)]


def _mcnemar_exact(left_only: int, right_only: int) -> float:
    discordant = left_only + right_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, index) for index in range(min(left_only, right_only) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def _holm_adjust(raw: dict[str, float]) -> dict[str, float]:
    ordered = sorted(raw.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, value * (total - index)))
        adjusted[name] = running
    return adjusted


def _stratified_bootstrap_delta(
    tasks: list[SpreadsheetTask],
    left: dict[str, bool],
    right: dict[str, bool],
    *,
    seed: int,
    samples: int = 5_000,
) -> list[float]:
    if not tasks:
        return [0.0, 0.0]
    strata: dict[str, list[SpreadsheetTask]] = {}
    for task in tasks:
        strata.setdefault(task.instruction_type, []).append(task)
    generator = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        difference = 0.0
        count = 0
        for members in strata.values():
            for _ in members:
                task = generator.choice(members)
                difference += int(right.get(task.task_id, False)) - int(
                    left.get(task.task_id, False)
                )
                count += 1
        deltas.append(difference / count if count else 0.0)
    deltas.sort()
    lower = deltas[max(math.floor(0.025 * len(deltas)), 0)]
    upper = deltas[min(math.ceil(0.975 * len(deltas)) - 1, len(deltas) - 1)]
    return [lower, upper]


def _usage_from_row(row: dict[str, Any]) -> dict[str, Any]:
    usage = (row.get("agent") or {}).get("usage") or {}
    budget_used = (row.get("budget") or {}).get("used") or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    agent_total = int(usage.get("total_tokens", 0) or 0)
    budget_total = int(budget_used.get("total_tokens", 0) or 0)
    total_tokens = budget_total if budget_total else agent_total
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "source": "budget" if budget_total else "agent" if usage else "none",
        "agent_budget_total_mismatch": bool(
            agent_total and budget_total and agent_total != budget_total
        ),
        "input_output_complete": bool(usage),
    }


def _scoring_metadata_sha256(task: SpreadsheetTask) -> str:
    encoded = json.dumps(
        {
            "answer_position": task.answer_position,
            "answer_sheet": task.answer_sheet,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_sha256(encoded)


def _dataset_manifest_sha256(tasks: list[SpreadsheetTask]) -> str | None:
    candidates: set[Path] = set()
    for task in tasks:
        resolved = task.input_path.resolve()
        if len(resolved.parents) >= 3:
            candidate = resolved.parents[2] / "dataset.json"
            if candidate.is_file():
                candidates.add(candidate)
    if len(candidates) != 1:
        return None
    return _sha256(next(iter(candidates)))


def _task_stratum(task: SpreadsheetTask) -> str:
    if "Cell" in task.instruction_type:
        return "cell"
    if "Sheet" in task.instruction_type:
        return "sheet"
    return "other"


def _arm_subset_summary(
    tasks: list[SpreadsheetTask],
    arm: str,
    latest: dict[str, dict[str, Any]],
    passes: dict[str, bool],
) -> dict[str, Any]:
    arm_rows = [
        latest[_run_key(task.task_id, arm)]
        for task in tasks
        if _run_key(task.task_id, arm) in latest
    ]
    completed = [row for row in arm_rows if row.get("status") == "completed"]
    passed = sum(passes[task.task_id] for task in tasks)
    usage = [_usage_from_row(row) for row in arm_rows]
    elapsed = [float(row.get("elapsed_seconds", 0) or 0) for row in arm_rows]
    calls = [
        float(((row.get("budget") or {}).get("used") or {}).get("model_calls", 0) or 0)
        for row in arm_rows
    ]
    request_attempt_audits: list[dict[str, int | bool]] = []
    for row in arm_rows:
        budget_calls = int(
            ((row.get("budget") or {}).get("used") or {}).get("model_calls", 0)
            or 0
        )
        timings = (row.get("agent") or {}).get("request_timings")
        attempts: list[int] = []
        valid_timings = isinstance(timings, list) and bool(timings)
        if valid_timings:
            for timing in timings:
                if not isinstance(timing, dict):
                    valid_timings = False
                    break
                raw_attempts = timing.get("attempts", 1)
                if (
                    not isinstance(raw_attempts, int)
                    or isinstance(raw_attempts, bool)
                    or raw_attempts < 1
                ):
                    valid_timings = False
                    break
                attempts.append(raw_attempts)
        failed_attempts = (row.get("provider_error") or {}).get("attempts", 0)
        if (
            not isinstance(failed_attempts, int)
            or isinstance(failed_attempts, bool)
            or failed_attempts < 0
        ):
            failed_attempts = 0
        exact = bool(
            row.get("status") == "completed"
            and valid_timings
            and len(attempts) == budget_calls
        )
        known_success_attempts = sum(attempts) if valid_timings else budget_calls
        request_attempt_audits.append(
            {
                "known_http_attempts": known_success_attempts + failed_attempts,
                "known_successful_retries": (
                    sum(attempts) - len(attempts) if valid_timings else 0
                ),
                "known_failed_attempts": failed_attempts,
                "has_audit": bool(valid_timings or budget_calls or failed_attempts),
                "exact": exact,
            }
        )
    errors = Counter(
        str(row.get("error_category") or "unspecified")
        for row in arm_rows
        if row.get("status") != "completed"
    )
    terminations = Counter(
        str(termination.get("reason") or "unspecified")
        for row in arm_rows
        if isinstance((row.get("budget") or {}).get("termination"), dict)
        for termination in [(row.get("budget") or {})["termination"]]
    )
    total_tokens = sum(item["total_tokens"] for item in usage)
    # Complete means every expected arm-task row is present and has an exact audit.
    request_attempt_audit_complete = len(request_attempt_audits) == len(tasks) and all(
        bool(item["exact"]) for item in request_attempt_audits
    )
    return {
        "expected": len(tasks),
        "attempted": len(arm_rows),
        "completed": len(completed),
        "errors": len(arm_rows) - len(completed),
        "missing": len(tasks) - len(arm_rows),
        "passed": passed,
        "end_to_end_accuracy": passed / len(tasks) if tasks else 0.0,
        "wilson_95": _wilson(passed, len(tasks)),
        "completed_only_accuracy": passed / len(completed) if completed else None,
        "completion_rate": len(completed) / len(tasks) if tasks else 0.0,
        "error_categories": dict(sorted(errors.items())),
        "budget_termination_reasons": dict(sorted(terminations.items())),
        "input_tokens": _distribution([item["input_tokens"] for item in usage]),
        "output_tokens": _distribution([item["output_tokens"] for item in usage]),
        "total_tokens": _distribution([item["total_tokens"] for item in usage]),
        "total_tokens_sum": total_tokens,
        "usage_rows_with_input_output": sum(
            item["input_output_complete"] for item in usage
        ),
        "usage_total_mismatches": sum(
            item["agent_budget_total_mismatch"] for item in usage
        ),
        "model_calls": _distribution(calls),
        "known_http_attempts": _distribution(
            [float(item["known_http_attempts"]) for item in request_attempt_audits]
        ),
        "known_http_attempts_sum": sum(
            int(item["known_http_attempts"]) for item in request_attempt_audits
        ),
        "known_successful_request_retries_sum": sum(
            int(item["known_successful_retries"]) for item in request_attempt_audits
        ),
        "known_failed_request_attempts_sum": sum(
            int(item["known_failed_attempts"]) for item in request_attempt_audits
        ),
        "request_attempt_audit_rows": sum(
            bool(item["has_audit"]) for item in request_attempt_audits
        ),
        "request_attempt_audit_complete": request_attempt_audit_complete,
        "elapsed_seconds": _distribution(elapsed),
        "tokens_per_pass": total_tokens / passed if passed else None,
    }


def _pairwise_result(
    tasks: list[SpreadsheetTask],
    left_arm: str,
    right_arm: str,
    latest: dict[str, dict[str, Any]],
    passes: dict[str, dict[str, bool]],
    *,
    seed: int,
) -> dict[str, Any]:
    left_only = sum(
        passes[left_arm][task.task_id] and not passes[right_arm][task.task_id]
        for task in tasks
    )
    right_only = sum(
        passes[right_arm][task.task_id] and not passes[left_arm][task.task_id]
        for task in tasks
    )
    complete_pairs = sum(
        latest.get(_run_key(task.task_id, left_arm), {}).get("status") == "completed"
        and latest.get(_run_key(task.task_id, right_arm), {}).get("status") == "completed"
        for task in tasks
    )
    inference_valid = bool(tasks) and complete_pairs == len(tasks)
    invalid_reasons = (
        []
        if inference_valid
        else ["no_tasks" if not tasks else "incomplete_or_error_arm_task_pairs"]
    )
    return {
        "inference_valid": inference_valid,
        "inference_invalid_reasons": invalid_reasons,
        "expected_pairs": len(tasks),
        "completed_pairs": complete_pairs,
        "accuracy_delta_right_minus_left": (
            sum(passes[right_arm][task.task_id] for task in tasks)
            - sum(passes[left_arm][task.task_id] for task in tasks)
        )
        / len(tasks)
        if tasks
        else 0.0,
        "stratified_bootstrap_95": (
            _stratified_bootstrap_delta(
                tasks,
                passes[left_arm],
                passes[right_arm],
                seed=seed,
            )
            if inference_valid
            else None
        ),
        "left_only_passes": left_only,
        "right_only_passes": right_only,
        "mcnemar_exact_p": (
            _mcnemar_exact(left_only, right_only) if inference_valid else None
        ),
    }


def _invalidate_pairwise_inference(
    result: dict[str, Any], reasons: list[str]
) -> None:
    """Remove inferential fields when collection integrity is not established."""

    merged = list(result.get("inference_invalid_reasons") or [])
    for reason in reasons:
        qualified = f"collection_integrity:{reason}"
        if qualified not in merged:
            merged.append(qualified)
    result["inference_valid"] = False
    result["inference_invalid_reasons"] = merged
    result["stratified_bootstrap_95"] = None
    result["mcnemar_exact_p"] = None
    result["holm_adjusted_p"] = None
    for stratum in (result.get("strata") or {}).values():
        if isinstance(stratum, dict):
            _invalidate_pairwise_inference(stratum, reasons)


def comparison_summary(
    results_path: str | Path,
    tasks: list[SpreadsheetTask],
    *,
    arms: tuple[str, ...] = COMPARISON_ARMS,
    bootstrap_seed: int = 20260811,
    collection_tasks: list[SpreadsheetTask] | None = None,
) -> dict[str, Any]:
    collection_tasks = tasks if collection_tasks is None else collection_tasks
    task_ids = [task.task_id for task in tasks]
    collection_task_ids = [task.task_id for task in collection_tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("analysis task IDs must be unique")
    if len(set(collection_task_ids)) != len(collection_task_ids):
        raise ValueError("collection task IDs must be unique")
    if not set(task_ids).issubset(collection_task_ids):
        raise ValueError("analysis tasks must be a subset of collection tasks")
    rows, invalid = _valid_jsonl_rows(Path(results_path))
    expected_task_ids = set(collection_task_ids)
    expected_arms = set(arms)
    identity_counts: Counter[str] = Counter()
    unknown_task_ids: set[str] = set()
    unknown_arms: set[str] = set()
    unexpected_arms: set[str] = set()
    unknown_task_rows = 0
    unknown_arm_rows = 0
    unexpected_arm_rows = 0
    for row in rows:
        raw_task_id = row.get("task_id")
        raw_arm = row.get("arm")
        task_id = str(raw_task_id) if raw_task_id is not None else "<missing>"
        arm = str(raw_arm) if raw_arm is not None else "<missing>"
        if raw_task_id is not None and raw_arm is not None:
            identity_counts[_run_key(task_id, arm)] += 1
        if raw_task_id is None or task_id not in expected_task_ids:
            unknown_task_rows += 1
            unknown_task_ids.add(task_id)
        if raw_arm is None or arm not in AVAILABLE_COMPARISON_ARMS:
            unknown_arm_rows += 1
            unknown_arms.add(arm)
        elif arm not in expected_arms:
            unexpected_arm_rows += 1
            unexpected_arms.add(arm)
    duplicate_arm_task_keys = sorted(
        key for key, count in identity_counts.items() if count > 1
    )
    duplicate_arm_task_rows = sum(
        count - 1 for count in identity_counts.values() if count > 1
    )
    latest = {
        _run_key(str(row.get("task_id")), str(row.get("arm"))): row
        for row in rows
        if row.get("task_id") is not None and row.get("arm") in arms
    }
    expected_keys = [_run_key(task.task_id, arm) for task in tasks for arm in arms]
    by_arm: dict[str, dict[str, Any]] = {}
    passes: dict[str, dict[str, bool]] = {}
    for arm in arms:
        passes[arm] = {
            task.task_id: bool(
                latest.get(_run_key(task.task_id, arm), {}).get("status") == "completed"
                and latest.get(_run_key(task.task_id, arm), {}).get("passed") is True
            )
            for task in tasks
        }
        arm_summary = _arm_subset_summary(tasks, arm, latest, passes[arm])
        strata = {
            stratum: _arm_subset_summary(
                [task for task in tasks if _task_stratum(task) == stratum],
                arm,
                latest,
                passes[arm],
            )
            for stratum in ("cell", "sheet", "other")
        }
        arm_summary["strata"] = strata
        # Retain the original field names while making their contents complete.
        arm_summary["cell_level"] = strata["cell"]
        arm_summary["sheet_level"] = strata["sheet"]
        by_arm[arm] = arm_summary

    pairwise: dict[str, dict[str, Any]] = {}
    raw_p: dict[str, float] = {}
    for left_index, left_arm in enumerate(arms):
        for right_arm in arms[left_index + 1 :]:
            name = f"{left_arm}_vs_{right_arm}"
            result = _pairwise_result(
                tasks,
                left_arm,
                right_arm,
                latest,
                passes,
                seed=bootstrap_seed + len(pairwise),
            )
            result["strata"] = {
                stratum: _pairwise_result(
                    [task for task in tasks if _task_stratum(task) == stratum],
                    left_arm,
                    right_arm,
                    latest,
                    passes,
                    seed=bootstrap_seed + len(pairwise) * 10 + stratum_index + 1,
                )
                for stratum_index, stratum in enumerate(("cell", "sheet", "other"))
            }
            pairwise[name] = result
            p_value = result["mcnemar_exact_p"]
            if isinstance(p_value, int | float):
                raw_p[name] = float(p_value)
    adjusted = _holm_adjust(raw_p)
    for name, value in adjusted.items():
        pairwise[name]["holm_adjusted_p"] = value
    for name in pairwise:
        pairwise[name].setdefault("holm_adjusted_p", None)

    attempted_keys = set(latest)
    expected_key_set = set(expected_keys)
    attempted_expected = attempted_keys & expected_key_set
    errored_arm_tasks = sum(
        latest[key].get("status") != "completed" for key in attempted_expected
    )
    missing_arm_tasks = len(expected_key_set - attempted_keys)
    inference_invalid_reasons: list[str] = []
    if invalid:
        inference_invalid_reasons.append("invalid_result_rows")
    if duplicate_arm_task_keys:
        inference_invalid_reasons.append("duplicate_arm_tasks")
    if unknown_task_rows:
        inference_invalid_reasons.append("unknown_tasks")
    if unknown_arm_rows:
        inference_invalid_reasons.append("unknown_arms")
    if unexpected_arm_rows:
        inference_invalid_reasons.append("unexpected_arms")
    if missing_arm_tasks:
        inference_invalid_reasons.append("missing_arm_tasks")
    if errored_arm_tasks:
        inference_invalid_reasons.append("errored_arm_tasks")
    if inference_invalid_reasons:
        for result in pairwise.values():
            _invalidate_pairwise_inference(result, inference_invalid_reasons)
    calculation_backends = Counter(
        str(latest[key].get("calculation_backend") or "unspecified")
        for key in attempted_expected
    )
    return {
        "protocol": COMPARISON_PROTOCOL_VERSION,
        "arm_display_names": {
            arm: COMPARISON_ARM_DISPLAY_NAMES[arm] for arm in arms
        },
        "scorer": "cleanroom-corrected-value-v1",
        "style_checked": False,
        "calculation_backends": dict(sorted(calculation_backends.items())),
        "dataset_revision": f"KAKA22/SpreadsheetBench@{VERIFIED_REVISION}",
        "task_count": len(tasks),
        "expected_arm_tasks": len(expected_keys),
        "attempted_arm_tasks": len(attempted_expected),
        "completed_arm_tasks": len(attempted_expected) - errored_arm_tasks,
        "errored_arm_tasks": errored_arm_tasks,
        "missing_arm_tasks": missing_arm_tasks,
        "invalid_result_rows_ignored": invalid,
        "duplicate_arm_tasks": len(duplicate_arm_task_keys),
        "duplicate_arm_task_rows": duplicate_arm_task_rows,
        "duplicate_arm_task_keys": duplicate_arm_task_keys,
        "unknown_task_rows": unknown_task_rows,
        "unknown_task_ids": sorted(unknown_task_ids),
        "unknown_arm_rows": unknown_arm_rows,
        "unknown_arms": sorted(unknown_arms),
        "unexpected_arm_rows": unexpected_arm_rows,
        "unexpected_arms": sorted(unexpected_arms),
        "inference_valid": not inference_invalid_reasons,
        "inference_invalid_reasons": inference_invalid_reasons,
        "arms": by_arm,
        "pairwise": pairwise,
    }


class ComparisonBenchmarkRunner:
    """Run task-matched arms in a deterministic rotating order with durable resume."""

    def __init__(
        self,
        config: ProviderConfig,
        output_dir: Path,
        *,
        skill_registry: SkillRegistry,
        arms: tuple[str, ...] = COMPARISON_ARMS,
        max_model_calls: int = 20,
        max_turns_per_arm: int = 20,
        max_total_tokens: int = 100_000,
        max_output_tokens: int = 4_096,
        task_timeout_seconds: float = 900,
        recalculate: bool = True,
        arm_order_seed: int = 20260811,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        if not arms or len(set(arms)) != len(arms) or any(
            arm not in AVAILABLE_COMPARISON_ARMS for arm in arms
        ):
            raise ValueError(f"arms must be unique members of {AVAILABLE_COMPARISON_ARMS}")
        if max_model_calls < 1 or max_total_tokens < 1 or max_output_tokens < 1:
            raise ValueError("comparison budgets must be positive")
        self.stage_turn_caps = comparison_stage_turn_caps(max_turns_per_arm, arms)
        if max_model_calls < max_turns_per_arm:
            raise ValueError(
                "max_model_calls must be at least max_turns_per_arm so the declared "
                "response ceiling is reachable"
            )
        if task_timeout_seconds <= 0 or circuit_breaker_threshold < 1:
            raise ValueError("timeouts and circuit breaker must be positive")
        self.config = config
        self.output_dir = output_dir.resolve()
        self.skill_registry = skill_registry.freeze()
        self.arms = arms
        self.max_model_calls = max_model_calls
        self.max_turns_per_arm = max_turns_per_arm
        self.max_total_tokens = max_total_tokens
        self.max_output_tokens = max_output_tokens
        self.task_timeout_seconds = task_timeout_seconds
        self.recalculate = recalculate
        self.arm_order_seed = arm_order_seed
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.relay_pacer = RelayPacer(config.request_interval_seconds)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = self.output_dir / "results.jsonl"
        self.manifest_path = self.output_dir / "comparison-manifest.json"
        self.summary_path = self.output_dir / "summary.json"
        self.lock_path = self.output_dir / ".comparison.lock"

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError(
                    f"Another comparison process is already using {self.output_dir}"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _manifest(self, tasks: list[SpreadsheetTask]) -> dict[str, Any]:
        skills = [
            {"name": skill.name, "sha256": skill.sha256}
            for skill in self.skill_registry.discover()
        ]
        arm_orders = _balanced_arm_orders(
            [task.task_id for task in tasks], self.arm_order_seed, self.arms
        )
        canonical_task_ids = "".join(f"{task_id}\n" for task_id in sorted(arm_orders))
        execution_task_ids = "".join(f"{task.task_id}\n" for task in tasks)
        profile_evidence = (
            {
                task.task_id: build_deterministic_profile(task.input_path)["profile_sha256"]
                for task in tasks
            }
            if {"profile", "ours"} & set(self.arms)
            else {}
        )
        return {
            "schema_version": 10,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "study": "SpreadsheetAgent-style adapted small-model comparison",
            "not_paper_reproduction": True,
            "dataset_revision": f"KAKA22/SpreadsheetBench@{VERIFIED_REVISION}",
            "dataset_archive_sha256": VERIFIED_SHA256,
            "dataset_manifest_sha256": _dataset_manifest_sha256(tasks),
            "protocol": "agent_per_workbook",
            "scorer": "cleanroom-corrected-value-v1",
            "task_count": len(tasks),
            "task_ids": [task.task_id for task in tasks],
            "task_id_set_sha256": _text_sha256(canonical_task_ids),
            "task_execution_order_sha256": _text_sha256(execution_task_ids),
            "tasks": [
                {
                    "task_id": task.task_id,
                    "instruction_sha256": _text_sha256(task.instruction),
                    "instruction_type": task.instruction_type,
                    "input_sha256": _sha256(task.input_path),
                    "golden_sha256": _sha256(task.golden_path),
                    "scoring_metadata_sha256": _scoring_metadata_sha256(task),
                }
                for task in tasks
            ],
            "arms": list(self.arms),
            "arm_display_names": {
                arm: COMPARISON_ARM_DISPLAY_NAMES[arm] for arm in self.arms
            },
            "arm_order_seed": self.arm_order_seed,
            "arm_order_policy": "seeded_hash_rank_cyclic_counterbalance_v1",
            "arm_order": arm_orders,
            "forced_tool_prefix_routing": {
                arm: {
                    stage: list(prefix)
                    for stage, prefix in COMPARISON_FORCED_TOOL_PREFIX_POLICY[arm].items()
                }
                for arm in self.arms
            },
            "post_prefix_routing": {
                "tool_choice": "auto",
                "terminal_tool": TERMINAL_TOOL_NAME,
                "applies_to": "comparison stages with workbook tools after forced prefix",
                "direct_text_stages": ["paper.reconcile"],
            },
            "forced_prefix_wire_policy": {
                "tool_choice": "explicit_function",
                "available_tools": "forced tool only",
                "terminal_tool_available": False,
            },
            "stage_turn_caps": {
                arm: dict(self.stage_turn_caps[arm]) for arm in self.arms
            },
            "turn_cap_policy": {
                "version": COMPARISON_TURN_CAP_POLICY_VERSION,
                "max_turns_per_arm": self.max_turns_per_arm,
                "paper_scaling_version": PAPER_TURN_CAP_SCALING_VERSION,
                "paper_base_stage_caps": {
                    "extract": 6,
                    "vision_verify": 3,
                    "latex_verify": 3,
                    "reconcile": 1,
                    "solve": 7,
                },
                "paper_stage_minimum": "forced_tool_prefix_length_plus_one_terminal_turn",
                "paper_allocation": (
                    "scale 6/3/3/1/7 proportions by arm ceiling, clamp each stage to its "
                    "forced-prefix-plus-terminal minimum, then adjust by largest remainder "
                    "with stage-name ties"
                ),
            },
            "deterministic_profile": {
                "enabled": bool({"profile", "ours"} & set(self.arms)),
                "consumed_by_arms": [
                    arm for arm in self.arms if arm in {"profile", "ours"}
                ],
                "schema_version": DETERMINISTIC_PROFILE_SCHEMA_VERSION,
                "bounds": dict(DETERMINISTIC_PROFILE_BOUNDS),
                "task_profile_sha256": profile_evidence,
                "task_independent": True,
                "model_calls": 0,
            },
            "hidden_from_models": [
                "instruction_type",
                "answer_position",
                "answer_sheet",
                "golden_path",
            ],
            "harness_source": _source_fingerprint(),
            "runtime": _runtime_fingerprint(),
            "configuration": {
                "model": self.config.model,
                "api_protocol": self.config.api_protocol,
                "requested_reasoning_effort": (
                    self.config.requested_reasoning_effort or self.config.reasoning_effort
                ),
                "reasoning_effort": self.config.reasoning_effort,
                "provider_base_url": self.config.base_url,
                "request_timeout_seconds": self.config.timeout_seconds,
                "litellm_timeout_seconds": self.config.litellm_timeout_seconds,
                "request_retries": self.config.max_retries,
                "request_interval_seconds": self.config.request_interval_seconds,
                "request_pacing_policy": PACING_POLICY,
                "request_pacing_scope": "comparison_runner_process",
                "request_pacing_retries_included": True,
                "request_pacing_first_attempt_immediate": True,
                "automatic_retry_policy": "delivery-aware-allowlist-v1",
                "safe_retry_http_statuses": sorted(SAFE_RETRY_HTTP_STATUSES),
                "safe_automatic_retry_reasons": sorted(
                    SAFE_AUTOMATIC_RETRY_REASONS
                ),
                "overload_retry_min_seconds": OVERLOAD_RETRY_MIN_SECONDS,
                "capacity_retry_delay_policy": (
                    "max-valid-retry-after-and-overload-min-then-global-cap"
                ),
                "connect_retry_min_seconds": CONNECT_RETRY_MIN_SECONDS,
                "retry_backoff_max_seconds": RETRY_BACKOFF_MAX_SECONDS,
                "read_timeout_policy": "fail-closed-no-replay",
                "http_408_policy": "fail-closed-no-replay",
                "stream_interruption_policy": "fail-closed-no-replay",
                "request_attempt_telemetry": (
                    "delivery-safe-retry-ids-headers-backoff-pacing-v4"
                ),
                "store_responses": self.config.store_responses,
                "generation": self.config.generation_dict(),
                "max_model_calls": self.max_model_calls,
                "max_turns_per_arm": self.max_turns_per_arm,
                "max_total_tokens": self.max_total_tokens,
                "max_output_tokens_per_call": self.max_output_tokens,
                "task_timeout_seconds": self.task_timeout_seconds,
                "recalculate": self.recalculate,
                "task_retries": 0,
                "circuit_breaker_threshold": self.circuit_breaker_threshold,
                "circuit_breaker_threshold_categories": [
                    "provider_transient",
                    "routing_protocol",
                ],
                "circuit_breaker_immediate_categories": ["provider_fatal"],
                "skills_for_ours_only": skills,
                "code_isolation": STRICT_ISOLATION_POLICY,
            },
        }

    def _prepare_manifest(self, tasks: list[SpreadsheetTask]) -> None:
        expected = self._manifest(tasks)
        if self.manifest_path.is_file():
            try:
                actual = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise HarnessError(f"Invalid comparison manifest: {self.manifest_path}") from exc
            if actual != expected:
                raise HarnessError("Refusing to resume comparison with a different frozen config")
            return
        if self.results_path.is_file() and self.results_path.stat().st_size:
            raise HarnessError("Comparison results exist without a compatibility manifest")
        _atomic_write_json(self.manifest_path, expected)

    def _latest(self) -> dict[str, dict[str, Any]]:
        rows, _ = _valid_jsonl_rows(self.results_path)
        latest: dict[str, dict[str, Any]] = {}
        seen_keys: set[str] = set()
        duplicate_keys: set[str] = set()
        for row in rows:
            if row.get("task_id") is None or row.get("arm") is None:
                continue
            key = _run_key(str(row["task_id"]), str(row["arm"]))
            if key in seen_keys:
                duplicate_keys.add(key)
            seen_keys.add(key)
            if row.get("arm") not in self.arms:
                continue
            latest[key] = row
        if duplicate_keys:
            duplicates = ", ".join(sorted(duplicate_keys))
            raise HarnessError(
                "Refusing to resume comparison with duplicate arm-task rows: "
                f"{duplicates}"
            )
        return latest

    def _append(self, row: dict[str, Any]) -> None:
        encoded = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode()
        descriptor = os.open(self.results_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _task_directory(self, task_id: str, arm: str) -> Path:
        path = self.output_dir / "runs" / task_id / arm
        if path.exists() and any(path.iterdir()):
            path = path.with_name(f"{arm}-{uuid.uuid4().hex[:8]}")
        return path

    def _run_one(self, task: SpreadsheetTask, arm: str) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started_clock = monotonic()
        task_dir = self._task_directory(task.task_id, arm)
        budget = RunBudget(
            max_model_calls=self.max_model_calls,
            max_total_tokens=self.max_total_tokens,
            max_elapsed_seconds=self.task_timeout_seconds,
        )
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "arm": arm,
            "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
            "instruction_type": task.instruction_type,
            "model": self.config.model,
            "api_protocol": self.config.api_protocol,
            "requested_reasoning_effort": (
                self.config.requested_reasoning_effort or self.config.reasoning_effort
            ),
            "reasoning_effort": self.config.reasoning_effort,
            "request_interval_seconds": self.config.request_interval_seconds,
            "litellm_timeout_seconds": self.config.litellm_timeout_seconds,
            "generation": self.config.generation_dict(),
            "max_model_calls": self.max_model_calls,
            "max_turns_per_arm": self.max_turns_per_arm,
            "stage_turn_caps": dict(self.stage_turn_caps[arm]),
            "run_dir": str(task_dir),
            "started_at": started_at.isoformat(),
            "calculation_backend": "libreoffice" if self.recalculate else "not_recalculated",
        }
        session: WorkbookSession | None = None

        def remaining_seconds(stage: str) -> float:
            budget.ensure_within_time(stage=stage)
            assert budget.deadline is not None
            return max(budget.deadline - monotonic(), 0.001)

        try:
            session = WorkbookSession.create(
                task.input_path,
                task_dir,
                run_id=f"{task.task_id}-{arm}",
            )
            session.recorder.record(
                "benchmark.configured",
                {
                    "schema_version": 10,
                    "comparison_protocol_version": COMPARISON_PROTOCOL_VERSION,
                    "arm": arm,
                    "api_protocol": self.config.api_protocol,
                    "request_interval_seconds": self.config.request_interval_seconds,
                    "litellm_timeout_seconds": self.config.litellm_timeout_seconds,
                    "generation": self.config.generation_dict(),
                    "request_pacing_policy": PACING_POLICY,
                    "request_pacing_scope": "comparison_runner_process",
                    "max_model_calls": self.max_model_calls,
                    "max_turns_per_arm": self.max_turns_per_arm,
                    "stage_turn_caps": dict(self.stage_turn_caps[arm]),
                    "max_total_tokens": self.max_total_tokens,
                    "max_output_tokens_per_call": self.max_output_tokens,
                    "task_timeout_seconds": self.task_timeout_seconds,
                },
            )
            result = run_arm(
                arm=arm,
                config=self.config,
                session=session,
                skills=self.skill_registry if arm == "ours" else None,
                instruction=task.instruction,
                max_output_tokens=self.max_output_tokens,
                max_elapsed_seconds=self.task_timeout_seconds,
                budget=budget,
                pacer=self.relay_pacer,
                max_turns_per_arm=self.max_turns_per_arm,
            )
            recalculation: dict[str, Any] | None = None
            if self.recalculate:
                from .render import recalculate_workbook

                recalculation = recalculate_workbook(
                    session.workbook_path,
                    session.workbook_path,
                    timeout_seconds=min(120.0, remaining_seconds("recalculate")),
                )
                budget.ensure_within_time(stage="recalculate")
            budget.ensure_within_time(stage="score")
            comparison = compare_workbooks(
                task.golden_path,
                session.workbook_path,
                task.answer_position,
                answer_sheet=task.answer_sheet,
            )
            budget.ensure_within_time(stage="score")
            row.update(
                {
                    "status": "completed",
                    "passed": comparison.passed,
                    "comparison": comparison.to_dict(),
                    "agent": result.to_dict(),
                    "recalculation": recalculation,
                    "output_workbook": str(session.workbook_path),
                    "output_sha256": _sha256(session.workbook_path),
                }
            )
            session.recorder.record(
                "benchmark.evaluated",
                {
                    "task_id": task.task_id,
                    "arm": arm,
                    "passed": comparison.passed,
                    "status": "completed",
                    "scorer": "cleanroom-corrected-value-v1",
                    "style_checked": False,
                    "calculation_backend": row["calculation_backend"],
                    **comparison_evidence(comparison),
                    "scoring_metadata_sha256": _scoring_metadata_sha256(task),
                },
            )
        except CodeIsolationError:
            # Comparison results are invalid if code can escape its arm. Stop
            # the entire run instead of recording an error and continuing.
            raise
        except Exception as caught:
            effective_exc = caught
            try:
                budget.ensure_within_time(stage="postprocess")
            except AgentBudgetError as budget_exc:
                effective_exc = budget_exc
            safe_error = str(effective_exc).replace(self.config.api_key, "[REDACTED]")
            row.update(
                {
                    "status": "error",
                    "passed": False,
                    "error": safe_error,
                    "error_type": type(effective_exc).__name__,
                    "error_retryable": False,
                    "error_category": "harness",
                }
            )
            if isinstance(effective_exc, AgentBudgetError):
                row["error_category"] = "budget_exhausted"
            elif isinstance(effective_exc, AgentTimeoutError):
                row["error_category"] = "task_timeout"
            elif isinstance(effective_exc, PaperStageValidationError):
                row.update(
                    {
                        "error_category": "paper_stage_validation",
                        "paper_stage": effective_exc.stage,
                        "paper_stage_reason": effective_exc.reason,
                    }
                )
            elif isinstance(effective_exc, AgentRoutingError):
                row["error_category"] = "routing_protocol"
            elif isinstance(effective_exc, ProviderError):
                row.update(
                    {
                        "error_retryable": bool(effective_exc.safe_to_retry),
                        "error_category": (
                            "provider_transient"
                            if effective_exc.retryable
                            else "provider_fatal"
                            if effective_exc.global_fatal
                            else "provider_task"
                        ),
                        "provider_error": effective_exc.public_dict(
                            secrets=(self.config.api_key,)
                        ),
                    }
                )
        finally:
            row["budget"] = budget.to_dict()
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            row["elapsed_seconds"] = round(monotonic() - started_clock, 3)
            if session is not None:
                if row.get("status") != "completed":
                    try:
                        session.recorder.record(
                            "benchmark.not_evaluated",
                            {
                                "task_id": task.task_id,
                                "arm": arm,
                                "status": str(row.get("status", "error")),
                                "scorer": "cleanroom-corrected-value-v1",
                                "style_checked": False,
                                "calculation_backend": row["calculation_backend"],
                                "error_category": row.get("error_category"),
                            },
                        )
                    except Exception:
                        pass
                try:
                    session.write_manifest(
                        {
                            "task_id": task.task_id,
                            "arm": arm,
                            "instruction_sha256": _text_sha256(task.instruction),
                            "result": row,
                        }
                    )
                except Exception:
                    pass
        return row

    def run(self, tasks: list[SpreadsheetTask]) -> dict[str, Any]:
        if not tasks or len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("comparison tasks must be non-empty with unique IDs")
        # Probe before creating a manifest, spending model budget, or writing a
        # result row. The probe imports openpyxl inside the active venv and
        # verifies that a sibling file is not visible in the sandbox.
        ensure_strict_code_isolation()
        with self._exclusive_lock():
            _repair_jsonl(self.results_path)
            self._prepare_manifest(tasks)
            latest = self._latest()
            arm_orders = _balanced_arm_orders(
                [task.task_id for task in tasks], self.arm_order_seed, self.arms
            )
            exhausted_transient = sum(
                row.get("error_category") == "provider_transient"
                for row in latest.values()
            )
            fatal_provider_errors = sum(
                row.get("error_category") == "provider_fatal"
                for row in latest.values()
            )
            routing_protocol_errors = sum(
                row.get("error_category") == "routing_protocol"
                for row in latest.values()
            )
            circuit_breaker = bool(
                fatal_provider_errors
                or exhausted_transient >= self.circuit_breaker_threshold
                or routing_protocol_errors >= self.circuit_breaker_threshold
            )
            expected = len(tasks) * len(self.arms)
            finished = sum(
                _run_key(task.task_id, arm) in latest for task in tasks for arm in self.arms
            )
            for task in tasks:
                for arm in arm_orders[task.task_id]:
                    key = _run_key(task.task_id, arm)
                    if key in latest:
                        continue
                    if circuit_breaker:
                        break
                    row = self._run_one(task, arm)
                    self._append(row)
                    latest[key] = row
                    finished += 1
                    if row.get("error_category") == "provider_fatal":
                        fatal_provider_errors += 1
                        circuit_breaker = True
                    elif row.get("error_category") == "provider_transient":
                        exhausted_transient += 1
                        if exhausted_transient >= self.circuit_breaker_threshold:
                            circuit_breaker = True
                    elif row.get("error_category") == "routing_protocol":
                        routing_protocol_errors += 1
                        if routing_protocol_errors >= self.circuit_breaker_threshold:
                            circuit_breaker = True
                    print(
                        json.dumps(
                            {
                                "event": "comparison.arm_task_finished",
                                "task_id": task.task_id,
                                "arm": arm,
                                "status": row.get("status"),
                                "passed": row.get("passed"),
                                "error_category": row.get("error_category"),
                                "elapsed_seconds": row.get("elapsed_seconds"),
                                "finished": finished,
                                "expected": expected,
                                "circuit_breaker_tripped": circuit_breaker,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                if circuit_breaker:
                    break
            summary = comparison_summary(
                self.results_path,
                tasks,
                arms=self.arms,
                bootstrap_seed=self.arm_order_seed,
            )
            summary["circuit_breaker_tripped"] = circuit_breaker
            summary["exhausted_transient_arm_tasks"] = exhausted_transient
            summary["fatal_provider_arm_tasks"] = fatal_provider_errors
            summary["routing_protocol_arm_tasks"] = routing_protocol_errors
            summary["circuit_breaker_threshold"] = self.circuit_breaker_threshold
            _atomic_write_json(self.summary_path, summary)
            return summary
