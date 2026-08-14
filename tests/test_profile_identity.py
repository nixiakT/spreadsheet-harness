from __future__ import annotations

from dataclasses import replace

import pytest

from spreadsheet_harness.profiles import (
    CONTRAST_CONTRACTS,
    EXECUTION_GROUP_CONTRACTS,
    PRE_MUTATION_BOUNDARY_POLICY_ID,
    PROFILE_ALIASES,
    PROFILE_REGISTRY,
    PROFILE_REGISTRY_SHA256,
    REGISTERED_PROFILES,
    ContrastId,
    IdentityProjection,
    ProfileId,
    ProfileRegistry,
    ProfileValidationError,
    SubmitGateMode,
    canonical_json_bytes,
    canonical_sha256,
    exact_diff_paths,
    profile_identity,
)


def _identity(profile_id: ProfileId):
    return PROFILE_REGISTRY.identity(profile_id)


def test_registry_identity_is_stable_and_constructor_order_independent() -> None:
    reordered = ProfileRegistry(
        tuple(reversed(REGISTERED_PROFILES)),
        tuple(reversed(PROFILE_ALIASES)),
        tuple(reversed(EXECUTION_GROUP_CONTRACTS)),
        tuple(reversed(CONTRAST_CONTRACTS)),
    )
    assert PROFILE_REGISTRY_SHA256 == (
        "b16f19f931811c47b8aa92b8f2141f0c26af6b8d66cf10e781b813364ef01421"
    )
    assert reordered.identity_dict() == PROFILE_REGISTRY.identity_dict()
    assert reordered.canonical_sha256 == PROFILE_REGISTRY_SHA256


def test_canonical_json_is_ascii_sorted_and_strict() -> None:
    assert canonical_json_bytes({"z": "caf\u00e9", "a": [True, 2]}) == (
        b'{"a":[true,2],"z":"caf\\u00e9"}'
    )
    assert canonical_sha256({"a": 1}) == canonical_sha256({"a": 1})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_sha256({"bad": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_sha256({"bad": float("inf")})
    with pytest.raises(ValueError, match="negative zero"):
        canonical_sha256({"bad": -0.0})
    with pytest.raises(TypeError, match="non-canonical JSON"):
        canonical_sha256({"bad": (1, 2)})
    with pytest.raises(TypeError, match="non-string JSON object key"):
        canonical_sha256({1: "bad"})
    with pytest.raises(
        TypeError, match=r"^\$ has a non-string JSON object key$"
    ):
        canonical_sha256({"sortable": "yes", 1: "not comparable"})


def test_exact_diff_paths_are_canonical_json_pointers() -> None:
    left = {"a/b": {"x~y": 1}, "same": [1, 2]}
    right = {"a/b": {"x~y": 2}, "same": [1, 2]}
    assert exact_diff_paths(left, right) == ("/a~1b/x~0y",)
    assert exact_diff_paths(None, {"value": 1}) == ("/",)
    with pytest.raises(ValueError, match="non-finite"):
        exact_diff_paths({"bad": float("nan")}, {})


def test_b5_b6_hold_every_declared_pre_gate_identity_equal() -> None:
    b5 = _identity(ProfileId.B5)
    b6 = _identity(ProfileId.B6)
    contract = PROFILE_REGISTRY.contrast(ContrastId.B6_MINUS_B5)

    for projection in contract.required_equal_identities:
        assert b5.projection(projection) == b6.projection(projection)
    assert b5.online_execution_identity_sha256 != (
        b6.online_execution_identity_sha256
    )
    assert b5.profile_sha256 != b6.profile_sha256


def test_b6_full_share_online_execution_and_postprocess_assessment_identity() -> None:
    b6 = _identity(ProfileId.B6)
    full = _identity(ProfileId.FULL)
    contract = PROFILE_REGISTRY.contrast(ContrastId.FULL_MINUS_B6)

    for projection in contract.required_equal_identities:
        assert b6.projection(projection) == full.projection(projection)
    assert b6.online_execution_identity_sha256 == (
        full.online_execution_identity_sha256
    )
    assert b6.postprocess_pipeline_sha256 == full.postprocess_pipeline_sha256
    assert b6.mechanism_sha256 != full.mechanism_sha256


def test_g1_g2_have_identical_pre_mutation_model_visible_identity() -> None:
    g1 = _identity(ProfileId.G1)
    g2 = _identity(ProfileId.G2)
    contract = PROFILE_REGISTRY.contrast(ContrastId.G2_MINUS_G1)

    assert (
        PROFILE_REGISTRY.identity_dict()["identity_policy_versions"][
            "pre_mutation_boundary"
        ]
        == PRE_MUTATION_BOUNDARY_POLICY_ID
        == "before-first-target-grounding-state-mutation-v1"
    )
    for projection in contract.required_equal_identities:
        assert g1.projection(projection) == g2.projection(projection)
    assert g1.prompt_policy_sha256 == g2.prompt_policy_sha256
    assert g1.tool_surface_sha256 == g2.tool_surface_sha256
    assert g1.diagnostics_policy_sha256 == g2.diagnostics_policy_sha256
    assert g1.pre_mutation_common_prefix_sha256 == (
        g2.pre_mutation_common_prefix_sha256
    )
    assert g1.pre_gate_runtime_sha256 != g2.pre_gate_runtime_sha256
    assert g1.online_execution_identity_sha256 != (
        g2.online_execution_identity_sha256
    )


def test_b2_t2s_only_prompt_identity_changes_from_procedure() -> None:
    b2 = _identity(ProfileId.B2)
    t2s = _identity(ProfileId.T2S)

    assert b2.prompt_policy_sha256 != t2s.prompt_policy_sha256
    assert b2.model_visible_identity_sha256 != t2s.model_visible_identity_sha256
    assert b2.tool_surface_sha256 == t2s.tool_surface_sha256
    assert b2.diagnostics_policy_sha256 == t2s.diagnostics_policy_sha256
    assert b2.postprocess_pipeline_sha256 == t2s.postprocess_pipeline_sha256


def test_every_contrast_exactly_matches_its_frozen_mechanism_diff() -> None:
    for contrast in PROFILE_REGISTRY.contrasts:
        baseline = PROFILE_REGISTRY.resolve(contrast.baseline)
        treatment = PROFILE_REGISTRY.resolve(contrast.treatment)
        assert exact_diff_paths(
            baseline.mechanism_dict(), treatment.mechanism_dict()
        ) == contrast.expected_mechanism_diff_paths


def test_identity_changes_when_non_metadata_mechanism_is_tampered() -> None:
    b6 = PROFILE_REGISTRY.resolve(ProfileId.B6)
    tampered = replace(b6, submit_gate=SubmitGateMode.OBSERVE)

    assert profile_identity(tampered).mechanism_sha256 != (
        PROFILE_REGISTRY.identity(ProfileId.B6).mechanism_sha256
    )
    with pytest.raises(ProfileValidationError, match="frozen mechanism"):
        ProfileRegistry(
            tuple(
                tampered if profile.profile_id is ProfileId.B6 else profile
                for profile in REGISTERED_PROFILES
            ),
            PROFILE_ALIASES,
            EXECUTION_GROUP_CONTRACTS,
            CONTRAST_CONTRACTS,
        )


def test_identity_projection_rejects_non_enum_and_has_expected_keys() -> None:
    identity = _identity(ProfileId.FULL)
    assert set(identity.to_dict()) == {
        "profile_sha256",
        "mechanism_sha256",
        *(field.value for field in IdentityProjection),
    }
    with pytest.raises(TypeError, match="identity projection"):
        identity.projection("online_execution_identity_sha256")
