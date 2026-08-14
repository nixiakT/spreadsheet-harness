"""Frozen design identities for the SheetLedger v29 experiment profiles.

This module describes the intended causal matrix.  It deliberately does not
make the profiles launchable: runner, lifecycle, finalization, and audit support
must be implemented and frozen separately before a profile can be executed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .target_grounding import TargetGroundingMode

PROFILE_SCHEMA_VERSION = 1
PROFILE_REGISTRY_SCHEMA_VERSION = 1
PROFILE_REGISTRY_STATUS = "design-identity-only-v1"
PROFILE_REGISTRY_LAUNCHABLE = False

FIXED_CONTRACT_ARTIFACT_ID = "researcher-fixed-spreadsheet-contract-v1"
T2S_PROCEDURE_ARTIFACT_ID = "trace2skill-inspired-frozen-procedure-v1"
FINALIZATION_PIPELINE_ID = "deterministic-finalization-assessment-v1"

PAPER_PROMPT_POLICY_ID = "paper-multiformat-prompt-v1"
PAPER_TOOL_SURFACE_POLICY_ID = "paper-multiformat-tools-v1"
TERMINAL_TOOL_POLICY_ID = "submit-result-required-tool-v1"
CONTRACT_GUIDANCE_POLICY_ID = "mode-neutral-contract-guidance-v1"
GROUNDING_GUIDANCE_POLICY_ID = "mode-neutral-grounding-elicitation-v1"
GROUNDING_TOOL_SCHEMA_POLICY_ID = "mode-neutral-grounding-tools-v1"
GROUNDING_DIAGNOSTIC_POLICY_ID = "mode-neutral-grounding-diagnostics-v1"
COMPLETION_CAPTURE_VISIBILITY_POLICY_ID = "observer-only-never-model-visible-v1"
PRE_MUTATION_BOUNDARY_POLICY_ID = (
    "before-first-target-grounding-state-mutation-v1"
)

LAUNCH_BLOCKER_CODE = "artifact-bytes-sha256-unbound"

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class ProfileValidationError(ValueError):
    """A profile registry or causal contrast is internally inconsistent."""


class ProfileId(str, Enum):
    B2 = "B2"
    T2S = "T2S"
    B5 = "B5"
    B6 = "B6"
    FULL = "FULL"
    G0 = "G0"
    G1 = "G1"
    G2 = "G2"


class UnderlyingAgentArm(str, Enum):
    BARE = "bare"
    PROFILE = "profile"
    NATIVE = "native"
    PAPER = "paper"
    OURS = "ours"


class ProcedureStage(str, Enum):
    SOLVE = "solve"


class DiagnosticsPolicy(str, Enum):
    OFF = "off"
    MODE_NEUTRAL_V1 = "mode_neutral_v1"


class EvidenceRouterPolicy(str, Enum):
    OFF = "off"
    FIXED_DEADLINE_RESERVATION_V1 = "fixed_deadline_reservation_v1"


class SubmitGateMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class CompletionAttemptPolicy(str, Enum):
    PRE_GATE_EVERY_SUBMIT_V1 = "pre_gate_every_submit_v1"


class FinalizationMode(str, Enum):
    OBSERVE = "observe"
    ENFORCE = "enforce"


class IdentityProjection(str, Enum):
    PROMPT_POLICY = "prompt_policy_sha256"
    TOOL_SURFACE = "tool_surface_sha256"
    DIAGNOSTICS = "diagnostics_policy_sha256"
    MODEL_VISIBLE = "model_visible_identity_sha256"
    PRE_MUTATION_COMMON_PREFIX = "pre_mutation_common_prefix_sha256"
    PRE_GATE_RUNTIME = "pre_gate_runtime_sha256"
    ONLINE_EXECUTION = "online_execution_identity_sha256"
    POSTPROCESS_PIPELINE = "postprocess_pipeline_sha256"


class ContrastId(str, Enum):
    T2S_MINUS_B2 = "T2S-B2"
    B6_MINUS_B5 = "B6-B5"
    FULL_MINUS_B6 = "FULL-B6"
    G1_MINUS_G0 = "G1-G0"
    G2_MINUS_G1 = "G2-G1"


def _canonical_string(value: str, *, path: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{path} is not NFC-normalized")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{path} contains an invalid Unicode scalar") from exc
    return value


def _canonical_json_value(value: Any, *, path: str = "$") -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        if value == 0.0 and math.copysign(1.0, value) < 0:
            raise ValueError(f"{path} contains non-canonical negative zero")
        return value
    if type(value) is str:
        return _canonical_string(value, path=path)
    if type(value) is list:
        return [
            _canonical_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        keys = list(value)
        if any(type(key) is not str for key in keys):
            raise TypeError(f"{path} has a non-string JSON object key")
        for key in sorted(keys):
            normalized_key = _canonical_string(key, path=f"{path}.<key>")
            normalized[normalized_key] = _canonical_json_value(
                value[key], path=f"{path}.{normalized_key}"
            )
        return normalized
    raise TypeError(
        f"{path} contains non-canonical JSON data of type {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return one strict ASCII JSON encoding for design-identity data."""

    normalized = _canonical_json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    """Hash strict canonical JSON design data."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_schema_version(value: Any, *, label: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer, not a boolean or integer subclass")
    if value != PROFILE_SCHEMA_VERSION:
        raise ProfileValidationError(
            f"{label} must equal {PROFILE_SCHEMA_VERSION}"
        )


def _require_identifier(value: Any, *, label: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ProfileValidationError(f"{label} must be a canonical ASCII identifier")
    return value


def _require_display_name(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 120
        or not value.isascii()
        or any(not character.isprintable() for character in value)
    ):
        raise ProfileValidationError("display_name must be short printable ASCII")
    return value


def _require_enum(value: Any, expected: type[Enum], *, label: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{label} must be a {expected.__name__}")


@dataclass(frozen=True)
class ExperimentProfile:
    """One immutable mechanism configuration plus non-causal display/scheduling metadata."""

    schema_version: int
    profile_id: ProfileId
    display_name: str
    agent_arm: UnderlyingAgentArm
    procedure_artifact_id: str | None
    procedure_stages: tuple[ProcedureStage, ...]
    contract_artifact_id: str | None
    diagnostics_policy: DiagnosticsPolicy
    evidence_router_policy: EvidenceRouterPolicy
    submit_gate: SubmitGateMode
    target_grounding_mode: TargetGroundingMode
    completion_attempt_policy: CompletionAttemptPolicy
    finalization_pipeline_id: str
    finalization_mode: FinalizationMode
    execution_group: str

    def __post_init__(self) -> None:
        _require_schema_version(self.schema_version, label="profile schema_version")
        _require_enum(self.profile_id, ProfileId, label="profile_id")
        _require_display_name(self.display_name)
        _require_enum(self.agent_arm, UnderlyingAgentArm, label="agent_arm")
        if type(self.procedure_stages) is not tuple:
            raise TypeError("procedure_stages must be a tuple")
        if len(self.procedure_stages) != len(set(self.procedure_stages)):
            raise ProfileValidationError("procedure_stages must not contain duplicates")
        for stage in self.procedure_stages:
            _require_enum(stage, ProcedureStage, label="procedure stage")
        if self.procedure_artifact_id is None:
            if self.procedure_stages:
                raise ProfileValidationError(
                    "procedure stages require a procedure artifact"
                )
        else:
            _require_identifier(
                self.procedure_artifact_id, label="procedure_artifact_id"
            )
            if not self.procedure_stages:
                raise ProfileValidationError(
                    "a procedure artifact requires at least one injection stage"
                )

        _require_enum(
            self.diagnostics_policy, DiagnosticsPolicy, label="diagnostics_policy"
        )
        _require_enum(
            self.evidence_router_policy,
            EvidenceRouterPolicy,
            label="evidence_router_policy",
        )
        _require_enum(self.submit_gate, SubmitGateMode, label="submit_gate")
        if self.contract_artifact_id is None:
            if (
                self.diagnostics_policy is not DiagnosticsPolicy.OFF
                or self.evidence_router_policy is not EvidenceRouterPolicy.OFF
                or self.submit_gate is not SubmitGateMode.OFF
            ):
                raise ProfileValidationError(
                    "contract diagnostics, routing, and submission gates require a contract"
                )
        else:
            _require_identifier(self.contract_artifact_id, label="contract_artifact_id")
            if (
                self.diagnostics_policy is DiagnosticsPolicy.OFF
                or self.evidence_router_policy is EvidenceRouterPolicy.OFF
                or self.submit_gate is SubmitGateMode.OFF
            ):
                raise ProfileValidationError(
                    "a configured contract requires visible diagnostics, routing, and a gate policy"
                )

        _require_enum(
            self.target_grounding_mode,
            TargetGroundingMode,
            label="target_grounding_mode",
        )
        if (
            self.target_grounding_mode is not TargetGroundingMode.OFF
            and self.contract_artifact_id is None
        ):
            raise ProfileValidationError(
                "target grounding is only defined for the fixed-contract profiles"
            )
        _require_enum(
            self.completion_attempt_policy,
            CompletionAttemptPolicy,
            label="completion_attempt_policy",
        )
        _require_identifier(
            self.finalization_pipeline_id, label="finalization_pipeline_id"
        )
        _require_enum(
            self.finalization_mode, FinalizationMode, label="finalization_mode"
        )
        if (
            self.finalization_mode is FinalizationMode.ENFORCE
            and self.submit_gate is not SubmitGateMode.ENFORCE
        ):
            raise ProfileValidationError(
                "lineage enforcement requires an enforced contract submission gate"
            )
        _require_identifier(self.execution_group, label="execution_group")

    def mechanism_dict(self) -> dict[str, Any]:
        """Return causal mechanism data, excluding display, alias, and scheduling metadata."""

        procedure = (
            None
            if self.procedure_artifact_id is None
            else {
                "artifact_id": self.procedure_artifact_id,
                "stages": [stage.value for stage in self.procedure_stages],
            }
        )
        contract = (
            None
            if self.contract_artifact_id is None
            else {
                "artifact_id": self.contract_artifact_id,
                "diagnostics_policy": self.diagnostics_policy.value,
                "evidence_router_policy": self.evidence_router_policy.value,
                "submit_gate": self.submit_gate.value,
            }
        )
        return {
            "schema_version": self.schema_version,
            "agent_arm": self.agent_arm.value,
            "procedure": procedure,
            "contract": contract,
            "target_grounding_mode": self.target_grounding_mode.value,
            "completion_attempt_policy": self.completion_attempt_policy.value,
            "finalization": {
                "pipeline_id": self.finalization_pipeline_id,
                "mode": self.finalization_mode.value,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id.value,
            "display_name": self.display_name,
            "execution_group": self.execution_group,
            "mechanism": self.mechanism_dict(),
        }


@dataclass(frozen=True)
class ProfileIdentity:
    profile_sha256: str
    mechanism_sha256: str
    prompt_policy_sha256: str
    tool_surface_sha256: str
    diagnostics_policy_sha256: str
    model_visible_identity_sha256: str
    pre_mutation_common_prefix_sha256: str
    pre_gate_runtime_sha256: str
    online_execution_identity_sha256: str
    postprocess_pipeline_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile_sha256": self.profile_sha256,
            "mechanism_sha256": self.mechanism_sha256,
            "prompt_policy_sha256": self.prompt_policy_sha256,
            "tool_surface_sha256": self.tool_surface_sha256,
            "diagnostics_policy_sha256": self.diagnostics_policy_sha256,
            "model_visible_identity_sha256": self.model_visible_identity_sha256,
            "pre_mutation_common_prefix_sha256": (
                self.pre_mutation_common_prefix_sha256
            ),
            "pre_gate_runtime_sha256": self.pre_gate_runtime_sha256,
            "online_execution_identity_sha256": self.online_execution_identity_sha256,
            "postprocess_pipeline_sha256": self.postprocess_pipeline_sha256,
        }

    def projection(self, field: IdentityProjection) -> str:
        _require_enum(field, IdentityProjection, label="identity projection")
        return self.to_dict()[field.value]


def _procedure_dict(profile: ExperimentProfile) -> dict[str, Any] | None:
    return profile.mechanism_dict()["procedure"]


def _contract_present(profile: ExperimentProfile) -> bool:
    return profile.contract_artifact_id is not None


def _grounding_elicitation_active(profile: ExperimentProfile) -> bool:
    return profile.target_grounding_mode is not TargetGroundingMode.OFF


def _prompt_policy_dict(profile: ExperimentProfile) -> dict[str, Any]:
    contract_guidance = (
        {
            "artifact_id": profile.contract_artifact_id,
            "policy_id": CONTRACT_GUIDANCE_POLICY_ID,
        }
        if _contract_present(profile)
        else None
    )
    grounding_guidance = (
        GROUNDING_GUIDANCE_POLICY_ID
        if _grounding_elicitation_active(profile)
        else None
    )
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "agent_arm": profile.agent_arm.value,
        "base_prompt_policy_id": PAPER_PROMPT_POLICY_ID,
        "procedure": _procedure_dict(profile),
        "contract_guidance": contract_guidance,
        "target_grounding_guidance_policy_id": grounding_guidance,
    }


def _tool_surface_dict(profile: ExperimentProfile) -> dict[str, Any]:
    grounding_active = _grounding_elicitation_active(profile)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "agent_arm": profile.agent_arm.value,
        "base_tool_surface_policy_id": PAPER_TOOL_SURFACE_POLICY_ID,
        "terminal_tool_policy_id": TERMINAL_TOOL_POLICY_ID,
        "target_grounding_control_tools": grounding_active,
        "target_grounding_tool_schema_policy_id": (
            GROUNDING_TOOL_SCHEMA_POLICY_ID if grounding_active else None
        ),
    }


def _diagnostics_policy_dict(profile: ExperimentProfile) -> dict[str, Any]:
    grounding_active = _grounding_elicitation_active(profile)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "contract_diagnostics_policy": profile.diagnostics_policy.value,
        "target_grounding_diagnostics_policy_id": (
            GROUNDING_DIAGNOSTIC_POLICY_ID if grounding_active else None
        ),
        "mode_fields_visible": False,
        "gate_permission_fields_visible": False,
    }


def profile_identity(profile: ExperimentProfile) -> ProfileIdentity:
    """Build stable design hashes without claiming a resolved executable payload."""

    if not isinstance(profile, ExperimentProfile):
        raise TypeError("profile must be an ExperimentProfile")
    prompt = _prompt_policy_dict(profile)
    tools = _tool_surface_dict(profile)
    diagnostics = _diagnostics_policy_dict(profile)
    prompt_sha256 = canonical_sha256(prompt)
    tool_sha256 = canonical_sha256(tools)
    diagnostics_sha256 = canonical_sha256(diagnostics)
    model_visible = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "prompt_policy_sha256": prompt_sha256,
        "tool_surface_sha256": tool_sha256,
        "diagnostics_policy_sha256": diagnostics_sha256,
        "evidence_router_policy": profile.evidence_router_policy.value,
    }
    model_visible_sha256 = canonical_sha256(model_visible)
    pre_mutation_common_prefix = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "boundary_policy_id": PRE_MUTATION_BOUNDARY_POLICY_ID,
        "model_visible_identity_sha256": model_visible_sha256,
        "contract_artifact_id": profile.contract_artifact_id,
        "target_grounding_runtime_family": (
            "elicitation-and-staged-assessment"
            if _grounding_elicitation_active(profile)
            else "off"
        ),
        "completion_attempt_policy": profile.completion_attempt_policy.value,
        "completion_attempt_visibility_policy_id": (
            COMPLETION_CAPTURE_VISIBILITY_POLICY_ID
        ),
    }
    pre_mutation_common_prefix_sha256 = canonical_sha256(
        pre_mutation_common_prefix
    )
    pre_gate_runtime = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "pre_mutation_common_prefix_sha256": (
            pre_mutation_common_prefix_sha256
        ),
        "target_grounding_mode": profile.target_grounding_mode.value,
    }
    pre_gate_sha256 = canonical_sha256(pre_gate_runtime)
    online_execution = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "pre_gate_runtime_sha256": pre_gate_sha256,
        "submit_gate": profile.submit_gate.value,
        "target_grounding_mode": profile.target_grounding_mode.value,
    }
    postprocess = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "pipeline_id": profile.finalization_pipeline_id,
    }
    return ProfileIdentity(
        profile_sha256=canonical_sha256(profile.to_dict()),
        mechanism_sha256=canonical_sha256(profile.mechanism_dict()),
        prompt_policy_sha256=prompt_sha256,
        tool_surface_sha256=tool_sha256,
        diagnostics_policy_sha256=diagnostics_sha256,
        model_visible_identity_sha256=model_visible_sha256,
        pre_mutation_common_prefix_sha256=(
            pre_mutation_common_prefix_sha256
        ),
        pre_gate_runtime_sha256=pre_gate_sha256,
        online_execution_identity_sha256=canonical_sha256(online_execution),
        postprocess_pipeline_sha256=canonical_sha256(postprocess),
    )


@dataclass(frozen=True)
class ProfileAlias:
    alias: ProfileId
    target: ProfileId

    def __post_init__(self) -> None:
        _require_enum(self.alias, ProfileId, label="profile alias")
        _require_enum(self.target, ProfileId, label="profile alias target")
        if self.alias is self.target:
            raise ProfileValidationError("a profile alias cannot target itself")

    def to_dict(self) -> dict[str, str]:
        return {"alias": self.alias.value, "target": self.target.value}


@dataclass(frozen=True)
class LaunchArtifactDigestBlocker:
    """One real artifact whose exact bytes are not yet bound to this design."""

    artifact_role: str
    design_id: str
    required_binding: str

    def __post_init__(self) -> None:
        _require_identifier(self.artifact_role, label="launch artifact role")
        _require_identifier(self.design_id, label="launch artifact design id")
        _require_identifier(self.required_binding, label="launch digest binding")
        if not self.required_binding.endswith("_bytes_sha256"):
            raise ProfileValidationError(
                "launch digest bindings must name an exact bytes SHA-256"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "code": LAUNCH_BLOCKER_CODE,
            "artifact_role": self.artifact_role,
            "design_id": self.design_id,
            "required_binding": self.required_binding,
        }


LAUNCH_ARTIFACT_DIGEST_BLOCKERS = (
    LaunchArtifactDigestBlocker(
        "contract",
        FIXED_CONTRACT_ARTIFACT_ID,
        "contract_artifact_bytes_sha256",
    ),
    LaunchArtifactDigestBlocker(
        "procedure",
        T2S_PROCEDURE_ARTIFACT_ID,
        "procedure_artifact_bytes_sha256",
    ),
    LaunchArtifactDigestBlocker(
        "prompt",
        PAPER_PROMPT_POLICY_ID,
        "prompt_policy_bytes_sha256",
    ),
    LaunchArtifactDigestBlocker(
        "tool_surface",
        PAPER_TOOL_SURFACE_POLICY_ID,
        "tool_surface_policy_bytes_sha256",
    ),
    LaunchArtifactDigestBlocker(
        "finalization",
        FINALIZATION_PIPELINE_ID,
        "finalization_pipeline_bytes_sha256",
    ),
)


@dataclass(frozen=True)
class ExecutionGroupContract:
    schema_version: int
    group_id: str
    profiles: tuple[ProfileId, ...]
    online_source_profile: ProfileId
    policy_fork_profile: ProfileId | None = None
    policy_fork_prefix_profiles: tuple[ProfileId, ...] = ()
    shared_online_execution_profiles: tuple[ProfileId, ...] = ()
    derived_from_online_source_profiles: tuple[ProfileId, ...] = ()
    shared_finalization_assessment_profiles: tuple[ProfileId, ...] = ()

    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version, label="execution contract schema_version"
        )
        _require_identifier(self.group_id, label="execution group id")
        for label, values in (
            ("profiles", self.profiles),
            (
                "policy_fork_prefix_profiles",
                self.policy_fork_prefix_profiles,
            ),
            (
                "shared_online_execution_profiles",
                self.shared_online_execution_profiles,
            ),
            (
                "derived_from_online_source_profiles",
                self.derived_from_online_source_profiles,
            ),
            (
                "shared_finalization_assessment_profiles",
                self.shared_finalization_assessment_profiles,
            ),
        ):
            if type(values) is not tuple:
                raise TypeError(f"{label} must be a tuple")
            if len(values) != len(set(values)):
                raise ProfileValidationError(f"{label} must not contain duplicates")
            for profile_id in values:
                _require_enum(profile_id, ProfileId, label=label)
        if not self.profiles:
            raise ProfileValidationError("an execution group must contain profiles")
        _require_enum(
            self.online_source_profile,
            ProfileId,
            label="online_source_profile",
        )
        if self.online_source_profile not in self.profiles:
            raise ProfileValidationError("online source must belong to its execution group")
        if self.policy_fork_profile is not None:
            _require_enum(
                self.policy_fork_profile, ProfileId, label="policy_fork_profile"
            )
            if (
                self.policy_fork_profile not in self.profiles
                or self.policy_fork_profile is self.online_source_profile
            ):
                raise ProfileValidationError(
                    "policy fork must be a distinct member of its execution group"
                )
            if self.policy_fork_prefix_profiles != (
                self.policy_fork_profile,
                self.online_source_profile,
            ):
                raise ProfileValidationError(
                    "policy-fork prefix must contain fork then online source"
                )
        elif self.policy_fork_prefix_profiles:
            raise ProfileValidationError(
                "policy-fork prefix profiles require a policy fork"
            )
        if not set(self.policy_fork_prefix_profiles).issubset(self.profiles):
            raise ProfileValidationError(
                "policy-fork prefix profiles escaped their group"
            )
        if not set(self.shared_online_execution_profiles).issubset(self.profiles):
            raise ProfileValidationError(
                "shared-online profiles escaped their group"
            )
        if self.shared_online_execution_profiles:
            if self.shared_online_execution_profiles != (
                self.online_source_profile,
                *self.derived_from_online_source_profiles,
            ):
                raise ProfileValidationError(
                    "shared-online profiles must list source then derived profiles"
                )
            if len(self.shared_online_execution_profiles) < 2:
                raise ProfileValidationError(
                    "shared-online execution requires at least two profiles"
                )
        elif self.derived_from_online_source_profiles:
            raise ProfileValidationError(
                "derived profiles require a shared online execution"
            )
        if self.online_source_profile in self.derived_from_online_source_profiles:
            raise ProfileValidationError(
                "the online source cannot be derived from itself"
            )
        if not set(self.shared_finalization_assessment_profiles).issubset(
            self.shared_online_execution_profiles
        ):
            raise ProfileValidationError(
                "shared-finalization profiles must share one online execution"
            )
        if (
            self.shared_finalization_assessment_profiles
            and len(self.shared_finalization_assessment_profiles) < 2
        ):
            raise ProfileValidationError(
                "a shared finalization assessment requires at least two profiles"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "group_id": self.group_id,
            "profiles": [item.value for item in self.profiles],
            "online_source_profile": self.online_source_profile.value,
            "policy_fork_profile": (
                self.policy_fork_profile.value
                if self.policy_fork_profile is not None
                else None
            ),
            "policy_fork_prefix_profiles": [
                item.value for item in self.policy_fork_prefix_profiles
            ],
            "shared_online_execution_profiles": [
                item.value for item in self.shared_online_execution_profiles
            ],
            "derived_from_online_source_profiles": [
                item.value
                for item in self.derived_from_online_source_profiles
            ],
            "shared_finalization_assessment_profiles": [
                item.value
                for item in self.shared_finalization_assessment_profiles
            ],
        }


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def exact_diff_paths(left: Any, right: Any) -> tuple[str, ...]:
    """Return deterministic JSON-pointer leaves at which canonical data differs."""

    normalized_left = _canonical_json_value(left)
    normalized_right = _canonical_json_value(right)
    paths: list[str] = []

    def compare(first: Any, second: Any, path: str) -> None:
        if type(first) is not type(second):
            paths.append(path or "/")
            return
        if type(first) is dict:
            keys = sorted(set(first) | set(second))
            for key in keys:
                child = f"{path}/{_json_pointer_token(key)}"
                if key not in first or key not in second:
                    paths.append(child)
                else:
                    compare(first[key], second[key], child)
            return
        if type(first) is list:
            if len(first) != len(second):
                paths.append(path or "/")
                return
            for index, (first_item, second_item) in enumerate(
                zip(first, second, strict=True)
            ):
                compare(first_item, second_item, f"{path}/{index}")
            return
        if first != second:
            paths.append(path or "/")

    compare(normalized_left, normalized_right, "")
    return tuple(sorted(paths))


@dataclass(frozen=True)
class ContrastContract:
    schema_version: int
    contrast_id: ContrastId
    baseline: ProfileId
    treatment: ProfileId
    estimand: str
    expected_mechanism_diff_paths: tuple[str, ...]
    required_equal_identities: tuple[IdentityProjection, ...]
    shared_online_execution: bool
    policy_fork_prefix: bool
    treatment_derived_from_baseline_online_execution: bool
    shared_finalization_assessment: bool

    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version, label="contrast contract schema_version"
        )
        _require_enum(self.contrast_id, ContrastId, label="contrast_id")
        _require_enum(self.baseline, ProfileId, label="contrast baseline")
        _require_enum(self.treatment, ProfileId, label="contrast treatment")
        if self.baseline is self.treatment:
            raise ProfileValidationError("a contrast requires distinct profile labels")
        _require_identifier(self.estimand, label="contrast estimand")
        if type(self.expected_mechanism_diff_paths) is not tuple or not (
            self.expected_mechanism_diff_paths
        ):
            raise ProfileValidationError(
                "expected_mechanism_diff_paths must be a non-empty tuple"
            )
        if tuple(sorted(set(self.expected_mechanism_diff_paths))) != (
            self.expected_mechanism_diff_paths
        ):
            raise ProfileValidationError(
                "mechanism diff paths must be sorted and unique"
            )
        if any(
            type(path) is not str or not path.startswith("/")
            for path in self.expected_mechanism_diff_paths
        ):
            raise ProfileValidationError("mechanism diff paths must be JSON pointers")
        if type(self.required_equal_identities) is not tuple:
            raise TypeError("required_equal_identities must be a tuple")
        if len(self.required_equal_identities) != len(
            set(self.required_equal_identities)
        ):
            raise ProfileValidationError(
                "required_equal_identities must not contain duplicates"
            )
        for field in self.required_equal_identities:
            _require_enum(
                field, IdentityProjection, label="required equal identity"
            )
        for label, value in (
            ("shared_online_execution", self.shared_online_execution),
            ("policy_fork_prefix", self.policy_fork_prefix),
            (
                "treatment_derived_from_baseline_online_execution",
                self.treatment_derived_from_baseline_online_execution,
            ),
            (
                "shared_finalization_assessment",
                self.shared_finalization_assessment,
            ),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contrast_id": self.contrast_id.value,
            "baseline": self.baseline.value,
            "treatment": self.treatment.value,
            "estimand": self.estimand,
            "expected_mechanism_diff_paths": list(
                self.expected_mechanism_diff_paths
            ),
            "required_equal_identities": [
                field.value for field in self.required_equal_identities
            ],
            "shared_online_execution": self.shared_online_execution,
            "policy_fork_prefix": self.policy_fork_prefix,
            "treatment_derived_from_baseline_online_execution": (
                self.treatment_derived_from_baseline_online_execution
            ),
            "shared_finalization_assessment": (
                self.shared_finalization_assessment
            ),
        }


_CANONICAL_PROFILE_ORDER = (
    ProfileId.B2,
    ProfileId.T2S,
    ProfileId.B5,
    ProfileId.B6,
    ProfileId.FULL,
    ProfileId.G0,
    ProfileId.G1,
)
_ALIAS_PROFILE_ORDER = (ProfileId.G2,)
_EXECUTION_GROUP_ORDER = (
    "b2_reference",
    "t2s_reference",
    "core_gate_pair",
    "g0_grounding",
    "g1_grounding",
)
_CONTRAST_ORDER = (
    ContrastId.T2S_MINUS_B2,
    ContrastId.B6_MINUS_B5,
    ContrastId.FULL_MINUS_B6,
    ContrastId.G1_MINUS_G0,
    ContrastId.G2_MINUS_G1,
)


def _profile(
    profile_id: ProfileId,
    display_name: str,
    *,
    procedure: bool = False,
    contract_gate: SubmitGateMode = SubmitGateMode.OFF,
    grounding: TargetGroundingMode = TargetGroundingMode.OFF,
    finalization: FinalizationMode = FinalizationMode.OBSERVE,
    execution_group: str,
) -> ExperimentProfile:
    contract_enabled = contract_gate is not SubmitGateMode.OFF
    return ExperimentProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        profile_id=profile_id,
        display_name=display_name,
        agent_arm=UnderlyingAgentArm.PAPER,
        procedure_artifact_id=(T2S_PROCEDURE_ARTIFACT_ID if procedure else None),
        procedure_stages=((ProcedureStage.SOLVE,) if procedure else ()),
        contract_artifact_id=(
            FIXED_CONTRACT_ARTIFACT_ID if contract_enabled else None
        ),
        diagnostics_policy=(
            DiagnosticsPolicy.MODE_NEUTRAL_V1
            if contract_enabled
            else DiagnosticsPolicy.OFF
        ),
        evidence_router_policy=(
            EvidenceRouterPolicy.FIXED_DEADLINE_RESERVATION_V1
            if contract_enabled
            else EvidenceRouterPolicy.OFF
        ),
        submit_gate=contract_gate,
        target_grounding_mode=grounding,
        completion_attempt_policy=(
            CompletionAttemptPolicy.PRE_GATE_EVERY_SUBMIT_V1
        ),
        finalization_pipeline_id=FINALIZATION_PIPELINE_ID,
        finalization_mode=finalization,
        execution_group=execution_group,
    )


REGISTERED_PROFILES = (
    _profile(ProfileId.B2, "B2 multi-format", execution_group="b2_reference"),
    _profile(
        ProfileId.T2S,
        "T2S procedure only",
        procedure=True,
        execution_group="t2s_reference",
    ),
    _profile(
        ProfileId.B5,
        "B5 fixed shadow",
        contract_gate=SubmitGateMode.OBSERVE,
        grounding=TargetGroundingMode.ENFORCE,
        execution_group="core_gate_pair",
    ),
    _profile(
        ProfileId.B6,
        "B6 fixed enforce",
        contract_gate=SubmitGateMode.ENFORCE,
        grounding=TargetGroundingMode.ENFORCE,
        execution_group="core_gate_pair",
    ),
    _profile(
        ProfileId.FULL,
        "FULL SheetLedger",
        contract_gate=SubmitGateMode.ENFORCE,
        grounding=TargetGroundingMode.ENFORCE,
        finalization=FinalizationMode.ENFORCE,
        execution_group="core_gate_pair",
    ),
    _profile(
        ProfileId.G0,
        "G0 no target grounding",
        contract_gate=SubmitGateMode.ENFORCE,
        grounding=TargetGroundingMode.OFF,
        finalization=FinalizationMode.ENFORCE,
        execution_group="g0_grounding",
    ),
    _profile(
        ProfileId.G1,
        "G1 advisory declaration",
        contract_gate=SubmitGateMode.ENFORCE,
        grounding=TargetGroundingMode.ADVISORY,
        finalization=FinalizationMode.ENFORCE,
        execution_group="g1_grounding",
    ),
)

PROFILE_ALIASES = (ProfileAlias(ProfileId.G2, ProfileId.FULL),)

EXECUTION_GROUP_CONTRACTS = (
    ExecutionGroupContract(
        PROFILE_SCHEMA_VERSION,
        "b2_reference",
        (ProfileId.B2,),
        ProfileId.B2,
    ),
    ExecutionGroupContract(
        PROFILE_SCHEMA_VERSION,
        "t2s_reference",
        (ProfileId.T2S,),
        ProfileId.T2S,
    ),
    ExecutionGroupContract(
        PROFILE_SCHEMA_VERSION,
        "core_gate_pair",
        (ProfileId.B5, ProfileId.B6, ProfileId.FULL),
        ProfileId.B6,
        policy_fork_profile=ProfileId.B5,
        policy_fork_prefix_profiles=(ProfileId.B5, ProfileId.B6),
        shared_online_execution_profiles=(ProfileId.B6, ProfileId.FULL),
        derived_from_online_source_profiles=(ProfileId.FULL,),
        shared_finalization_assessment_profiles=(ProfileId.B6, ProfileId.FULL),
    ),
    ExecutionGroupContract(
        PROFILE_SCHEMA_VERSION,
        "g0_grounding",
        (ProfileId.G0,),
        ProfileId.G0,
    ),
    ExecutionGroupContract(
        PROFILE_SCHEMA_VERSION,
        "g1_grounding",
        (ProfileId.G1,),
        ProfileId.G1,
    ),
)

_PROMPT_TOOL_DIAGNOSTIC_VISIBLE = (
    IdentityProjection.PROMPT_POLICY,
    IdentityProjection.TOOL_SURFACE,
    IdentityProjection.DIAGNOSTICS,
    IdentityProjection.MODEL_VISIBLE,
)

CONTRAST_CONTRACTS = (
    ContrastContract(
        PROFILE_SCHEMA_VERSION,
        ContrastId.T2S_MINUS_B2,
        ProfileId.B2,
        ProfileId.T2S,
        "procedure-elicitation",
        ("/procedure",),
        (
            IdentityProjection.TOOL_SURFACE,
            IdentityProjection.DIAGNOSTICS,
            IdentityProjection.POSTPROCESS_PIPELINE,
        ),
        shared_online_execution=False,
        policy_fork_prefix=False,
        treatment_derived_from_baseline_online_execution=False,
        shared_finalization_assessment=False,
    ),
    ContrastContract(
        PROFILE_SCHEMA_VERSION,
        ContrastId.B6_MINUS_B5,
        ProfileId.B5,
        ProfileId.B6,
        "contract-submit-enforcement",
        ("/contract/submit_gate",),
        (
            *_PROMPT_TOOL_DIAGNOSTIC_VISIBLE,
            IdentityProjection.PRE_MUTATION_COMMON_PREFIX,
            IdentityProjection.PRE_GATE_RUNTIME,
            IdentityProjection.POSTPROCESS_PIPELINE,
        ),
        shared_online_execution=False,
        policy_fork_prefix=True,
        treatment_derived_from_baseline_online_execution=False,
        shared_finalization_assessment=False,
    ),
    ContrastContract(
        PROFILE_SCHEMA_VERSION,
        ContrastId.FULL_MINUS_B6,
        ProfileId.B6,
        ProfileId.FULL,
        "posttermination-lineage-enforcement",
        ("/finalization/mode",),
        (
            *_PROMPT_TOOL_DIAGNOSTIC_VISIBLE,
            IdentityProjection.PRE_MUTATION_COMMON_PREFIX,
            IdentityProjection.PRE_GATE_RUNTIME,
            IdentityProjection.ONLINE_EXECUTION,
            IdentityProjection.POSTPROCESS_PIPELINE,
        ),
        shared_online_execution=True,
        policy_fork_prefix=False,
        treatment_derived_from_baseline_online_execution=True,
        shared_finalization_assessment=True,
    ),
    ContrastContract(
        PROFILE_SCHEMA_VERSION,
        ContrastId.G1_MINUS_G0,
        ProfileId.G0,
        ProfileId.G1,
        "target-declaration-elicitation",
        ("/target_grounding_mode",),
        (IdentityProjection.POSTPROCESS_PIPELINE,),
        shared_online_execution=False,
        policy_fork_prefix=False,
        treatment_derived_from_baseline_online_execution=False,
        shared_finalization_assessment=False,
    ),
    ContrastContract(
        PROFILE_SCHEMA_VERSION,
        ContrastId.G2_MINUS_G1,
        ProfileId.G1,
        ProfileId.G2,
        "staged-containment-enforcement",
        ("/target_grounding_mode",),
        (
            *_PROMPT_TOOL_DIAGNOSTIC_VISIBLE,
            IdentityProjection.PRE_MUTATION_COMMON_PREFIX,
            IdentityProjection.POSTPROCESS_PIPELINE,
        ),
        shared_online_execution=False,
        policy_fork_prefix=False,
        treatment_derived_from_baseline_online_execution=False,
        shared_finalization_assessment=False,
    ),
)


def _expected_mechanism(profile_id: ProfileId) -> dict[str, Any]:
    expected = next(
        profile
        for profile in REGISTERED_PROFILES
        if profile.profile_id is profile_id
    )
    return expected.mechanism_dict()


def _expected_execution_group(profile_id: ProfileId) -> str:
    return next(
        profile.execution_group
        for profile in REGISTERED_PROFILES
        if profile.profile_id is profile_id
    )


class ProfileRegistry:
    """Strict registry whose identity is independent of constructor input order."""

    def __init__(
        self,
        profiles: tuple[ExperimentProfile, ...],
        aliases: tuple[ProfileAlias, ...],
        execution_groups: tuple[ExecutionGroupContract, ...],
        contrasts: tuple[ContrastContract, ...],
    ) -> None:
        for label, values in (
            ("profiles", profiles),
            ("aliases", aliases),
            ("execution_groups", execution_groups),
            ("contrasts", contrasts),
        ):
            if type(values) is not tuple:
                raise TypeError(f"{label} must be a tuple")

        profile_ids = [profile.profile_id for profile in profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ProfileValidationError("duplicate canonical profile id")
        if set(profile_ids) != set(_CANONICAL_PROFILE_ORDER):
            raise ProfileValidationError("canonical profile set is incomplete or unexpected")
        self._profiles = {profile.profile_id: profile for profile in profiles}
        for profile_id in _CANONICAL_PROFILE_ORDER:
            profile = self._profiles[profile_id]
            if profile.mechanism_dict() != _expected_mechanism(profile_id):
                raise ProfileValidationError(
                    f"profile {profile_id.value} does not match its frozen mechanism"
                )
            if profile.execution_group != _expected_execution_group(profile_id):
                raise ProfileValidationError(
                    f"profile {profile_id.value} has the wrong execution group"
                )

        alias_ids = [alias.alias for alias in aliases]
        if len(alias_ids) != len(set(alias_ids)):
            raise ProfileValidationError("duplicate profile alias")
        if set(alias_ids) & set(profile_ids):
            raise ProfileValidationError("an alias collides with a canonical profile")
        if set(alias_ids) != set(_ALIAS_PROFILE_ORDER):
            raise ProfileValidationError("profile alias set is incomplete or unexpected")
        self._aliases = {alias.alias: alias.target for alias in aliases}
        if self._aliases != {ProfileId.G2: ProfileId.FULL}:
            raise ProfileValidationError("G2 must be the sole alias of FULL")
        if any(target not in self._profiles for target in self._aliases.values()):
            raise ProfileValidationError("profile alias target is not canonical")

        group_ids = [group.group_id for group in execution_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ProfileValidationError("duplicate execution group id")
        if set(group_ids) != set(_EXECUTION_GROUP_ORDER):
            raise ProfileValidationError("execution group set is incomplete or unexpected")
        self._execution_groups = {group.group_id: group for group in execution_groups}
        covered: list[ProfileId] = []
        for group in execution_groups:
            covered.extend(group.profiles)
            for profile_id in group.profiles:
                if profile_id not in self._profiles:
                    raise ProfileValidationError(
                        "execution group references a non-canonical profile"
                    )
                if self._profiles[profile_id].execution_group != group.group_id:
                    raise ProfileValidationError(
                        "execution group disagrees with profile scheduling metadata"
                    )
        if len(covered) != len(set(covered)) or set(covered) != set(profile_ids):
            raise ProfileValidationError(
                "canonical profiles must occur in exactly one execution group"
            )
        core = self._execution_groups["core_gate_pair"]
        if core.to_dict() != next(
            item.to_dict()
            for item in EXECUTION_GROUP_CONTRACTS
            if item.group_id == "core_gate_pair"
        ):
            raise ProfileValidationError("core gate execution sharing contract changed")

        contrast_ids = [contrast.contrast_id for contrast in contrasts]
        if len(contrast_ids) != len(set(contrast_ids)):
            raise ProfileValidationError("duplicate contrast id")
        if set(contrast_ids) != set(_CONTRAST_ORDER):
            raise ProfileValidationError("contrast set is incomplete or unexpected")
        self._contrasts = {
            contrast.contrast_id: contrast for contrast in contrasts
        }
        self._identities = {
            profile_id: profile_identity(self._profiles[profile_id])
            for profile_id in _CANONICAL_PROFILE_ORDER
        }
        self._validate_contrasts()

    @property
    def profiles(self) -> tuple[ExperimentProfile, ...]:
        return tuple(self._profiles[item] for item in _CANONICAL_PROFILE_ORDER)

    @property
    def aliases(self) -> tuple[ProfileAlias, ...]:
        return tuple(
            ProfileAlias(item, self._aliases[item]) for item in _ALIAS_PROFILE_ORDER
        )

    @property
    def execution_groups(self) -> tuple[ExecutionGroupContract, ...]:
        return tuple(self._execution_groups[item] for item in _EXECUTION_GROUP_ORDER)

    @property
    def contrasts(self) -> tuple[ContrastContract, ...]:
        return tuple(self._contrasts[item] for item in _CONTRAST_ORDER)

    def canonical_profile_id(self, value: ProfileId | str) -> ProfileId:
        if isinstance(value, ProfileId):
            profile_id = value
        elif type(value) is str:
            try:
                profile_id = ProfileId(value)
            except ValueError as exc:
                raise ProfileValidationError(f"unknown profile id: {value!r}") from exc
        else:
            raise TypeError("profile id must be a ProfileId or exact string")
        return self._aliases.get(profile_id, profile_id)

    def resolve(self, value: ProfileId | str) -> ExperimentProfile:
        canonical_id = self.canonical_profile_id(value)
        try:
            return self._profiles[canonical_id]
        except KeyError as exc:
            raise ProfileValidationError(
                f"unknown canonical profile id: {canonical_id.value!r}"
            ) from exc

    def identity(self, value: ProfileId | str) -> ProfileIdentity:
        return self._identities[self.canonical_profile_id(value)]

    def contrast(self, value: ContrastId | str) -> ContrastContract:
        if isinstance(value, ContrastId):
            contrast_id = value
        elif type(value) is str:
            try:
                contrast_id = ContrastId(value)
            except ValueError as exc:
                raise ProfileValidationError(
                    f"unknown contrast id: {value!r}"
                ) from exc
        else:
            raise TypeError("contrast id must be a ContrastId or exact string")
        return self._contrasts[contrast_id]

    def _validate_contrasts(self) -> None:
        expected_contracts = {
            item.contrast_id: item.to_dict() for item in CONTRAST_CONTRACTS
        }
        for contrast_id in _CONTRAST_ORDER:
            contrast = self._contrasts[contrast_id]
            baseline = self.resolve(contrast.baseline)
            treatment = self.resolve(contrast.treatment)
            actual_paths = exact_diff_paths(
                baseline.mechanism_dict(), treatment.mechanism_dict()
            )
            if actual_paths != contrast.expected_mechanism_diff_paths:
                raise ProfileValidationError(
                    f"contrast {contrast_id.value} mechanism diff is {actual_paths!r}"
                )
            baseline_identity = self.identity(contrast.baseline)
            treatment_identity = self.identity(contrast.treatment)
            for projection in contrast.required_equal_identities:
                if baseline_identity.projection(
                    projection
                ) != treatment_identity.projection(projection):
                    raise ProfileValidationError(
                        f"contrast {contrast_id.value} violates required equality "
                        f"{projection.value}"
                    )
            same_group = baseline.execution_group == treatment.execution_group
            group = (
                self._execution_groups[baseline.execution_group]
                if same_group
                else None
            )
            pair = {baseline.profile_id, treatment.profile_id}
            expected_policy_fork_prefix = bool(
                group is not None
                and baseline.profile_id is group.policy_fork_profile
                and treatment.profile_id is group.online_source_profile
                and group.policy_fork_prefix_profiles
                == (baseline.profile_id, treatment.profile_id)
            )
            expected_shared_online_execution = bool(
                group is not None
                and pair.issubset(group.shared_online_execution_profiles)
            )
            expected_treatment_derived = bool(
                group is not None
                and baseline.profile_id is group.online_source_profile
                and treatment.profile_id
                in group.derived_from_online_source_profiles
            )
            expected_shared_finalization = bool(
                group is not None
                and pair.issubset(
                    group.shared_finalization_assessment_profiles
                )
            )
            declared_relationships = (
                (
                    "policy_fork_prefix",
                    contrast.policy_fork_prefix,
                    expected_policy_fork_prefix,
                ),
                (
                    "shared_online_execution",
                    contrast.shared_online_execution,
                    expected_shared_online_execution,
                ),
                (
                    "treatment_derived_from_baseline_online_execution",
                    contrast.treatment_derived_from_baseline_online_execution,
                    expected_treatment_derived,
                ),
                (
                    "shared_finalization_assessment",
                    contrast.shared_finalization_assessment,
                    expected_shared_finalization,
                ),
            )
            for label, declared, expected in declared_relationships:
                if declared is not expected:
                    raise ProfileValidationError(
                        f"contrast {contrast_id.value} {label} disagrees with "
                        "its execution group"
                    )
            if (
                contrast.policy_fork_prefix
                and baseline_identity.pre_gate_runtime_sha256
                != treatment_identity.pre_gate_runtime_sha256
            ):
                raise ProfileValidationError(
                    "policy-fork prefix profiles must share pre-gate runtime"
                )
            if (
                contrast.shared_online_execution
                and baseline_identity.online_execution_identity_sha256
                != treatment_identity.online_execution_identity_sha256
            ):
                raise ProfileValidationError(
                    "shared-online profiles must share online execution identity"
                )
            if (
                contrast.shared_finalization_assessment
                and baseline_identity.postprocess_pipeline_sha256
                != treatment_identity.postprocess_pipeline_sha256
            ):
                raise ProfileValidationError(
                    "shared-finalization profiles must share assessment identity"
                )
            if contrast.to_dict() != expected_contracts[contrast_id]:
                raise ProfileValidationError(
                    f"contrast {contrast_id.value} changed from its frozen design"
                )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE_REGISTRY_SCHEMA_VERSION,
            "status": PROFILE_REGISTRY_STATUS,
            "launchable": PROFILE_REGISTRY_LAUNCHABLE,
            "launch_blockers": [
                blocker.to_dict()
                for blocker in LAUNCH_ARTIFACT_DIGEST_BLOCKERS
            ],
            "identity_policy_versions": {
                "paper_prompt": PAPER_PROMPT_POLICY_ID,
                "paper_tool_surface": PAPER_TOOL_SURFACE_POLICY_ID,
                "terminal_tool": TERMINAL_TOOL_POLICY_ID,
                "contract_guidance": CONTRACT_GUIDANCE_POLICY_ID,
                "grounding_guidance": GROUNDING_GUIDANCE_POLICY_ID,
                "grounding_tool_schema": GROUNDING_TOOL_SCHEMA_POLICY_ID,
                "grounding_diagnostics": GROUNDING_DIAGNOSTIC_POLICY_ID,
                "completion_capture_visibility": (
                    COMPLETION_CAPTURE_VISIBILITY_POLICY_ID
                ),
                "pre_mutation_boundary": PRE_MUTATION_BOUNDARY_POLICY_ID,
            },
            "profiles": [
                {
                    **profile.to_dict(),
                    "identity": self.identity(profile.profile_id).to_dict(),
                }
                for profile in self.profiles
            ],
            "aliases": [alias.to_dict() for alias in self.aliases],
            "execution_groups": [
                group.to_dict() for group in self.execution_groups
            ],
            "contrasts": [contrast.to_dict() for contrast in self.contrasts],
        }

    @property
    def canonical_sha256(self) -> str:
        return canonical_sha256(self.identity_dict())


PROFILE_REGISTRY = ProfileRegistry(
    REGISTERED_PROFILES,
    PROFILE_ALIASES,
    EXECUTION_GROUP_CONTRACTS,
    CONTRAST_CONTRACTS,
)
PROFILE_REGISTRY_IDENTITY = PROFILE_REGISTRY.identity_dict()
PROFILE_REGISTRY_SHA256 = PROFILE_REGISTRY.canonical_sha256


__all__ = [
    "COMPLETION_CAPTURE_VISIBILITY_POLICY_ID",
    "CONTRACT_GUIDANCE_POLICY_ID",
    "CONTRAST_CONTRACTS",
    "ContrastContract",
    "ContrastId",
    "CompletionAttemptPolicy",
    "DiagnosticsPolicy",
    "EXECUTION_GROUP_CONTRACTS",
    "EvidenceRouterPolicy",
    "ExecutionGroupContract",
    "ExperimentProfile",
    "FINALIZATION_PIPELINE_ID",
    "FIXED_CONTRACT_ARTIFACT_ID",
    "FinalizationMode",
    "GROUNDING_DIAGNOSTIC_POLICY_ID",
    "GROUNDING_GUIDANCE_POLICY_ID",
    "GROUNDING_TOOL_SCHEMA_POLICY_ID",
    "IdentityProjection",
    "LAUNCH_ARTIFACT_DIGEST_BLOCKERS",
    "LAUNCH_BLOCKER_CODE",
    "LaunchArtifactDigestBlocker",
    "PAPER_PROMPT_POLICY_ID",
    "PAPER_TOOL_SURFACE_POLICY_ID",
    "PROFILE_ALIASES",
    "PROFILE_REGISTRY",
    "PROFILE_REGISTRY_IDENTITY",
    "PROFILE_REGISTRY_LAUNCHABLE",
    "PROFILE_REGISTRY_SCHEMA_VERSION",
    "PROFILE_REGISTRY_SHA256",
    "PROFILE_REGISTRY_STATUS",
    "PROFILE_SCHEMA_VERSION",
    "PRE_MUTATION_BOUNDARY_POLICY_ID",
    "ProfileAlias",
    "ProfileId",
    "ProfileIdentity",
    "ProfileRegistry",
    "ProfileValidationError",
    "ProcedureStage",
    "REGISTERED_PROFILES",
    "SubmitGateMode",
    "T2S_PROCEDURE_ARTIFACT_ID",
    "TERMINAL_TOOL_POLICY_ID",
    "UnderlyingAgentArm",
    "canonical_json_bytes",
    "canonical_sha256",
    "exact_diff_paths",
    "profile_identity",
]
