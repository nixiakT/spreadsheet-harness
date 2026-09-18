from __future__ import annotations

import importlib.util
import json
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
    "paper24_infrastructure_recovery_v2_test",
    BASE / "benchmarks/recover_paper24_infrastructure_v2_20260918.py",
)


def write_cell(path: Path, source_files: dict[str, str], *, scored: bool = True) -> None:
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(
        json.dumps({"implementation": {"source_files": source_files}}),
        encoding="utf-8",
    )
    if scored:
        (path / "summary.json").write_text("{}", encoding="utf-8")


def fake_runtime(tmp_path: Path, monkeypatch):
    canonical_files = {"agent.py": "canonical"}
    canonical_variant = recovery.hashlib.sha256(
        recovery.json.dumps(canonical_files, sort_keys=True).encode()
    ).hexdigest()
    monkeypatch.setattr(recovery.v1, "CANONICAL_SOURCE_VARIANT", canonical_variant)

    controller = SimpleNamespace(
        is_scored_summary=lambda path: path.is_file(),
        has_unrecovered_model_failure=lambda _path: False,
    )

    class BaseExperiment:
        def __init__(self):
            self.executions = 0
            self.skips = 0

        def task_dir(self, _phase, label, task):
            return tmp_path / label / task.task_id

        def recover_scored_archive(self, output):
            return (output / "summary.json").is_file()

        def run_one(self, phase, label, _skill_root, task, _max_model_calls=None):
            output = self.task_dir(phase, label, task)
            if self.recover_scored_archive(output):
                self.skips += 1
                return
            self.executions += 1
            if output.exists():
                output.rename(output.with_name(output.name + ".incomplete.base"))
            write_cell(output, canonical_files)

    experiment_class = recovery.source_aware_experiment_class(
        BaseExperiment, controller
    )
    return canonical_files, controller, experiment_class()


def test_noncanonical_scored_live_cell_is_quarantined_and_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    canonical_files, _controller, experiment = fake_runtime(tmp_path, monkeypatch)
    task = SimpleNamespace(task_id="task")
    output = experiment.task_dir("heldout-pilot", "alt_h_only", task)
    write_cell(output, {"agent.py": "noncanonical"})

    experiment.run_one("heldout-pilot", "alt_h_only", tmp_path, task, 50)

    assert experiment.executions == 1
    assert experiment.skips == 0
    assert recovery.source_variant(output) == recovery.v1.CANONICAL_SOURCE_VARIANT
    quarantines = list(output.parent.glob(output.name + ".source-quarantine.*"))
    assert len(quarantines) == 1
    assert json.loads((quarantines[0] / "manifest.json").read_text())[
        "implementation"
    ]["source_files"] == {"agent.py": "noncanonical"}
    assert canonical_files == {"agent.py": "canonical"}


def test_noncanonical_incomplete_archive_is_never_restored(
    tmp_path: Path, monkeypatch
) -> None:
    _canonical_files, _controller, experiment = fake_runtime(tmp_path, monkeypatch)
    task = SimpleNamespace(task_id="task")
    output = experiment.task_dir("heldout-pilot", "initial", task)
    archive = output.with_name(output.name + ".incomplete.old")
    write_cell(archive, {"agent.py": "noncanonical"})

    experiment.run_one("heldout-pilot", "initial", tmp_path, task, 50)

    assert experiment.executions == 1
    assert archive.is_dir()
    assert recovery.source_variant(output) == recovery.v1.CANONICAL_SOURCE_VARIANT


def test_canonical_valid_archive_can_be_restored(tmp_path: Path, monkeypatch) -> None:
    canonical_files, _controller, experiment = fake_runtime(tmp_path, monkeypatch)
    task = SimpleNamespace(task_id="task")
    output = experiment.task_dir("heldout-pilot", "initial", task)
    archive = output.with_name(output.name + ".incomplete.old")
    write_cell(archive, canonical_files)

    experiment.run_one("heldout-pilot", "initial", tmp_path, task, 50)

    assert experiment.executions == 0
    assert experiment.skips == 1
    assert output.is_dir()
    assert not archive.exists()


def test_postcondition_rejects_noncanonical_return(tmp_path: Path, monkeypatch) -> None:
    _canonical_files, controller, experiment = fake_runtime(tmp_path, monkeypatch)
    task = SimpleNamespace(task_id="task")

    def always_invalid(_controller, _output):
        return False

    monkeypatch.setattr(recovery.v1, "is_valid_scored_cell", always_invalid)
    with pytest.raises(RuntimeError, match="without a canonical valid scored cell"):
        experiment.run_one("heldout-pilot", "initial", tmp_path, task, 50)
    assert controller is not None


def test_superseding_amendment_seals_v1_and_v2_runners(
    tmp_path: Path, monkeypatch
) -> None:
    v1_runner = tmp_path / "v1.py"
    v1_runner.write_text("# sealed v1\n", encoding="utf-8")
    v1_amendment = tmp_path / recovery.V1_AMENDMENT
    v1_amendment.write_text(
        json.dumps(
            {
                "schema_version": "paper24-infrastructure-recovery-amendment-v1",
                "runner": str(v1_runner),
                "runner_sha256": recovery.v1.digest(v1_runner),
            }
        ),
        encoding="utf-8",
    )
    task = SimpleNamespace(dataset="Fin-269", task_id="Financial_Model/task")
    amendment = recovery.write_or_verify_superseding_amendment(
        tmp_path, {"contract": "fixed"}, [], [("initial", task)]
    )
    document = json.loads(amendment.read_text())
    assert document["supersedes"]["sha256"] == recovery.v1.digest(v1_amendment)
    assert document["runner_sha256"] == recovery.v1.digest(
        Path(recovery.__file__).resolve()
    )
    assert document["initial_scope"]["cells_eligible_for_recovery"] == 1

    recovery.write_or_verify_superseding_amendment(
        tmp_path, {"contract": "fixed"}, [], [("initial", task)]
    )
    v1_amendment.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="v1 recovery amendment"):
        recovery.write_or_verify_superseding_amendment(
            tmp_path, {"contract": "fixed"}, [], [("initial", task)]
        )


def test_orphaned_worker_detection_is_scoped_to_pilot(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    pilot_root = tmp_path / "pilot"
    for pid, target in ((101, pilot_root / "runs/cell"), (102, tmp_path / "unrelated")):
        path = proc / str(pid)
        path.mkdir(parents=True)
        (path / "cmdline").write_bytes(b"\0".join(value.encode() for value in (
            "python", "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare", "--output", str(target)
        )))
    assert recovery.active_pilot_processes(pilot_root, proc) == [101]


def test_identity_mismatch_archive_cannot_be_restored(tmp_path: Path, monkeypatch) -> None:
    canonical_files, controller, experiment = fake_runtime(tmp_path, monkeypatch)
    base_class = type(experiment).__bases__[0]
    checked = recovery.source_aware_experiment_class(
        base_class, controller, compatible=lambda candidate, output: candidate == output
    )()
    task = SimpleNamespace(task_id="task")
    output = checked.task_dir("heldout-pilot", "initial", task)
    archive = output.with_name(output.name + ".incomplete.other-task")
    write_cell(archive, canonical_files)
    checked.run_one("heldout-pilot", "initial", tmp_path, task, 50)
    assert checked.executions == 1
    assert checked.skips == 0
    assert archive.is_dir()
