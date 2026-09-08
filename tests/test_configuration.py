from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openclaw_ephemeral.configuration import (
    DUMMY_MODEL,
    NOTE_MODEL,
    build_config as _build_config,
    configure,
    discover_mcp_servers,
)
from openclaw_ephemeral.environment import ConfigurationError
from openclaw_ephemeral.providers import OpenAIV1Provider


CONTROL_UI_ORIGINS = (
    "http://127.0.0.1:18789,"
    "http://localhost:18789,"
    "http://127.0.0.1:20789"
)


def build_config(environ: dict[str, str], **kwargs: object):
    return _build_config(
        {"OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": CONTROL_UI_ORIGINS, **environ},
        **kwargs,
    )


def provider(
    *,
    index: int = 1,
    provider_id: str = "litellm",
    models: tuple[str, ...] = ("model-a",),
    streaming: bool = False,
) -> OpenAIV1Provider:
    suffix = "" if index == 1 else f"_{index}"
    return OpenAIV1Provider(
        index=index,
        provider_id=provider_id,
        configured_name=provider_id,
        base_url=f"http://provider-{index}.test:4000/v1",
        key_env=f"OPENAI_V1_KEY{suffix}",
        models=models,
        streaming=streaming,
    )


class ConfigBuilderTests(unittest.TestCase):
    def test_optional_repeated_mcp_servers_are_global_and_unrestricted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "MCP_SERVER_NAME": "",
                    "MCP_SERVER_URL": (
                        "http://kachelmann-mcp.dns.podman:11041/mcp"
                    ),
                    "MCP_SERVER_BEARER": "first-secret",
                    "MCP_SERVER_ALLOW_PRIVATE": "1",
                    "MCP_SERVER_NAME_02": "paperless-mcp",
                    "MCP_SERVER_URL_02": "http://paperless-mcp:5000/mcp",
                    "MCP_SERVER_BEARER_02": "",
                    "MCP_SERVER_NAME_03": "ignored-without-url",
                },
                destination=Path(raw) / "openclaw.json",
            )

        servers = config["mcp"]["servers"]
        self.assertEqual(
            list(servers),
            ["kachelmann-mcp", "paperless-mcp"],
        )
        kachelmann = servers["kachelmann-mcp"]
        self.assertEqual(
            kachelmann["headers"]["Authorization"],
            "Bearer ${MCP_SERVER_BEARER}",
        )
        self.assertEqual(kachelmann["transport"], "streamable-http")
        self.assertTrue(kachelmann["supportsParallelToolCalls"])
        self.assertEqual(
            kachelmann["codex"]["defaultToolsApprovalMode"],
            "approve",
        )
        self.assertIs(kachelmann["allowPrivateNetwork"], True)
        self.assertNotIn("toolFilter", kachelmann)
        self.assertNotIn("headers", servers["paperless-mcp"])
        self.assertNotIn("allowPrivateNetwork", servers["paperless-mcp"])
        self.assertNotIn("first-secret", json.dumps(config))

    def test_empty_optional_mcp_groups_do_not_create_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "MCP_SERVER_NAME": "ignored-without-url",
                    "MCP_SERVER_BEARER": "unused-secret",
                },
                destination=Path(raw) / "openclaw.json",
            )

        self.assertNotIn("mcp", config)

    def test_invalid_mcp_groups_are_rejected(self) -> None:
        cases = (
            (
                {"MCP_SERVER_URL": "ftp://invalid.example/mcp"},
                "http(s) URL",
            ),
            (
                {"MCP_SERVER_URL": "http://user:pass@invalid.example/mcp"},
                "must not contain credentials",
            ),
            (
                {
                    "MCP_SERVER_NAME": "duplicate",
                    "MCP_SERVER_URL": "http://one.example/mcp",
                    "MCP_SERVER_NAME_02": "DUPLICATE",
                    "MCP_SERVER_URL_02": "http://two.example/mcp",
                },
                "duplicate MCP server name",
            ),
            (
                {
                    "MCP_SERVER_URL": "http://one.example/mcp",
                    "MCP_SERVER_BEARER": "Bearer already-prefixed",
                },
                "without 'Bearer '",
            ),
            (
                {"MCP_SERVER_URL_01": "http://one.example/mcp"},
                "between 02 and 50",
            ),
            (
                {
                    "MCP_SERVER_URL": "http://one.example/mcp",
                    "MCP_SERVER_ALLOW_PRIVATE": "sometimes",
                },
                "MCP_SERVER_ALLOW_PRIVATE",
            ),
        )
        for environ, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as caught:
                    discover_mcp_servers(environ)
                self.assertIn(message, str(caught.exception))

    def test_minimal_config_has_both_deterministic_routes_and_main_agent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "state" / "openclaw.json"
            config, primary, full_mode = build_config(
                {
                    "HOME": str(root),
                    "OPENCLAW_AGENT_WORKSPACE": str(root / "workspace"),
                },
                destination=destination,
            )

            self.assertEqual(primary, NOTE_MODEL)
            self.assertTrue(full_mode)
            self.assertEqual(
                config["agents"]["defaults"]["models"],
                {DUMMY_MODEL: {}, NOTE_MODEL: {}},
            )
            self.assertEqual(
                config["agents"]["defaults"]["modelPolicy"]["allow"],
                [DUMMY_MODEL, NOTE_MODEL],
            )
            self.assertEqual(
                config["agents"]["defaults"]["model"]["primary"],
                NOTE_MODEL,
            )
            self.assertEqual(
                config["agents"]["defaults"]["sandbox"],
                {"mode": "off"},
            )
            self.assertEqual(
                config["agents"]["entries"]["main"]["tools"],
                {"allow": ["*"], "deny": []},
            )
            self.assertEqual(
                config["tools"],
                {
                    "profile": "full",
                    "fs": {"workspaceOnly": False},
                    "exec": {
                        "host": "gateway",
                        "mode": "full",
                        "applyPatch": {"workspaceOnly": False},
                    },
                },
            )
            self.assertNotIn("list", config["agents"])
            main = config["agents"]["entries"]["main"]
            self.assertNotIn("id", main)
            self.assertNotIn("default", main)
            self.assertTrue(Path(main["workspace"]).is_dir())
            self.assertTrue(Path(main["agentDir"]).is_dir())

            note = config["plugins"]["entries"]["note"]
            self.assertEqual(
                note,
                {
                    "enabled": True,
                    "hooks": {"allowConversationAccess": True},
                },
            )
            self.assertTrue(config["plugins"]["entries"]["codex"]["enabled"])

    def test_note_full_mode_can_be_disabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "openclaw.json"
            config, primary, full_mode = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_NOTE_FULL_MODE": "0",
                },
                destination=destination,
            )

            self.assertFalse(full_mode)
            self.assertEqual(primary, DUMMY_MODEL)
            self.assertEqual(
                config["plugins"]["entries"]["note"],
                {"enabled": True},
            )
            self.assertIn(DUMMY_MODEL, config["agents"]["defaults"]["models"])
            self.assertIn(NOTE_MODEL, config["agents"]["defaults"]["models"])

    def test_native_and_repeated_custom_models_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_OPENAI_V1_DEFAULT_LLM": "second/model-c",
                },
                destination=Path(raw) / "openclaw.json",
                native_models=("anthropic/claude-a", "gemini/gemini-b"),
                openai_v1_providers=(
                    provider(models=("model-a",), streaming=True),
                    provider(
                        index=2,
                        provider_id="second",
                        models=("model-c",),
                    ),
                ),
            )

            self.assertEqual(primary, "second/model-c")
            allowlist = config["agents"]["defaults"]["models"]
            for model in (
                DUMMY_MODEL,
                NOTE_MODEL,
                "anthropic/claude-a",
                "gemini/gemini-b",
                "litellm/model-a",
                "second/model-c",
            ):
                self.assertIn(model, allowlist)
            self.assertEqual(allowlist["litellm/model-a"], {})
            self.assertEqual(
                set(config["models"]["providers"]),
                {"litellm", "second"},
            )
            self.assertEqual(
                config["agents"]["defaults"]["modelPolicy"]["allow"],
                list(allowlist),
            )

    def test_bare_custom_primary_keeps_its_qualified_model_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, _ = build_config(
                {"HOME": raw, "OPENCLAW_MODEL": "selected"},
                destination=Path(raw) / "openclaw.json",
                openai_v1_providers=(provider(models=("selected", "other")),),
            )

        self.assertEqual(primary, "selected")
        defaults = config["agents"]["defaults"]
        self.assertEqual(defaults["model"]["primary"], "selected")
        self.assertIn("selected", defaults["models"])
        self.assertEqual(
            defaults["modelPolicy"]["allow"],
            [DUMMY_MODEL, NOTE_MODEL, "litellm/selected", "litellm/other"],
        )

    def test_openclaw_model_overrides_openai_v1_default_and_is_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, full_mode = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_MODEL": "anthropic/claude-explicit",
                    "OPENCLAW_OPENAI_V1_DEFAULT_LLM": "litellm/model-a",
                },
                destination=Path(raw) / "openclaw.json",
                native_models=("anthropic/claude-discovered",),
                openai_v1_providers=(provider(),),
            )

            self.assertEqual(primary, "anthropic/claude-explicit")
            self.assertTrue(full_mode)
            self.assertIn(
                "anthropic/claude-explicit",
                config["agents"]["defaults"]["models"],
            )
            self.assertIn(
                "litellm/model-a",
                config["agents"]["defaults"]["models"],
            )

    def test_openclaw_model_dummy_note_enables_full_mode_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, full_mode = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_MODEL": NOTE_MODEL,
                },
                destination=Path(raw) / "openclaw.json",
            )

            self.assertEqual(primary, NOTE_MODEL)
            self.assertTrue(full_mode)
            self.assertEqual(
                config["plugins"]["entries"]["note"]["hooks"],
                {"allowConversationAccess": True},
            )

    def test_openclaw_model_adds_missing_custom_model_to_provider_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_MODEL": "litellm/not-discovered",
                },
                destination=Path(raw) / "openclaw.json",
                openai_v1_providers=(provider(),),
            )

            self.assertEqual(primary, "litellm/not-discovered")
            model_ids = [
                item["id"]
                for item in config["models"]["providers"]["litellm"]["models"]
            ]
            self.assertEqual(model_ids, ["not-discovered", "model-a"])

    def test_gateway_telegram_and_origins_use_environment_refs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            secrets = {
                "gateway": "gateway-secret",
                "hooks": "hooks-secret",
                "telegram": "telegram-secret",
                "telegram_auto": "telegram-auto-secret",
            }
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "FASTAPI_HOST": "10.0.0.5",
                    "OPENCLAW_GATEWAY_PORT": "19000",
                    "OPENCLAW_GATEWAY_PUBLISH_PORT": "29000",
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": (
                        "https://control.example.test/,"
                        "http://localhost:19000,"
                        "https://control.example.test"
                    ),
                    "OPENCLAW_GATEWAY_TOKEN": secrets["gateway"],
                    "OPENCLAW_HOOKS_TOKEN": secrets["hooks"],
                    "OPENCLAW_TELEGRAMTOKEN": secrets["telegram"],
                    "OPENCLAW_TELEGRAM_AGENT": "main",
                    "OPENCLAW_TELEGRAM_CHAT_ID": "5475045993",
                    "OPENCLAW_TELEGRAM_DEFAULT": "1",
                    "OPENCLAW_TELEGRAMTOKEN_02": secrets["telegram_auto"],
                    "OPENCLAW_TELEGRAM_AGENT_02": "auto",
                    "OPENCLAW_TELEGRAM_CHAT_ID_02": "987654321",
                    "OPENCLAW_TELEGRAM_DEFAULT_02": "0",
                },
                destination=Path(raw) / "openclaw.json",
            )

            gateway = config["gateway"]
            self.assertEqual(gateway["port"], 19000)
            self.assertEqual(
                gateway["auth"]["token"]["id"],
                "OPENCLAW_GATEWAY_TOKEN",
            )
            self.assertNotIn("allowInsecureAuth", gateway["controlUi"])
            self.assertNotIn("dangerouslyDisableDeviceAuth", gateway["controlUi"])
            self.assertEqual(
                gateway["controlUi"]["allowedOrigins"],
                ["https://control.example.test", "http://localhost:19000"],
            )
            telegram = config["channels"]["telegram"]
            self.assertEqual(
                telegram["accounts"]["main"]["botToken"]["id"],
                "OPENCLAW_TELEGRAMTOKEN",
            )
            self.assertEqual(
                telegram["accounts"]["auto"]["botToken"]["id"],
                "OPENCLAW_TELEGRAMTOKEN_02",
            )
            self.assertEqual(
                telegram["defaultAccount"],
                "main",
            )
            self.assertEqual(telegram["streaming"], {"mode": "off"})
            self.assertEqual(
                telegram["accounts"]["main"]["streaming"],
                {"mode": "partial"},
            )
            self.assertEqual(
                config["commands"]["ownerAllowFrom"],
                ["telegram:5475045993", "telegram:987654321"],
            )
            self.assertEqual(
                config["hooks"],
                {
                    "enabled": True,
                    "token": "${OPENCLAW_HOOKS_TOKEN}",
                    "path": "/hooks",
                },
            )
            self.assertEqual(
                config["bindings"],
                [
                    {
                        "agentId": "main",
                        "match": {"channel": "telegram", "accountId": "main"},
                    },
                    {
                        "agentId": "auto",
                        "match": {"channel": "telegram", "accountId": "auto"},
                    },
                    {
                        "agentId": "main",
                        "match": {"channel": "telegram", "accountId": "*"},
                    },
                ],
            )
            self.assertEqual(
                list(config["agents"]["entries"]),
                ["main", "auto"],
            )
            self.assertTrue(
                all(
                    agent["tools"] == {"allow": ["*"], "deny": []}
                    for agent in config["agents"]["entries"].values()
                )
            )
            self.assertEqual(config["agents"]["ownership"], "explicit")
            self.assertEqual(
                config["agents"]["defaults"]["systemAgent"], {"agentId": "main"}
            )
            self.assertNotIn("sessionStore", config["agents"]["defaults"])
            self.assertEqual(config["talk"]["agentId"], "main")
            serialized = json.dumps(config)
            self.assertNotIn(secrets["gateway"], serialized)
            self.assertNotIn(secrets["hooks"], serialized)
            self.assertNotIn(secrets["telegram"], serialized)
            self.assertNotIn(secrets["telegram_auto"], serialized)

    def test_heartbeats_are_disabled_without_a_telegram_account(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {"HOME": raw}, destination=Path(raw) / "openclaw.json"
            )
        self.assertEqual(config["agents"]["entries"]["main"]["heartbeat"]["every"], "0m")

    def test_telegram_heartbeats_are_disabled_by_default(self) -> None:
        for value in (None, "", "0"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                env = {
                    "HOME": raw,
                    "OPENCLAW_TELEGRAMTOKEN": "test-token",
                    "OPENCLAW_TELEGRAM_CHAT_ID": "12345",
                }
                if value is not None:
                    env["OPENCLAW_TELEGRAM_HEARTBEAT_MINUTES"] = value
                config, _, _ = build_config(env, destination=Path(raw) / "openclaw.json")
                self.assertEqual(
                    config["agents"]["entries"]["main"]["heartbeat"]["every"], "0m"
                )

    def test_telegram_heartbeat_intervals_are_scoped_to_their_agents(self) -> None:
        for suffix in ("_02", "_2"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as raw:
                config, _, _ = build_config(
                    {
                        "HOME": raw,
                        "OPENCLAW_TELEGRAMTOKEN": "first-token",
                        "OPENCLAW_TELEGRAM_CHAT_ID": "12345",
                        "OPENCLAW_TELEGRAM_HEARTBEAT_MINUTES": "30",
                        f"OPENCLAW_TELEGRAM_AGENT{suffix}": "worker",
                        f"OPENCLAW_TELEGRAMTOKEN{suffix}": "second-token",
                        f"OPENCLAW_TELEGRAM_CHAT_ID{suffix}": "67890",
                        f"OPENCLAW_TELEGRAM_HEARTBEAT_MINUTES{suffix}": "360",
                        "OPENCLAW_TELEGRAM_AGENT_03": "quiet",
                        "OPENCLAW_TELEGRAMTOKEN_03": "third-token",
                        "OPENCLAW_TELEGRAM_CHAT_ID_03": "54321",
                    },
                    destination=Path(raw) / "openclaw.json",
                )
                entries = config["agents"]["entries"]
                self.assertEqual(entries["main"]["heartbeat"]["every"], "30m")
                self.assertEqual(entries["worker"]["heartbeat"]["every"], "360m")
                self.assertEqual(entries["quiet"]["heartbeat"]["every"], "0m")

    def test_invalid_telegram_heartbeat_intervals_are_rejected(self) -> None:
        for value in ("-1", "1.5", "30m", "invalid"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                with self.assertRaisesRegex(ConfigurationError, "HEARTBEAT_MINUTES_02"):
                    build_config(
                        {
                            "HOME": raw,
                            "OPENCLAW_TELEGRAM_AGENT_02": "worker",
                            "OPENCLAW_TELEGRAMTOKEN_02": "test-token",
                            "OPENCLAW_TELEGRAM_CHAT_ID_02": "12345",
                            "OPENCLAW_TELEGRAM_HEARTBEAT_MINUTES_02": value,
                        },
                        destination=Path(raw) / "openclaw.json",
                    )

    def test_gateway_uses_example_origin_preset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {"HOME": raw},
                destination=Path(raw) / "openclaw.json",
            )

        self.assertEqual(
            config["gateway"]["controlUi"]["allowedOrigins"],
            [
                "http://127.0.0.1:18789",
                "http://localhost:18789",
                "http://127.0.0.1:20789",
            ],
        )
        self.assertNotIn("hooks", config)

    def test_gateway_trusts_only_configured_proxy_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                    "OPENCLAW_TRUSTED_PROXIES": (
                        " 127.0.0.1, ::1, 127.0.0.1, 0:0:0:0:0:0:0:1,"
                        "10.20.30.0/24, 2001:db8::/64"
                    ),
                },
                destination=Path(raw) / "openclaw.json",
            )

        self.assertEqual(
            config["gateway"]["trustedProxies"],
            ["127.0.0.1", "::1", "10.20.30.0/24", "2001:db8::/64"],
        )
        self.assertEqual(config["gateway"]["auth"]["mode"], "token")
        self.assertNotIn("allowRealIpFallback", config["gateway"])

    def test_gateway_has_no_implicit_trusted_proxies(self) -> None:
        for environ in ({}, {"OPENCLAW_TRUSTED_PROXIES": "  "}):
            with self.subTest(environ=environ), tempfile.TemporaryDirectory() as raw:
                config, _, _ = build_config(
                    {"HOME": raw, **environ},
                    destination=Path(raw) / "openclaw.json",
                )
                self.assertNotIn("trustedProxies", config["gateway"])

    def test_gateway_rejects_invalid_proxy_addresses(self) -> None:
        for value in (
            "https://proxy.example.test", "proxy.example.test", "*",
            "127.0.0.1:18789", "127.0.0.1,", "10.0.0.0/33", "fe80::1%eth0",
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                with self.assertRaisesRegex(ValueError, "OPENCLAW_TRUSTED_PROXIES"):
                    build_config(
                        {"HOME": raw, "OPENCLAW_TRUSTED_PROXIES": value},
                        destination=Path(raw) / "openclaw.json",
                    )

    def test_gateway_auto_adds_cloudflare_and_tailscale_origins_once(self) -> None:
        status = {
            "Self": {
                "DNSName": "runtime.example.ts.net.",
                "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::10"],
            }
        }

        def tailscale_runner(
            *_args: object,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(status))

        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_GATEWAY_PORT": "19000",
                    "OPENCLAW_GATEWAY_PUBLISH_PORT": "29000",
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": (
                        "http://localhost:19000,"
                        "http://runtime.example.ts.net:19000"
                    ),
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS_AUTO": "1",
                    "CITADEL_CLOUDFLARE": "1",
                    "CITADEL_CLOUDFLARE_DOMAIN": "Services.Example.Test.",
                    "TS_HOSTNAME": "configured.example.ts.net",
                },
                destination=Path(raw) / "openclaw.json",
                tailscale_runner=tailscale_runner,
            )

        origins = config["gateway"]["controlUi"]["allowedOrigins"]
        self.assertIn("https://19000.services.example.test", origins)
        for host in (
            "runtime.example.ts.net",
            "100.64.0.10",
            "[fd7a:115c:a1e0::10]",
            "configured.example.ts.net",
        ):
            self.assertIn(f"http://{host}:19000", origins)
            self.assertIn(f"http://{host}:29000", origins)
        self.assertEqual(origins.count("http://runtime.example.ts.net:19000"), 1)

    def test_gateway_auto_discovery_failures_are_skipped(self) -> None:
        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("tailscale unavailable")

        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": (
                        "https://control.example.test"
                    ),
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS_AUTO": "1",
                    "CITADEL_CLOUDFLARE": "1",
                    "CITADEL_CLOUDFLARE_DOMAIN": "invalid domain",
                    "OPENCLAW_GATEWAY_PUBLISH_PORT": "invalid",
                    "TS_HOSTNAME": "configured.example.ts.net",
                },
                destination=Path(raw) / "openclaw.json",
                tailscale_runner=unavailable,
            )

        self.assertEqual(
            config["gateway"]["controlUi"]["allowedOrigins"],
            ["https://control.example.test"],
        )

    def test_gateway_auto_zero_keeps_csv_exact(self) -> None:
        def must_not_run(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("tailscale discovery must be disabled")

        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": (
                        "https://control.example.test"
                    ),
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS_AUTO": "0",
                    "CITADEL_CLOUDFLARE": "1",
                    "CITADEL_CLOUDFLARE_DOMAIN": "services.example.test",
                    "TS_HOSTNAME": "configured.example.ts.net",
                },
                destination=Path(raw) / "openclaw.json",
                tailscale_runner=must_not_run,
            )

        self.assertEqual(
            config["gateway"]["controlUi"]["allowedOrigins"],
            ["https://control.example.test"],
        )

    def test_gateway_rejects_non_origin_control_ui_entries(self) -> None:
        for value in (
            "https://*.example.test",
            "https://example.test/control-ui",
            "example.test",
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                with self.assertRaisesRegex(
                    ValueError,
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS",
                ):
                    build_config(
                        {
                            "HOME": raw,
                            "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": value,
                        },
                        destination=Path(raw) / "openclaw.json",
                    )

    def test_custom_provider_secret_is_only_an_env_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {"HOME": raw},
                destination=Path(raw) / "openclaw.json",
                openai_v1_providers=(provider(),),
            )
            key = config["models"]["providers"]["litellm"]["apiKey"]
            self.assertEqual(
                key,
                {
                    "source": "env",
                    "provider": "default",
                    "id": "OPENAI_V1_KEY",
                },
            )


