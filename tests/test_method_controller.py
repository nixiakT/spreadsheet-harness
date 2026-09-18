from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


controller = module("method_controller_test", BASE / "benchmarks/run_evidence_gated_method.py")
runner = module("method_runner_test", BASE / "benchmarks/run_attributed_evolution.py")


def test_prepare_freezes_selected_solver_and_probe_interval(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    # Argument plumbing is checked independently of the large frozen snapshot.
    seen = {}
    monkeypatch.setattr(controller, "prepare", lambda root, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_evidence_gated_method.py",
            "--root",
            str(source),
            "--prepare",
            "--solver",
            "qwen3.6-plus",
            "--probe-interval-seconds",
            "120",
        ],
    )
    controller.main()
    assert seen == {"solver": "qwen3.6-plus", "probe_interval_seconds": 120}


def test_provider_outage_keeps_same_trial_and_search_version(tmp_path, monkeypatch):
    controller.write(tmp_path / "search.json", {"incumbent": "seed", "completed_trials": 0})
    monkeypatch.setattr(controller, "solver_health", lambda _: {"healthy": False, "http_status": 500})
    assert not controller.ensure_health(tmp_path, {}, "trial-01/smoke")
    assert controller.load(tmp_path / "status.json")["phase"] == "paused_infrastructure"
    assert controller.load(tmp_path / "search.json") == {"incumbent": "seed", "completed_trials": 0}


def test_locked_test_resume_never_generates_another_candidate(tmp_path, monkeypatch):
    controller.write(tmp_path / "search.json", {"incumbent": "trial-01", "completed_trials": 4})
    controller.write(tmp_path / "locked-test-version.json", {"version": "trial-01"})
    seen = []
    monkeypatch.setattr(controller, "finish_test", lambda *args: seen.append("test") or True)
    monkeypatch.setattr(controller, "create_trial", lambda *args: pytest.fail("Test leaked into adaptation"))
    assert controller.run_once(tmp_path, {"max_completed_trials": 4})
    assert seen == ["test"]


def test_paused_batch_runs_no_solver_calls_and_preserves_scored(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "runner", runner)
    task = {"dataset": "v06", "task_id": "Financial_Model/x", "role": "replay"}
    output = tmp_path / "runs/round-00/seed/replay/v06/Financial_Model_x/cell.json"
    controller.write(output, {"status": "scored", "exact": 1.0})
    monkeypatch.setattr(controller, "ensure_health", lambda *args: False)
    monkeypatch.setattr(controller, "run_version_cell", lambda *args: pytest.fail("Must pause"))
    result = controller.checked_batch(tmp_path, {}, [task], ["seed", "trial-01"], "smoke")
    assert result is None
    assert controller.load(output)["exact"] == 1.0


def test_infrastructure_recovery_uses_new_attempt_not_cached_error(tmp_path, monkeypatch):
    task = {"dataset": "v06", "task_id": "Financial_Model/x", "role": "replay",
            "input_path": str(tmp_path / "input.xlsx")}
    Path(task["input_path"]).write_bytes(b"input")
    task["input_sha256"] = runner.digest(task["input_path"])
    cellroot = tmp_path / "runs/round-00/trial-01/replay/v06/Financial_Model_x"
    runner.write(cellroot / "cell.json", {"status": "infrastructure", "attempt": 1, "exact": None})
    skillroot = tmp_path / "skills"
    skillroot.mkdir()
    composition = tmp_path / "composition.json"
    composition.write_text("{}")
    monkeypatch.setattr(runner, "arm_paths", lambda *args: (skillroot, composition))
    monkeypatch.setattr(runner, "expected_skills", lambda *args: {"H": "hash"})
    monkeypatch.setattr(runner, "audit_activation", lambda *args: True)
    monkeypatch.setattr(runner, "child_environment", lambda *args: {})
    key = tmp_path / "key"
    key.write_text("fake")
    monkeypatch.setattr(runner, "KEY_FILE", key)
    called = []
    def run(command, **kwargs):
        out = Path(command[command.index("--output")+1])
        called.append(out.name)
        out.mkdir(parents=True)
        trace = out / "runs/Financial_Model/x/ours/trajectory.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text('{"event":"agent.started","payload":{}}\n')
        runner.write(out / "summary.json", {"study_complete": True, "arms":{"ours":{
            "accuracy": 0.0, "modification_accuracy": 0.4, "regression_accuracy": 1.0}}})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", run)
    protocol = {"recover_infrastructure":True,"task_timeout":3600,"request_timeout":600,
                "base_url":"http://example.invalid","solver":"fake"}
    result = runner.run_cell(tmp_path, protocol, task, "trial-01", 0)
    assert result["status"] == "scored" and result["exact"] == 0
    assert called == ["attempt-2"]
    assert (cellroot/"cell-attempt-1.json").exists()
    runner.run_cell(tmp_path, protocol, task, "trial-01", 0)
    assert called == ["attempt-2"]


def test_rejection_does_not_change_incumbent_and_is_idempotent(tmp_path):
    controller.write(tmp_path/"search.json", {"incumbent":"seed","completed_trials":0,
                                            "rejections":[],"preferred_group":"D"})
    record = {"number":1,"candidate":"trial-01","parent":"seed",
              "hypothesis":{"id":"h","plugin":"spreadsheet-financial-model","intervention":"test","group":"D"}}
    decision = {"status":"rejected","reason":"no win"}
    controller.complete_trial(tmp_path,record,decision)
    controller.complete_trial(tmp_path,record,decision)
    s = controller.load(tmp_path/"search.json")
    assert s["incumbent"] == "seed" and s["completed_trials"] == 1
    assert len(s["rejections"]) == 1
