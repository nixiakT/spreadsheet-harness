from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spreadsheet_harness.continuous_evolution import (
    CandidateProposal,
    ContinuousEvolutionConfig,
    ContinuousEvolutionEngine,
    DeterministicEvidenceRouter,
    EvidenceRef,
    EvolutionRoute,
    SpreadsheetBenchV2EvaluationAdapter,
    distill_evidence,
    evaluate_validation_report,
)
from spreadsheet_harness.errors import HarnessError
from spreadsheet_harness.plugins import BUILTIN_COMPOSITIONS, default_plugin_registry

ROOT = Path(__file__).resolve().parents[1]


def _config(evidence: Path) -> ContinuousEvolutionConfig:
    return ContinuousEvolutionConfig.from_document(
        {
            "repository_root": str(ROOT),
            "composition": "spreadsheet-harness-financial",
            "groups": {
                "harness": ["policy-ours"],
                "domain": ["skill-spreadsheet-formula", "skill-spreadsheet-financial-model"],
            },
            "contexts": [
                {
                    "name": "replay",
                    "kind": "replay",
                    "task_ids": ["replay/1", "replay/2"],
                    "workbook_families": ["family-replay-a", "family-replay-b"],
                    "weight": 1,
                },
                {
                    "name": "transfer",
                    "kind": "transfer",
                    "task_ids": ["transfer/1", "transfer/2"],
                    "workbook_families": ["family-transfer-a", "family-transfer-b"],
                    "weight": 1,
                },
                {
                    "name": "regression",
                    "kind": "regression",
                    "task_ids": ["regression/1", "regression/2"],
                    "workbook_families": ["family-regression-a", "family-regression-b"],
                    "weight": 1,
                },
            ],
            "heldout_task_ids": ["heldout/1"],
            "initial_evidence": [
                {
                    "path": str(evidence),
                    "task_id": "replay/1",
                    "task_type": "formula",
                    "workbook_family": "family-replay-a",
                }
            ],
            "evaluation_binding": {"model": "fixed-test-model", "budget": 1},
            "promotion": {"bootstrap_samples": 200, "min_pairs_per_context": 2},
            "proposer_command": ["proposal-adapter"],
            "evaluator_command": ["evaluation-adapter"],
            "static_checks": [],
        }
    )


class _Proposal:
    def propose(self, request, _round_dir):
        policy = request["plugin_contract"]["edit_policies"]
        prompt = next(item for item in policy if item["surface"] == "prompt")
        return [
            CandidateProposal.from_document(
                {
                    "candidate_id": "formula-v2",
                    "base_revision_sha256": request["base_revision_sha256"],
                    "operation": "edit",
                    "target_plugin": request["route"]["target_plugin"],
                    "surface": request["route"]["surface"],
                    "operator": "replace-file",
                    "files": [
                        {"path": prompt["paths"][0], "content": "---\nname: formula\n---\nnew\n"}
                    ],
                }
            )
        ]


class _Evaluation:
    def __init__(self, evidence: Path):
        self.evidence = evidence

    def evaluate(self, request, _candidate_dir):
        contexts = []
        for context in request["contexts"]:
            contexts.append(
                {
                    "name": context["name"],
                    "kind": context["kind"],
                    "task_set_sha256": context["task_set_sha256"],
                    "pairs": [
                        {
                            "id": task_id,
                            "baseline": 0.0,
                            "candidate": 1.0,
                            "baseline_status": "scored",
                            "candidate_status": "scored",
                        }
                        for task_id in context["task_ids"]
                    ],
                }
            )
        return {
            "schema_version": "continuous-plugin-validation-report-v1",
            "incumbent_revision_sha256": request["incumbent_revision_sha256"],
            "candidate_revision_sha256": request["candidate_revision_sha256"],
            "evaluation_binding_sha256": request["evaluation_binding_sha256"],
            "contexts_sha256": request["contexts_sha256"],
            "heldout_task_ids_sha256": request["heldout_task_ids_sha256"],
            "contexts": contexts,
            "candidate_evidence": [
                {
                    "path": str(self.evidence),
                    "task_id": "replay/1",
                    "task_type": "formula",
                    "workbook_family": "family-replay-a",
                },
                {
                    "path": str(self.evidence),
                    "task_id": "transfer/1",
                    "task_type": "formula",
                    "workbook_family": "family-transfer-a",
                },
            ],
        }


