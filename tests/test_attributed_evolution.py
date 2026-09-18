from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from spreadsheet_harness.arms import _routed_skill_names

spec = importlib.util.spec_from_file_location(
    "evolution_runner_v3", Path(__file__).parents[1] / "benchmarks/run_attributed_evolution.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_factorial_route_keeps_h_d_coordination_and_verification(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_EVOLUTION_ROUTE_STRUCTURE", "1")
    available = (
        "spreadsheet-structure", "spreadsheet-financial-model", "spreadsheet-formula",
        "spreadsheet-manipulation", "spreadsheet-coordination", "spreadsheet-verification",
    )
    selected = _routed_skill_names("Sort and calculate.", available, task_category="Financial_Model")
    assert set(selected) == set(available) - {"spreadsheet-manipulation"}
    monkeypatch.delenv("SPREADSHEET_EVOLUTION_ROUTE_STRUCTURE")
    legacy = _routed_skill_names("Sort and calculate.", available, task_category="Financial_Model")
    assert len(legacy) <= 3


def test_diverse_routes_do_not_fill_quota_with_one_case():
    records = []
    for task in range(10):
        for arm in range(7):
            records.append({
                "task_id": str(task), "trajectory": f"{task}/{arm}", "passed": task == 9,
                "attribution_scores": {"h": 100.0 if task == 0 else 1.0},
            })
    selected = runner.diverse_select(records, "h", 8)
    assert len(selected) == 8
    assert len({r["task_id"] for r in selected}) == 8
    assert any(r["passed"] for r in selected)


def row(task, exact, mod=1.0, reg=1.0, status="scored"):
    return {"task": task, "exact": exact, "modification": mod, "regression": reg, "status": status}


def test_missing_cases_cannot_produce_a_false_win():
    tasks = [{"dataset": "v06", "task_id": str(i)} for i in range(2)]
    baseline = [row(tasks[0], 0), row(tasks[1], None, status="execution_error")]
    candidate = [row(tasks[0], 1), row(tasks[1], 1)]
    result = runner.compare(baseline, candidate, tasks)
    assert result["paired_scored"] == 1
    assert not result["complete"]
    assert not result["qualifies"]


def test_paired_gate_counts_wins_losses_and_regression():
    tasks = [{"dataset": "v06", "task_id": str(i)} for i in range(3)]
    baseline = [row(t, 0) for t in tasks]
    candidate = [row(t, 1) for t in tasks]
    assert runner.compare(baseline, candidate, tasks)["qualifies"]
    candidate[-1]["regression"] = 0.5
    assert not runner.compare(baseline, candidate, tasks)["qualifies"]


def test_split_is_workbook_disjoint_and_seeded():
    catalog = []
    for dataset in runner.DATASETS:
        for difficulty in ("C1", "C2", "C3"):
            for i in range(10):
                catalog.append({
                    "dataset": dataset, "task_id": f"{dataset}/{difficulty}/{i}",
                    "complexity": difficulty, "source_workbook": f"{dataset}-{difficulty}-{i}",
                })
    legacy = catalog[:2]
    result = runner.split_tasks(catalog, legacy, {"quarantined"})
    assert result == runner.split_tasks(catalog, legacy, {"quarantined"})
    groups = [{t["source_workbook"] for t in v} for v in result.values()]
    assert not groups[0] & groups[1]
    assert not groups[1] & groups[2]
    assert not groups[0] & groups[2]


def test_activation_checks_hashes_in_every_stage(tmp_path):
    import json
    trajectory = tmp_path / "trajectory.jsonl"
    entries = [
        {"event": "agent.started", "payload": {"skills": [{"name": "H", "sha256": "a"}]}},
        {"event": "agent.started", "payload": {"skills": [{"name": "H", "sha256": "b"}]}},
    ]
    trajectory.write_text("\n".join(json.dumps(r) for r in entries))
    assert not runner.audit_activation(trajectory, {"H": "a"})


def test_checkpoint_rejects_changed_snapshot(tmp_path):
    p = tmp_path / "controller.py"
    p.write_text("original")
    runner.write(tmp_path / "protocol.json", {"hashes": {"controller.py": runner.digest(p)}})
    p.write_text("changed")
    with pytest.raises(ValueError, match="Frozen"):
        runner.verify_snapshot(tmp_path)

def test_materialized_arms_change_only_declared_coordinates(tmp_path, monkeypatch):
    import hashlib
    import shutil

    from spreadsheet_harness.skills import SkillRegistry

    root = tmp_path / "experiment"
    seed = root / "baseline-skills"
    for name in ("spreadsheet-structure", "spreadsheet-financial-model",
                 "spreadsheet-formula", "spreadsheet-verification", "spreadsheet-coordination"):
        source = Path(__file__).parents[1] / "skills" / name / "SKILL.md"
        dest = seed / name / "SKILL.md"
        dest.parent.mkdir(parents=True)
        shutil.copy2(source, dest)
    candidates = {}
    for coordinate, name in runner.SKILLS.items():
        p = tmp_path / f"{coordinate}.md"
        p.write_text(f"---\nname: {name}\ndescription: Candidate.\n---\n\nBounded {coordinate} changes.\n")
        candidates[coordinate] = p
    runner.build_arms(root, 1, candidates)
    monkeypatch.setenv("SPREADSHEET_EVOLUTION_ROUTE_STRUCTURE", "1")
    for arm, edits in runner.ARM_EDITS.items():
        skillroot, composition = runner.arm_paths(root, 1, arm)
        assert composition.is_file()
        expected = runner.expected_skills(skillroot, arm)
        selected = _routed_skill_names("Complete targets", tuple(expected), task_category="Financial_Model")
        discovered = SkillRegistry([skillroot]).select(selected).discover()
        assert {s.name: s.sha256 for s in discovered} == expected
        for coordinate in ("h", "d"):
            name = runner.SKILLS[coordinate]
            wanted = candidates[coordinate] if coordinate in edits else seed / name / "SKILL.md"
            assert expected[name] == hashlib.sha256(wanted.read_bytes()).hexdigest()

