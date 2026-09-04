"""Contract-constrained plugin compositions for harness evolution.

The scorer, sandbox, session log, and benchmark runner are deliberately outside
this module.  Evolution may change one registered plugin implementation or one
composition slot at a time, but it may not synthesize hooks or capabilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, pvariance
from types import MappingProxyType
from typing import Any, Literal

from .errors import HarnessError

PluginKind = Literal["observe", "act", "control", "verify", "knowledge", "repair", "workflow"]
SpreadsheetCapability = Literal[
    "structure",
    "formula",
    "manipulation",
    "analysis",
    "visualization",
    "verification",
    "memory",
    "composition",
]
Scalar = str | int | float | bool | None

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")

ALLOWED_PLUGIN_HOOKS = frozenset(
    {
        "before_task",
        "before_model_request",
        "tool_registry",
        "after_tool",
        "before_submit",
        "after_run",
    }
)

KERNEL_CAPABILITIES = frozenset(
    {
        "agent.execute",
        "artifact.validate",
        "model.request",
        "trajectory.record",
        "workbook.read",
        "workbook.write",
    }
)

SPREADSHEET_CAPABILITIES: tuple[SpreadsheetCapability, ...] = (
    "structure",
    "formula",
    "manipulation",
    "analysis",
    "visualization",
    "verification",
    "memory",
    "composition",
)


def _identifier(value: str, *, label: str) -> str:
    normalized = str(value).strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase dotted/dashed identifier: {value!r}")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _scalar(value: Any, *, label: str) -> Scalar:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{label} must be a finite JSON scalar")


@dataclass(frozen=True)
class ConfigField:
    """One code-owned, evolvable scalar configuration field."""

    name: str
    value_type: Literal["string", "integer", "number", "boolean"]
    default: Scalar
    choices: tuple[Scalar, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="config field"))
        if self.value_type not in {"string", "integer", "number", "boolean"}:
            raise ValueError(f"Unsupported config type: {self.value_type!r}")
        if self.minimum is not None and not math.isfinite(self.minimum):
            raise ValueError("minimum must be finite")
        if self.maximum is not None and not math.isfinite(self.maximum):
            raise ValueError("maximum must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        normalized_choices = tuple(
            _scalar(value, label=f"choice for {self.name}") for value in self.choices
        )
        object.__setattr__(self, "choices", normalized_choices)
        self.validate(self.default)

    def validate(self, value: Any) -> Scalar:
        value = _scalar(value, label=self.name)
        valid_type = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, int | float) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }[self.value_type]
        if not valid_type:
            raise ValueError(f"Plugin config {self.name!r} must be {self.value_type}")
        if self.choices and value not in self.choices:
            raise ValueError(f"Plugin config {self.name!r} must be one of {list(self.choices)!r}")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"Plugin config {self.name!r} is below its minimum")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"Plugin config {self.name!r} exceeds its maximum")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.value_type,
            "default": self.default,
            "choices": list(self.choices),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True)
class EvolutionStrategy:
    """Code-owned evidence and update policy for one plugin family."""

    evidence: tuple[str, ...]
    operators: tuple[str, ...]
    validation_contexts: tuple[str, ...]

    def __post_init__(self) -> None:
        for attribute, label in (
            ("evidence", "evolution evidence"),
            ("operators", "evolution operator"),
            ("validation_contexts", "validation context"),
        ):
            values = tuple(
                _identifier(str(value), label=label) for value in getattr(self, attribute)
            )
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{label} entries must be non-empty and unique")
            object.__setattr__(self, attribute, values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence": list(self.evidence),
            "operators": list(self.operators),
            "validation_contexts": list(self.validation_contexts),
        }


@dataclass(frozen=True)
class PluginContract:
    """Immutable ABI and mutation boundary for one harness plugin."""

    name: str
    version: str
    kind: PluginKind
    implementation: str
    provides: frozenset[str]
    requires: frozenset[str] = frozenset()
    hooks: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    config_fields: tuple[ConfigField, ...] = ()
    evolvable_surfaces: frozenset[str] = frozenset()
    conflicts: frozenset[str] = frozenset()
    spreadsheet_capabilities: frozenset[SpreadsheetCapability] = frozenset()
    evolution_strategy: EvolutionStrategy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="plugin name"))
        if not _VERSION.fullmatch(self.version):
            raise ValueError(f"Plugin version must be semver-like: {self.version!r}")
        if self.kind not in {
            "observe",
            "act",
            "control",
            "verify",
            "knowledge",
            "repair",
            "workflow",
        }:
            raise ValueError(f"Unsupported plugin kind: {self.kind!r}")
        _identifier(self.implementation, label="implementation")
        for attribute, label, values in (
            ("provides", "provided capability", self.provides),
            ("requires", "required capability", self.requires),
            ("hooks", "hook", self.hooks),
            ("permissions", "permission", self.permissions),
            ("evolvable_surfaces", "evolvable surface", self.evolvable_surfaces),
            ("conflicts", "conflict", self.conflicts),
        ):
            normalized = frozenset(_identifier(value, label=label) for value in values)
            object.__setattr__(self, attribute, normalized)
        object.__setattr__(self, "config_fields", tuple(self.config_fields))
        if not self.provides:
            raise ValueError("A plugin must provide at least one capability")
        unknown_hooks = self.hooks - ALLOWED_PLUGIN_HOOKS
        if unknown_hooks:
            raise ValueError(f"Plugin declares unknown hooks: {sorted(unknown_hooks)}")
        field_names = [field.name for field in self.config_fields]
        if len(field_names) != len(set(field_names)):
            raise ValueError(f"Plugin {self.name!r} has duplicate config fields")
        allowed_surfaces = {"config", "implementation", "prompt", "description"}
        unknown_surfaces = self.evolvable_surfaces - allowed_surfaces
        if unknown_surfaces:
            raise ValueError(
                f"Plugin {self.name!r} has unsupported evolvable surfaces: "
                f"{sorted(unknown_surfaces)}"
            )
        unknown_capabilities = set(self.spreadsheet_capabilities) - set(
            SPREADSHEET_CAPABILITIES
        )
        if unknown_capabilities:
            raise ValueError(
                f"Plugin {self.name!r} has unknown spreadsheet capabilities: "
                f"{sorted(unknown_capabilities)}"
            )
        object.__setattr__(
            self,
            "spreadsheet_capabilities",
            frozenset(self.spreadsheet_capabilities),
        )
        if self.evolution_strategy is not None and not self.evolvable_surfaces:
            raise ValueError("A frozen plugin cannot declare an evolution strategy")

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("ascii")).hexdigest()

    def configure(self, overrides: Mapping[str, Any] | None = None) -> PluginInstance:
        fields = {field.name: field for field in self.config_fields}
        values: dict[str, Scalar] = {name: field.default for name, field in fields.items()}
        for key, value in (overrides or {}).items():
            if key not in fields:
                raise ValueError(f"Plugin {self.name!r} has no configurable field {key!r}")
            values[key] = fields[key].validate(value)
        return PluginInstance(self, tuple(sorted(values.items())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "implementation": self.implementation,
            "provides": sorted(self.provides),
            "requires": sorted(self.requires),
            "hooks": sorted(self.hooks),
            "permissions": sorted(self.permissions),
            "config_fields": [field.to_dict() for field in self.config_fields],
            "evolvable_surfaces": sorted(self.evolvable_surfaces),
            "conflicts": sorted(self.conflicts),
            "spreadsheet_capabilities": sorted(self.spreadsheet_capabilities),
            "evolution_strategy": (
                self.evolution_strategy.to_dict()
                if self.evolution_strategy is not None
                else None
            ),
        }


@dataclass(frozen=True)
class PluginInstance:
    contract: PluginContract
    _config: tuple[tuple[str, Scalar], ...]

    def __post_init__(self) -> None:
        fields = {field.name: field for field in self.contract.config_fields}
        provided = dict(self._config)
        if len(provided) != len(self._config) or set(provided) != set(fields):
            raise ValueError("Plugin instance config must contain each declared field exactly once")
        normalized = tuple(
            sorted((name, fields[name].validate(value)) for name, value in provided.items())
        )
        object.__setattr__(self, "_config", normalized)

    @property
    def config(self) -> Mapping[str, Scalar]:
        return MappingProxyType(dict(self._config))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.contract.name,
            "version": self.contract.version,
            "manifest_sha256": self.contract.manifest_sha256,
            "config": dict(self._config),
        }


@dataclass(frozen=True)
class CompositionSpec:
    name: str
    plugins: tuple[str, ...]
    _overrides: tuple[tuple[str, tuple[tuple[str, Scalar], ...]], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="composition name"))
        normalized = tuple(_identifier(name, label="plugin name") for name in self.plugins)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("A composition needs unique plugin names")
        object.__setattr__(self, "plugins", normalized)
        normalized_overrides: list[tuple[str, tuple[tuple[str, Scalar], ...]]] = []
        for raw_name, raw_values in self._overrides:
            plugin_name = _identifier(raw_name, label="override plugin")
            values = tuple(
                sorted(
                    (
                        _identifier(str(key), label="config field"),
                        _scalar(value, label=f"{plugin_name}.{key}"),
                    )
                    for key, value in raw_values
                )
            )
            if len(dict(values)) != len(values):
                raise ValueError("Composition config fields must be unique per plugin")
            normalized_overrides.append((plugin_name, values))
        normalized_override_tuple = tuple(sorted(normalized_overrides))
        object.__setattr__(self, "_overrides", normalized_override_tuple)
        override_names = [name for name, _values in normalized_override_tuple]
        if len(override_names) != len(set(override_names)):
            raise ValueError("Composition config overrides must target unique plugins")
        if any(name not in normalized for name in override_names):
            raise ValueError("Composition config overrides may target only enabled plugins")

    @classmethod
    def create(
        cls,
        name: str,
        plugins: Sequence[str],
        overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> CompositionSpec:
        normalized_overrides: list[tuple[str, tuple[tuple[str, Scalar], ...]]] = []
        for plugin_name, values in sorted((overrides or {}).items()):
            normalized_overrides.append(
                (
                    str(plugin_name),
                    tuple(
                        sorted(
                            (str(key), _scalar(value, label=f"{plugin_name}.{key}"))
                            for key, value in values.items()
                        )
                    ),
                )
            )
        return cls(str(name), tuple(str(item) for item in plugins), tuple(normalized_overrides))

    @property
    def overrides(self) -> Mapping[str, Mapping[str, Scalar]]:
        return MappingProxyType(
            {name: MappingProxyType(dict(values)) for name, values in self._overrides}
        )

    def with_plugins(
        self,
        *,
        name: str,
        plugins: Sequence[str],
        overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> CompositionSpec:
        return CompositionSpec.create(
            name, plugins, overrides if overrides is not None else self.overrides
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "plugins": list(self.plugins),
            "overrides": {plugin: dict(values) for plugin, values in self._overrides},
        }


@dataclass(frozen=True)
class ResolvedComposition:
    spec: CompositionSpec
    plugins: tuple[PluginInstance, ...]
    _providers: tuple[tuple[str, str], ...]

    @property
    def providers(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._providers))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("ascii")).hexdigest()

    def provider(self, capability: str) -> PluginInstance | None:
        name = self.providers.get(capability)
        return next((plugin for plugin in self.plugins if plugin.contract.name == name), None)

    def has(self, plugin_name: str) -> bool:
        return any(plugin.contract.name == plugin_name for plugin in self.plugins)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "plugevolve-composition-v1",
            "name": self.spec.name,
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "providers": dict(self._providers),
            "kernel_capabilities": sorted(KERNEL_CAPABILITIES),
        }


class PluginRegistry:
    def __init__(self, plugins: Sequence[PluginContract]) -> None:
        by_name: dict[str, PluginContract] = {}
        for plugin in plugins:
            if plugin.name in by_name:
                raise ValueError(f"Duplicate plugin name: {plugin.name}")
            by_name[plugin.name] = plugin
        self._plugins = MappingProxyType(by_name)

    def contracts(self) -> tuple[PluginContract, ...]:
        return tuple(self._plugins[name] for name in sorted(self._plugins))

    def get(self, name: str) -> PluginContract:
        try:
            return self._plugins[name]
        except KeyError as exc:
            raise HarnessError(f"Unknown harness plugin: {name}") from exc

    def resolve(self, spec: CompositionSpec) -> ResolvedComposition:
        selected = [self.get(name) for name in spec.plugins]
        providers: dict[str, str] = {}
        for plugin in selected:
            for capability in plugin.provides:
                prior = providers.get(capability)
                if prior is not None:
                    raise HarnessError(
                        f"Composition {spec.name!r} has two providers for {capability!r}: "
                        f"{prior!r} and {plugin.name!r}"
                    )
                providers[capability] = plugin.name
        available = KERNEL_CAPABILITIES | frozenset(providers)
        for plugin in selected:
            missing = plugin.requires - available
            if missing:
                raise HarnessError(
                    f"Plugin {plugin.name!r} has unsatisfied capabilities: {sorted(missing)}"
                )
            conflicts = plugin.conflicts & (available | frozenset(spec.plugins))
            if conflicts:
                raise HarnessError(
                    f"Plugin {plugin.name!r} conflicts with active entries: {sorted(conflicts)}"
                )

        # Stable topological order: providers precede consumers while original
        # composition order breaks otherwise independent ties.
        pending = list(selected)
        ordered: list[PluginContract] = []
        ready_capabilities = set(KERNEL_CAPABILITIES)
        while pending:
            ready = [plugin for plugin in pending if plugin.requires <= ready_capabilities]
            if not ready:
                raise HarnessError(f"Composition {spec.name!r} contains a capability cycle")
            plugin = ready[0]
            pending.remove(plugin)
            ordered.append(plugin)
            ready_capabilities.update(plugin.provides)

        overrides = spec.overrides
        instances = tuple(plugin.configure(overrides.get(plugin.name)) for plugin in ordered)
        return ResolvedComposition(spec, instances, tuple(sorted(providers.items())))


@dataclass(frozen=True)
class PluginExecutionPlan:
    """Narrow adapter from plugin contracts to the existing arm runtime."""

    workflow: Literal["single-stage", "paper"]
    tool_mode: Literal["code-only", "code-plus-formula-validation", "native"] | None
    profile_mode: Literal["none", "full", "compact"]
    profile_config: Mapping[str, Scalar]
    policy: Literal["bare", "profile", "native", "ours"] | None
    skill_names: tuple[str, ...]
    financial_model_runtime: bool
    require_formula_runtime_validation: bool
    repair_date_text: bool

    @property
    def load_skills(self) -> bool:
        return bool(self.skill_names)


def execution_plan(composition: ResolvedComposition) -> PluginExecutionPlan:
    workflow = composition.provider("workflow.paper")
    if workflow is not None:
        if len(composition.plugins) != 1:
            raise HarnessError("The paper workflow is an atomic legacy workflow in plugin v1")
        return PluginExecutionPlan(
            "paper", None, "none", MappingProxyType({}), None, (), False, False, False
        )

    action = composition.provider("action.spreadsheet")
    policy = composition.provider("policy.solve")
    if action is None or policy is None:
        raise HarnessError(
            "A single-stage composition requires action.spreadsheet and policy.solve"
        )
    tool_modes = {
        "runtime-code-interpreter": "code-only",
        "runtime-code-plus-formula-validation": "code-plus-formula-validation",
        "runtime-native-tools": "native",
    }
    policies = {
        "policy-bare": "bare",
        "policy-profile": "profile",
        "policy-native": "native",
        "policy-ours": "ours",
    }
    try:
        tool_mode = tool_modes[action.contract.name]
        policy_name = policies[policy.contract.name]
    except KeyError as exc:
        raise HarnessError(f"No runtime adapter for plugin {exc.args[0]!r}") from exc

    profile = composition.provider("context.workbook-profile")
    profile_mode: Literal["none", "full", "compact"] = "none"
    profile_config: Mapping[str, Scalar] = MappingProxyType({})
    if profile is not None:
        profile_modes = {
            "profile-deterministic-full": "full",
            "profile-deterministic-compact": "compact",
        }
        try:
            profile_mode = profile_modes[profile.contract.name]
        except KeyError as exc:
            raise HarnessError(f"No runtime adapter for plugin {exc.args[0]!r}") from exc
        profile_config = profile.config

    if policy_name in {"profile", "ours"} and profile is None:
        raise HarnessError(f"Policy {policy_name!r} requires a workbook profile provider")
    if policy_name == "native" and tool_mode != "native":
        raise HarnessError("The native policy requires the native tool provider")

    knowledge_implementations = {
        "knowledge.spreadsheet-core": "spreadsheet-core",
        "knowledge.spreadsheet-structure": "spreadsheet-structure",
        "knowledge.spreadsheet-formula": "spreadsheet-formula",
        "knowledge.spreadsheet-financial-model": "spreadsheet-financial-model",
        "knowledge.spreadsheet-manipulation": "spreadsheet-manipulation",
        "knowledge.spreadsheet-analysis": "spreadsheet-analysis",
        "knowledge.spreadsheet-visualization": "visual-review",
        "knowledge.spreadsheet-verification": "spreadsheet-verification",
        "knowledge.spreadsheet-memory": "spreadsheet-memory",
    }
    skill_names: list[str] = []
    for plugin in composition.plugins:
        if plugin.contract.kind != "knowledge":
            continue
        try:
            skill_names.append(knowledge_implementations[plugin.contract.implementation])
        except KeyError as exc:
            raise HarnessError(
                f"No skill adapter for knowledge plugin {plugin.contract.name!r}"
            ) from exc

    return PluginExecutionPlan(
        "single-stage",
        tool_mode,  # type: ignore[arg-type]
        profile_mode,
        profile_config,
        policy_name,  # type: ignore[arg-type]
        tuple(skill_names),
        composition.provider("knowledge.spreadsheet-financial-model") is not None,
        composition.provider("verification.formula-runtime") is not None,
        composition.provider("repair.date-text") is not None,
    )


@dataclass(frozen=True)
class CompositionCandidate:
    operation: Literal["enable", "disable", "replace", "configure"]
    target: str
    composition: ResolvedComposition
    replaced_plugin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "target": self.target,
            "replaced_plugin": self.replaced_plugin,
            "composition_sha256": self.composition.sha256,
            "composition": self.composition.to_dict(),
        }


@dataclass(frozen=True)
class CompositionRoute:
    task_type: str
    matched_rule: str
    composition: ResolvedComposition
    router_manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "matched_rule": self.matched_rule,
            "router_manifest_sha256": self.router_manifest_sha256,
            "composition_sha256": self.composition.sha256,
            "composition_name": self.composition.spec.name,
        }


@dataclass(frozen=True)
class ConstrainedCompositionRouter:
    """Code-owned exact task-type routing over prevalidated compositions."""

    name: str
    allowed_task_types: tuple[str, ...]
    _routes: tuple[tuple[str, CompositionSpec], ...]
    fallback: CompositionSpec

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, label="router name"))
        allowed = tuple(str(value).strip() for value in self.allowed_task_types)
        if not allowed or any(not value for value in allowed) or len(set(allowed)) != len(allowed):
            raise ValueError("Router task types must be unique non-empty strings")
        object.__setattr__(self, "allowed_task_types", allowed)
        routes = tuple(sorted((str(key).strip(), spec) for key, spec in self._routes))
        route_types = [key for key, _spec in routes]
        if (
            any(not key for key in route_types)
            or len(set(route_types)) != len(route_types)
            or not set(route_types) <= set(allowed)
        ):
            raise ValueError("Router rules must uniquely target allowed task types")
        object.__setattr__(self, "_routes", routes)

    @classmethod
    def create(
        cls,
        name: str,
        *,
        allowed_task_types: Sequence[str],
        routes: Mapping[str, CompositionSpec],
        fallback: CompositionSpec,
    ) -> ConstrainedCompositionRouter:
        return cls(
            name,
            tuple(str(value) for value in allowed_task_types),
            tuple((str(key), spec) for key, spec in routes.items()),
            fallback,
        )

    def manifest(self, registry: PluginRegistry) -> dict[str, Any]:
        resolved_routes: dict[str, str] = {}
        for task_type, spec in self._routes:
            resolved = registry.resolve(spec)
            execution_plan(resolved)
            resolved_routes[task_type] = resolved.sha256
        fallback = registry.resolve(self.fallback)
        execution_plan(fallback)
        return {
            "schema_version": "plugevolve-router-v1",
            "name": self.name,
            "allowed_task_types": list(self.allowed_task_types),
            "routes": resolved_routes,
            "fallback_composition_sha256": fallback.sha256,
        }

    def route(self, registry: PluginRegistry, task_type: str) -> CompositionRoute:
        normalized = str(task_type).strip()
        if normalized not in self.allowed_task_types:
            raise HarnessError(f"Router {self.name!r} rejects unknown task type {task_type!r}")
        route_map = dict(self._routes)
        spec = route_map.get(normalized, self.fallback)
        resolved = registry.resolve(spec)
        execution_plan(resolved)
        manifest = self.manifest(registry)
        digest = hashlib.sha256(_canonical_json(manifest).encode("ascii")).hexdigest()
        return CompositionRoute(
            normalized,
            normalized if normalized in route_map else "fallback",
            resolved,
            digest,
        )


@dataclass(frozen=True)
class PluginMutation:
    """One auditable plugin-local edit; contracts are references, never payloads."""

    target_plugin: str
    base_version: str
    base_manifest_sha256: str
    surface: Literal["config", "implementation", "prompt", "description"]
    candidate_artifact_sha256: str
    changed_paths: tuple[str, ...] = ()
    evidence_sha256: tuple[str, ...] = ()
    _config_patch: tuple[tuple[str, Scalar], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_plugin", _identifier(self.target_plugin, label="target plugin")
        )
        if not _VERSION.fullmatch(self.base_version):
            raise ValueError("Plugin mutation base_version must be semver-like")
        for label, digest in (
            ("base manifest", self.base_manifest_sha256),
            ("candidate artifact", self.candidate_artifact_sha256),
            *(("evidence", digest) for digest in self.evidence_sha256),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Plugin mutation {label} SHA-256 is invalid")
        if self.surface not in {"config", "implementation", "prompt", "description"}:
            raise ValueError(f"Unsupported plugin mutation surface: {self.surface!r}")
        normalized_paths: list[str] = []
        for raw_path in self.changed_paths:
            path = str(raw_path).strip().replace("\\", "/")
            parts = path.split("/")
            if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError(
                    f"Plugin mutation path must be relative and contained: {raw_path!r}"
                )
            normalized_paths.append(path)
        if len(normalized_paths) != len(set(normalized_paths)):
            raise ValueError("Plugin mutation changed_paths must be unique")
        object.__setattr__(self, "changed_paths", tuple(sorted(normalized_paths)))
        if self.surface == "config" and not self._config_patch:
            raise ValueError("A config mutation requires a non-empty config patch")
        if self.surface != "config" and self._config_patch:
            raise ValueError("Only config mutations may carry a config patch")
        if self.surface != "config" and not self.changed_paths:
            raise ValueError("A file-backed plugin mutation requires changed_paths")

    @classmethod
    def create(
        cls,
        *,
        target_plugin: str,
        base_version: str,
        base_manifest_sha256: str,
        surface: Literal["config", "implementation", "prompt", "description"],
        candidate_artifact_sha256: str,
        changed_paths: Sequence[str] = (),
        evidence_sha256: Sequence[str] = (),
        config_patch: Mapping[str, Any] | None = None,
    ) -> PluginMutation:
        return cls(
            target_plugin,
            base_version,
            base_manifest_sha256,
            surface,
            candidate_artifact_sha256,
            tuple(changed_paths),
            tuple(evidence_sha256),
            tuple(
                sorted(
                    (str(key), _scalar(value, label=f"config patch {key}"))
                    for key, value in (config_patch or {}).items()
                )
            ),
        )

    @property
    def config_patch(self) -> Mapping[str, Scalar]:
        return MappingProxyType(dict(self._config_patch))

    def validate(self, registry: PluginRegistry) -> PluginContract:
        contract = registry.get(self.target_plugin)
        if contract.version != self.base_version:
            raise HarnessError("Plugin mutation targets a different base version")
        if contract.manifest_sha256 != self.base_manifest_sha256:
            raise HarnessError("Plugin mutation targets a different contract manifest")
        if self.surface not in contract.evolvable_surfaces:
            raise HarnessError(
                f"Plugin {contract.name!r} does not allow evolution of {self.surface!r}"
            )
        if self.surface == "config":
            contract.configure(self.config_patch)
        return contract

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "plugevolve-plugin-mutation-v1",
            "target_plugin": self.target_plugin,
            "base_version": self.base_version,
            "base_manifest_sha256": self.base_manifest_sha256,
            "surface": self.surface,
            "candidate_artifact_sha256": self.candidate_artifact_sha256,
            "changed_paths": list(self.changed_paths),
            "evidence_sha256": list(self.evidence_sha256),
            "config_patch": dict(self._config_patch),
        }


def enumerate_single_plugin_candidates(
    registry: PluginRegistry,
    base: CompositionSpec,
    *,
    config_variants: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> tuple[CompositionCandidate, ...]:
    """Enumerate code-controlled one-slot mutations; invalid compositions are omitted."""

    baseline = registry.resolve(base)
    active = set(base.plugins)
    candidates: list[CompositionCandidate] = []
    seen = {baseline.sha256}

    def add(
        operation: Literal["enable", "disable", "replace", "configure"],
        target: str,
        spec: CompositionSpec,
        *,
        replaced: str | None = None,
    ) -> None:
        try:
            resolved = registry.resolve(spec)
            execution_plan(resolved)
        except (HarnessError, ValueError):
            return
        if resolved.sha256 in seen:
            return
        seen.add(resolved.sha256)
        candidates.append(CompositionCandidate(operation, target, resolved, replaced))

    base_overrides = {name: dict(values) for name, values in base.overrides.items()}
    for plugin_name in base.plugins:
        plugins = tuple(name for name in base.plugins if name != plugin_name)
        overrides = {name: values for name, values in base_overrides.items() if name != plugin_name}
        if plugins:
            add(
                "disable",
                plugin_name,
                CompositionSpec.create(f"{base.name}.disable-{plugin_name}", plugins, overrides),
            )

    contracts = registry.contracts()
    for plugin in contracts:
        if plugin.name not in active:
            add(
                "enable",
                plugin.name,
                CompositionSpec.create(
                    f"{base.name}.enable-{plugin.name}",
                    (*base.plugins, plugin.name),
                    base_overrides,
                ),
            )

    for current_name in base.plugins:
        current = registry.get(current_name)
        for replacement in contracts:
            if replacement.name in active or replacement.kind != current.kind:
                continue
            if replacement.provides != current.provides:
                continue
            plugins = tuple(
                replacement.name if name == current_name else name for name in base.plugins
            )
            overrides = {
                name: values for name, values in base_overrides.items() if name != current_name
            }
            add(
                "replace",
                next(iter(sorted(current.provides))),
                CompositionSpec.create(
                    f"{base.name}.replace-{current_name}-with-{replacement.name}",
                    plugins,
                    overrides,
                ),
                replaced=current_name,
            )

    for plugin_name, variants in sorted((config_variants or {}).items()):
        if plugin_name not in active:
            raise ValueError(f"Config variants target inactive plugin {plugin_name!r}")
        for index, variant in enumerate(variants, start=1):
            overrides = {name: dict(values) for name, values in base_overrides.items()}
            overrides[plugin_name] = {**overrides.get(plugin_name, {}), **dict(variant)}
            add(
                "configure",
                plugin_name,
                CompositionSpec.create(
                    f"{base.name}.configure-{plugin_name}-{index}",
                    base.plugins,
                    overrides,
                ),
            )

    return tuple(
        sorted(candidates, key=lambda item: (item.operation, item.target, item.composition.sha256))
    )


@dataclass(frozen=True)
class CompositionEvaluation:
    composition: ResolvedComposition
    context_scores: tuple[tuple[str, float], ...]
    failed_contexts: tuple[str, ...] = ()
    cost: float = 0.0

    @classmethod
    def create(
        cls,
        composition: ResolvedComposition,
        context_scores: Mapping[str, float],
        *,
        failed_contexts: Sequence[str] = (),
        cost: float = 0.0,
    ) -> CompositionEvaluation:
        normalized: list[tuple[str, float]] = []
        for context, score in sorted(context_scores.items()):
            value = float(score)
            if not context or not math.isfinite(value):
                raise ValueError("Context scores require non-empty names and finite values")
            normalized.append((str(context), value))
        if not normalized or not math.isfinite(cost) or cost < 0:
            raise ValueError("An evaluation needs scores and a finite non-negative cost")
        failures = tuple(sorted(set(str(item) for item in failed_contexts)))
        return cls(composition, tuple(normalized), failures, float(cost))

    @property
    def scores(self) -> Mapping[str, float]:
        return MappingProxyType(dict(self.context_scores))

    @property
    def mean_score(self) -> float:
        return fmean(self.scores.values())

    @property
    def score_variance(self) -> float:
        return pvariance(self.scores.values()) if len(self.context_scores) > 1 else 0.0


def select_composition(
    evaluations: Sequence[CompositionEvaluation],
    *,
    baseline: CompositionEvaluation,
    min_mean_delta: float = 0.0,
    max_context_regression: float = 0.0,
) -> CompositionEvaluation | None:
    """Select a non-regressive candidate by mean, context variance, cost, then hash."""

    if (
        not math.isfinite(min_mean_delta)
        or not math.isfinite(max_context_regression)
        or min_mean_delta < 0
        or max_context_regression < 0
    ):
        raise ValueError("Selection tolerances must be non-negative")
    if baseline.failed_contexts:
        raise ValueError("Baseline evaluation must not contain failed contexts")
    baseline_scores = baseline.scores
    eligible: list[CompositionEvaluation] = []
    for evaluation in evaluations:
        if evaluation.failed_contexts or set(evaluation.scores) != set(baseline_scores):
            continue
        if evaluation.mean_score <= baseline.mean_score + min_mean_delta:
            continue
        if any(
            evaluation.scores[name] < score - max_context_regression
            for name, score in baseline_scores.items()
        ):
            continue
        eligible.append(evaluation)
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            -item.mean_score,
            item.score_variance,
            item.cost,
            item.composition.sha256,
        ),
    )


def contextual_marginal_contribution(
    with_plugin: CompositionEvaluation,
    without_plugin: CompositionEvaluation,
) -> dict[str, Any]:
    if set(with_plugin.scores) != set(without_plugin.scores):
        raise ValueError("Marginal contribution requires identical evaluation contexts")
    deltas = {
        name: with_plugin.scores[name] - without_plugin.scores[name] for name in with_plugin.scores
    }
    values = list(deltas.values())
    return {
        "contexts": deltas,
        "mean": fmean(values),
        "minimum": min(values),
        "variance": pvariance(values) if len(values) > 1 else 0.0,
        "positive_context_rate": sum(value > 0 for value in values) / len(values),
    }


def _field(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> ConfigField:
    return ConfigField(name, "integer", default, minimum=minimum, maximum=maximum)


def _strategy(
    evidence: Sequence[str],
    operators: Sequence[str],
    validation_contexts: Sequence[str],
) -> EvolutionStrategy:
    return EvolutionStrategy(tuple(evidence), tuple(operators), tuple(validation_contexts))


def default_plugin_registry() -> PluginRegistry:
    compact_profile_fields = (
        _field("max-sheets", 8, 1, 12),
        _field("max-cells-per-sheet", 192, 32, 512),
        _field("max-regions-per-sheet", 3, 1, 8),
        _field("max-sample-rows-per-region", 1, 1, 8),
        _field("max-number-formats-per-region", 3, 1, 12),
        _field("max-formula-clusters-per-sheet", 2, 1, 12),
        _field("max-rendered-chars", 4000, 500, 12000),
    )
    return PluginRegistry(
        (
            PluginContract(
                "runtime-code-interpreter",
                "1.0.0",
                "act",
                "runtime.code-interpreter",
                frozenset({"action.spreadsheet"}),
                frozenset({"agent.execute", "workbook.read", "workbook.write"}),
                frozenset({"tool_registry"}),
                frozenset({"workbook.read", "workbook.write", "subprocess.execute"}),
                evolvable_surfaces=frozenset({"description", "implementation"}),
                spreadsheet_capabilities=frozenset(SPREADSHEET_CAPABILITIES),
                evolution_strategy=_strategy(
                    ("tool-errors", "execution-trace", "workbook-diff"),
                    ("tool-description", "helper-implementation"),
                    ("target-capability", "co-activated-capabilities", "workbook-regression"),
                ),
            ),
            PluginContract(
                "runtime-native-tools",
                "1.0.0",
                "act",
                "runtime.native-tools",
                frozenset({"action.spreadsheet", "tool.recalculate-and-read"}),
                frozenset({"agent.execute", "workbook.read", "workbook.write"}),
                frozenset({"tool_registry"}),
                frozenset({"workbook.read", "workbook.write", "subprocess.execute"}),
                evolvable_surfaces=frozenset({"description", "implementation"}),
                spreadsheet_capabilities=frozenset(SPREADSHEET_CAPABILITIES),
                evolution_strategy=_strategy(
                    ("tool-errors", "execution-trace", "workbook-diff"),
                    ("tool-description", "helper-implementation"),
                    ("target-capability", "co-activated-capabilities", "workbook-regression"),
                ),
            ),
            PluginContract(
                "runtime-code-plus-formula-validation",
                "1.0.0",
                "act",
                "runtime.code-plus-formula-validation",
                frozenset({"action.spreadsheet", "tool.recalculate-and-read"}),
                frozenset({"agent.execute", "workbook.read", "workbook.write"}),
                frozenset({"tool_registry"}),
                frozenset({"workbook.read", "workbook.write", "subprocess.execute"}),
                evolvable_surfaces=frozenset({"description", "implementation"}),
                spreadsheet_capabilities=frozenset({"formula", "verification"}),
                evolution_strategy=_strategy(
                    ("tool-errors", "formula-validation-trace", "workbook-diff"),
                    ("tool-description", "helper-implementation"),
                    ("verification-formula", "co-activated-capabilities", "workbook-regression"),
                ),
            ),
            PluginContract(
                "profile-deterministic-full",
                "1.1.0",
                "observe",
                "profile.deterministic-full",
                frozenset({"context.workbook-profile"}),
                frozenset({"workbook.read"}),
                frozenset({"before_model_request"}),
                frozenset({"workbook.read"}),
                evolvable_surfaces=frozenset({"implementation"}),
                spreadsheet_capabilities=frozenset({"structure"}),
                evolution_strategy=_strategy(
                    ("header-boundaries", "cross-sheet-relations", "profile-truncation"),
                    ("relation-rule", "profile-implementation"),
                    ("structure-formula", "structure-manipulation", "workbook-regression"),
                ),
            ),
            PluginContract(
                "profile-deterministic-compact",
                "1.1.0",
                "observe",
                "profile.deterministic-compact",
                frozenset({"context.workbook-profile"}),
                frozenset({"workbook.read"}),
                frozenset({"before_model_request"}),
                frozenset({"workbook.read"}),
                compact_profile_fields,
                frozenset({"config", "implementation"}),
                spreadsheet_capabilities=frozenset({"structure"}),
                evolution_strategy=_strategy(
                    ("header-boundaries", "cross-sheet-relations", "profile-truncation"),
                    ("bounded-config", "relation-rule", "profile-implementation"),
                    ("structure-formula", "structure-manipulation", "workbook-regression"),
                ),
            ),
            PluginContract(
                "policy-bare",
                "1.0.0",
                "control",
                "policy.bare",
                frozenset({"policy.solve"}),
                frozenset({"action.spreadsheet", "model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"composition"}),
                evolution_strategy=_strategy(
                    ("routing-trace", "unused-capability", "tool-sequence"),
                    ("policy-prompt",),
                    ("workflow-replay", "cross-capability", "workbook-regression"),
                ),
            ),
            PluginContract(
                "policy-profile",
                "1.0.0",
                "control",
                "policy.profile",
                frozenset({"policy.solve"}),
                frozenset({"action.spreadsheet", "context.workbook-profile", "model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"composition"}),
                evolution_strategy=_strategy(
                    ("routing-trace", "profile-usage", "tool-sequence"),
                    ("policy-prompt",),
                    ("workflow-replay", "cross-capability", "workbook-regression"),
                ),
            ),
            PluginContract(
                "policy-native",
                "1.0.0",
                "control",
                "policy.native",
                frozenset({"policy.solve"}),
                frozenset({"action.spreadsheet", "model.request"}),
                frozenset({"before_model_request", "after_tool"}),
                evolvable_surfaces=frozenset({"prompt", "implementation"}),
                spreadsheet_capabilities=frozenset({"composition"}),
                evolution_strategy=_strategy(
                    ("routing-trace", "unused-capability", "tool-sequence"),
                    ("policy-prompt", "routing-middleware"),
                    ("workflow-replay", "cross-capability", "workbook-regression"),
                ),
            ),
            PluginContract(
                "policy-ours",
                "1.28.0",
                "control",
                "policy.ours",
                frozenset({"policy.solve"}),
                frozenset({"action.spreadsheet", "context.workbook-profile", "model.request"}),
                frozenset({"before_model_request", "after_tool"}),
                evolvable_surfaces=frozenset({"prompt", "implementation"}),
                spreadsheet_capabilities=frozenset({"composition"}),
                evolution_strategy=_strategy(
                    ("routing-trace", "unused-capability", "tool-sequence"),
                    ("policy-prompt", "routing-middleware"),
                    ("workflow-replay", "cross-capability", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-core",
                "1.0.0",
                "knowledge",
                "knowledge.spreadsheet-core",
                frozenset({"knowledge.spreadsheet-skill"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset(SPREADSHEET_CAPABILITIES),
                evolution_strategy=_strategy(
                    ("redacted-trajectories", "evaluator-outcomes", "repeated-failures"),
                    ("skill-rule", "skill-template"),
                    ("failure-replay", "neighbor-transfer", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-structure",
                "1.0.0",
                "knowledge",
                "knowledge.spreadsheet-structure",
                frozenset({"knowledge.spreadsheet-structure"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"structure"}),
                evolution_strategy=_strategy(
                    ("header-boundaries", "cross-sheet-relations", "range-grounding"),
                    ("semantic-edge-rule", "structure-skill-rule"),
                    ("structure-formula", "structure-manipulation", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-formula",
                "1.1.0",
                "knowledge",
                "knowledge.spreadsheet-formula",
                frozenset({"knowledge.spreadsheet-formula"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"formula"}),
                evolution_strategy=_strategy(
                    ("failed-formula", "expected-value", "dependency-graph", "reference-ast"),
                    ("formula-template", "reference-rewrite", "formula-skill-rule"),
                    ("formula-structure", "formula-manipulation", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-financial-model",
                "1.3.0",
                "knowledge",
                "knowledge.spreadsheet-financial-model",
                frozenset({"knowledge.spreadsheet-financial-model"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"formula", "verification"}),
                evolution_strategy=_strategy(
                    (
                        "historical-forecast-boundary",
                        "dependency-graph",
                        "model-check",
                        "workbook-diff",
                    ),
                    ("financial-model-rule", "forecast-fill-rule"),
                    ("financial-formula", "financial-boundary", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-manipulation",
                "1.1.0",
                "knowledge",
                "knowledge.spreadsheet-manipulation",
                frozenset({"knowledge.spreadsheet-manipulation"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"manipulation"}),
                evolution_strategy=_strategy(
                    ("workbook-diff", "boundary-evidence", "format-metadata"),
                    ("mutation-procedure", "boundary-rule", "format-rule"),
                    ("manipulation-structure", "manipulation-formula", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-analysis",
                "1.0.0",
                "knowledge",
                "knowledge.spreadsheet-analysis",
                frozenset({"knowledge.spreadsheet-analysis"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"analysis"}),
                evolution_strategy=_strategy(
                    ("expected-aggregation", "grouping-keys", "source-output-pairs"),
                    ("analysis-template", "aggregation-rule"),
                    ("analysis-structure", "analysis-visualization", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-visualization",
                "1.0.0",
                "knowledge",
                "knowledge.spreadsheet-visualization",
                frozenset({"knowledge.spreadsheet-visualization"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"visualization"}),
                evolution_strategy=_strategy(
                    ("rendered-pages", "chart-metadata", "visual-diff"),
                    ("chart-selection-rule", "chart-range-rule", "visual-skill-rule"),
                    ("visualization-analysis", "visualization-structure", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-verification",
                "1.1.0",
                "knowledge",
                "knowledge.spreadsheet-verification",
                frozenset({"knowledge.spreadsheet-verification"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"verification"}),
                evolution_strategy=_strategy(
                    ("false-positive", "false-negative", "execution-postconditions"),
                    ("postcondition", "verification-skill-rule"),
                    ("verification-formula", "verification-structure", "workbook-regression"),
                ),
            ),
            PluginContract(
                "skill-spreadsheet-memory",
                "1.0.0",
                "knowledge",
                "knowledge.spreadsheet-memory",
                frozenset({"knowledge.spreadsheet-memory"}),
                frozenset({"model.request"}),
                frozenset({"before_model_request"}),
                evolvable_surfaces=frozenset({"prompt"}),
                spreadsheet_capabilities=frozenset({"memory"}),
                evolution_strategy=_strategy(
                    ("repeated-successes", "backend-workarounds", "transfer-evidence"),
                    ("experience-rule", "template-merge", "redundancy-prune"),
                    ("failure-replay", "neighbor-transfer", "workbook-regression"),
                ),
            ),
            PluginContract(
                "verifier-formula-runtime",
                "1.0.0",
                "verify",
                "verification.formula-runtime",
                frozenset({"verification.formula-runtime"}),
                frozenset(
                    {
                        "artifact.validate",
                        "tool.recalculate-and-read",
                        "workbook.read",
                    }
                ),
                frozenset({"before_submit"}),
                frozenset({"workbook.read", "subprocess.execute"}),
                evolvable_surfaces=frozenset({"config", "implementation"}),
                spreadsheet_capabilities=frozenset({"formula", "verification"}),
                evolution_strategy=_strategy(
                    ("failed-formula", "cached-values", "dependency-graph", "false-negative"),
                    ("verifier-postcondition", "bounded-config", "verifier-implementation"),
                    ("verification-formula", "verification-structure", "workbook-regression"),
                ),
            ),
            PluginContract(
                "repair-date-text",
                "1.0.0",
                "repair",
                "repair.date-text",
                frozenset({"repair.date-text"}),
                frozenset({"artifact.validate", "workbook.read", "workbook.write"}),
                frozenset({"after_run"}),
                frozenset({"workbook.read", "workbook.write"}),
                evolvable_surfaces=frozenset({"implementation"}),
                spreadsheet_capabilities=frozenset({"formula", "manipulation"}),
                evolution_strategy=_strategy(
                    ("date-types", "number-formats", "backend-behavior"),
                    ("repair-rule", "repair-implementation"),
                    ("formula-manipulation", "format-regression", "workbook-regression"),
                ),
            ),
            PluginContract(
                "workflow-paper",
                "1.0.0",
                "workflow",
                "workflow.paper",
                frozenset({"workflow.paper"}),
                frozenset({"agent.execute", "model.request"}),
                frozenset({"before_task", "before_model_request", "after_run"}),
                spreadsheet_capabilities=frozenset(SPREADSHEET_CAPABILITIES),
            ),
        )
    )


# Frozen paper-facing ablation pair.  The two compositions differ by exactly
# one domain-knowledge provider, so any paired Financial_Model delta can be
# attributed to the business plugin rather than to a runtime or policy change.
SPREADSHEET_HARNESS_BASIC_COMPOSITION = CompositionSpec.create(
    "spreadsheet-harness-basic",
    (
        "runtime-code-plus-formula-validation",
        "profile-deterministic-compact",
        "policy-ours",
        "skill-spreadsheet-structure",
        "skill-spreadsheet-formula",
        "skill-spreadsheet-manipulation",
        "skill-spreadsheet-analysis",
        "skill-spreadsheet-visualization",
        "skill-spreadsheet-verification",
        "skill-spreadsheet-memory",
        "verifier-formula-runtime",
        "repair-date-text",
    ),
)

SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION = CompositionSpec.create(
    "spreadsheet-harness-financial",
    (
        *SPREADSHEET_HARNESS_BASIC_COMPOSITION.plugins,
        "skill-spreadsheet-financial-model",
    ),
)


ARM_COMPOSITIONS: Mapping[str, CompositionSpec] = MappingProxyType(
    {
        "bare": CompositionSpec.create("bare", ("runtime-code-interpreter", "policy-bare")),
        "profile": CompositionSpec.create(
            "profile",
            ("runtime-code-interpreter", "profile-deterministic-full", "policy-profile"),
        ),
        "native": CompositionSpec.create("native", ("runtime-native-tools", "policy-native")),
        "paper": CompositionSpec.create("paper", ("workflow-paper",)),
        # Clean-room Spreadsheet-RL ablations.  These names are intentionally
        # explicit so benchmark reports do not conflate a tool-interface proxy
        # with the paper's RL-trained checkpoint.
        "spreadsheet-rl-minimal": CompositionSpec.create(
            "spreadsheet-rl-minimal",
            ("runtime-code-plus-formula-validation", "policy-bare"),
        ),
        "spreadsheet-rl-native": CompositionSpec.create(
            "spreadsheet-rl-native",
            ("runtime-native-tools", "policy-native"),
        ),
        "paper-vision": CompositionSpec.create("paper-vision", ("workflow-paper",)),
        "spreadsheet-harness-basic": SPREADSHEET_HARNESS_BASIC_COMPOSITION,
        "spreadsheet-harness-financial": SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION,
        "ours": CompositionSpec.create(
            "ours",
            (
                "runtime-code-interpreter",
                "profile-deterministic-compact",
                "policy-ours",
                "repair-date-text",
            ),
        ),
    }
)

PLUGEOLVE_SEED_COMPOSITION = CompositionSpec.create(
    "plugevolve-seed",
    (
        "runtime-code-plus-formula-validation",
        "profile-deterministic-compact",
        "policy-ours",
        "skill-spreadsheet-structure",
        "skill-spreadsheet-formula",
        "skill-spreadsheet-financial-model",
        "skill-spreadsheet-manipulation",
        "skill-spreadsheet-analysis",
        "skill-spreadsheet-visualization",
        "skill-spreadsheet-verification",
        "skill-spreadsheet-memory",
        "verifier-formula-runtime",
        "repair-date-text",
    ),
)

BUILTIN_COMPOSITIONS: Mapping[str, CompositionSpec] = MappingProxyType(
    {
        **ARM_COMPOSITIONS,
        PLUGEOLVE_SEED_COMPOSITION.name: PLUGEOLVE_SEED_COMPOSITION,
        SPREADSHEET_HARNESS_BASIC_COMPOSITION.name: SPREADSHEET_HARNESS_BASIC_COMPOSITION,
        SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION.name: (
            SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION
        ),
    }
)


def resolve_arm_composition(
    arm: str,
    *,
    registry: PluginRegistry | None = None,
    composition: CompositionSpec | None = None,
) -> ResolvedComposition:
    resolved_registry = registry or default_plugin_registry()
    try:
        spec = composition or ARM_COMPOSITIONS[arm]
    except KeyError as exc:
        raise ValueError(f"Unknown comparison arm: {arm!r}") from exc
    resolved = resolved_registry.resolve(spec)
    execution_plan(resolved)
    return resolved


__all__ = [
    "ALLOWED_PLUGIN_HOOKS",
    "ARM_COMPOSITIONS",
    "BUILTIN_COMPOSITIONS",
    "KERNEL_CAPABILITIES",
    "PLUGEOLVE_SEED_COMPOSITION",
    "SPREADSHEET_HARNESS_BASIC_COMPOSITION",
    "SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION",
    "SPREADSHEET_CAPABILITIES",
    "CompositionCandidate",
    "CompositionEvaluation",
    "CompositionRoute",
    "CompositionSpec",
    "ConfigField",
    "ConstrainedCompositionRouter",
    "EvolutionStrategy",
    "PluginContract",
    "PluginExecutionPlan",
    "PluginInstance",
    "PluginMutation",
    "PluginRegistry",
    "ResolvedComposition",
    "SpreadsheetCapability",
    "contextual_marginal_contribution",
    "default_plugin_registry",
    "enumerate_single_plugin_candidates",
    "execution_plan",
    "resolve_arm_composition",
    "select_composition",
]
