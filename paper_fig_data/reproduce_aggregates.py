#!/usr/bin/env python3
"""Reproduce the simple (non-bootstrap) aggregates used in paper_fig_data.

Read-only: prints CSV-like audit rows and never changes experiment outputs.
Run from the repository root with: python paper_fig_data/reproduce_aggregates.py
"""

from __future__ import annotations

import glob
import json
import statistics
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def harness_v2(run: str) -> None:
    rows = []
    prompt = completion = calls = 0
    outcomes: dict[str, int] = {}
    for name in glob.glob(str(ROOT / "benchmarks/results" / run / "tasks/*/results.json")):
        record = load(Path(name))
        if isinstance(record, list):
            record = record[0]
        if record["task_id"].startswith("Visualization/"):
            continue
        score = record.get("official_score") or {}
        if score.get("accuracy") is not None:
            rows.append(record)
        outcome = record.get("outcome_kind", "")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        agent = record.get("agent") or {}
        calls += agent.get("turns") or 0
        usage = agent.get("usage") or {}
        prompt += usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion += usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    by_category: dict[str, list] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)
    for category, group in sorted(by_category.items()):
        exact = statistics.fmean(r["official_score"]["accuracy"] for r in group) * 100
        modification = statistics.fmean(
            r["official_score"]["modification_accuracy"] for r in group
        ) * 100
        print(run, category, len(group), f"{exact:.4f}", f"{modification:.4f}")
    n = 297
    exact = statistics.fmean(r["official_score"]["accuracy"] for r in rows) * 100
    modification = statistics.fmean(
        r["official_score"]["modification_accuracy"] for r in rows
    ) * 100
    invalid = (outcomes.get("model_execution_failure", 0) + outcomes.get("not_scored", 0)) / n * 100
    provider = outcomes.get("scored_after_provider_failure", 0) / n * 100
    print(
        run,
        "Overall",
        len(rows),
        f"{exact:.4f}",
        f"{modification:.4f}",
        f"calls={calls/n:.4f}",
        f"prompt={prompt/n:.4f}",
        f"completion={completion/n:.4f}",
        f"invalid={invalid:.4f}",
        f"provider={provider:.4f}",
    )


def external_v2(run: str) -> None:
    raw = load(ROOT / "benchmarks/results" / run / "results.json")
    if isinstance(raw, dict):
        raw = raw.get("results") or raw.get("tasks") or []
    rows = [r for r in raw if isinstance(r, dict) and not r.get("task_id", "").startswith("Visualization/")]
    scored = [r for r in rows if (r.get("official_score") or {}).get("accuracy") is not None]
    latency = [r["elapsed_seconds"] for r in rows if isinstance(r.get("elapsed_seconds"), (int, float))]
    calls = [r.get("model_requests") or r.get("turns") for r in rows]
    calls = [x for x in calls if isinstance(x, (int, float))]
    print(
        run,
        len(rows),
        len(scored),
        f"exact={statistics.fmean(r['official_score']['accuracy'] for r in scored)*100:.4f}",
        f"latency_mean={statistics.fmean(latency):.4f}",
        f"latency_p50={statistics.median(latency):.4f}",
        f"latency_p95={sorted(latency)[int(.95*(len(latency)-1))]:.4f}",
        f"calls={statistics.fmean(calls):.4f}",
    )


def evolution() -> None:
    for name in sorted(glob.glob(str(ROOT / "benchmarks/results/paper36-search-*/decisions/*.json"))):
        decision = load(Path(name))
        quality = lambda x: (x["accuracy"] + .25 * x["modification_accuracy"] + .10 * x["regression_accuracy"]) * 100
        print(
            Path(name).parent.parent.name,
            decision["candidate_id"],
            f"incumbent={quality(decision['incumbent']['all']):.4f}",
            f"candidate={quality(decision['candidate']['all']):.4f}",
            "promote" if decision["accepted"] else "reject",
        )


if __name__ == "__main__":
    harness_v2("deepseek-v4-flash-harness-v26-basic-v2-p6-20260915")
    harness_v2("deepseek-v4-flash-harness-v26-all-v2-p6-20260915")
    for run_name in (
        "deepseek-v4-flash-codex-core-full-v1-20260914",
        "deepseek-v4-flash-claude-core-full-v1-20260914",
        "deepseek-v4-flash-official-dsh-full-v2-20260913",
        "qwen3-coder-480b-a35b-instruct-codex-full-v1-20260913",
        "qwen3-coder-480b-a35b-instruct-claude-full-v1-20260913",
        "qwen3-coder-480b-a35b-instruct-deepseek-full-v1-20260913",
    ):
        external_v2(run_name)
    evolution()
