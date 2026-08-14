from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).parents[1] / "scripts" / "design_mcnemar_power.py"
    spec = importlib.util.spec_from_file_location("design_mcnemar_power", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


power = _load_module()


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0.05, 0.1192),
        (0.10, 0.4399),
        (0.15, 0.8367),
        (0.20, 0.9908),
    ],
)
def test_exact_power_matches_independent_reference_values(delta: float, expected: float) -> None:
    observed = power.exact_mcnemar_power(
        tasks=100,
        discordance_rate=0.25,
        accuracy_delta=delta,
        alpha=0.05,
    )
    assert observed == pytest.approx(expected, abs=5e-5)


def test_zero_discordance_has_no_power() -> None:
    assert (
        power.exact_mcnemar_power(
            tasks=100,
            discordance_rate=0.0,
            accuracy_delta=0.0,
            alpha=0.05,
        )
        == 0.0
    )


def test_report_skips_infeasible_delta_and_discloses_scope() -> None:
    report = power.build_report(
        tasks=100,
        discordance_rates=[0.1],
        accuracy_deltas=[0.05, 0.2],
        alphas=[0.05],
    )
    assert len(report["rows"]) == 1
    assert report["rows"][0]["accuracy_delta"] == 0.05
    assert report["assumptions"]["benchmark_outcomes_used"] is False
    assert report["assumptions"]["multi_seed_cluster_analysis_covered"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tasks": 0, "discordance_rate": 0.25, "accuracy_delta": 0.1, "alpha": 0.05},
        {"tasks": 100, "discordance_rate": -0.1, "accuracy_delta": 0.0, "alpha": 0.05},
        {"tasks": 100, "discordance_rate": 0.1, "accuracy_delta": 0.2, "alpha": 0.05},
        {"tasks": 100, "discordance_rate": 0.1, "accuracy_delta": 0.0, "alpha": 1.0},
    ],
)
def test_invalid_design_inputs_fail_closed(kwargs: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        power.exact_mcnemar_power(**kwargs)
