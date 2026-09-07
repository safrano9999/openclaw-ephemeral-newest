"""Construct a complete OpenClaw config exclusively from injected state."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from .environment import (
    ConfigurationError,
    agent_dir_path,
    boolean,
    clean,
    config_path,
    expand_api_key_aliases,
    integer,
    openclaw_command,
    secret_ref,
    state_dir_path,
    workspace_path,
    without_secret_values,
)
from .filesystem import atomic_write_json
from .plugins import discover_openclaw_plugins, register_openclaw_plugins
from .providers import (
    OpenAIV1Provider,
    discover_native_models,
    discover_openai_v1_providers,
    select_openai_v1_default,
)
from .scheduling import build_schedule_plan


DUMMY_MODEL = "dummy/dummy"
NOTE_MODEL = "dummy/note"
MAX_MCP_SERVERS = 50
MCP_FIELDS = ("NAME", "URL", "BEARER", "ALLOW_PRIVATE")
MCP_SUFFIX = re.compile(
    r"^MCP_SERVER_(?:NAME|URL|BEARER|ALLOW_PRIVATE)_(\d+)$"
)
SAFE_MCP_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TELEGRAM_SUFFIX = re.compile(
    r"^OPENCLAW_TELEGRAM(?:TOKEN|_(?:AGENT|CHAT_ID|DEFAULT))_(0?[2-9]|[1-4][0-9]|50)$"
)
SAFE_AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ConfigurationResult:
    """Non-sensitive summary of one complete configuration rebuild."""

    path: Path
    primary_model: str
    native_model_count: int
    openai_v1_provider_count: int
    openai_v1_model_count: int
    mcp_server_count: int
    telegram_configured: bool
    note_full_mode: bool
    warnings: tuple[str, ...]
    plugin_count: int = 0


@dataclass(frozen=True)
class McpServer:
    """One normalized HTTP MCP server without a resolved credential."""

    index: int
    name: str
    url: str
    bearer_env: str | None
    allow_private: bool

    def openclaw_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "enabled": True,
            "url": self.url,
            "transport": "streamable-http",
            "supportsParallelToolCalls": True,
            "codex": {"defaultToolsApprovalMode": "approve"},
        }
        if self.bearer_env is not None:
            config["headers"] = {
                "Authorization": f"Bearer ${{{self.bearer_env}}}"
            }
        if self.allow_private:
            config["allowPrivateNetwork"] = True
        return config


def _mcp_field_env_name(
    environ: Mapping[str, str],
    field: str,
    index: int,
) -> str:
    base = f"MCP_SERVER_{field}"
    if index == 1:
        return base
    padded = f"{base}_{index:02d}"
    unpadded = f"{base}_{index}"
    for candidate in (padded, unpadded):
        if candidate in environ:
            return candidate
    return padded


def _configured_mcp_indexes(environ: Mapping[str, str]) -> tuple[int, ...]:
    indexes = {1}
    for key in environ:
        match = MCP_SUFFIX.fullmatch(key)
        if match is None:
            continue
        suffix = match.group(1)
        index = int(suffix, 10)
        if not 2 <= index <= MAX_MCP_SERVERS:
            raise ConfigurationError(
                f"{key} index must be between 02 and {MAX_MCP_SERVERS:02d}"
            )
        if suffix not in {str(index), f"{index:02d}"}:
            raise ConfigurationError(f"{key} has an unsupported numeric suffix")
        indexes.add(index)
    return tuple(sorted(indexes))


def _mcp_server_url(raw_url: str, index: int) -> str:
    url = clean(raw_url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(
            f"MCP_SERVER_URL for server {index:02d} must be an http(s) URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(
            f"MCP_SERVER_URL for server {index:02d} must not contain credentials"
        )
    return url


def _mcp_server_name(raw_name: str, url: str, index: int) -> str:
    name = clean(raw_name)
    if not name:
        name = clean(urlsplit(url).hostname)
        if name.endswith(".dns.podman"):
            name = name[: -len(".dns.podman")]
        name = name or f"mcp-{index:02d}"
    if SAFE_MCP_SERVER_NAME.fullmatch(name) is None:
        raise ConfigurationError(
            f"MCP server {index:02d} name must match "
            f"{SAFE_MCP_SERVER_NAME.pattern}"
        )
    return name


def discover_mcp_servers(
    environ: Mapping[str, str],
) -> tuple[McpServer, ...]:
    """Read the optional suffixless, then suffix02-style MCP groups."""

    servers: list[McpServer] = []
    seen_names: set[str] = set()
    for index in _configured_mcp_indexes(environ):
        fields = {
            field: _mcp_field_env_name(environ, field, index)
            for field in MCP_FIELDS
        }
        raw_url = clean(environ.get(fields["URL"]))
        if not raw_url:
            continue
        url = _mcp_server_url(raw_url, index)
        name = _mcp_server_name(environ.get(fields["NAME"], ""), url, index)
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise ConfigurationError(f"duplicate MCP server name: {name}")
        seen_names.add(folded_name)

        bearer = clean(environ.get(fields["BEARER"]))
        if bearer.lower().startswith("bearer "):
            raise ConfigurationError(
                f"{fields['BEARER']} must contain only the token, without 'Bearer '"
            )
        servers.append(
            McpServer(
                index=index,
                name=name,
                url=url,
                bearer_env=fields["BEARER"] if bearer else None,
                allow_private=boolean(
                    environ,
                    fields["ALLOW_PRIVATE"],
                    default=False,
                ),
            )
        )
    return tuple(servers)


def _trusted_proxies(environ: Mapping[str, str]) -> list[str]:
    name = "OPENCLAW_TRUSTED_PROXIES"
    raw = clean(environ.get(name))
    if not raw:
        return []
    proxies: list[str] = []
    for candidate in raw.split(","):
        candidate = clean(candidate)
        try:
            if "%" in candidate:
                raise ValueError("scoped addresses are not supported")
            proxy = str(
                ip_network(candidate, strict=False)
                if "/" in candidate
                else ip_address(candidate)
            )
        except ValueError as exc:
            raise ConfigurationError(
                f"{name} must contain comma-separated proxy IP addresses or CIDRs"
            ) from exc
        if proxy not in proxies:
            proxies.append(proxy)
    return proxies


def _control_ui_allowed_origins(environ: Mapping[str, str]) -> list[str]:
    name = "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS"
    raw = clean(environ.get(name))
    if not raw:
        return []
    candidates = raw.split(",")
    origins: list[str] = []
    for candidate in candidates:
        candidate = clean(candidate)
        parsed = urlsplit(candidate)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConfigurationError(f"{name} contains an invalid port") from exc
        if (
            not candidate
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "*" in candidate
            or any(character.isspace() for character in candidate)
        ):
            raise ConfigurationError(
                f"{name} must contain comma-separated exact HTTP(S) origins"
            )
        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname if port is None else f"{hostname}:{port}"
        origin = f"{parsed.scheme.lower()}://{netloc}"
        if origin not in origins:
            origins.append(origin)
    return origins


def _origin(host: str, port: int) -> str:
    host = host.strip() or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _tailscale_hosts(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
) -> list[str]:
    ts_hostname = clean(environ.get("TS_HOSTNAME")).rstrip(".")
    if not ts_hostname:
        return []

    try:
        result = runner(
            ["tailscale", "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        payload = json.loads(result.stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ):
        return []

    hosts: list[str] = []
    self_info = payload.get("Self") if isinstance(payload, dict) else {}
    if isinstance(self_info, dict):
        dns_name = clean(self_info.get("DNSName")).rstrip(".")
        if dns_name:
            hosts.append(dns_name)
        for ip_addr in self_info.get("TailscaleIPs") or []:
            ip_addr = clean(ip_addr)
            if ip_addr:
                hosts.append(ip_addr)
    if "." in ts_hostname:
        hosts.append(ts_hostname)
    return list(dict.fromkeys(hosts))


def _automatic_control_ui_origins(
    environ: Mapping[str, str],
    *,
    port: int,
    runner: Callable[..., Any],
) -> list[str]:
    candidates: list[str] = []
    if clean(environ.get("CITADEL_CLOUDFLARE")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        domain = clean(environ.get("CITADEL_CLOUDFLARE_DOMAIN")).strip(".")
        if domain:
            candidates.append(f"https://{port}.{domain}")

    try:
        publish_port = int(
            clean(environ.get("OPENCLAW_GATEWAY_PUBLISH_PORT")) or "20789"
        )
        if not 1 <= publish_port <= 65_535:
            publish_port = None
    except ValueError:
        publish_port = None
    for host in _tailscale_hosts(environ, runner=runner):
        candidates.append(_origin(host, port))
        if publish_port is not None:
            candidates.append(_origin(host, publish_port))

    origins: list[str] = []
    for candidate in candidates:
        try:
            normalized = _control_ui_allowed_origins(
                {"OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": candidate}
            )[0]
        except (ConfigurationError, ValueError):
            continue
        if normalized not in origins:
            origins.append(normalized)
    return origins


def _gateway_config(
    environ: Mapping[str, str],
    *,
    tailscale_runner: Callable[..., Any],
) -> dict[str, Any]:
    port = integer(
        environ,
        "OPENCLAW_GATEWAY_PORT",
        default=18_789,
        maximum=65_535,
    )
    origins = _control_ui_allowed_origins(environ)
    auto = clean(
        environ.get("OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS_AUTO", "0")
    ).lower()
    if auto in {"1", "true", "yes", "on"}:
        for origin in _automatic_control_ui_origins(
            environ,
            port=port,
            runner=tailscale_runner,
        ):
            if origin not in origins:
                origins.append(origin)
    gateway: dict[str, Any] = {
        "mode": "local",
        "bind": "lan",
        "port": port,
        "controlUi": {
            "allowedOrigins": origins,
        },
    }
    proxies = _trusted_proxies(environ)
    if proxies:
        gateway["trustedProxies"] = proxies
    if clean(environ.get("OPENCLAW_GATEWAY_TOKEN")):
        gateway["auth"] = {
            "mode": "token",
            "token": secret_ref("OPENCLAW_GATEWAY_TOKEN"),
        }
    return gateway


def _hooks_config(environ: Mapping[str, str]) -> dict[str, Any]:
    """Enable native agent hooks without persisting their bearer token."""

    if not clean(environ.get("OPENCLAW_HOOKS_TOKEN")):
        return {}
    return {
        "hooks": {
            "enabled": True,
            "token": "${OPENCLAW_HOOKS_TOKEN}",
            "path": "/hooks",
        }
    }


def _telegram_key(environ: Mapping[str, str], base: str, index: int) -> str:
    if index == 1:
        return base
    for key in (f"{base}_{index:02d}", f"{base}_{index}"):
        if key in environ:
            return key
    return f"{base}_{index:02d}"


def _telegram_accounts(environ: Mapping[str, str]) -> tuple[dict[str, Any], ...]:
    indexes = {
        1,
        *(
            int(match.group(1))
            for key in environ
            if (match := TELEGRAM_SUFFIX.fullmatch(key))
        ),
    }
    accounts: list[dict[str, Any]] = []
    for index in sorted(indexes):
        token_key = _telegram_key(environ, "OPENCLAW_TELEGRAMTOKEN", index)
        if not clean(environ.get(token_key)):
            continue
        agent = clean(
            environ.get(_telegram_key(environ, "OPENCLAW_TELEGRAM_AGENT", index))
        ).lower() or ("main" if index == 1 else "")
        chat = clean(
            environ.get(_telegram_key(environ, "OPENCLAW_TELEGRAM_CHAT_ID", index))
        ).removeprefix("telegram:")
        if not SAFE_AGENT_ID.fullmatch(agent):
            raise ConfigurationError(f"invalid Telegram agent id: {agent or '<empty>'}")
        if not chat:
            raise ConfigurationError(f"Telegram account {agent} requires a chat id")
        accounts.append(
            {
                "agent": agent,
                "token": token_key,
                "chat": chat,
                "default": boolean(
                    environ,
                    _telegram_key(environ, "OPENCLAW_TELEGRAM_DEFAULT", index),
                    default=index == 1,
                ),
            }
        )
    names = [account["agent"] for account in accounts]
    if len(names) != len(set(names)):
        raise ConfigurationError("duplicate Telegram agent id")
    if sum(account["default"] for account in accounts) > 1:
        raise ConfigurationError("only one Telegram account may be default")
    return tuple(accounts)


def _main_agent_config(
    environ: Mapping[str, str],
    destination: Path,
    primary_model: str,
    model_allowlist: dict[str, dict[str, Any]],
    telegram_accounts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    workspace = workspace_path(environ, destination)
    agent_dir = agent_dir_path(environ, destination)
    tools = {"allow": ["*"], "deny": []}
    defaults = {
        "workspace": str(workspace),
        "model": {"primary": primary_model},
        "models": model_allowlist,
        # Bare primary names resolve through the qualified provider catalog; the
        # explicit override policy accepts qualified refs and OpenRouter selectors.
        "modelPolicy": {
            "allow": [
                model for model in model_allowlist
                if "/" in model or model in {"openrouter:auto", "openrouter:free"}
            ]
        },
        "sandbox": {"mode": "off"},
    }
    agents = {}
    names = dict.fromkeys(
        ("main", *(account["agent"] for account in telegram_accounts))
    )
    for name in names:
        current_workspace = (
            workspace if name == "main" else workspace.parent / f"{workspace.name}-{name}"
        )
        current_agent_dir = (
            agent_dir
            if name == "main"
            else agent_dir.parent.parent / name / "agent"
        )
        current_workspace.mkdir(parents=True, exist_ok=True)
        current_agent_dir.mkdir(parents=True, exist_ok=True)
        agents[name] = {
            "name": name,
            "workspace": str(current_workspace),
            "agentDir": str(current_agent_dir),
            "heartbeat": {
                "every": "360m",
                "target": "last",
                "directPolicy": "allow",
            },
            "tools": tools,
        }
    if len(agents) > 1:
        # Preserve the retired main default marker's ambient system owner.
        defaults["systemAgent"] = {"agentId": "main"}
    return {"ownership": "explicit", "defaults": defaults, "entries": agents}


def _telegram_config(accounts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not accounts:
        return {}
    chats = list(dict.fromkeys(account["chat"] for account in accounts))
    default = next(
        (account["agent"] for account in accounts if account["default"]),
        accounts[0]["agent"],
    )
    telegram = {
        "enabled": True,
        "dmPolicy": "allowlist",
        "allowFrom": chats,
        "groupPolicy": "open",
        "groupAllowFrom": ["*"],
        "groups": {"*": {"requireMention": False}},
        "capabilities": {"inlineButtons": "dm"},
        "commands": {"native": False, "nativeSkills": False},
        "streaming": {"mode": "off"},
        "execApprovals": {
            "enabled": False,
            "approvers": [],
            "target": "dm",
        },
        "network": {
            "autoSelectFamily": False,
            "dnsResultOrder": "ipv4first",
        },
        "accounts": {
            account["agent"]: {
                "name": account["agent"],
                "enabled": True,
                "dmPolicy": "allowlist",
                "allowFrom": [account["chat"]],
                "botToken": secret_ref(account["token"]),
                "groupPolicy": "open",
                "groupAllowFrom": ["*"],
                "groups": {"*": {"requireMention": False}},
                "streaming": {"mode": "partial"},
            }
            for account in accounts
        },
        "defaultAccount": default,
    }
    bindings = [
        {
            "agentId": account["agent"],
            "match": {"channel": "telegram", "accountId": account["agent"]},
        }
        for account in accounts
    ]
    if any(account["agent"] != "main" for account in accounts):
        bindings.append({
            "agentId": "main",
            "match": {"channel": "telegram", "accountId": "*"},
        })
    return {
        "channels": {"telegram": telegram},
        "commands": {"ownerAllowFrom": [f"telegram:{chat}" for chat in chats]},
        "bindings": bindings,
    }


def _plugins_config(note_full_mode: bool) -> dict[str, Any]:
    # The deterministic OpenClaw patch supplies dummy/dummy. The separately
    # installed NOTE extension supplies dummy/note and its direct-capture hook.
    note: dict[str, Any] = {"enabled": True}
    if note_full_mode:
        note["hooks"] = {"allowConversationAccess": True}
    return {
        "entries": {
            "codex": {"enabled": True},
            "note": note,
        }
    }


def _trusted_container_tools() -> dict[str, Any]:
    """Return the trusted policy in OpenClaw's canonical config form."""

    return {
        "profile": "full",
        "fs": {"workspaceOnly": False},
        "exec": {
            "host": "gateway",
            "mode": "full",
            "applyPatch": {"workspaceOnly": False},
        },
    }


