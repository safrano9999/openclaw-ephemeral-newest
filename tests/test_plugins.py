from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from openclaw_ephemeral.configuration import configure
from openclaw_ephemeral.environment import ConfigurationError
from openclaw_ephemeral.plugins import (
    OpenClawPlugin,
    discover_openclaw_plugins,
    register_openclaw_plugins,
    restore_image_plugin_installs,
)


class ManagedPluginDiscoveryTests(unittest.TestCase):
    def test_image_upgrade_refreshes_plugin_metadata_and_preserves_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            records = {}
            for name in ("codex", "brave", "note"):
                path = root / "image" / name
                path.mkdir(parents=True)
                (path / "openclaw.plugin.json").write_text(json.dumps({"id": name}))
                (path / "package.json").write_text(json.dumps({
                    "version": "2026.9.3" if name != "note" else "operator-version",
                }))
                records[name] = {"installPath": str(path), "version": "2026.9.3"}
            previous = {
                "codex": {**records["codex"], "version": "2026.9.2"},
                "brave": {"installPath": str(root / "operator-brave"), "version": "custom"},
                "note": {**records["note"], "version": "operator-version"},
                "custom": {"installPath": str(root / "custom"), "version": "1"},
            }
            database_path = root / "state" / "openclaw.sqlite"
            database_path.parent.mkdir()
            with sqlite3.connect(database_path) as database:
                database.execute(
                    "CREATE TABLE config_machine_state (state_key TEXT PRIMARY KEY, value_json TEXT)"
                )
                database.execute("INSERT INTO config_machine_state VALUES (?, ?)", (
                    "plugins.installedIndex",
                    json.dumps({"revision": 1, "index": {"installRecords": previous}}),
                ))
                database.execute("INSERT INTO config_machine_state VALUES ('private-state', 'preserve-me')")
            seed_path = root / "image-plugin-installs.json"
            seed_path.write_text(json.dumps({"schemaVersion": 1, "installRecords": records}))

            def read_rows():
                with sqlite3.connect(database_path) as database:
                    return dict(database.execute("SELECT state_key, value_json FROM config_machine_state"))

            arguments = {"destination": root / "openclaw.json", "seed_path": seed_path}
            restore_image_plugin_installs({"OPENCLAW_STATE_DIR": raw}, **arguments)
            updated = read_rows()
            ledger = json.loads(updated["plugins.installedIndex"])
            self.assertEqual(ledger["revision"], 2)
            self.assertEqual(ledger["index"]["installRecords"], {**previous, "codex": records["codex"]})
            self.assertEqual(updated["private-state"], "preserve-me")
            restore_image_plugin_installs({"OPENCLAW_STATE_DIR": raw}, **arguments)
            self.assertEqual(read_rows(), updated)

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
                current = json.loads(destination.read_text(encoding="utf-8"))
                # OpenClaw config writes persist keyed entries, never its internal list projection.
                self.assertNotIn("list", current["agents"])
                current["agents"]["entries"]["main"]["tools"] = {}
                destination.write_text(json.dumps(current), encoding="utf-8")
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
                str(paths["brave"]),
            })
            for name in ("codex", "brave"):
                self.assertTrue(config["plugins"]["entries"][name]["enabled"])
            self.assertNotIn("installs", config["plugins"])
            self.assertNotIn("allow", config["plugins"])
            self.assertEqual(config["agents"]["defaults"]["model"]["primary"], "luna")
            self.assertTrue(config["commands"]["mcp"])
            self.assertEqual(config["agents"]["entries"]["main"]["tools"], {"allow": ["*"], "deny": []})
            self.assertEqual(calls, [["openclaw", "plugins", "registry", "--refresh"]])
            self.assertEqual(database_path.read_bytes(), original_database)

    def test_codex_stays_enabled_without_an_explicit_path_on_reregistration(self) -> None:
        codex_path = Path("/root/.openclaw/extensions/codex")
        note_path = "/root/.openclaw/extensions/note"
        config = {"plugins": {"load": {"paths": [str(codex_path), note_path]}}}
        plugin = OpenClawPlugin("codex", "codex", codex_path, {}, None)

        for _ in range(2):
            registered = register_openclaw_plugins(
                config, [plugin], environ={},
                destination=Path("/root/.openclaw/openclaw.json"),
            )
            self.assertEqual(registered, ("codex",))
            self.assertEqual(config["plugins"]["load"]["paths"], [note_path])
            self.assertTrue(config["plugins"]["entries"]["codex"]["enabled"])

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
