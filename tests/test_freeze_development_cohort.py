from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_tool() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "freeze_development_cohort.py"
    spec = importlib.util.spec_from_file_location("freeze_development_cohort", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


@dataclass(frozen=True)
class SyntheticInventory:
    args: tuple[str, ...]
    prereg: Path
    task_list: Path
    selected_ids: tuple[str, ...]
    all_ids: tuple[str, ...]
    results_root: Path


def _write_json(path: Path, document: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


@pytest.fixture
def synthetic_inventory(tmp_path: Path) -> SyntheticInventory:
    repository_root = tmp_path / "repository"
    public_root = repository_root / "public-protocols"
    results_root = repository_root / "results"
    private_root = tmp_path / "private"
    for root in (public_root, results_root, private_root):
        root.mkdir(parents=True)
    private_root.chmod(0o700)

    ids = tuple(f"opaque-task-{index:02d}" for index in range(13))
    instruction_types = (
        "Cell",
        "Sheet",
        "Cell",
        "Sheet",
        "Cell",
        "Sheet",
        "Cell",
        "Sheet",
        "Cell",
        "Sheet",
        "Sheet",
        "Cell",
        "Cell",
    )
    dataset = repository_root / "dataset.json"
    _write_json(
        dataset,
        [
            {
                "id": task_id,
                "instruction_type": instruction_type,
                "instruction": f"unused-instruction-secret-{index}",
                "answer_position": f"unused-answer-{index}",
                "golden_path": f"unused-golden-{index}.xlsx",
            }
            for index, (task_id, instruction_type) in enumerate(
                zip(ids, instruction_types, strict=True)
            )
        ],
    )

    # A manifest queue is exposure even when none of its tasks has a result row.
    _write_json(
        public_root / "frozen-protocol.json",
        {
            "task_ids": [ids[0]],
            "task_count": 1,
            "instruction": ids[10],
            "outcomes": {ids[0]: ids[10]},
        },
    )
    (public_root / "listed-task-ids.txt").write_text(f"{ids[1]}\n", encoding="utf-8")

    _write_json(
        results_root / "comparison-manifest.json",
        {"task_ids": [ids[2]], "tasks": [{"task_id": ids[2]}]},
    )
    (results_root / "results.jsonl").write_text(
        json.dumps({"task_id": ids[3], "passed": True, "outcome": "unused"}) + "\n",
        encoding="utf-8",
    )
    _write_json(
        results_root / ".inflight-arm-task.json",
        {"task_id": ids[4], "arm": "ours"},
    )
    _write_json(
        results_root / "interrupted-arm-tasks.json",
        {"seals": [{"task_id": ids[5], "status": "interrupted"}]},
    )
    _write_json(
        results_root / "runs" / "opaque" / "ours" / "run.json",
        {
            "task": {"task_id": ids[6], "instruction": "unused-run-instruction"},
            "result": {"passed": False, "outcome": "unused-run-outcome"},
        },
    )

    _write_json(
        private_root / "prior-prereg.json",
        {"selection": {"task_ids": [ids[7]]}},
        mode=0o600,
    )
    prior_task_list = private_root / "prior-task-ids.txt"
    prior_task_list.write_text(f"{ids[8]}\n", encoding="utf-8")
    prior_task_list.chmod(0o600)

    code_inventory = repository_root / "protected_cohorts.py"
    code_inventory.write_text(
        'PROTECTED_TASKS = ("' + ids[9] + '",)\n'
        'PROTECTED_EVALUATION_COHORTS = {"frozen": PROTECTED_TASKS}\n',
        encoding="utf-8",
    )

    prereg = private_root / "new-prereg.json"
    task_list = private_root / "new-task-ids.txt"
    common_args = (
        "--repository-root",
        str(repository_root),
        "--dataset",
        str(dataset),
        "--expected-dataset-sha256",
        hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "--window-start",
        "0",
        "--window-stop",
        str(len(ids)),
        "--public-protocol-root",
        str(public_root),
        "--results-root",
        str(results_root),
        "--private-inventory-root",
        str(private_root),
        "--code-inventory",
        str(code_inventory),
        "--count",
        "2",
        "--quota",
        "Cell=1",
        "--quota",
        "Sheet=1",
        "--expected-window-count",
        str(len(ids)),
        "--expected-exposed-count",
        "10",
        "--expected-eligible-count",
        "3",
        "--expected-eligible-type-count",
        "Cell=2",
        "--expected-eligible-type-count",
        "Sheet=1",
        "--prereg",
        str(prereg),
        "--task-list",
        str(task_list),
    )
    return SyntheticInventory(
        args=common_args,
        prereg=prereg,
        task_list=task_list,
        selected_ids=(ids[10], ids[11]),
        all_ids=ids,
        results_root=results_root,
    )


def _run(command: str, inventory: SyntheticInventory) -> int:
    return TOOL.main([command, *inventory.args])


def test_freeze_scans_every_exposure_artifact_and_writes_owner_only_outputs(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("freeze", synthetic_inventory) == 0

    captured = capsys.readouterr()
    counts = json.loads(captured.out)
    assert counts == {
        "eligible_rows": 3,
        "exposed_rows": 10,
        "inventory_sources": 10,
        "selected_rows": 2,
        "window_rows": 13,
    }
    assert captured.err == ""
    assert not any(task_id in captured.out for task_id in synthetic_inventory.all_ids)
    assert re.search(r"\b[0-9a-f]{64}\b", captured.out) is None

    assert stat.S_IMODE(synthetic_inventory.prereg.stat().st_mode) == 0o600
    assert stat.S_IMODE(synthetic_inventory.task_list.stat().st_mode) == 0o600
    assert synthetic_inventory.task_list.read_text(encoding="utf-8").splitlines() == list(
        synthetic_inventory.selected_ids
    )
    prereg = json.loads(synthetic_inventory.prereg.read_text(encoding="ascii"))
    assert prereg["selection"]["task_ids"] == list(synthetic_inventory.selected_ids)
    assert [task["raw_index"] for task in prereg["selection"]["tasks"]] == [10, 11]


def test_verify_accepts_unchanged_freeze_and_prints_counts_only(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("freeze", synthetic_inventory) == 0
    capsys.readouterr()

    assert _run("verify", synthetic_inventory) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["selected_rows"] == 2
    assert captured.err == ""
    assert not any(task_id in captured.out for task_id in synthetic_inventory.all_ids)
    assert re.search(r"\b[0-9a-f]{64}\b", captured.out) is None


def test_verify_fails_closed_when_orphan_results_journal_drifts(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("freeze", synthetic_inventory) == 0
    capsys.readouterr()
    results = synthetic_inventory.results_root / "results.jsonl"
    with results.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"task_id": synthetic_inventory.all_ids[0]}) + "\n")

    assert _run("verify", synthetic_inventory) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "verification failed" in captured.err
    assert not any(task_id in captured.err for task_id in synthetic_inventory.all_ids)


@pytest.mark.parametrize("target", ["prereg", "task_list"])
def test_verify_rejects_output_tampering(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    assert _run("freeze", synthetic_inventory) == 0
    capsys.readouterr()
    path = getattr(synthetic_inventory, target)
    if target == "prereg":
        document = json.loads(path.read_text(encoding="ascii"))
        document["selection"]["task_count"] = 99
        path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="ascii")
    else:
        path.write_text("tampered-task-id\n", encoding="utf-8")
    path.chmod(0o600)

    assert _run("verify", synthetic_inventory) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "verification failed" in captured.err


def test_freeze_refuses_overwrite_without_changing_outputs(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("freeze", synthetic_inventory) == 0
    capsys.readouterr()
    prereg_before = synthetic_inventory.prereg.read_bytes()
    task_list_before = synthetic_inventory.task_list.read_bytes()

    assert _run("freeze", synthetic_inventory) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "overwrite" in captured.err
    assert synthetic_inventory.prereg.read_bytes() == prereg_before
    assert synthetic_inventory.task_list.read_bytes() == task_list_before


def test_freeze_rejects_output_inside_repository_before_creating_parent(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(synthetic_inventory.args)
    repository_root = Path(args[args.index("--repository-root") + 1])
    bad_parent = repository_root / "bad-private-output"
    args[args.index("--prereg") + 1] = str(bad_parent / "prereg.json")

    assert TOOL.main(["freeze", *args]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parent directory" in captured.err
    assert not bad_parent.exists()


def test_freeze_rejects_existing_output_parent_inside_repository(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(synthetic_inventory.args)
    repository_root = Path(args[args.index("--repository-root") + 1])
    bad_parent = repository_root / "bad-private-output"
    bad_parent.mkdir()
    bad_parent.chmod(0o700)
    args[args.index("--prereg") + 1] = str(bad_parent / "prereg.json")

    assert TOOL.main(["freeze", *args]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside the repository" in captured.err


def test_freeze_rejects_output_outside_private_root_before_creating_parent(
    synthetic_inventory: SyntheticInventory,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(synthetic_inventory.args)
    bad_parent = tmp_path / "not-private-root" / "nested"
    args[args.index("--task-list") + 1] = str(bad_parent / "task-ids.txt")

    assert TOOL.main(["freeze", *args]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parent directory" in captured.err
    assert not bad_parent.exists()


def test_freeze_rejects_existing_output_parent_outside_private_root(
    synthetic_inventory: SyntheticInventory,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(synthetic_inventory.args)
    bad_parent = tmp_path / "not-private-root"
    bad_parent.mkdir()
    bad_parent.chmod(0o700)
    args[args.index("--task-list") + 1] = str(bad_parent / "task-ids.txt")

    assert TOOL.main(["freeze", *args]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inside a private inventory root" in captured.err


def test_verify_rejects_non_owner_only_private_outputs(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("freeze", synthetic_inventory) == 0
    capsys.readouterr()
    synthetic_inventory.task_list.chmod(0o640)

    assert _run("verify", synthetic_inventory) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "0600" in captured.err


def test_freeze_rejects_non_owner_only_output_parent(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    synthetic_inventory.prereg.parent.chmod(0o750)

    assert _run("freeze", synthetic_inventory) == 2
    captured = capsys.readouterr()
    assert "0700" in captured.err
    assert not synthetic_inventory.prereg.exists()
    assert not synthetic_inventory.task_list.exists()


def test_verify_rejects_non_owner_only_output_parent(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("freeze", synthetic_inventory) == 0
    capsys.readouterr()
    synthetic_inventory.prereg.parent.chmod(0o750)

    assert _run("verify", synthetic_inventory) == 2
    captured = capsys.readouterr()
    assert "0700" in captured.err


def test_expected_inventory_count_is_an_explicit_gate(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(synthetic_inventory.args)
    position = args.index("--expected-eligible-count") + 1
    args[position] = "69"

    assert TOOL.main(["freeze", *args]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "expected eligible count" in captured.err
    assert not synthetic_inventory.prereg.exists()
    assert not synthetic_inventory.task_list.exists()


def test_dataset_root_resolves_only_its_dataset_metadata_file(
    synthetic_inventory: SyntheticInventory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = list(synthetic_inventory.args)
    position = args.index("--dataset") + 1
    args[position] = str(Path(args[position]).parent)

    assert TOOL.main(["freeze", *args]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["selected_rows"] == 2
    assert captured.err == ""