def _models_config(
    providers: Sequence[OpenAIV1Provider],
) -> dict[str, Any]:
    if not providers:
        return {}
    return {
        "mode": "merge",
        "providers": {
            provider.provider_id: provider.openclaw_config()
            for provider in providers
        },
    }


def _model_allowlist(
    native_models: Sequence[str],
    providers: Sequence[OpenAIV1Provider],
    explicit_model: str = "",
) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {
        DUMMY_MODEL: {},
        NOTE_MODEL: {},
    }
    for model in sorted(set(native_models)):
        models[model] = {}
    for provider in providers:
        for model in provider.models:
            models[f"{provider.provider_id}/{model}"] = {}
    if explicit_model:
        models[explicit_model] = {}
    return models


def _select_explicit_custom_model(
    providers: Sequence[OpenAIV1Provider],
    explicit_model: str,
) -> tuple[str, tuple[OpenAIV1Provider, ...]] | None:
    """Resolve an explicit model only when it belongs to a custom provider."""

    wanted = clean(explicit_model)
    if not wanted:
        return None
    for provider in providers:
        aliases = {provider.provider_id}
        if provider.configured_name:
            aliases.add(provider.configured_name.lower())
        if any(wanted.lower().startswith(f"{alias}/") for alias in aliases):
            return select_openai_v1_default(providers, wanted)
    if "/" not in wanted and any(wanted in provider.models for provider in providers):
        return select_openai_v1_default(providers, wanted)
    return None


