"""Configuration discovery without copying secrets into the project."""

from __future__ import annotations

import hmac
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import HarnessError

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
REASONING_ALIASES = {"ultra": "max"}
_MAX_API_KEY_FILE_BYTES = 16 * 1024
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _optional_environment_number(name: str, converter: type[float] | type[int]) -> Any:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return converter(raw)
    except ValueError as exc:
        kind = "integer" if converter is int else "number"
        raise HarnessError(f"{name} must be a valid {kind}") from exc


def _optional_environment_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise HarnessError(f"{name} must be one of true/false, yes/no, on/off, or 1/0")


def _validate_finite_number(
    name: str,
    value: float | None,
    *,
    minimum: float,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HarnessError(f"Generation {name} must be a finite number")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise HarnessError(f"Generation {name} must be a finite number")
    below_minimum = resolved < minimum if minimum_inclusive else resolved <= minimum
    if below_minimum or (maximum is not None and resolved > maximum):
        left = "[" if minimum_inclusive else "("
        right = f", {maximum}]" if maximum is not None else ", infinity)"
        raise HarnessError(f"Generation {name} must be in {left}{minimum}{right}")
    return resolved


def _read_api_key_file(value: str | Path) -> str:
    """Read one API key from an owner-only regular file without logging content."""

    path = Path(value).expanduser()
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise HarnessError(f"Unable to open API key file: {path}") from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise HarnessError(f"API key file must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(f"Unable to open API key file: {path}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessError(f"API key file must be a regular file: {path}")
        if os.name == "posix" and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise HarnessError(
                f"API key file must not grant group or other permissions: {path}"
            )
        if metadata.st_size > _MAX_API_KEY_FILE_BYTES:
            raise HarnessError(f"API key file is too large: {path}")

        chunks: list[bytes] = []
        remaining = _MAX_API_KEY_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > _MAX_API_KEY_FILE_BYTES:
            raise HarnessError(f"API key file is too large: {path}")
        try:
            raw = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError(f"API key file must be valid UTF-8 text: {path}") from exc
    except OSError as exc:
        raise HarnessError(f"Unable to read API key file: {path}") from exc
    finally:
        os.close(descriptor)

    if raw.endswith("\r\n"):
        key = raw[:-2]
    elif raw.endswith("\n"):
        key = raw[:-1]
    else:
        key = raw
    if not key or not key.isprintable() or any(character.isspace() for character in key):
        raise HarnessError(f"API key file must contain exactly one non-empty key: {path}")
    return key


def _codex_auth_api_key(codex_home: Path) -> str | None:
    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        return None
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = auth.get("OPENAI_API_KEY")
    return str(value) if value else None


def _reject_key_file_credential_collision(key: str, codex_home: Path) -> None:
    """Keep an experiment key separate from ambient and interactive credentials."""

    candidates = (
        os.environ.get("OPENAI_API_KEY"),
        _codex_auth_api_key(codex_home),
    )
    if any(
        candidate is not None and hmac.compare_digest(key, candidate)
        for candidate in candidates
    ):
        raise HarnessError(
            "API key file credential must differ from OPENAI_API_KEY and Codex auth"
        )


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str = "high"
    timeout_seconds: float = 180.0
    max_retries: int = 1
    requested_reasoning_effort: str | None = None
    store_responses: bool = False
    request_interval_seconds: float = 0.0
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repetition_penalty: float | None = None
    enable_thinking: bool | None = None

    def __post_init__(self) -> None:
        requested = self.requested_reasoning_effort or self.reasoning_effort
        resolved = REASONING_ALIASES.get(self.reasoning_effort, self.reasoning_effort)
        if resolved not in REASONING_EFFORTS:
            allowed = ", ".join((*REASONING_EFFORTS, *REASONING_ALIASES))
            raise HarnessError(f"Unsupported reasoning effort {requested!r}; use {allowed}")
        if self.timeout_seconds <= 0:
            raise HarnessError("Provider timeout must be positive")
        if self.max_retries < 0 or self.max_retries > 5:
            raise HarnessError("Provider retries must be between 0 and 5")
        if (
            isinstance(self.request_interval_seconds, bool)
            or not isinstance(self.request_interval_seconds, int | float)
            or not math.isfinite(float(self.request_interval_seconds))
            or self.request_interval_seconds < 0
        ):
            raise HarnessError("Request interval must be a non-negative finite number")
        object.__setattr__(self, "reasoning_effort", resolved)
        object.__setattr__(self, "requested_reasoning_effort", requested)
        object.__setattr__(
            self, "request_interval_seconds", float(self.request_interval_seconds)
        )
        object.__setattr__(
            self,
            "temperature",
            _validate_finite_number(
                "temperature", self.temperature, minimum=0.0, maximum=2.0
            ),
        )
        object.__setattr__(
            self,
            "top_p",
            _validate_finite_number(
                "top_p", self.top_p, minimum=0.0, maximum=1.0, minimum_inclusive=False
            ),
        )
        object.__setattr__(
            self,
            "presence_penalty",
            _validate_finite_number(
                "presence_penalty", self.presence_penalty, minimum=-2.0, maximum=2.0
            ),
        )
        object.__setattr__(
            self,
            "min_p",
            _validate_finite_number("min_p", self.min_p, minimum=0.0, maximum=1.0),
        )
        object.__setattr__(
            self,
            "repetition_penalty",
            _validate_finite_number(
                "repetition_penalty",
                self.repetition_penalty,
                minimum=0.0,
                minimum_inclusive=False,
            ),
        )
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not _INT64_MIN <= self.seed <= _INT64_MAX
        ):
            raise HarnessError("Generation seed must be a signed 64-bit integer")
        if self.top_k is not None and (
            isinstance(self.top_k, bool)
            or not isinstance(self.top_k, int)
            or self.top_k < -1
        ):
            raise HarnessError("Generation top_k must be -1, 0, or a positive integer")
        if self.enable_thinking is not None and not isinstance(self.enable_thinking, bool):
            raise HarnessError("Generation enable_thinking must be a boolean")

    def generation_dict(self) -> dict[str, Any]:
        """Return only explicitly configured generation controls."""

        return {
            name: value
            for name, value in (
                ("temperature", self.temperature),
                ("top_p", self.top_p),
                ("seed", self.seed),
                ("presence_penalty", self.presence_penalty),
                ("top_k", self.top_k),
                ("min_p", self.min_p),
                ("repetition_penalty", self.repetition_penalty),
                ("enable_thinking", self.enable_thinking),
            )
            if value is not None
        }

    def apply_generation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Copy a request payload and add configured standard/vLLM controls."""

        result = dict(payload)
        generation = self.generation_dict()
        for name in ("temperature", "top_p", "presence_penalty"):
            if name not in generation:
                continue
            if name in result and result[name] != generation[name]:
                raise HarnessError(f"Generation field {name!r} conflicts with request payload")
            result[name] = generation[name]

        configured_extra: dict[str, Any] = {
            name: generation[name]
            for name in ("seed", "top_k", "min_p", "repetition_penalty")
            if name in generation
        }
        if "enable_thinking" in generation:
            configured_extra["chat_template_kwargs"] = {
                "enable_thinking": generation["enable_thinking"]
            }
        if configured_extra:
            existing_extra = result.get("extra_body", {})
            if not isinstance(existing_extra, dict):
                raise HarnessError("Responses extra_body must be a JSON object")
            merged_extra = dict(existing_extra)
            for name, value in configured_extra.items():
                if name == "chat_template_kwargs" and name in merged_extra:
                    existing_template_kwargs = merged_extra[name]
                    if not isinstance(existing_template_kwargs, dict):
                        raise HarnessError(
                            "Responses chat_template_kwargs must be a JSON object"
                        )
                    merged_template_kwargs = dict(existing_template_kwargs)
                    for template_name, template_value in value.items():
                        if (
                            template_name in merged_template_kwargs
                            and merged_template_kwargs[template_name] != template_value
                        ):
                            raise HarnessError(
                                "Generation extension field "
                                f"chat_template_kwargs.{template_name!s} conflicts with "
                                "request payload"
                            )
                        merged_template_kwargs[template_name] = template_value
                    merged_extra[name] = merged_template_kwargs
                    continue
                if name in merged_extra and merged_extra[name] != value:
                    raise HarnessError(
                        f"Generation extension field {name!r} conflicts with request payload"
                    )
                merged_extra[name] = value
            result["extra_body"] = merged_extra
        return result

    @classmethod
    def discover(
        cls,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_file: str | Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        request_interval_seconds: float | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        repetition_penalty: float | None = None,
        enable_thinking: bool | None = None,
    ) -> ProviderConfig:
        """Read explicit values, environment, then the user's Codex config.

        API key precedence is an explicit key, an explicit or environment key
        file, ``OPENAI_API_KEY``, then the Codex authentication file.

        Secret values remain in memory and are never written into a run directory.
        """

        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        config_data: dict[str, Any] = {}
        config_path = codex_home / "config.toml"
        if config_path.is_file():
            with config_path.open("rb") as handle:
                config_data = tomllib.load(handle)

        provider_name = str(config_data.get("model_provider", ""))
        providers = config_data.get("model_providers", {})
        provider_data = providers.get(provider_name, {}) if isinstance(providers, dict) else {}

        if api_key is not None:
            resolved_key = api_key
        else:
            environment_key_file = os.environ.get("SHEET_AGENT_API_KEY_FILE") or None
            resolved_key_file = (
                api_key_file if api_key_file is not None else environment_key_file
            )
            resolved_key = (
                _read_api_key_file(resolved_key_file)
                if resolved_key_file is not None
                else os.environ.get("OPENAI_API_KEY")
            )
            if resolved_key_file is not None:
                _reject_key_file_credential_collision(resolved_key, codex_home)
            if not resolved_key and resolved_key_file is None:
                resolved_key = _codex_auth_api_key(codex_home)

        resolved_url = (
            base_url or os.environ.get("SHEET_AGENT_BASE_URL") or provider_data.get("base_url")
        )
        resolved_model = model or os.environ.get("SHEET_AGENT_MODEL") or config_data.get("model")
        requested_effort = str(
            reasoning_effort
            or os.environ.get("SHEET_AGENT_REASONING_EFFORT")
            or config_data.get("model_reasoning_effort")
            or "high"
        )
        resolved_effort = REASONING_ALIASES.get(requested_effort, requested_effort)
        resolved_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.environ.get("SHEET_AGENT_TIMEOUT", "180"))
        )
        resolved_retries = (
            max_retries
            if max_retries is not None
            else int(os.environ.get("SHEET_AGENT_RETRIES", "1"))
        )
        resolved_request_interval = (
            request_interval_seconds
            if request_interval_seconds is not None
            else float(os.environ.get("SHEET_AGENT_REQUEST_INTERVAL", "0"))
        )
        resolved_generation = {
            "temperature": (
                temperature
                if temperature is not None
                else _optional_environment_number("SHEET_AGENT_TEMPERATURE", float)
            ),
            "top_p": (
                top_p
                if top_p is not None
                else _optional_environment_number("SHEET_AGENT_TOP_P", float)
            ),
            "seed": (
                seed
                if seed is not None
                else _optional_environment_number("SHEET_AGENT_SEED", int)
            ),
            "presence_penalty": (
                presence_penalty
                if presence_penalty is not None
                else _optional_environment_number("SHEET_AGENT_PRESENCE_PENALTY", float)
            ),
            "top_k": (
                top_k
                if top_k is not None
                else _optional_environment_number("SHEET_AGENT_TOP_K", int)
            ),
            "min_p": (
                min_p
                if min_p is not None
                else _optional_environment_number("SHEET_AGENT_MIN_P", float)
            ),
            "repetition_penalty": (
                repetition_penalty
                if repetition_penalty is not None
                else _optional_environment_number("SHEET_AGENT_REPETITION_PENALTY", float)
            ),
            "enable_thinking": (
                enable_thinking
                if enable_thinking is not None
                else _optional_environment_bool("SHEET_AGENT_ENABLE_THINKING")
            ),
        }
        raw_store = os.environ.get("SHEET_AGENT_STORE_RESPONSES")
        resolved_store = (
            raw_store.strip().lower() in {"1", "true", "yes", "on"}
            if raw_store is not None
            else not bool(config_data.get("disable_response_storage", True))
        )
        if resolved_timeout <= 0:
            raise HarnessError("Provider timeout must be positive")
        if resolved_retries < 0 or resolved_retries > 5:
            raise HarnessError("Provider retries must be between 0 and 5")
        if resolved_effort not in REASONING_EFFORTS:
            allowed = ", ".join((*REASONING_EFFORTS, *REASONING_ALIASES))
            raise HarnessError(f"Unsupported reasoning effort {requested_effort!r}; use {allowed}")
        missing = [
            name
            for name, value in (
                ("base URL", resolved_url),
                ("API key", resolved_key),
                ("model", resolved_model),
            )
            if not value
        ]
        if missing:
            raise HarnessError(
                "Missing provider " + ", ".join(missing) + ". Configure Codex or pass CLI flags."
            )
        return cls(
            base_url=str(resolved_url).rstrip("/"),
            api_key=str(resolved_key),
            model=str(resolved_model),
            reasoning_effort=str(resolved_effort),
            timeout_seconds=float(resolved_timeout),
            max_retries=resolved_retries,
            requested_reasoning_effort=requested_effort,
            store_responses=resolved_store,
            request_interval_seconds=resolved_request_interval,
            **resolved_generation,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "requested_reasoning_effort": (
                self.requested_reasoning_effort or self.reasoning_effort
            ),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "request_interval_seconds": self.request_interval_seconds,
            "store_responses": self.store_responses,
            "generation": self.generation_dict(),
            "api_key_configured": bool(self.api_key),
        }