def test_continuous_engine_promotes_and_rolls_back(tmp_path):
    evidence = tmp_path / "trajectory.jsonl"
    evidence.write_text(
        json.dumps(
            {
                "event": "evaluation.completed",
                "payload": {"passed": False, "error_category": "formula"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = _config(evidence)
    engine = ContinuousEvolutionEngine(
        config,
        tmp_path / "workspace",
        proposer=_Proposal(),
        evaluator=_Evaluation(evidence),
    )
    state = engine.run(rounds=1)
    assert state["accepted_rounds"] == 1
    assert {item["task_id"] for item in state["evidence"]} == {"replay/1"}
    assert state["current_revision_sha256"] != state["history"][0]
    assert engine.store.rollback()["current_revision_sha256"] == state["history"][0]


def test_validation_report_rejects_binding_and_unscored_pairs(tmp_path):
    evidence = tmp_path / "trajectory.jsonl"
    evidence.write_text(
        '{"event":"evaluation.completed","payload":{"passed":false}}\n', encoding="utf-8"
    )
    config = _config(evidence)
    report = {
        "incumbent_revision_sha256": "0" * 64,
        "candidate_revision_sha256": "1" * 64,
        "evaluation_binding_sha256": "bad",
        "contexts": [],
    }
    decision = evaluate_validation_report(
        report,
        config=config,
        incumbent_revision="0" * 64,
        candidate_revision="1" * 64,
    )
    assert not decision.promoted
    assert "evaluation-binding-mismatch" in decision.blockers
    assert "missing-candidate-evidence" in decision.blockers


def test_contexts_allow_repeated_family_within_context_but_not_across_contexts(tmp_path):
    evidence = tmp_path / "trajectory.jsonl"
    evidence.write_text(
        '{"event":"evaluation.completed","payload":{"passed":false}}\n', encoding="utf-8"
    )
    document = _config(evidence).to_dict()
    replay = next(item for item in document["contexts"] if item["name"] == "replay")
    replay["task_ids"] = ["replay/1", "replay/1b", "replay/2"]
    replay["workbook_families"] = [
        "family-replay-a",
        "family-replay-a",
        "family-replay-b",
    ]
    config = ContinuousEvolutionConfig.from_document(document)
    assert config.contexts[0].workbook_families.count("family-replay-a") == 2

    transfer = next(item for item in document["contexts"] if item["name"] == "transfer")
    transfer["workbook_families"][0] = "family-replay-a"
    with pytest.raises(HarnessError, match="disjoint by workbook family"):
        ContinuousEvolutionConfig.from_document(document)


def test_validation_aggregates_by_family_and_uses_conjunctive_context_gates(tmp_path):
    evidence = tmp_path / "trajectory.jsonl"
    evidence.write_text(
        '{"event":"evaluation.completed","payload":{"passed":false}}\n', encoding="utf-8"
    )
    document = _config(evidence).to_dict()
    replay_context = next(item for item in document["contexts"] if item["name"] == "replay")
    replay_context["task_ids"] = ["replay/1", "replay/1b", "replay/2"]
    replay_context["workbook_families"] = [
        "family-replay-a",
        "family-replay-a",
        "family-replay-b",
    ]
    config = ContinuousEvolutionConfig.from_document(document)
    heldout_hash = hashlib.sha256(
        json.dumps(
            list(config.heldout_task_ids),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    request = {
        "incumbent_revision_sha256": "0" * 64,
        "candidate_revision_sha256": "1" * 64,
        "evaluation_binding_sha256": config.binding_sha256,
        "contexts_sha256": config.contexts_sha256,
        "heldout_task_ids_sha256": heldout_hash,
        "contexts": [context.to_dict() for context in config.contexts],
    }
    report = dict(_Evaluation(evidence).evaluate(request, tmp_path))
    replay = next(item for item in report["contexts"] if item["name"] == "replay")
    replay["pairs"][0]["candidate"] = 1.0
    replay["pairs"][1]["candidate"] = -1.0
    replay["pairs"][2]["candidate"] = 1.0
    transfer = next(item for item in report["contexts"] if item["name"] == "transfer")
    transfer["pairs"][0]["candidate"] = 0.0
    transfer["pairs"][1]["candidate"] = 0.0
    decision = evaluate_validation_report(
        report,
        config=config,
        incumbent_revision="0" * 64,
        candidate_revision="1" * 64,
    )
    replay_result = next(item for item in decision.context_results if item["name"] == "replay")
    assert replay_result["pair_count"] == 2
    assert decision.aggregate_mean_delta is not None and decision.aggregate_mean_delta > 0
    assert "transfer-lcb-below-delta:transfer" in decision.blockers
    assert not decision.promoted


def _trajectory(path: Path, *, passed: bool, marker: str) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "tool.failed",
                        "payload": {
                            "name": "recalculate",
                            "error_category": "formula",
                            "private_long_text": marker * 1000,
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "evaluation.completed",
                        "payload": {"passed": passed, "error_category": "formula"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_evidence_distillation_clusters_and_bounds_trace_context(tmp_path):
    registry = default_plugin_registry()
    composition = BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"]
    contract = registry.get("skill-spreadsheet-financial-model")
    route = EvolutionRoute(
        "domain",
        "edit",
        contract.name,
        "implementation",
        (),
        12,
        ("active-provider-produced-failed-workbook",),
    )
    evidence = []
    for index in range(12):
        path = tmp_path / f"failure-{index}.jsonl"
        _trajectory(path, passed=False, marker=f"SECRET-{index}")
        evidence.append(
            EvidenceRef(path, f"Financial_Model/{index}", "Financial_Model", f"family-{index}")
        )
    anchor = tmp_path / "anchor.jsonl"
    _trajectory(anchor, passed=True, marker="ANCHOR-SECRET")
    evidence.append(EvidenceRef(anchor, "Financial_Model/pass", "Financial_Model", "family-pass"))

    packet = distill_evidence(
        evidence,
        registry=registry,
        composition=composition,
        route=route,
        representatives_per_prototype=2,
    )

    assert packet["input_trace_count"] == 13
    assert len(packet["failure_prototypes"]) == 1
    prototype = packet["failure_prototypes"][0]
    assert prototype["support_count"] == 12
    assert len(prototype["representatives"]) == 2
    assert len(packet["no_regression_anchors"]) == 1
    encoded = json.dumps(packet)
    assert "SECRET-" not in encoded and "ANCHOR-SECRET" not in encoded
    sketch = prototype["representatives"][0]["causal_sketch"]
    assert sketch["event_counts"]["tool_failure"] == 1
    assert sketch["event_counts"]["evaluation"] == 1
    assert sketch["causal_markers"][-1]["passed"] is False
    assert len(sketch["phase_sequence"]) <= 128
    assert len(packet["packet_sha256"]) == 64


def test_evidence_distillation_causal_sketch_preserves_order_not_payload(tmp_path):
    registry = default_plugin_registry()
    composition = BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"]
    trajectory = tmp_path / "causal.jsonl"
    trajectory.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "tool.called",
                        "payload": {
                            "name": "code_interpreter",
                            "arguments": {"code": "DO-NOT-LEAK-ANSWER=42"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "workbook.mutation.committed",
                        "payload": {"operation": "write_formula", "result": "SECRET-CELL-A1"},
                    }
                ),
                json.dumps(
                    {
                        "event": "agent.formula_runtime_validation_failed",
                        "payload": {"calculation_valid": False, "coordinates": ["SECRET-CELL-A1"]},
                    }
                ),
                json.dumps(
                    {
                        "event": "evaluation.completed",
                        "payload": {"passed": False, "error_category": "formula"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    route = EvolutionRoute(
        "domain",
        "edit",
        "skill-spreadsheet-financial-model",
        "implementation",
        (),
        1,
        ("active-provider-produced-failed-workbook",),
    )
    packet = distill_evidence(
        [EvidenceRef(trajectory, "Financial_Model/causal", "Financial_Model", "family")],
        registry=registry,
        composition=composition,
        route=route,
    )
    sketch = packet["failure_prototypes"][0]["representatives"][0]["causal_sketch"]
    assert sketch["phase_sequence"] == [
        "tool_call",
        "mutation_commit",
        "formula_validation_failure",
        "evaluation",
    ]
    assert sketch["operation_counts"] == {"write_formula": 1}
    assert [item["kind"] for item in sketch["causal_markers"]] == [
        "mutation_commit",
        "formula_validation_failure",
        "evaluation",
    ]
    encoded = json.dumps(packet)
    assert "DO-NOT-LEAK" not in encoded
    assert "SECRET-CELL" not in encoded
    assert "42" not in encoded


def test_coordination_gap_routes_to_template_synthesis(tmp_path):
    trajectory = tmp_path / "gap.jsonl"
    trajectory.write_text(
        json.dumps(
            {
                "event": "evaluation.completed",
                "payload": {
                    "passed": False,
                    "error_category": "missing-provider",
                    "required_capability": "composition",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = default_plugin_registry()
    composition = BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"]
    route = DeterministicEvidenceRouter().route(
        [EvidenceRef(trajectory, "Financial_Model/1", "Financial_Model", "family")],
        registry=registry,
        composition=composition,
        groups={
            "harness": ("skill-spreadsheet-coordination", "policy-ours"),
            "domain": ("skill-spreadsheet-financial-model",),
        },
        preferred_group="harness",
    )
    assert route is not None
    assert route.operation == "synthesize"
    assert route.surface == "prompt"
    assert route.to_dict()["method_operator"] == "synthesis"
    contract = registry.get(route.target_plugin)
    assert contract.synthesis_template == "knowledge-skill-v1"
    assert contract.edit_policy("prompt").paths == ("skills/spreadsheet-coordination/SKILL.md",)


def test_operator_ablation_is_frozen_and_filters_route(tmp_path):
    trajectory = tmp_path / "gap.jsonl"
    trajectory.write_text(
        json.dumps(
            {
                "event": "evaluation.completed",
                "payload": {
                    "passed": False,
                    "error_category": "missing-provider",
                    "required_capability": "composition",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = default_plugin_registry()
    composition = BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"]
    kwargs = {
        "registry": registry,
        "composition": composition,
        "groups": {
            "harness": ("skill-spreadsheet-coordination", "policy-ours"),
            "domain": ("skill-spreadsheet-financial-model",),
        },
        "preferred_group": "harness",
    }
    evidence = [EvidenceRef(trajectory, "Financial_Model/1", "Financial_Model", "family")]
    assert (
        DeterministicEvidenceRouter().route(evidence, allowed_operators=("revision",), **kwargs)
        is None
    )
    synthesis = DeterministicEvidenceRouter().route(
        evidence, allowed_operators=("revision", "synthesis"), **kwargs
    )
    assert synthesis is not None and synthesis.to_dict()["method_operator"] == "synthesis"

    config = _config(trajectory)
    assert config.allowed_operators == ("revision", "recomposition", "synthesis")
    assert config.to_dict()["allowed_operators"] == [
        "revision",
        "recomposition",
        "synthesis",
    ]


def test_profile_truncation_routes_to_deterministic_provider_recomposition(tmp_path):
    trajectory = tmp_path / "truncated.jsonl"
    trajectory.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "preprocess.profile",
                        "payload": {"truncation": {"sheets": True, "rendered": False}},
                    }
                ),
                json.dumps(
                    {
                        "event": "evaluation.completed",
                        "payload": {"passed": False, "error_category": "formula"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    registry = default_plugin_registry()
    route = DeterministicEvidenceRouter().route(
        [EvidenceRef(trajectory, "Financial_Model/1", "Financial_Model", "family")],
        registry=registry,
        composition=BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"],
        groups={
            "harness": ("profile-deterministic-compact", "profile-deterministic-full"),
            "domain": ("skill-spreadsheet-financial-model", "skill-spreadsheet-formula"),
        },
        preferred_group="harness",
    )
    assert route is not None
    assert route.operation == "replace"
    assert route.target_plugin == "profile-deterministic-compact"
    assert route.replacement_plugin == "profile-deterministic-full"
    assert route.to_dict()["method_operator"] == "recomposition"


def test_validated_multi_plugin_handoff_failure_routes_to_synthesis(tmp_path):
    trajectory = tmp_path / "handoff.jsonl"
    trajectory.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "harness.skills.routed",
                        "payload": {
                            "selected": ["spreadsheet-financial-model", "spreadsheet-formula"],
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "workbook.mutation.committed",
                        "payload": {"operation": "write_formula"},
                    }
                ),
                json.dumps(
                    {
                        "event": "agent.formula_runtime_validation_passed",
                        "payload": {"calculation_valid": True},
                    }
                ),
                json.dumps(
                    {
                        "event": "evaluation.completed",
                        "payload": {"passed": False, "error_category": "formula"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    registry = default_plugin_registry()
    route = DeterministicEvidenceRouter().route(
        [EvidenceRef(trajectory, "Financial_Model/1", "Financial_Model", "family")],
        registry=registry,
        composition=BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"],
        groups={
            "harness": ("skill-spreadsheet-coordination", "policy-ours"),
            "domain": ("skill-spreadsheet-financial-model", "skill-spreadsheet-formula"),
        },
        preferred_group="harness",
    )
    assert route is not None
    assert route.operation == "synthesize"
    assert route.target_plugin == "skill-spreadsheet-coordination"
    assert "multi-plugin-handoff-locally-validated-but-evaluator-failed" in route.reasons


def test_spreadsheetbench_adapter_accumulates_evidence_from_all_contexts(tmp_path, monkeypatch):
    from spreadsheet_harness import spreadsheetbench_v2

    dataset = tmp_path / "dataset-a"
    dataset.mkdir()
    second_dataset = tmp_path / "dataset-b"
    second_dataset.mkdir()
    tasks = [
        SimpleNamespace(task_id="replay/1", category="formula", item_id="replay-item"),
        SimpleNamespace(task_id="replay/2", category="formula", item_id="replay-item-2"),
        SimpleNamespace(task_id="transfer/1", category="format", item_id="transfer-item"),
    ]
    monkeypatch.setattr(spreadsheetbench_v2, "load_spreadsheetbench_v2_tasks", lambda _root: tasks)
    monkeypatch.setattr(
        spreadsheetbench_v2,
        "select_spreadsheetbench_v2_tasks",
        lambda available, task_ids: [
            task for task_id in task_ids for task in available if task.task_id == task_id
        ],
    )

    composition = BUILTIN_COMPOSITIONS["spreadsheet-harness-financial"].to_dict()
    incumbent = tmp_path / "incumbent"
    candidate = tmp_path / "candidate"
    for revision in (incumbent, candidate):
        (revision / "artifact").mkdir(parents=True)
        (revision / "composition.json").write_text(json.dumps(composition), encoding="utf-8")

    executed_outputs = []

    def fake_run(argv, **_kwargs):
        payload = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        output_dir = Path(payload["output_dir"])
        executed_outputs.append(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        score = 1.0 if output_dir.name == "candidate" else 0.0
        for task_id in payload["task_ids"]:
            row = {
                "task_id": task_id,
                "official_score": {
                    "accuracy": score,
                    "modification_accuracy": score,
                    "regression_accuracy": score,
                },
            }
            if output_dir.name == "candidate":
                run_dir = output_dir / f"run-{task_id.replace('/', '-')}"
                run_dir.mkdir()
                (run_dir / "trajectory.jsonl").write_text(
                    '{"event":"evaluation.completed","payload":{"passed":true}}\n',
                    encoding="utf-8",
                )
                row["run_dir"] = str(run_dir)
            rows.append(row)
        (output_dir / "results.json").write_text(json.dumps(rows), encoding="utf-8")
        Path(payload["summary_path"]).write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("spreadsheet_harness.continuous_evolution.subprocess.run", fake_run)
    adapter = SpreadsheetBenchV2EvaluationAdapter(
        provider_config=SimpleNamespace(api_key="test-key", model="test-model"),
        dataset_root=dataset,
        evaluator_path=tmp_path / "evaluator.py",
        output_root=tmp_path / "output",
        incumbent_cache_root=tmp_path / "incumbent-cache",
    )
    request = {
        "incumbent_directory": str(incumbent),
        "incumbent_revision_sha256": "0" * 64,
        "candidate_revision_sha256": "1" * 64,
        "evaluation_binding_sha256": "e" * 64,
        "evaluation_binding": {
            "score_weights": {
                "accuracy": 1.0,
                "modification_accuracy": 0.25,
                "regression_accuracy": 0.10,
            }
        },
        "contexts": [
            {
                "name": "replay",
                "kind": "replay",
                "task_ids": ["replay/1", "replay/2"],
                "workbook_families": ["family-replay", "family-replay-2"],
                "task_set_sha256": "a" * 64,
                "runner": {
                    "task_datasets": {
                        "replay/1": str(dataset),
                        "replay/2": str(second_dataset),
                    }
                },
            },
            {
                "name": "transfer",
                "kind": "transfer",
                "task_ids": ["transfer/1"],
                "workbook_families": ["family-transfer"],
                "task_set_sha256": "b" * 64,
            },
        ],
    }
    report = adapter.evaluate(request, candidate)
    adapter.evaluate({**request, "candidate_revision_sha256": "2" * 64}, candidate)
    assert [item["task_id"] for item in report["candidate_evidence"]] == [
        "replay/1",
        "replay/2",
        "transfer/1",
    ]
    assert all(
        pair["baseline"] == 0.0 and pair["candidate"] == 1.0
        for context in report["contexts"]
        for pair in context["pairs"]
    )
    assert sum(path.name == "incumbent" for path in executed_outputs) == 0
    assert sum(path.name == "output" for path in executed_outputs) == 3
    assert sum(path.name == "candidate" for path in executed_outputs) == 6