def build_config(
    environ: Mapping[str, str],
    *,
    destination: Path,
    native_models: Sequence[str] = (),
    openai_v1_providers: Sequence[OpenAIV1Provider] = (),
    mcp_servers: Sequence[McpServer] | None = None,
    tailscale_runner: Callable[..., Any] = subprocess.run,
) -> tuple[dict[str, Any], str, bool]:
    """Build a complete config without opening the destination file."""

    requested_note_full_mode = boolean(
        environ,
        "OPENCLAW_NOTE_FULL_MODE",
        default=boolean(environ, "NOTE_FULL_MODE", default=True),
    )
    explicit_model = clean(environ.get("OPENCLAW_MODEL"))
    note_full_mode = requested_note_full_mode or explicit_model == NOTE_MODEL
    providers = tuple(openai_v1_providers)
    configured_default = clean(environ.get("OPENCLAW_OPENAI_V1_DEFAULT_LLM"))
    selected = select_openai_v1_default(providers, configured_default)
    custom_primary = ""
    if selected is not None:
        custom_primary, providers = selected
    explicit_custom = _select_explicit_custom_model(providers, explicit_model)
    if explicit_custom is not None:
        _, providers = explicit_custom
    primary_model = explicit_model or custom_primary or (
        NOTE_MODEL if requested_note_full_mode else DUMMY_MODEL
    )
    allowlist = _model_allowlist(
        native_models,
        providers,
        explicit_model=explicit_model,
    )

    telegram_accounts = _telegram_accounts(environ)
    config: dict[str, Any] = {
        "gateway": _gateway_config(environ, tailscale_runner=tailscale_runner),
        "agents": _main_agent_config(
            environ,
            destination,
            primary_model,
            allowlist,
            telegram_accounts,
        ),
        "plugins": _plugins_config(note_full_mode),
        "tools": _trusted_container_tools(),
    }
    configured_mcp_servers = (
        tuple(mcp_servers)
        if mcp_servers is not None
        else discover_mcp_servers(environ)
    )
    if configured_mcp_servers:
        config["mcp"] = {
            "servers": {
                server.name: server.openclaw_config()
                for server in configured_mcp_servers
            }
        }
    custom_models = _models_config(providers)
    if custom_models:
        config["models"] = custom_models
    config.update(_hooks_config(environ))
    config.update(_telegram_config(telegram_accounts))
    if len(config["agents"]["entries"]) > 1:
        config["talk"] = {"agentId": "main"}
    config.setdefault("commands", {})["mcp"] = True
    return config, primary_model, note_full_mode


