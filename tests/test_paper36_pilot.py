from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest


BASE = Path(__file__).parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


pilot = module(
    "paper36_pilot_test",
    BASE / "benchmarks/run_paper36_pilot_20260917.py",
)


def test_preregistered_split_is_balanced_and_family_disjoint():
    root = BASE / "benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917"
    manifest = pilot.load(root / "pilot-split.json")
    tasks = manifest["tasks"]
    assert len(tasks) == 36
    assert len({(task["dataset"], task["source_workbook"]) for task in tasks}) == 36
    assert Counter((task["dataset"], task["complexity"]) for task in tasks) == {
        (dataset, complexity): 6
        for dataset in ("Fin-269", "Fin-1.5K")
        for complexity in ("C1", "C2", "C3")
    }


def test_preregistered_split_excludes_formal_search_families():
    root = BASE / "benchmarks/results/paper36-heldout-pilot-qwen36plus-20260917"
    manifest = pilot.load(root / "pilot-split.json")
    shared = pilot.load(
        BASE
        / "benchmarks/results/paper36-shared-baseline-qwen36plus-20260917/split-manifest.json"
    )
    aliases = {"v06": "Fin-269", "enhanced-v2": "Fin-1.5K"}
    search = {
        (aliases[task["dataset"]], task["source_workbook"])
        for task in shared["tasks"]
        if task["role"] != "heldout"
    }
    heldout = {(task["dataset"], task["source_workbook"]) for task in manifest["tasks"]}
    assert heldout.isdisjoint(search)


def test_interaction_uses_difference_in_differences():
    assert pilot.interaction_values(
        [0.2, 0.0], [0.3, 0.1], [0.4, 0.0], [0.8, 0.4]
    ) == pytest.approx([
        0.3,
        0.3,
    ])


def test_exact_mcnemar_and_holm_are_deterministic():
    result = pilot.exact_mcnemar([1, 1, 0, 0], [0, 0, 1, 0])
    assert result == {
        "left_wins": 2,
        "right_wins": 1,
        "ties": 1,
        "discordant": 3,
        "two_sided_exact_p": 1.0,
    }
    assert pilot.holm([("a", 0.01), ("b", 0.04), ("c", 0.03)]) == {
        "a": 0.03,
        "c": 0.06,
        "b": 0.06,
    }


def test_recovered_output_limit_is_not_a_fatal_infrastructure_event(tmp_path):
    trace = tmp_path / "trajectory.jsonl"
    trace.write_text(
        '{"event":"model.failed"}\n{"event":"model.responded"}\n', encoding="utf-8"
    )
    assert pilot.fatal_infrastructure_events(tmp_path) == []
    trace.write_text(
        '{"event":"model.responded"}\n{"event":"model.failed"}\n', encoding="utf-8"
    )
    assert pilot.fatal_infrastructure_events(tmp_path) == ["trajectory.jsonl"]
