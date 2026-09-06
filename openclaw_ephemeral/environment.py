"""Environment parsing shared by the OpenClaw runtime modules."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
ENV_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
API_KEY_ALIAS = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")


class ConfigurationError(ValueError):
    """Raised when injected runtime configuration is invalid."""


def clean(value: str | None) -> str:
    """Return a stripped environment value."""

    return (value or "").strip()


def first_value(environ: Mapping[str, str], *names: str) -> str:
    """Return the first non-empty value for the supplied variable names."""

    for name in names:
        value = clean(environ.get(name))
        if value:
            return value
    return ""


def boolean(
    environ: Mapping[str, str],
    name: str,
    *,
    default: bool = False,
) -> bool:
    """Parse one strict boolean environment value."""

    raw = clean(environ.get(name)).lower()
    if not raw:
        return default
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def integer(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int = 2_147_483_647,
) -> int:
    """Parse one bounded integer environment value."""

    raw = clean(environ.get(name))
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def floating(
    environ: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float = 0.1,
    maximum: float = 300.0,
) -> float:
    """Parse one bounded floating-point environment value."""

    raw = clean(environ.get(name))
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum:g} and {maximum:g}"
        )
    return value


def secret_ref(name: str) -> dict[str, str]:
    """Return an OpenClaw environment SecretRef without resolving it."""

    if not ENV_SECRET_NAME.fullmatch(name):
        raise ConfigurationError(f"Invalid secret environment variable name: {name}")
    return {"source": "env", "provider": "default", "id": name}


def _expand_injected_path(raw: str, environ: Mapping[str, str]) -> Path:
    if raw == "~" or raw.startswith("~/"):
        home = clean(environ.get("HOME"))
        if not home:
            raise ConfigurationError("HOME is required to expand an injected path")
        raw = home + raw[1:]
    return Path(raw).resolve()


def config_path(environ: Mapping[str, str]) -> Path:
    """Resolve the active config path from the established OpenClaw variables."""

    raw = first_value(environ, "OPENCLAW_CONFIG", "OPENCLAW_CONFIG_PATH")
    if not raw:
        home = first_value(environ, "OPENCLAW_HOME")
        if home:
            raw = str(Path(home) / "openclaw.json")
        else:
            process_home = clean(environ.get("HOME")) or "/root"
            raw = str(Path(process_home) / ".openclaw" / "openclaw.json")
    return _expand_injected_path(raw, environ)


def workspace_path(environ: Mapping[str, str], destination: Path) -> Path:
    """Resolve the main-agent workspace from the established variables."""

    raw = first_value(
        environ,
        "OPENCLAW_AGENT_WORKSPACE",
        "OPENCLAW_WORKSPACE_DIR",
    )
    if not raw:
        return (destination.parent / "workspace").resolve()
    return _expand_injected_path(raw, environ)


def agent_dir_path(environ: Mapping[str, str], destination: Path) -> Path:
    """Resolve the main agent's state directory."""

    raw = first_value(environ, "OPENCLAW_AGENT_DIR")
    if not raw:
        return (destination.parent / "agents" / "main" / "agent").resolve()
    return _expand_injected_path(raw, environ)


def state_dir_path(environ: Mapping[str, str], destination: Path) -> Path:
    """Resolve the persistent shared-state root without reading the config."""

    raw = first_value(environ, "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_DIR")
    return _expand_injected_path(raw, environ) if raw else destination.parent


def openclaw_command(environ: Mapping[str, str]) -> list[str]:
    """Return the configured OpenClaw CLI command as an argument vector."""

    raw = clean(environ.get("OPENCLAW_BIN"))
    if not raw:
        return ["openclaw"]
    try:
        command = shlex.split(raw)
    except ValueError as exc:
        raise ConfigurationError("OPENCLAW_BIN is not valid shell-style syntax") from exc
    if not command:
        raise ConfigurationError("OPENCLAW_BIN must name an executable")
    return command


def _group_env_name(
    environ: Mapping[str, str],
    field: str,
    index: int,
) -> str:
    field = field.upper()
    if index == 1:
        return f"OPENAI_V1_{field}"
    canonical = f"OPENAI_V1_{field}_{index}"
    padded = f"OPENAI_V1_{field}_{index:02d}"
    for candidate in (canonical, padded):
        if clean(environ.get(candidate)):
            return candidate
    for candidate in (canonical, padded):
        if candidate in environ:
            return candidate
    return canonical


def expand_api_key_aliases(environ: Mapping[str, str]) -> dict[str, str]:
    """Materialize configured OPENAI_V1 reverse aliases only in process memory."""

    expanded = dict(environ)
    indexes = {1}
    pattern = re.compile(r"^OPENAI_V1_API_KEY_ALIAS_(\d+)$")
    for name in environ:
        match = pattern.fullmatch(name)
        if match:
            indexes.add(int(match.group(1)))

    for index in sorted(indexes):
        alias_name = _group_env_name(environ, "API_KEY_ALIAS", index)
        alias = clean(environ.get(alias_name))
        if not alias:
            continue
        if not API_KEY_ALIAS.fullmatch(alias):
            raise ConfigurationError(
                f"{alias_name} must name an uppercase *_API_KEY variable"
            )
        key_name = _group_env_name(environ, "KEY", index)
        key_value = clean(environ.get(key_name))
        if key_value and not clean(expanded.get(alias)):
            expanded[alias] = key_value
    return expanded


def without_secret_values(value: Any, secret_values: set[str]) -> bool:
    """Return whether a nested value contains none of the supplied secrets."""

    if isinstance(value, str):
        return value not in secret_values
    if isinstance(value, Mapping):
        return all(
            without_secret_values(key, secret_values)
            and without_secret_values(item, secret_values)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return all(without_secret_values(item, secret_values) for item in value)
    return True
