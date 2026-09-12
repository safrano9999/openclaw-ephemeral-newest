from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from openclaw_ephemeral.environment import ConfigurationError
from openclaw_ephemeral.scheduling import (
    CRON_CLI_TIMEOUT_SECONDS,
    CRON_COMMAND_TIMEOUT_SECONDS,
    LocalTime,
    _add_cron_job,
    _approve_current_device,
    _list_cron_jobs,
    _remove_cron_job,
)


ROOT = Path(__file__).resolve().parents[1]


class CronReadinessTests(unittest.TestCase):
    def pairing_environment(self) -> dict[str, str]:
        return {"OPENCLAW_DEVICE_BOOTSTRAP_MODULE": str(Path(__file__).resolve())}

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the pairing bridge")
    def test_pairing_uses_only_current_sqlite_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            with sqlite3.connect(state / "openclaw.sqlite") as database:
                database.execute(
                    "CREATE TABLE device_identities (identity_key TEXT, device_id TEXT)"
                )
                database.execute(
                    "INSERT INTO device_identities VALUES ('primary', 'current-device')"
                )
            module = root / "bootstrap.mjs"
            module.write_text('''
import fs from "node:fs";
export async function listDevicePairing() {
  return { pending: [
    { deviceId: "another-device", requestId: "unrelated" },
    { deviceId: "current-device", requestId: "current" },
    { deviceId: "override-device", requestId: "override" },
  ] };
}
export async function approveDevicePairing(requestId, options) {
  fs.appendFileSync(process.env.TEST_APPROVAL_OUTPUT, JSON.stringify({ requestId, options }) + "\\n");
}
''', encoding="utf-8")
            output = root / "approval.json"
            environ = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": raw,
                "OPENCLAW_STATE_DIR": raw,
                "OPENCLAW_DEVICE_BOOTSTRAP_MODULE": str(module),
                "TEST_APPROVAL_OUTPUT": str(output),
            }
            legacy = root / "identity" / "device.json"
            legacy.parent.mkdir()
            legacy.write_text('{"deviceId":"another-device"}', encoding="utf-8")
            _approve_current_device(environ, runner=subprocess.run)
            override = root / "explicit-identity.json"
            override.write_text('{"deviceId":"override-device"}', encoding="utf-8")
            _approve_current_device(
                {**environ, "OPENCLAW_DEVICE_IDENTITY": str(override)}, runner=subprocess.run,
            )
            approvals = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual([row["requestId"] for row in approvals], ["current", "override"])
            for approval in approvals:
                self.assertEqual(approval["options"]["callerScopes"], [
                    "operator.admin", "operator.pairing", "operator.read", "operator.write",
                ])

    def test_lists_once_after_systemd_readiness(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return subprocess.CompletedProcess(arguments, 0, '{"jobs": []}', "")

        self.assertEqual(
            _list_cron_jobs(self.pairing_environment(), runner=runner),
            (),
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], "node")
        self.assertEqual(calls[0][1]["timeout"], CRON_CLI_TIMEOUT_SECONDS)
        self.assertEqual(calls[1][1]["timeout"], CRON_CLI_TIMEOUT_SECONDS)

    def test_pairing_timeout_is_bounded_and_best_effort(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if arguments[0] == "node":
                raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
            return subprocess.CompletedProcess(arguments, 0, '{"jobs": []}', "")

        self.assertEqual(
            _list_cron_jobs(self.pairing_environment(), runner=runner),
            (),
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], "node")
        self.assertEqual(calls[0][1]["timeout"], CRON_CLI_TIMEOUT_SECONDS)
        self.assertEqual(calls[1][0][1:3], ["cron", "list"])

    def test_failed_list_is_fail_closed_without_retry(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return subprocess.CompletedProcess(arguments, 1, "", "gateway error")

        with self.assertRaisesRegex(ConfigurationError, "gateway error"):
            _list_cron_jobs(self.pairing_environment(), runner=runner)
        self.assertEqual(len(calls), 2)

    def test_timeout_is_fail_closed_without_retry(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if arguments[0] == "node":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

        with self.assertRaisesRegex(ConfigurationError, "timed out"):
            _list_cron_jobs(self.pairing_environment(), runner=runner)
        self.assertEqual(len(calls), 2)

    def test_remove_has_bounded_cli_timeout(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return subprocess.CompletedProcess(arguments, 0, "", "")

        _remove_cron_job("job-id", {}, runner=runner)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1:3], ["cron", "rm"])
        self.assertEqual(calls[0][1]["timeout"], CRON_CLI_TIMEOUT_SECONDS)

    def test_remove_timeout_is_reported_without_retry(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

        with self.assertRaisesRegex(ConfigurationError, "cron rm timed out"):
            _remove_cron_job("job-id", {}, runner=runner)
        self.assertEqual(len(calls), 1)

    def test_add_has_bounded_cli_timeout_separate_from_payload_timeout(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return subprocess.CompletedProcess(arguments, 0, "", "")

        _add_cron_job(
            LocalTime(hour=19, minute=0),
            ["/usr/local/bin/openclaw-ephemeral.py", "webhook", "--webhook", "WEBHOOK_URL"],
            {},
            runner=runner,
        )

        self.assertEqual(len(calls), 1)
        arguments, kwargs = calls[0]
        self.assertEqual(arguments[1:3], ["cron", "add"])
        self.assertEqual(kwargs["timeout"], CRON_CLI_TIMEOUT_SECONDS)
        payload_timeout_index = arguments.index("--timeout-seconds") + 1
        self.assertEqual(
            arguments[payload_timeout_index],
            str(CRON_COMMAND_TIMEOUT_SECONDS),
        )
        self.assertNotEqual(
            kwargs["timeout"],
            int(arguments[payload_timeout_index]),
        )

    def test_add_timeout_is_reported_without_pairing_retry(self) -> None:
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

        with self.assertRaisesRegex(ConfigurationError, "cron add timed out"):
            _add_cron_job(
                LocalTime(hour=7, minute=0),
                ["/usr/local/bin/openclaw-ephemeral.py", "webhook", "--webhook", "WEBHOOK_URL"],
                self.pairing_environment(),
                runner=runner,
            )
        self.assertEqual(len(calls), 1)

    def test_systemd_schedule_is_last_without_starting_optional_apps(self) -> None:
        unit = (
            ROOT
            / "image/runtime/etc/systemd/system/openclaw-ephemeral-schedule.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Requires=openclaw.service", unit)
        self.assertIn("After=openclaw.service", unit)
        self.assertNotIn("Wants=", unit)
        for service in (
            "codeanalyst.service",
            "kachelmann-webui.service",
            "jugo.service",
            "kiwix-bridge.service",
            "napoleon.service",
            "naturalgrounding.service",
            "pvdach.service",
            "spanker-webui.service",
            "vikai-bootstrap-openclaw-agents.service",
        ):
            self.assertIn(service, unit)
        self.assertNotIn("After=citadel-scan.service", unit)

    def test_configuration_unit_is_owned_by_openclaw_ephemeral(self) -> None:
        unit = (
            ROOT / "image/runtime/etc/systemd/system/openclaw-config.service"
        ).read_text(encoding="utf-8")
        self.assertIn("Requires=persistainer.service", unit)
        self.assertIn("fedora45-ai-init-hooks.service", unit)
        self.assertIn("tailscale-up.service", unit)
        self.assertIn("Before=openclaw.service", unit)


if __name__ == "__main__":
    unittest.main()