def configure(
    environ: Mapping[str, str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
    opener: Callable[..., Any] = urlopen,
) -> ConfigurationResult:
    """Discover providers, rebuild the config from scratch, and atomically write it."""

    injected = expand_api_key_aliases(os.environ if environ is None else environ)
    destination = config_path(injected)
    discovered_plugins, plugin_warnings = discover_openclaw_plugins(
        injected,
        destination=destination,
    )
    native_models, native_warnings = discover_native_models(
        injected,
        runner=runner,
        plugins=discovered_plugins,
    )
    # Shared OAuth moved to state/openclaw.sqlite; agent-local overrides remain.
    # Keep the runtime-discovered OpenAI catalog visible without reading secrets.
    state_root = state_dir_path(injected, destination)
    auth_databases = (
        agent_dir_path(injected, destination) / "openclaw-agent.sqlite",
        state_root / "agents" / "main" / "agent" / "openclaw-agent.sqlite",
        state_root / "state" / "openclaw.sqlite",
    )
    if any(path.is_file() and path.stat().st_size for path in auth_databases):
        native_models = tuple(sorted({*native_models, "openai/*"}))
    providers, openai_warnings = discover_openai_v1_providers(
        injected,
        opener=opener,
    )
    mcp_servers = discover_mcp_servers(injected)
    # Validate the two explicit repository selections while the complete final
    # image is visible. The actual gateway-side scheduling happens later.
    build_schedule_plan(injected, discovered_plugins)
    config, primary_model, note_full_mode = build_config(
        injected,
        destination=destination,
        native_models=native_models,
        openai_v1_providers=providers,
        mcp_servers=mcp_servers,
        tailscale_runner=runner,
    )
    registered_plugins = register_openclaw_plugins(
        config,
        discovered_plugins,
        environ=injected,
        destination=destination,
    )
    secret_values = {
        clean(value)
        for name, value in injected.items()
        if clean(value)
        and (
            name.endswith("_API_KEY")
            or name.startswith("OPENAI_V1_KEY")
            or name.startswith("MCP_SERVER_BEARER")
            or name.startswith("OPENCLAW_TELEGRAMTOKEN")
            or name in {
                "OPENCLAW_GATEWAY_TOKEN",
                "OPENCLAW_HOOKS_TOKEN",
            }
        )
    }
    if not without_secret_values(config, secret_values):
        raise RuntimeError("Refusing to persist a resolved secret value")
    atomic_write_json(destination, config)
    if registered_plugins:
        try:
            runner(
                [*openclaw_command(injected), "plugins", "registry", "--refresh"],
                check=True,
                capture_output=True,
                text=True,
                env=dict(injected),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigurationError(
                f"cannot refresh the OpenClaw plugin registry: {exc}"
            ) from exc
        config = json.loads(destination.read_text(encoding="utf-8"))
        for agent in config["agents"]["entries"].values():
            agent["tools"] = {"allow": ["*"], "deny": []}
        atomic_write_json(destination, config)

    return ConfigurationResult(
        path=destination,
        primary_model=primary_model,
        native_model_count=len(native_models),
        openai_v1_provider_count=len(providers),
        openai_v1_model_count=sum(len(provider.models) for provider in providers),
        mcp_server_count=len(mcp_servers),
        telegram_configured="telegram" in config.get("channels", {}),
        note_full_mode=note_full_mode,
        warnings=(*native_warnings, *openai_warnings, *plugin_warnings),
        plugin_count=len(registered_plugins),
    )
