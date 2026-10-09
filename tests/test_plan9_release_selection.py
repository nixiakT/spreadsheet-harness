"""Offline regression checks for the portable release scheduler."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "releases/fin15k-plan9-20261009"
sys.path.insert(0, str(RELEASE / "baseline/artifact/src"))
spec = importlib.util.spec_from_file_location("plan9_release_scheduler", RELEASE / "runtime/scheduler.py")
scheduler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scheduler
spec.loader.exec_module(scheduler)


def test_evidence_size_selection_is_optional_and_repeatable():
    default = scheduler.parser().parse_args(["--results", "out"])
    assert default.evidence_size is None
    selected = scheduler.parser().parse_args(["--results", "out", "--evidence-size", "500"])
    assert selected.evidence_size == [500]
    multiple = scheduler.parser().parse_args([
        "--results", "out", "--evidence-size", "200", "--evidence-size", "500",
    ])
    assert multiple.evidence_size == [200, 500]


def test_jobs_are_four_arms_per_task_for_500_group():
    from types import SimpleNamespace
    tasks = [SimpleNamespace(task_id="Debugging/1", category="Debugging", item_id="1")]
    arms = [SimpleNamespace(label=name) for name in ["baseline", "500-d-only", "500-h-only", "500-joint"]]
    jobs = scheduler.make_jobs(arms, tasks)
    assert len(jobs) == 4
    assert {job["arm"] for job in jobs} == {arm.label for arm in arms}
