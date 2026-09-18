from __future__ import annotations

import importlib.util
import json
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


audit_module = module(
    "paper24_pilot_audit_test",
    BASE / "tools/audit_paper24_pilot.py",
)
pilot = module(
    "paper24_pilot_report_test",
    BASE / "benchmarks/run_paper24_pilot_20260917.py",
)


def test_only_registered_cell_timeout_is_normalized() -> None:
    key = ("initial", "Fin-269", "Financial_Model/task")
    policy = {"eligible_cells": {key}, "created_at": "2026-09-18T00:00:00+00:00"}
    started = audit_module._iso_timestamp("2026-09-18T00:01:00+00:00")
    resources = {"task_timeout_seconds": 7200, "max_model_calls": 50}
    assert audit_module.normalized_resources(resources, key, started, policy) == {
        "task_timeout_seconds": 3600, "max_model_calls": 50
    }
    assert resources["task_timeout_seconds"] == 7200
    for candidate, start, amendment in ((key, started, None), (("wrong", *key[1:]), started, policy), (key, started - 120, policy)):
        with pytest.raises(ValueError, match="not authorized"):
            audit_module.normalized_resources(resources, candidate, start, amendment)
    assert audit_module.normalized_resources({"task_timeout_seconds": 3600}, key, started, None)["task_timeout_seconds"] == 3600


def test_expected_matrix_has_24_families_by_six_arms() -> None:
    manifest = {
        "tasks": [
            {
                "dataset": "Fin-269" if index < 12 else "Fin-1.5K",
                "task_id": f"Financial_Model/task-{index:02d}",
            }
            for index in range(24)
        ]
    }

    cells = audit_module.expected_cells(manifest)

    assert len(cells) == 144
    assert {key[0] for key in cells} == set(audit_module.ARMS)
    assert all(sum(key[0] == arm for key in cells) == 24 for arm in audit_module.ARMS)


def test_source_variant_details_are_auditable_and_pairwise_diffed() -> None:
    details, pair_diffs = audit_module.describe_source_variants(
        {
            "older-digest": {
                "source_files": {"agent.py": "agent-old", "arms.py": "arms-old"},
                "cells": [
                    {
                        "cell": "initial/Fin-269/Financial_Model/task-00",
                        "arm": "initial",
                        "dataset": "Fin-269",
                        "task_id": "Financial_Model/task-00",
                        "status": "scored",
                        "manifest_mtime": 10.0,
                    },
                    {
                        "cell": "alternating/Fin-269/Financial_Model/task-01",
                        "arm": "alternating",
                        "dataset": "Fin-269",
                        "task_id": "Financial_Model/task-01",
                        "status": "active",
                        "manifest_mtime": 20.0,
                    },
                ],
            },
            "newer-digest": {
                "source_files": {
                    "agent.py": "agent-new",
                    "arms.py": "arms-old",
                    "evolution.py": "evolution-new",
                },
                "cells": [
                    {
                        "cell": "alt_h_only/Fin-1.5K/Financial_Model/task-02",
                        "arm": "alt_h_only",
                        "dataset": "Fin-1.5K",
                        "task_id": "Financial_Model/task-02",
                        "status": "failed-attempt",
                        "manifest_mtime": 30.0,
                    }
                ],
            },
        }
    )

    assert [variant["digest"] for variant in details] == ["older-digest", "newer-digest"]
    assert details[0]["cell_count"] == 2
    assert details[0]["arm_counts"] == {"alternating": 1, "initial": 1}
    assert details[0]["status_counts"] == {"active": 1, "scored": 1}
    assert [cell["cell"] for cell in details[0]["cells"]] == [
        "initial/Fin-269/Financial_Model/task-00",
        "alternating/Fin-269/Financial_Model/task-01",
    ]
    assert pair_diffs == [
        {
            "before_digest": "older-digest",
            "after_digest": "newer-digest",
            "changed_file_count": 2,
            "changed_files": [
                {
                    "filename": "agent.py",
                    "before_sha256": "agent-old",
                    "after_sha256": "agent-new",
                },
                {
                    "filename": "evolution.py",
                    "before_sha256": None,
                    "after_sha256": "evolution-new",
                },
            ],
        }
    ]


