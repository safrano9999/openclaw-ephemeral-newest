from __future__ import annotations

import io
import json
import subprocess
import unittest

from openclaw_ephemeral.cli import main
from openclaw_ephemeral.environment import ConfigurationError
from openclaw_ephemeral.scheduling import (
    CRON_COMMAND_TIMEOUT_SECONDS, CRON_NAME_PREFIX, build_schedule_plan,
    reconcile_cron_jobs, schedule,
)
from openclaw_ephemeral.scheduling import discover_webhooks, dispatch_webhook


class WebhookTests(unittest.TestCase):
    def test_optional_defaults_numeric_order_and_vienna_times(self):
        env = {"WEBHOOK_URL_10": "https://ten.invalid", "WEBHOOK_INIT_10": "true",
               "WEBHOOK_URL_02": "https://two.invalid", "WEBHOOK_URL": "https://one.invalid",
               "WEBHOOK_URL_03": "", "WEBHOOK_TIMES": "CET 07:10,13:10,19:10,07:10"}
        plan = build_schedule_plan(env)
        self.assertEqual([entry.hook.key for entry in plan.webhooks],
                         ["WEBHOOK_URL", "WEBHOOK_URL_02", "WEBHOOK_URL_10"])
        self.assertEqual([t.expression for t in plan.webhooks[0].times],
                         ["10 7 * * *", "10 13 * * *", "10 19 * * *"])
        self.assertEqual(plan.webhooks[1].times[0].expression, "0 0 * * *")
        self.assertFalse(plan.webhooks[0].hook.init)
        self.assertTrue(plan.webhooks[2].hook.init)

    def test_invalid_fields_do_not_mutate_cron_jobs(self):
        for fields in ({"WEBHOOK_INIT": "1"}, {"WEBHOOK_TIMES": "25:00"},
                       {"WEBHOOK_BEARER": "secret\nheader: value"}, {"WEBHOOK_URL": "file:///etc/passwd"}):
            calls = []
            with self.assertRaises(ConfigurationError):
                schedule({"WEBHOOK_URL": "https://example.invalid", **fields},
                         runner=lambda *args, **kwargs: calls.append(args))
            self.assertEqual(calls, [])

    def test_cron_migration_is_owned_scoped_idempotent_and_secret_free(self):
        env = {"WEBHOOK_URL": "https://example.invalid", "WEBHOOK_BEARER": "secret",
               "WEBHOOK_TIMES": "07:10,13:10,19:10"}
        jobs = [{"id": "old", "name": "openclaw-ephemeral-repositories-europe-vienna-0710"},
                {"id": "unrelated", "name": "user-reminder"}]
        mutations = []
        def runner(argv, **kwargs):
            if argv[1:3] == ["cron", "list"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"jobs": jobs}), "")
            if argv[1:3] == ["cron", "rm"]:
                mutations.append(("rm", argv[3]))
                jobs[:] = [job for job in jobs if job["id"] != argv[3]]
            elif argv[1:3] == ["cron", "add"]:
                field = lambda flag: argv[argv.index(flag) + 1]
                mutations.append(("add", field("--name")))
                self.assertEqual(field("--tz"), "Europe/Vienna")
                payload = json.loads(field("--command-argv"))
                self.assertNotIn("secret", json.dumps(payload))
                self.assertNotIn(env["WEBHOOK_URL"], json.dumps(payload))
                jobs.append({"id": field("--name"), "name": field("--name"), "enabled": True,
                             "agentId": "main", "sessionTarget": "isolated", "wakeMode": "now",
                             "schedule": {"kind": "cron", "expr": field("--cron"),
                                          "tz": "Europe/Vienna", "staggerMs": 0},
                             "payload": {"kind": "command", "argv": payload,
                                         "timeoutSeconds": CRON_COMMAND_TIMEOUT_SECONDS},
                             "delivery": {"mode": "none"}})
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        plan = build_schedule_plan(env)
        self.assertEqual(reconcile_cron_jobs(plan, env, runner=runner), (0, 1, 3))
        self.assertEqual(reconcile_cron_jobs(plan, env, runner=runner), (3, 0, 0))
        self.assertEqual(len(mutations), 4)
        self.assertIn("unrelated", [job["id"] for job in jobs])
        self.assertEqual(reconcile_cron_jobs(build_schedule_plan({}), {}, runner=runner), (0, 3, 0))

    def test_init_calls_only_true_groups_in_order_and_continues_after_failure(self):
        env = {"WEBHOOK_URL_03": "https://three.invalid", "WEBHOOK_INIT_03": "true",
               "WEBHOOK_URL_02": "https://two.invalid", "WEBHOOK_INIT_02": "false",
               "WEBHOOK_URL": "https://one.invalid", "WEBHOOK_INIT": "true"}
        requests = []
        def runner(argv, **kwargs):
            if argv[0] == "curl":
                requests.append(argv[-1])
                if len(requests) == 1:
                    raise subprocess.CalledProcessError(22, argv)
            return subprocess.CompletedProcess(argv, 0, '{"jobs": [], "delivered": true}', "")
        with self.assertRaisesRegex(ConfigurationError, "WEBHOOK_URL: HTTP request failed"):
            schedule(env, runner=runner)
        self.assertEqual(requests, ["https://one.invalid", "https://three.invalid"])

    def test_auth_is_optional_and_check_output_is_delivered_once(self):
        response = "✅ OpenClaw version 2026.9.4 is actual\n✅ Hermes version 0.21.2 is actual\n"
        env = {"WEBHOOK_URL": "https://example.invalid/webhook?--check",
               "OPENCLAW_TELEGRAM_CHAT_ID": "test-target"}
        for bearer in ("", 'a"b\\c'):
            calls = []
            def runner(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, response if argv[0] == "curl" else "{}", "")
            target_env = {**env, "WEBHOOK_BEARER": bearer}
            output = io.StringIO()
            self.assertEqual(main(["webhook", "--webhook", "WEBHOOK_URL"],
                                  environ=target_env, runner=runner, stdout=output), 0)
            self.assertEqual(output.getvalue(), response)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0][-1], env["WEBHOOK_URL"])
            self.assertEqual(calls[0][1]["input"],
                             'header = ' + json.dumps('Authorization: Bearer ' + bearer) + '\n' if bearer else '')
            self.assertEqual(calls[1][0][-1], response.strip())

    def test_module_delivery_acknowledgements_never_send_again(self):
        env = {"WEBHOOK_URL": "http://127.0.0.1:18789/plugins/welcome",
               "OPENCLAW_TELEGRAM_CHAT_ID": "test-target"}
        for delivered in (True, False):
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps({"delivered": delivered}), "")
            self.assertEqual(dispatch_webhook(discover_webhooks(env)[0], env, runner=runner), "")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
