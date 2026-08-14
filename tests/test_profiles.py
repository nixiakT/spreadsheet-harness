from __future__ import annotations

from dataclasses import replace

import pytest

from spreadsheet_harness.profiles import (
    CONTRAST_CONTRACTS,
    EXECUTION_GROUP_CONTRACTS,
    FINALIZATION_PIPELINE_ID,
    FIXED_CONTRACT_ARTIFACT_ID,
    LAUNCH_ARTIFACT_DIGEST_BLOCKERS,
    PROFILE_ALIASES,
    PROFILE_REGISTRY,
    PROFILE_REGISTRY_LAUNCHABLE,
    PROFILE_REGISTRY_STATUS,
    PROFILE_SCHEMA_VERSION,
    REGISTERED_PROFILES,
    T2S_PROCEDURE_ARTIFACT_ID,
    ContrastId,
    DiagnosticsPolicy,
    EvidenceRouterPolicy,
    FinalizationMode,
    ProcedureStage,
    ProfileAlias,
    ProfileId,
    ProfileRegistry,
    ProfileValidationError,
    SubmitGateMode,
    UnderlyingAgentArm,
    exact_diff_paths,
)
from spreadsheet_harness.target_grounding import TargetGroundingMode


def _profile(profile_id: ProfileId):
    return PROFILE_REGISTRY.resolve(profile_id)


def test_registry_exposes_design_only_profile_matrix() -> None:
    assert PROFILE_REGISTRY_STATUS == "design-identity-only-v1"
    assert PROFILE_REGISTRY_LAUNCHABLE is False
    assert PROFILE_REGISTRY.identity_dict()["launchable"] is False
    assert [item.profile_id for item in PROFILE_REGISTRY.profiles] == [
        ProfileId.B2,
        ProfileId.T2S,
        ProfileId.B5,
        ProfileId.B6,
        ProfileId.FULL,
        ProfileId.G0,
        ProfileId.G1,
    ]
    assert PROFILE_REGISTRY.canonical_profile_id(ProfileId.G2) is ProfileId.FULL
    assert PROFILE_REGISTRY.resolve("G2") is PROFILE_REGISTRY.resolve("FULL")
    assert PROFILE_REGISTRY.identity("G2") == PROFILE_REGISTRY.identity("FULL")


def test_design_only_registry_names_every_unbound_artifact_digest() -> None:
    registry_identity = PROFILE_REGISTRY.identity_dict()
    blockers = registry_identity["launch_blockers"]

    assert PROFILE_REGISTRY_LAUNCHABLE is False
    assert blockers == [item.to_dict() for item in LAUNCH_ARTIFACT_DIGEST_BLOCKERS]
    assert {item["artifact_role"] for item in blockers} == {
        "contract",
        "procedure",
        "prompt",
        "tool_surface",
        "finalization",
    }
    assert all(
        item["code"] == "artifact-bytes-sha256-unbound" for item in blockers
    )
    assert all(
        item["required_binding"].endswith("_bytes_sha256")
        for item in blockers
    )
    assert all(
        set(item) == {
            "code",
            "artifact_role",
            "design_id",
            "required_binding",
        }
        for item in blockers
    )


def test_every_registered_profile_uses_paper_and_common_observer_instrumentation() -> None:
    for profile in PROFILE_REGISTRY.profiles:
        assert profile.schema_version == PROFILE_SCHEMA_VERSION
        assert profile.agent_arm is UnderlyingAgentArm.PAPER
        assert profile.finalization_pipeline_id == FINALIZATION_PIPELINE_ID
        assert profile.completion_attempt_policy.value == "pre_gate_every_submit_v1"


def test_b2_and_t2s_differ_only_by_solve_stage_procedure() -> None:
    b2 = _profile(ProfileId.B2)
    t2s = _profile(ProfileId.T2S)

    assert exact_diff_paths(b2.mechanism_dict(), t2s.mechanism_dict()) == (
        "/procedure",
    )
    assert b2.procedure_artifact_id is None
    assert b2.procedure_stages == ()
    assert t2s.procedure_artifact_id == T2S_PROCEDURE_ARTIFACT_ID
    assert t2s.procedure_stages == (ProcedureStage.SOLVE,)
    assert b2.contract_artifact_id is None
    assert t2s.contract_artifact_id is None


