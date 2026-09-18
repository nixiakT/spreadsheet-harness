from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


controller = module(
    "true_coevolution_controller_test",
    BASE / "benchmarks/run_true_coevolution_20260911.py",
)


def row(*, role: str, exact: float, modification: float, regression: float):
    return {
        "role": role,
        "dataset": "v06",
        "complexity": "C1",
        "accuracy": exact,
        "modification_accuracy": modification,
        "regression_accuracy": regression,
        "model_calls": 1,
    }


def test_solver_and_generator_are_separate_and_coordinate_schedule_is_configurable():
    args = controller.parse_args(
        [
            "--model",
            "qwen3.6-plus",
            "--generator-model",
            "dashscope/glm-5.2",
            "--coordinate-sequence",
            "D,H",
            "--target-rounds",
            "2",
        ]
    )
    assert args.model == "qwen3.6-plus"
    assert args.generator_model == "dashscope/glm-5.2"
    assert args.coordinates == (
        ("domain", "spreadsheet-financial-model"),
        ("harness", "spreadsheet-structure"),
    )


def test_strict_gate_requires_positive_quality_and_nonnegative_modification():
    incumbent = [
        row(role="development", exact=0, modification=0.50, regression=1),
        row(role="transfer", exact=0, modification=0.50, regression=1),
        row(role="regression", exact=0, modification=0.50, regression=1),
    ]
    tied = [dict(item) for item in incumbent]
    decision = controller.candidate_decision(
        incumbent,
        tied,
        min_quality_delta=1e-6,
        min_modification_delta=0.0,
    )
    assert not decision["accepted"]
    assert "weighted-quality-below-required-delta" in decision["blockers"]

    improved = [dict(item, modification_accuracy=0.60) for item in incumbent]
    decision = controller.candidate_decision(
        incumbent,
        improved,
        min_quality_delta=1e-6,
        min_modification_delta=0.0,
    )
    assert decision["accepted"]


def test_only_unrecovered_final_model_failure_is_infrastructure(tmp_path):
    trace = tmp_path / "runs/task/arm/trajectory.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text(
        '{"event":"model.failed"}\n{"event":"model.responded"}\n', encoding="utf-8"
    )
    assert not controller.has_unrecovered_model_failure(tmp_path)
    trace.write_text(
        '{"event":"model.responded"}\n{"event":"model.failed"}\n', encoding="utf-8"
    )
    assert controller.has_unrecovered_model_failure(tmp_path)


def test_resume_does_not_accept_scored_summary_with_unrecovered_failure(tmp_path, monkeypatch):
    experiment = controller.Experiment(
        SimpleNamespace(result_root=tmp_path),
        [],
    )
    output = tmp_path / "runs/task"
    output.mkdir(parents=True)
    monkeypatch.setattr(controller, "is_scored_summary", lambda _: True)
    monkeypatch.setattr(controller, "has_unrecovered_model_failure", lambda _: True)
    assert not experiment.recover_scored_archive(output)


def test_accelerated_gate_excludes_known_infrastructure_long_tail_case():
    assert (
        "Financial_Model/fina_Fina_eus_140c5d9253_FinStmtEx_c0"
        in controller.RECURRENT_GATE_EXCLUSIONS
    )


def test_attribution_is_multilabel_and_passing_tool_friction_is_not_a_failure(
    tmp_path, monkeypatch
):
    args = SimpleNamespace(result_root=tmp_path)
    failed = controller.Task("v06", BASE, "Financial_Model/failed", "C2", "a", "development")
    passed = controller.Task("v06", BASE, "Financial_Model/passed", "C2", "b", "development")
    experiment = controller.Experiment(args, [failed, passed])
    scores = {
        "failed": {"accuracy": 0.0, "modification_accuracy": 0.5, "regression_accuracy": 0.9},
        "passed": {"accuracy": 1.0, "modification_accuracy": 1.0, "regression_accuracy": 1.0},
    }
    monkeypatch.setattr(
        controller,
        "summary_payload",
        lambda path: {
            **scores[path.name.removeprefix("Financial_Model_")],
            "model_calls": 1,
            "errors": 0,
            "error_message": "",
        },
    )
    monkeypatch.setattr(controller, "has_unrecovered_model_failure", lambda _: False)
    for task in (failed, passed):
        trace = experiment.task_dir("phase", "label", task) / "trajectory.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text(
            '{"event":"agent.completed","payload":{"tool_errors":1}}\n', encoding="utf-8"
        )
    rows = experiment.attribute_failures("phase", "label", [failed, passed], tmp_path / "a.json")
    assert rows[0]["routes"] == ["interface", "harness", "domain"]
    assert rows[1]["routes"] == ["none"]


def test_candidate_generation_passes_explicit_attribution_context(tmp_path, monkeypatch):
    args = SimpleNamespace(
        result_root=tmp_path,
        base_url="http://example.invalid",
        api_key_file=tmp_path / "key",
        generator_model="dashscope/glm-5.2",
        request_interval=0.5,
    )
    task = controller.Task("v06", BASE, "Financial_Model/failed", "C2", "a", "development")
    experiment = controller.Experiment(args, [task])
    run = experiment.task_dir("phase", "label", task)
    trace = run / "trajectory.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"event":"spreadsheetbench_v2.evaluated","payload":{}}\n')
    monkeypatch.setattr(
        experiment,
        "attribute_failures",
        lambda *a, **k: [
            {
                **task.to_dict(),
                "routes": ["domain"],
                "reasons": ["target failure"],
                "score": {
                    "accuracy": 0.0,
                    "modification_accuracy": 0.5,
                    "regression_accuracy": 1.0,
                },
            }
        ],
    )
    incumbent = tmp_path / "incumbent"
    skill = incumbent / "spreadsheet-financial-model/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: spreadsheet-financial-model\ndescription: test\n---\nbase\n")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        candidate = tmp_path / "generation/r01-domain-a1/candidates/r01-domain-a1/SKILL.md"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(skill.read_text())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    experiment.generate_candidate(
        round_number=1,
        attempt=1,
        coordinate="domain",
        skill_name="spreadsheet-financial-model",
        incumbent_root=incumbent,
        evidence_phase="phase",
        evidence_label="label",
        evidence_tasks=[task],
    )
    command = seen["command"]
    assert command[command.index("--request-timeout") + 1] == "1800"
    assert command[command.index("--request-retries") + 1] == "5"
    context_path = Path(command[command.index("--attribution-context") + 1])
    context = controller.load_json(context_path)
    assert context["requested_coordinate"] == "domain"
    assert next(iter(context["records"].values()))["evidence_role"] == "repair"


