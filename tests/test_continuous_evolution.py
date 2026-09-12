from __future__ import annotations

import json
from pathlib import Path

from spreadsheet_harness.continuous_evolution import (
    CandidateProposal,
    ContinuousEvolutionConfig,
    ContinuousEvolutionEngine,
    evaluate_validation_report,
)

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
                    "task_ids": ["replay/1"],
                    "workbook_families": ["family-replay"],
                    "weight": 1,
                },
                {
                    "name": "transfer",
                    "kind": "transfer",
                    "task_ids": ["transfer/1"],
                    "workbook_families": ["family-transfer"],
                    "weight": 1,
                },
                {
                    "name": "regression",
                    "kind": "regression",
                    "task_ids": ["regression/1"],
                    "workbook_families": ["family-regression"],
                    "weight": 1,
                },
            ],
            "heldout_task_ids": ["heldout/1"],
            "initial_evidence": [
                {
                    "path": str(evidence),
                    "task_id": "replay/1",
                    "task_type": "formula",
                    "workbook_family": "family-replay",
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
                    "files": [{"path": prompt["paths"][0], "content": "---\nname: formula\n---\nnew\n"}],
                }
            )
        ]


class _Evaluation:
    def __init__(self, evidence: Path):
        self.evidence = evidence

    def evaluate(self, request, _candidate_dir):
        contexts = []
        for context in request["contexts"]:
            task_id = context["task_ids"][0]
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
                        },
                        {
                            "id": task_id + ":seed2",
                            "baseline": 0.0,
                            "candidate": 1.0,
                            "baseline_status": "scored",
                            "candidate_status": "scored",
                        },
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
                    "workbook_family": "family-replay",
                }
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