def test_structural_audit_does_not_require_or_read_scores(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(audit_module, "REPO", tmp_path)
    search_roots = {
        name: tmp_path / "search" / name
        for name in ("general_only", "domain_only", "alternating")
    }
    monkeypatch.setattr(audit_module, "SEARCH_ROOTS", search_roots)
    for path in search_roots.values():
        path.mkdir(parents=True)

    datasets = {}
    tasks = []
    for dataset in ("Fin-269", "Fin-1.5K"):
        dataset_root = tmp_path / "datasets" / dataset
        category = dataset_root / "Financial_Model"
        category.mkdir(parents=True)
        rows = []
        for index in range(12):
            task_id = f"{dataset.lower()}-{index:02d}"
            rows.append(
                {
                    "id": task_id,
                    "golden_response_path": f"spreadsheet/{task_id}_golden.xlsx",
                    "answer_position": "'Model'!A1:A2",
                }
            )
            tasks.append(
                {
                    "dataset": dataset,
                    "dataset_root": str(dataset_root.relative_to(tmp_path)),
                    "task_id": f"Financial_Model/{task_id}",
                    "complexity": f"C{index // 4 + 1}",
                    "source_workbook": f"{dataset}-family-{index:02d}.xlsx",
                    "role": "heldout-pilot",
                }
            )
        metadata = category / "dataset.json"
        metadata.write_text(json.dumps(rows), encoding="utf-8")
        datasets[dataset] = {
            "root": str(dataset_root.relative_to(tmp_path)),
            "metadata_sha256": audit_module.digest(metadata),
        }

    manifest = {"tasks": tasks, "datasets": datasets}
    runner = SimpleNamespace(verify=lambda _root, _parent: manifest)
    monkeypatch.setattr(audit_module, "load_runner", lambda: runner)
    root = tmp_path / "pilot"
    root.mkdir()
    (root / "protocol.json").write_text(
        json.dumps(
            {
                "unique_compositions": list(audit_module.ARMS),
                "pilot_split_sha256": "a" * 64,
                "parent_pilot_split_sha256": "b" * 64,
                "parent_protocol_sha256": "c" * 64,
                "script_sha256": "d" * 64,
                "amendment_runner_sha256": "e" * 64,
                "solver": "qwen3.6-plus",
                "temperature": 0.0,
                "top_p": 1.0,
                "thinking": True,
                "max_model_calls": 50,
            }
        ),
        encoding="utf-8",
    )
    (root / "endpoint-lock.json").write_text(
        json.dumps({"skill_hashes": {arm: {} for arm in audit_module.ARMS}}),
        encoding="utf-8",
    )

    report = audit_module.audit(root, tmp_path / "parent")

    assert report["score_values_read_or_reported"] is False
    assert report["matrix"]["expected_cells"] == 144
    assert report["matrix"]["attempted_cells"] == 0
    assert report["matrix"]["status_scored_cells"] == 0
    assert report["structurally_valid_so_far"] is True
    assert report["complete"] is False


def test_synthetic_24_by_six_report_generation(tmp_path: Path, monkeypatch) -> None:
    tasks = [
        SimpleNamespace(
            dataset="Fin-269" if index < 12 else "Fin-1.5K",
            task_id=f"Financial_Model/task-{index:02d}",
        )
        for index in range(24)
    ]
    arms = {arm: tmp_path / arm for arm in audit_module.ARMS}

    class Experiment:
        @staticmethod
        def result_rows(_phase, arm, selected_tasks):
            arm_index = audit_module.ARMS.index(arm)
            return [
                {
                    "dataset": task.dataset,
                    "task_id": task.task_id,
                    "accuracy": float((index + arm_index) % 3 == 0),
                    "modification_accuracy": (index + arm_index) % 11 / 10,
                    "regression_accuracy": 1.0,
                    "model_calls": 1,
                    "errors": 0,
                    "error_message": "",
                }
                for index, task in enumerate(selected_tasks)
            ]

        @staticmethod
        def task_dir(_phase, arm, task):
            return tmp_path / "runs" / arm / task.task_id.replace("/", "_")

    monkeypatch.setattr(pilot.pilot, "fatal_infrastructure_events", lambda _path: [])
    report = pilot.pilot.summarize(None, Experiment(), tasks, arms, tmp_path)
    pilot.correct_labels(tmp_path, report)

    assert report["complete"] is True
    assert sum(len(rows) for rows in report["raw_rows"].values()) == 144
    assert all(len(report["raw_rows"][arm]) == 24 for arm in audit_module.ARMS)
    assert set(report["table2"]) == {"Fin-269", "Fin-1.5K"}
    assert set(report["table3"]) == {"Fin-269", "Fin-1.5K"}
    assert all("interaction" in report["table3"][dataset] for dataset in report["table3"])
    assert len((tmp_path / "raw-records.jsonl").read_text().splitlines()) == 144
    assert all(
        (tmp_path / name).is_file()
        for name in ("report.json", "TABLES.md", "table2.tex", "table3.tex")
    )
    assert "24-family" in (tmp_path / "TABLES.md").read_text()