def test_retry_pairs_rejected_regression_with_current_incumbent_anchor(tmp_path, monkeypatch):
    args = SimpleNamespace(
        result_root=tmp_path,
        base_url="http://example.invalid",
        api_key_file=tmp_path / "key",
        generator_model="dashscope/glm-5.2",
        request_interval=0.5,
    )
    task = controller.Task("v06", BASE, "Financial_Model/control", "C2", "a", "development")
    experiment = controller.Experiment(args, [task])
    for label in ("incumbent-r00", "r01-harness-a1"):
        trace = experiment.task_dir("evolution", label, task) / "trajectory.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text('{"event":"spreadsheetbench_v2.evaluated","payload":{}}\n')
    def attribution(_phase, label, _tasks, _output):
        passed = label == "incumbent-r00"
        return [{
            **task.to_dict(),
            "routes": ["none"] if passed else ["harness"],
            "reasons": ["incumbent passed"] if passed else ["rejected candidate regressed"],
            "score": {
                "accuracy": 1.0 if passed else 0.0,
                "modification_accuracy": 1.0 if passed else 0.5,
                "regression_accuracy": 1.0 if passed else 0.9,
            },
        }]
    monkeypatch.setattr(experiment, "attribute_failures", attribution)
    monkeypatch.setattr(
        controller,
        "summary_payload",
        lambda path: {
            "accuracy": 1.0 if "incumbent-r00" in str(path) else 0.0,
            "modification_accuracy": 1.0,
            "regression_accuracy": 1.0,
            "model_calls": 1,
            "errors": 0,
            "error_message": "",
        },
    )
    incumbent = tmp_path / "incumbent"
    skill = incumbent / "spreadsheet-structure/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: spreadsheet-structure\ndescription: test\n---\nbase\n")
    seen = {}
    def fake_run(command, **kwargs):
        seen["command"] = command
        candidate = tmp_path / "generation/r01-harness-a2/candidates/r01-harness-a2/SKILL.md"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(skill.read_text())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    experiment.generate_candidate(
        round_number=1, attempt=2, coordinate="harness", skill_name="spreadsheet-structure",
        incumbent_root=incumbent, evidence_phase="evolution", evidence_label="r01-harness-a1",
        evidence_tasks=[task], anchor_phase="evolution", anchor_label="incumbent-r00",
    )
    context_path = Path(seen["command"][seen["command"].index("--attribution-context") + 1])
    roles = {record["evidence_role"] for record in controller.load_json(context_path)["records"].values()}
    assert roles == {"rejected-candidate-regression", "current-incumbent-no-regression-anchor"}


def test_retry_uses_current_incumbent_failure_as_repair_target(tmp_path, monkeypatch):
    args = SimpleNamespace(
        result_root=tmp_path, base_url="http://example.invalid", api_key_file=tmp_path / "key",
        generator_model="dashscope/glm-5.2", request_interval=0.5,
    )
    task = controller.Task("v06", BASE, "Financial_Model/failure", "C2", "a", "development")
    experiment = controller.Experiment(args, [task])
    for label in ("accepted-incumbent", "rejected-a1"):
        trace = experiment.task_dir("evolution", label, task) / "trajectory.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text('{"event":"spreadsheetbench_v2.evaluated","payload":{}}\n')

    def attribution(_phase, label, _tasks, _output):
        candidate_passed = label == "rejected-a1"
        return [{
            **task.to_dict(), "routes": ["none"] if candidate_passed else ["domain"],
            "reasons": ["candidate fixed target"] if candidate_passed else ["incumbent target miss"],
            "score": {"accuracy": 1.0 if candidate_passed else 0.0,
                      "modification_accuracy": 1.0 if candidate_passed else 0.5,
                      "regression_accuracy": 1.0},
        }]
    monkeypatch.setattr(experiment, "attribute_failures", attribution)
    incumbent = tmp_path / "incumbent"
    skill = incumbent / "spreadsheet-financial-model/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: spreadsheet-financial-model\ndescription: test\n---\nbase\n")
    seen = {}
    def fake_run(command, **kwargs):
        seen["command"] = command
        candidate = tmp_path / "generation/r02-domain-a2/candidates/r02-domain-a2/SKILL.md"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(skill.read_text())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    experiment.generate_candidate(
        round_number=2, attempt=2, coordinate="domain",
        skill_name="spreadsheet-financial-model", incumbent_root=incumbent,
        evidence_phase="evolution", evidence_label="rejected-a1", evidence_tasks=[task],
        anchor_phase="evolution", anchor_label="accepted-incumbent",
    )
    context_path = Path(seen["command"][seen["command"].index("--attribution-context") + 1])
    roles = {record["evidence_role"] for record in controller.load_json(context_path)["records"].values()}
    assert roles == {"repair", "successful-rejected-candidate-example"}
