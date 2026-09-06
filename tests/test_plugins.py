from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from openclaw_ephemeral.configuration import configure
from openclaw_ephemeral.environment import ConfigurationError
from openclaw_ephemeral.plugins import discover_openclaw_plugins


class ManagedPluginDiscoveryTests(unittest.TestCase):
    def test_configure_registers_only_ledger_owned_managed_plugin_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "persistent-state"
            destination = root / "config" / "openclaw.json"
            destination.parent.mkdir()
            destination.write_text("not a readable prior config", encoding="utf-8")
            paths = {
                name: state / "npm" / "projects" / name / "node_modules" / "@openclaw" / name
                for name in ("codex", "brave", "retired")
            }
            paths["dependency"] = paths["codex"] / "node_modules" / "dependency"
            for name, path in paths.items():
                path.mkdir(parents=True)
                (path / "openclaw.plugin.json").write_text(
                    json.dumps({"id": name, "configSchema": {"type": "object"}}),
                    encoding="utf-8",
                )
            ledger = {"revision": 1, "index": {"installRecords": {
                name: {"source": "npm", "installPath": str(paths[name])}
                for name in ("codex", "brave")
            }}}
            database_path = state / "state" / "openclaw.sqlite"
            database_path.parent.mkdir()
            with sqlite3.connect(database_path) as database:
                database.execute(
                    "CREATE TABLE config_machine_state (state_key TEXT PRIMARY KEY, value_json TEXT)"
                )
                database.execute(
                    "INSERT INTO config_machine_state VALUES (?, ?)",
                    ("plugins.installedIndex", json.dumps(ledger)),
                )
                database.execute(
                    "INSERT INTO config_machine_state VALUES ('private-unrelated', 'not-json')"
                )
            original_database = database_path.read_bytes()
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

            result = configure({
                "HOME": raw,
                "OPENCLAW_CONFIG": str(destination),
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_MODEL": "luna",
                "OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS": "http://localhost:18789",
            }, runner=runner)
            config = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(result.plugin_count, 2)
            self.assertEqual(set(config["plugins"]["load"]["paths"]), {
                str(paths["codex"]), str(paths["brave"]),
            })
            for name in ("codex", "brave"):
                self.assertTrue(config["plugins"]["entries"][name]["enabled"])
            self.assertNotIn("installs", config["plugins"])
            self.assertNotIn("allow", config["plugins"])
            self.assertEqual(config["agents"]["defaults"]["model"]["primary"], "luna")
            self.assertTrue(config["commands"]["mcp"])
            self.assertEqual(config["agents"]["list"][0]["tools"], {"allow": ["*"], "deny": []})
            self.assertEqual(calls, [["openclaw", "plugins", "registry", "--refresh"]])
            self.assertEqual(database_path.read_bytes(), original_database)

    def test_invalid_canonical_metadata_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "state").mkdir()
            with sqlite3.connect(root / "state" / "openclaw.sqlite") as database:
                database.execute("CREATE TABLE config_machine_state (state_key TEXT, value_json TEXT)")
                database.execute(
                    "INSERT INTO config_machine_state VALUES ('plugins.installedIndex', 'invalid')"
                )
            with self.assertRaisesRegex(ConfigurationError, "installed-plugin metadata"):
                discover_openclaw_plugins(
                    {"HOME": raw, "OPENCLAW_STATE_DIR": raw},
                    destination=root / "openclaw.json",
                )


if __name__ == "__main__":
    unittest.main()