@pytest.mark.parametrize("profile_id", [ProfileId.B5, ProfileId.B6, ProfileId.FULL])
def test_core_profiles_share_fixed_contract_g2_and_execution_group(
    profile_id: ProfileId,
) -> None:
    profile = _profile(profile_id)
    assert profile.execution_group == "core_gate_pair"
    assert profile.contract_artifact_id == FIXED_CONTRACT_ARTIFACT_ID
    assert profile.diagnostics_policy is DiagnosticsPolicy.MODE_NEUTRAL_V1
    assert (
        profile.evidence_router_policy
        is EvidenceRouterPolicy.FIXED_DEADLINE_RESERVATION_V1
    )
    assert profile.target_grounding_mode is TargetGroundingMode.ENFORCE


def test_core_profile_treatments_are_exactly_submit_then_finalization() -> None:
    b5 = _profile(ProfileId.B5)
    b6 = _profile(ProfileId.B6)
    full = _profile(ProfileId.FULL)

    assert b5.submit_gate is SubmitGateMode.OBSERVE
    assert b6.submit_gate is SubmitGateMode.ENFORCE
    assert full.submit_gate is SubmitGateMode.ENFORCE
    assert b5.finalization_mode is FinalizationMode.OBSERVE
    assert b6.finalization_mode is FinalizationMode.OBSERVE
    assert full.finalization_mode is FinalizationMode.ENFORCE
    assert exact_diff_paths(b5.mechanism_dict(), b6.mechanism_dict()) == (
        "/contract/submit_gate",
    )
    assert exact_diff_paths(b6.mechanism_dict(), full.mechanism_dict()) == (
        "/finalization/mode",
    )


def test_grounding_profiles_form_off_advisory_enforce_sequence() -> None:
    g0 = _profile(ProfileId.G0)
    g1 = _profile(ProfileId.G1)
    g2 = _profile(ProfileId.G2)

    assert g0.target_grounding_mode is TargetGroundingMode.OFF
    assert g1.target_grounding_mode is TargetGroundingMode.ADVISORY
    assert g2.target_grounding_mode is TargetGroundingMode.ENFORCE
    assert exact_diff_paths(g0.mechanism_dict(), g1.mechanism_dict()) == (
        "/target_grounding_mode",
    )
    assert exact_diff_paths(g1.mechanism_dict(), g2.mechanism_dict()) == (
        "/target_grounding_mode",
    )


def test_execution_contract_encodes_policy_fork_and_shared_assessment() -> None:
    core = next(
        item
        for item in PROFILE_REGISTRY.execution_groups
        if item.group_id == "core_gate_pair"
    )
    assert core.profiles == (ProfileId.B5, ProfileId.B6, ProfileId.FULL)
    assert core.online_source_profile is ProfileId.B6
    assert core.policy_fork_profile is ProfileId.B5
    assert core.policy_fork_prefix_profiles == (ProfileId.B5, ProfileId.B6)
    assert core.shared_online_execution_profiles == (
        ProfileId.B6,
        ProfileId.FULL,
    )
    assert core.derived_from_online_source_profiles == (ProfileId.FULL,)
    assert core.shared_finalization_assessment_profiles == (
        ProfileId.B6,
        ProfileId.FULL,
    )


def test_contrast_contracts_are_complete_and_bind_shared_execution() -> None:
    assert [item.contrast_id for item in PROFILE_REGISTRY.contrasts] == [
        ContrastId.T2S_MINUS_B2,
        ContrastId.B6_MINUS_B5,
        ContrastId.FULL_MINUS_B6,
        ContrastId.G1_MINUS_G0,
        ContrastId.G2_MINUS_G1,
    ]
    gate = PROFILE_REGISTRY.contrast(ContrastId.B6_MINUS_B5)
    lineage = PROFILE_REGISTRY.contrast(ContrastId.FULL_MINUS_B6)
    assert gate.shared_online_execution is False
    assert gate.policy_fork_prefix is True
    assert gate.treatment_derived_from_baseline_online_execution is False
    assert gate.shared_finalization_assessment is False
    assert lineage.shared_online_execution is True
    assert lineage.policy_fork_prefix is False
    assert lineage.treatment_derived_from_baseline_online_execution is True
    assert lineage.shared_finalization_assessment is True


