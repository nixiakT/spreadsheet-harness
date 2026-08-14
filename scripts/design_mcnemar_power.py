#!/usr/bin/env python3
"""Design-stage exact power grid for paired binary spreadsheet outcomes.

The calculation integrates the conditional exact McNemar test over a binomial
number of discordant task pairs. It is intentionally independent of benchmark
outcomes and is not a substitute for the preregistered clustered analysis used
when several seeds are observed for the same task.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Any


def _binomial_pmf(n: int, probability: float) -> list[float]:
    if n < 0:
        raise ValueError("n must be non-negative")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be in [0, 1]")
    if probability == 0.0:
        return [1.0, *([0.0] * n)]
    if probability == 1.0:
        return [*([0.0] * n), 1.0]

    mode = min(n, int((n + 1) * probability))
    values = [0.0] * (n + 1)
    values[mode] = math.exp(
        math.lgamma(n + 1)
        - math.lgamma(mode + 1)
        - math.lgamma(n - mode + 1)
        + mode * math.log(probability)
        + (n - mode) * math.log1p(-probability)
    )
    odds = probability / (1.0 - probability)
    for k in range(mode, 0, -1):
        values[k - 1] = values[k] * k / (n - k + 1) / odds
    for k in range(mode, n):
        values[k + 1] = values[k] * (n - k) / (k + 1) * odds

    total = math.fsum(values)
    if not total > 0.0:
        raise ArithmeticError("binomial probability mass underflowed")
    return [value / total for value in values]


def _conditional_rejection_probability(
    discordant_pairs: int,
    right_only_probability: float,
    alpha: float,
) -> float:
    null_mass = _binomial_pmf(discordant_pairs, 0.5)
    null_cdf = 0.0
    lower_critical = -1
    for right_only in range(discordant_pairs // 2 + 1):
        null_cdf += null_mass[right_only]
        if min(1.0, 2.0 * null_cdf) <= alpha + 1e-14:
            lower_critical = right_only
    if lower_critical < 0:
        return 0.0

    alternative_mass = _binomial_pmf(discordant_pairs, right_only_probability)
    lower = math.fsum(alternative_mass[: lower_critical + 1])
    upper = math.fsum(alternative_mass[discordant_pairs - lower_critical :])
    return min(1.0, lower + upper)


def exact_mcnemar_power(
    *,
    tasks: int,
    discordance_rate: float,
    accuracy_delta: float,
    alpha: float,
) -> float:
    """Return unconditional power for a two-sided exact paired McNemar test.

    ``accuracy_delta`` is the right-arm minus left-arm pass-rate difference.
    ``discordance_rate`` is the probability that exactly one arm passes. The
    model is feasible only when ``abs(accuracy_delta) <= discordance_rate``.
    """

    if tasks <= 0:
        raise ValueError("tasks must be positive")
    if not 0.0 <= discordance_rate <= 1.0:
        raise ValueError("discordance_rate must be in [0, 1]")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if abs(accuracy_delta) > discordance_rate + 1e-15:
        raise ValueError("abs(accuracy_delta) cannot exceed discordance_rate")
    if discordance_rate == 0.0:
        return 0.0

    right_only_probability = (discordance_rate + accuracy_delta) / (2.0 * discordance_rate)
    discordant_mass = _binomial_pmf(tasks, discordance_rate)
    return math.fsum(
        discordant_mass[count]
        * _conditional_rejection_probability(count, right_only_probability, alpha)
        for count in range(tasks + 1)
    )


def build_report(
    *,
    tasks: int,
    discordance_rates: list[float],
    accuracy_deltas: list[float],
    alphas: list[float],
) -> dict[str, Any]:
    rows: list[dict[str, float | int]] = []
    for alpha in alphas:
        for discordance_rate in discordance_rates:
            for accuracy_delta in accuracy_deltas:
                if abs(accuracy_delta) > discordance_rate:
                    continue
                rows.append(
                    {
                        "tasks": tasks,
                        "discordance_rate": discordance_rate,
                        "accuracy_delta": accuracy_delta,
                        "alpha": alpha,
                        "exact_power": exact_mcnemar_power(
                            tasks=tasks,
                            discordance_rate=discordance_rate,
                            accuracy_delta=accuracy_delta,
                            alpha=alpha,
                        ),
                    }
                )
    return {
        "schema": "paired-mcnemar-design-power-v1",
        "analysis_unit": "task",
        "test": "two-sided-exact-mcnemar",
        "assumptions": {
            "independent_task_clusters": True,
            "fixed_discordance_rate": True,
            "multi_seed_cluster_analysis_covered": False,
            "benchmark_outcomes_used": False,
        },
        "rows": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument(
        "--discordance-rate",
        type=float,
        nargs="+",
        default=[0.15, 0.25, 0.40],
    )
    parser.add_argument(
        "--accuracy-delta",
        type=float,
        nargs="+",
        default=[0.05, 0.10, 0.15, 0.20],
    )
    parser.add_argument("--alpha", type=float, nargs="+", default=[0.05, 0.025])
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = build_report(
        tasks=args.tasks,
        discordance_rates=args.discordance_rate,
        accuracy_deltas=args.accuracy_delta,
        alphas=args.alpha,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
