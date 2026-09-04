from __future__ import annotations

import hashlib
import json

import pytest

from spreadsheet_harness.cli import main
from spreadsheet_harness.errors import HarnessError
from spreadsheet_harness.plugins import (
    ARM_COMPOSITIONS,
    PLUGEOLVE_SEED_COMPOSITION,
    SPREADSHEET_HARNESS_BASIC_COMPOSITION,
    SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION,
    CompositionEvaluation,
    CompositionSpec,
    ConfigField,
    ConstrainedCompositionRouter,
    PluginContract,
    PluginMutation,
    contextual_marginal_contribution,
    default_plugin_registry,
    enumerate_single_plugin_candidates,
    execution_plan,
    select_composition,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_builtin_arm_compositions_preserve_current_runtime_contracts() -> None:
    registry = default_plugin_registry()
    plans = {
        name: execution_plan(registry.resolve(spec)) for name, spec in ARM_COMPOSITIONS.items()
    }

    assert plans["bare"].tool_mode == "code-only"
    assert plans["bare"].profile_mode == "none"
    assert plans["profile"].profile_mode == "full"
    assert plans["native"].tool_mode == "native"
    assert plans["paper"].workflow == "paper"
    assert plans["ours"].profile_mode == "compact"
    assert plans["ours"].repair_date_text is True
    assert plans["ours"].load_skills is False
    assert plans["ours"].require_formula_runtime_validation is False

    seed = execution_plan(registry.resolve(PLUGEOLVE_SEED_COMPOSITION))
    assert seed.tool_mode == "code-plus-formula-validation"
    assert seed.load_skills is True
    assert set(seed.skill_names) == {
        "spreadsheet-structure",
        "spreadsheet-formula",
        "spreadsheet-financial-model",
        "spreadsheet-manipulation",
        "spreadsheet-analysis",
        "visual-review",
        "spreadsheet-verification",
        "spreadsheet-memory",
    }
    assert seed.require_formula_runtime_validation is True
    assert seed.repair_date_text is True


def test_financial_ablation_compositions_differ_by_one_domain_plugin() -> None:
    registry = default_plugin_registry()
    basic = execution_plan(registry.resolve(SPREADSHEET_HARNESS_BASIC_COMPOSITION))
    financial = execution_plan(registry.resolve(SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION))

    assert SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION.plugins == (
        *SPREADSHEET_HARNESS_BASIC_COMPOSITION.plugins,
        "skill-spreadsheet-financial-model",
    )
    assert basic.tool_mode == financial.tool_mode == "code-plus-formula-validation"
    assert basic.profile_mode == financial.profile_mode == "compact"
    assert basic.policy == financial.policy == "ours"
    assert basic.require_formula_runtime_validation is True
    assert financial.require_formula_runtime_validation is True
    assert basic.financial_model_runtime is False
    assert financial.financial_model_runtime is True
    assert "spreadsheet-financial-model" not in basic.skill_names
    assert set(financial.skill_names) - set(basic.skill_names) == {
        "spreadsheet-financial-model"
    }


def test_formula_runtime_verifier_requires_scope_aware_native_tools() -> None:
    registry = default_plugin_registry()

    with pytest.raises(HarnessError, match="tool.recalculate-and-read"):
        registry.resolve(
            CompositionSpec.create(
                "invalid-code-only-formula-verifier",
                (
                    "runtime-code-interpreter",
                    "policy-bare",
                    "verifier-formula-runtime",
                ),
            )
        )


def test_composition_hash_binds_contract_versions_and_config() -> None:
    registry = default_plugin_registry()
    original = registry.resolve(ARM_COMPOSITIONS["ours"])
    same = registry.resolve(ARM_COMPOSITIONS["ours"])
    changed = registry.resolve(
        CompositionSpec.create(
            "ours-profile-four-regions",
            ARM_COMPOSITIONS["ours"].plugins,
            {"profile-deterministic-compact": {"max-regions-per-sheet": 4}},
        )
    )

    assert original.sha256 == same.sha256
    assert original.sha256 != changed.sha256
    assert all(len(plugin.contract.manifest_sha256) == 64 for plugin in original.plugins)


def test_contract_rejects_free_form_hooks_and_invalid_config() -> None:
    with pytest.raises(ValueError, match="unknown hooks"):
        PluginContract(
            "bad-plugin",
            "1.0.0",
            "control",
            "bad.plugin",
            frozenset({"policy.bad"}),
            hooks=frozenset({"invented_hook"}),
        )

    field = ConfigField("limit", "integer", 2, minimum=1, maximum=3)
    plugin = PluginContract(
        "bounded-plugin",
        "1.0.0",
        "observe",
        "bounded.plugin",
        frozenset({"context.bounded"}),
        config_fields=(field,),
        evolvable_surfaces=frozenset({"config"}),
    )
    with pytest.raises(ValueError, match="exceeds"):
        plugin.configure({"limit": 4})
    with pytest.raises(ValueError, match="no configurable field"):
        plugin.configure({"new-hook": True})


def test_registry_rejects_missing_and_duplicate_capability_providers() -> None:
    registry = default_plugin_registry()
    with pytest.raises(HarnessError, match="unsatisfied capabilities"):
        registry.resolve(CompositionSpec.create("missing-action", ("policy-bare",)))
    with pytest.raises(HarnessError, match="two providers"):
        registry.resolve(
            CompositionSpec.create(
                "duplicate-action",
                ("runtime-code-interpreter", "runtime-native-tools", "policy-bare"),
            )
        )


def test_plugin_mutation_can_change_only_declared_surface_of_one_plugin() -> None:
    registry = default_plugin_registry()
    contract = registry.get("policy-ours")
    mutation = PluginMutation.create(
        target_plugin=contract.name,
        base_version=contract.version,
        base_manifest_sha256=contract.manifest_sha256,
        surface="prompt",
        candidate_artifact_sha256=_digest("candidate"),
        changed_paths=("policy.md",),
        evidence_sha256=(_digest("trajectory"),),
    )

    assert mutation.validate(registry) is contract
    assert "hooks" not in mutation.to_dict()
    assert mutation.to_dict()["target_plugin"] == "policy-ours"

    forbidden = PluginMutation.create(
        target_plugin="workflow-paper",
        base_version=registry.get("workflow-paper").version,
        base_manifest_sha256=registry.get("workflow-paper").manifest_sha256,
        surface="prompt",
        candidate_artifact_sha256=_digest("candidate"),
        changed_paths=("prompt.md",),
    )
    with pytest.raises(HarnessError, match="does not allow"):
        forbidden.validate(registry)


def test_config_mutation_is_validated_against_code_owned_schema() -> None:
    registry = default_plugin_registry()
    contract = registry.get("profile-deterministic-compact")
    mutation = PluginMutation.create(
        target_plugin=contract.name,
        base_version=contract.version,
        base_manifest_sha256=contract.manifest_sha256,
        surface="config",
        candidate_artifact_sha256=_digest("config-candidate"),
        config_patch={"max-regions-per-sheet": 4},
    )
    assert mutation.validate(registry) is contract

    invalid = PluginMutation.create(
        target_plugin=contract.name,
        base_version=contract.version,
        base_manifest_sha256=contract.manifest_sha256,
        surface="config",
        candidate_artifact_sha256=_digest("bad-config"),
        config_patch={"max-regions-per-sheet": 999},
    )
    with pytest.raises(ValueError, match="exceeds"):
        invalid.validate(registry)


def test_code_controlled_search_emits_only_valid_single_slot_candidates() -> None:
    registry = default_plugin_registry()
    candidates = enumerate_single_plugin_candidates(
        registry,
        PLUGEOLVE_SEED_COMPOSITION,
        config_variants={
            "profile-deterministic-compact": [
                {"max-regions-per-sheet": 2},
                {"max-regions-per-sheet": 4},
            ]
        },
    )

    assert candidates
    assert {candidate.operation for candidate in candidates} >= {
        "disable",
        "replace",
        "configure",
    }
    assert all(candidate.composition.sha256 for candidate in candidates)
    assert len({candidate.composition.sha256 for candidate in candidates}) == len(candidates)
    assert all(execution_plan(candidate.composition) for candidate in candidates)
    configured = [candidate for candidate in candidates if candidate.operation == "configure"]
    assert len(configured) == 2
    assert all(candidate.target == "profile-deterministic-compact" for candidate in configured)


def test_selection_requires_cross_context_improvement_without_regression() -> None:
    registry = default_plugin_registry()
    baseline_composition = registry.resolve(ARM_COMPOSITIONS["ours"])
    candidate_composition = registry.resolve(PLUGEOLVE_SEED_COMPOSITION)
    baseline = CompositionEvaluation.create(
        baseline_composition, {"cell": 0.40, "sheet": 0.30}, cost=10
    )
    robust = CompositionEvaluation.create(
        candidate_composition, {"cell": 0.45, "sheet": 0.35}, cost=12
    )
    regressive = CompositionEvaluation.create(
        candidate_composition, {"cell": 0.60, "sheet": 0.20}, cost=8
    )
    failed = CompositionEvaluation.create(
        candidate_composition,
        {"cell": 0.50, "sheet": 0.40},
        failed_contexts=("sheet",),
    )

    assert select_composition([regressive, failed, robust], baseline=baseline) is robust
    assert select_composition([regressive, failed], baseline=baseline) is None

    marginal = contextual_marginal_contribution(robust, baseline)
    assert marginal["mean"] == pytest.approx(0.05)
    assert marginal["minimum"] == pytest.approx(0.05)
    assert marginal["positive_context_rate"] == 1


def test_constrained_router_uses_only_declared_task_types_and_compositions() -> None:
    registry = default_plugin_registry()
    router = ConstrainedCompositionRouter.create(
        "spreadsheet-task-router",
        allowed_task_types=("Cell-Level Manipulation", "Sheet-Level Manipulation"),
        routes={"Cell-Level Manipulation": PLUGEOLVE_SEED_COMPOSITION},
        fallback=ARM_COMPOSITIONS["ours"],
    )

    cell = router.route(registry, "Cell-Level Manipulation")
    sheet = router.route(registry, "Sheet-Level Manipulation")

    assert cell.composition.spec.name == "plugevolve-seed"
    assert cell.matched_rule == "Cell-Level Manipulation"
    assert sheet.composition.spec.name == "ours"
    assert sheet.matched_rule == "fallback"
    assert cell.router_manifest_sha256 == sheet.router_manifest_sha256
    assert len(cell.router_manifest_sha256) == 64
    with pytest.raises(HarnessError, match="rejects unknown task type"):
        router.route(registry, "Unseen Model-Proposed Type")


def test_plugin_cli_exposes_resolved_catalog_and_candidates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["plugins", "resolve", "ours"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["name"] == "ours"
    assert len(resolved["composition_sha256"]) == 64

    assert main(["plugins", "candidates", "plugevolve-seed"]) == 0
    candidates = json.loads(capsys.readouterr().out)
    assert candidates["candidate_count"] > 0
    assert all(
        item["operation"] in {"enable", "disable", "replace", "configure"}
        for item in candidates["candidates"]
    )

    assert main(
        [
            "plugins",
            "maintain",
            "--usage-count",
            "12",
            "--marginal-utility",
            "0.04",
            "--redundancy",
            "0.15",
        ]
    ) == 0
    maintenance = json.loads(capsys.readouterr().out)
    assert maintenance["action"] == "retain"