def test_execution_group_rejects_conflated_prefix_and_online_relationships() -> None:
    core = next(
        item
        for item in EXECUTION_GROUP_CONTRACTS
        if item.group_id == "core_gate_pair"
    )

    with pytest.raises(ProfileValidationError, match="source then derived"):
        replace(
            core,
            shared_online_execution_profiles=(ProfileId.B5, ProfileId.B6),
            derived_from_online_source_profiles=(ProfileId.B6,),
        )
    with pytest.raises(ProfileValidationError, match="must share one online"):
        replace(
            core,
            shared_finalization_assessment_profiles=(
                ProfileId.B5,
                ProfileId.B6,
            ),
        )


def test_registry_cross_checks_contrast_against_execution_relationships() -> None:
    gate = next(
        item
        for item in CONTRAST_CONTRACTS
        if item.contrast_id is ContrastId.B6_MINUS_B5
    )
    tampered_gate = replace(gate, shared_online_execution=True)

    with pytest.raises(
        ProfileValidationError,
        match="shared_online_execution disagrees with its execution group",
    ):
        ProfileRegistry(
            REGISTERED_PROFILES,
            PROFILE_ALIASES,
            EXECUTION_GROUP_CONTRACTS,
            tuple(
                tampered_gate if item is gate else item
                for item in CONTRAST_CONTRACTS
            ),
        )


def test_mechanism_identity_excludes_display_alias_and_execution_metadata() -> None:
    profile = _profile(ProfileId.B6)
    mechanism = profile.mechanism_dict()
    serialized = profile.to_dict()

    assert "profile_id" not in mechanism
    assert "display_name" not in mechanism
    assert "execution_group" not in mechanism
    assert "alias" not in mechanism
    assert serialized["profile_id"] == "B6"
    assert serialized["display_name"] == "B6 fixed enforce"
    assert serialized["execution_group"] == "core_gate_pair"


def test_profile_validation_rejects_bool_schema_and_non_enum_values() -> None:
    b2 = _profile(ProfileId.B2)
    with pytest.raises(TypeError, match="schema_version"):
        replace(b2, schema_version=True)
    with pytest.raises(TypeError, match="target_grounding_mode"):
        replace(b2, target_grounding_mode="off")
    with pytest.raises(TypeError, match="procedure_stages"):
        replace(b2, procedure_stages=[])


def test_profile_validation_rejects_invalid_component_combinations() -> None:
    b2 = _profile(ProfileId.B2)
    b5 = _profile(ProfileId.B5)

    with pytest.raises(ProfileValidationError, match="require a contract"):
        replace(b2, submit_gate=SubmitGateMode.OBSERVE)
    with pytest.raises(ProfileValidationError, match="procedure stages require"):
        replace(b2, procedure_stages=(ProcedureStage.SOLVE,))
    with pytest.raises(ProfileValidationError, match="target grounding"):
        replace(b2, target_grounding_mode=TargetGroundingMode.ADVISORY)
    with pytest.raises(ProfileValidationError, match="lineage enforcement"):
        replace(b5, finalization_mode=FinalizationMode.ENFORCE)


def test_registry_rejects_duplicate_profiles_before_mapping_collapse() -> None:
    with pytest.raises(ProfileValidationError, match="duplicate canonical profile"):
        ProfileRegistry(
            (*REGISTERED_PROFILES, REGISTERED_PROFILES[0]),
            PROFILE_ALIASES,
            EXECUTION_GROUP_CONTRACTS,
            CONTRAST_CONTRACTS,
        )


def test_registry_rejects_duplicate_and_colliding_aliases() -> None:
    with pytest.raises(ProfileValidationError, match="duplicate profile alias"):
        ProfileRegistry(
            REGISTERED_PROFILES,
            (*PROFILE_ALIASES, ProfileAlias(ProfileId.G2, ProfileId.FULL)),
            EXECUTION_GROUP_CONTRACTS,
            CONTRAST_CONTRACTS,
        )
    with pytest.raises(ProfileValidationError, match="alias collides"):
        ProfileRegistry(
            REGISTERED_PROFILES,
            (
                ProfileAlias(ProfileId.G2, ProfileId.FULL),
                ProfileAlias(ProfileId.B2, ProfileId.FULL),
            ),
            EXECUTION_GROUP_CONTRACTS,
            CONTRAST_CONTRACTS,
        )


def test_registry_lookup_rejects_boolean_and_unknown_ids() -> None:
    with pytest.raises(TypeError, match="profile id"):
        PROFILE_REGISTRY.resolve(True)
    with pytest.raises(ProfileValidationError, match="unknown profile"):
        PROFILE_REGISTRY.resolve("UNKNOWN")