class CompleteConfigureTests(unittest.TestCase):
    def test_state_directory_selects_config_after_explicit_path_overrides(self) -> None:
        for override in ("state", "config-path", "config", "legacy-home"):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                environ = {
                    "HOME": str(root / "home"),
                    "OPENCLAW_HOME": str(root / "legacy-home"),
                    "OPENCLAW_STATE_DIR": str(root / "state"),
                    "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": CONTROL_UI_ORIGINS,
                }
                expected = root / "state" / "openclaw.json"
                if override in {"config-path", "config"}:
                    expected = root / "explicit-path" / "openclaw.json"
                    environ["OPENCLAW_CONFIG_PATH"] = str(expected)
                if override == "config":
                    expected = root / "explicit-config" / "openclaw.json"
                    environ["OPENCLAW_CONFIG"] = str(expected)
                if override == "legacy-home":
                    environ["OPENCLAW_STATE_DIR"] = " "
                    expected = root / "legacy-home" / "openclaw.json"
                expected.parent.mkdir(parents=True)
                expected.write_text('{"discarded": true}\n', encoding="utf-8")
                with (
                    patch(
                        "openclaw_ephemeral.configuration.discover_native_models",
                        return_value=((), ()),
                    ),
                    patch(
                        "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                        return_value=((), ()),
                    ),
                ):
                    result = configure(environ)
                self.assertEqual(result.path, expected)
                written = json.loads(expected.read_text(encoding="utf-8"))
                self.assertNotIn("discarded", written)
                self.assertEqual(
                    written["agents"]["defaults"]["workspace"],
                    str(expected.parent / "workspace"),
                )

    def test_configure_reports_mcp_count_without_serializing_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "openclaw.json"
            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=((), ()),
                ),
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=((), ()),
                ),
            ):
                result = configure(
                    {
                        "HOME": raw,
                        "OPENCLAW_CONFIG": str(destination),
                        "MCP_SERVER_URL": "https://mcp.example/mcp",
                        "MCP_SERVER_BEARER": "mcp-secret",
                    }
                )

            self.assertEqual(result.mcp_server_count, 1)
            serialized = destination.read_text(encoding="utf-8")
            self.assertIn("Bearer ${MCP_SERVER_BEARER}", serialized)
            self.assertNotIn("mcp-secret", serialized)

    def test_existing_openai_oauth_database_enables_openai_models(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "state" / "openclaw.json"
            auth_db = (
                destination.parent
                / "agents"
                / "main"
                / "agent"
                / "openclaw-agent.sqlite"
            )
            auth_db.parent.mkdir(parents=True)
            auth_db.write_bytes(b"oauth-state")

            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=((), ()),
                ),
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=((), ()),
                ),
            ):
                configure(
                    {
                        "HOME": raw,
                        "OPENCLAW_CONFIG": str(destination),
                    }
                )

            written = json.loads(destination.read_text(encoding="utf-8"))
            self.assertIn(
                "openai/*",
                written["agents"]["defaults"]["models"],
            )

    def test_shared_or_custom_agent_auth_store_keeps_direct_models_visible(self) -> None:
        for location in ("shared", "custom-agent", "legacy-main"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                destination = root / "config" / "openclaw.json"
                environ = {
                    "HOME": raw,
                    "OPENCLAW_CONFIG": str(destination),
                    "OPENCLAW_STATE_DIR": str(root / "persistent-state"),
                    "OPENCLAW_AGENT_DIR": str(root / "persistent-agent"),
                    "OPENCLAW_MODEL": "litellm/chosen-model",
                }
                auth_db = {
                    "shared": root / "persistent-state" / "state" / "openclaw.sqlite",
                    "custom-agent": root / "persistent-agent" / "openclaw-agent.sqlite",
                    "legacy-main": root / "persistent-state" / "agents" / "main" / "agent" / "openclaw-agent.sqlite",
                }[location]
                auth_db.parent.mkdir(parents=True)
                with sqlite3.connect(auth_db) as database:
                    database.execute("CREATE TABLE opaque_auth (value TEXT)")
                    database.execute("INSERT INTO opaque_auth VALUES ('persisted-auth')")
                original_auth = auth_db.read_bytes()
                with (
                    patch(
                        "openclaw_ephemeral.configuration.discover_native_models",
                        return_value=((), ()),
                    ),
                    patch(
                        "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                        return_value=((), ()),
                    ),
                ):
                    configure(environ)
                written = json.loads(destination.read_text(encoding="utf-8"))
                self.assertIn("openai/*", written["agents"]["defaults"]["models"])
                self.assertEqual(
                    written["agents"]["defaults"]["model"]["primary"],
                    "litellm/chosen-model",
                )
                self.assertEqual(auth_db.read_bytes(), original_auth)

    def test_configure_replaces_destination_without_reading_or_merging_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "state" / "openclaw.json"
            destination.parent.mkdir()
            destination.write_text(
                '{"discarded": true, "plaintext": "old-secret"}\n',
                encoding="utf-8",
            )
            environ = {
                "HOME": str(root),
                "OPENCLAW_CONFIG": str(destination),
                "OPENCLAW_GATEWAY_TOKEN": "new-gateway-secret",
                "ANTHROPIC_API_KEY": "native-provider-secret",
            }

            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=(("anthropic/claude-a",), ()),
                ) as native,
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=((), ()),
                ) as custom,
            ):
                result = configure(environ)

            written = json.loads(destination.read_text(encoding="utf-8"))
            self.assertNotIn("discarded", written)
            self.assertNotIn("plaintext", written)
            serialized = destination.read_text(encoding="utf-8")
            self.assertNotIn("old-secret", serialized)
            self.assertNotIn("new-gateway-secret", serialized)
            self.assertNotIn("native-provider-secret", serialized)
            self.assertEqual(result.native_model_count, 1)
            self.assertEqual(
                stat.S_IMODE(destination.stat().st_mode),
                0o600,
            )
            self.assertEqual(list(destination.parent.glob("*.tmp")), [])
            native.assert_called_once()
            custom.assert_called_once()

    def test_configure_reports_discovery_counts_and_generic_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "openclaw.json"
            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=(("gemini/model",), ("native warning",)),
                ),
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=(
                        (provider(models=("one", "two")),),
                        ("custom warning",),
                    ),
                ),
            ):
                result = configure(
                    {
                        "HOME": raw,
                        "OPENCLAW_CONFIG": str(destination),
                        "OPENAI_V1_KEY": "custom-secret",
                    }
                )

            self.assertEqual(result.native_model_count, 1)
            self.assertEqual(result.openai_v1_provider_count, 1)
            self.assertEqual(result.openai_v1_model_count, 2)
            self.assertEqual(
                result.warnings,
                ("native warning", "custom warning"),
            )
            self.assertNotIn(
                "custom-secret",
                destination.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
