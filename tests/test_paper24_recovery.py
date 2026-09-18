from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


BASE = Path(__file__).parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


recovery = module(
    "paper24_infrastructure_recovery_test",
    BASE / "benchmarks/recover_paper24_infrastructure_20260918.py",
)


def test_recovery_protocol_is_fixed_to_original_generation_contract():
    recovery.verify_protocol(
        {
            "solver": "qwen3.6-plus",
            "temperature": 0.0,
            "top_p": 1.0,
            "thinking": True,
            "max_model_calls": 50,
            "max_total_tokens": None,
            "max_output_tokens": None,
            "unique_compositions": list(recovery.EXPECTED_ARMS),
        }
    )
    with pytest.raises(RuntimeError, match="execution contract changed"):
        recovery.verify_protocol(
            {
                "solver": "qwen3.6-plus",
                "temperature": 1.0,
                "top_p": 1.0,
                "thinking": True,
                "max_model_calls": 50,
                "max_total_tokens": None,
                "max_output_tokens": None,
                "unique_compositions": list(recovery.EXPECTED_ARMS),
            }
        )


def test_valid_scored_cell_rejects_unrecovered_model_failure(tmp_path, monkeypatch):
    source_files = {"agent.py": "a" * 64}
    monkeypatch.setattr(
        recovery,
        "CANONICAL_SOURCE_VARIANT",
        recovery.hashlib.sha256(
            recovery.json.dumps(source_files, sort_keys=True).encode()
        ).hexdigest(),
    )
    (tmp_path / "manifest.json").write_text(
        recovery.json.dumps({"implementation": {"source_files": source_files}}),
        encoding="utf-8",
    )
    controller = SimpleNamespace(
        is_scored_summary=lambda _: True,
        has_unrecovered_model_failure=lambda _: False,
    )
    assert recovery.is_valid_scored_cell(controller, tmp_path)
    controller.has_unrecovered_model_failure = lambda _: True
    assert not recovery.is_valid_scored_cell(controller, tmp_path)


def test_valid_scored_cell_rejects_noncanonical_runtime(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(
        recovery.json.dumps({"implementation": {"source_files": {"agent.py": "bad"}}}),
        encoding="utf-8",
    )
    controller = SimpleNamespace(
        is_scored_summary=lambda _: True,
        has_unrecovered_model_failure=lambda _: False,
    )
    assert not recovery.is_valid_scored_cell(controller, tmp_path)


def test_partition_only_retries_unscored_cells(tmp_path, monkeypatch):
    tasks = [SimpleNamespace(task_id=f"Financial_Model/t{i}") for i in range(24)]
    arms = {name: tmp_path / name for name in recovery.EXPECTED_ARMS}
    experiment = SimpleNamespace(
        task_dir=lambda phase, label, task: tmp_path / label / task.task_id.replace("/", "_")
    )
    scored_path = experiment.task_dir("heldout-pilot", "initial", tasks[0])
    monkeypatch.setattr(
        recovery,
        "is_valid_scored_cell",
        lambda controller, output: output == scored_path,
    )
    scored, pending = recovery.partition_cells(object(), experiment, tasks, arms)
    assert scored == [("initial", tasks[0])]
    assert len(pending) == 143
    assert ("initial", tasks[0]) not in pending


def test_recovery_contract_changes_only_task_timeout(tmp_path):
    for name in ("pilot-split.json", "protocol.json", "endpoint-lock.json"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    runtime_lock = tmp_path / "frozen-runtime-source/lock.json"
    runtime_lock.parent.mkdir(parents=True)
    runtime_lock.write_text("{}\n", encoding="utf-8")
    protocol = {
        "solver": "qwen3.6-plus",
        "max_model_calls": 50,
        "max_total_tokens": None,
        "max_output_tokens": None,
        "temperature": 0.0,
        "top_p": 1.0,
        "thinking": True,
    }
    contract = recovery.recovery_contract(
        tmp_path,
        protocol,
        tmp_path / "endpoint-lock.json",
        task_attempts=2,
    )
    assert contract["only_execution_change"] == {
        "task_timeout_seconds": {"from": 3600, "to": 7200}
    }
    assert contract["unchanged_execution"]["max_model_calls"] == 50
    assert contract["unchanged_execution"]["max_turns_per_arm"] == 50
    assert contract["unchanged_execution"]["seed"] == 41
    assert contract["unchanged_execution"]["temperature"] == 0.0
    assert contract["unchanged_execution"]["top_p"] == 1.0
    assert contract["unchanged_execution"]["thinking"] is True
    assert contract["unchanged_execution"]["arm_order_seed"] == 20260911
    assert contract["method_invariants"]["acceptance_gate_changed"] is False
