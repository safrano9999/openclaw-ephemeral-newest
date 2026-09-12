"""Generic webhook schedules and idempotent OpenClaw cron reconciliation."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import urlsplit

from .environment import ConfigurationError, clean, integer, openclaw_command


URL_KEY = re.compile(r"^WEBHOOK_URL(?P<suffix>_\d{2,})?$")


@dataclass(frozen=True)
class Webhook:
    key: str
    url: str
    bearer: str
    times: str
    init: bool


def discover_webhooks(environ: Mapping[str, str]) -> tuple[Webhook, ...]:
    keys = sorted((key for key in environ if URL_KEY.fullmatch(key)),
                  key=lambda key: int(key.removeprefix("WEBHOOK_URL_") or "1")
                  if key != "WEBHOOK_URL" else 1)
    hooks = []
    for key in keys:
        url = clean(environ.get(key))
        if not url:
            continue
        suffix = key.removeprefix("WEBHOOK_URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError(f"{key} must be an HTTP or HTTPS URL")
        init = clean(environ.get(f"WEBHOOK_INIT{suffix}")) or "false"
        if init not in {"true", "false"}:
            raise ConfigurationError(f"WEBHOOK_INIT{suffix} must be true or false")
        bearer = clean(environ.get(f"WEBHOOK_BEARER{suffix}"))
        if "\n" in bearer or "\r" in bearer:
            raise ConfigurationError(f"WEBHOOK_BEARER{suffix} must be a single line")
        hooks.append(Webhook(key, url, bearer,
                             clean(environ.get(f"WEBHOOK_TIMES{suffix}")) or "00:00",
                             init == "true"))
    return tuple(hooks)


def dispatch_webhook(
    hook: Webhook,
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """POST once; endpoints that own delivery return their own acknowledgement."""
    config = ("header = " + json.dumps(f"Authorization: Bearer {hook.bearer}") + "\n"
              if hook.bearer else "")
    try:
        result = runner(
            ["curl", "--silent", "--show-error", "--fail-with-body", "--max-time", "300",
             "--config", "-", "--request", "POST", "--url", hook.url],
            input=config, capture_output=True, text=True, check=True, timeout=310,
            env=dict(environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError(f"{hook.key}: HTTP request failed") from exc
    text = result.stdout.strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    # Existing module hooks send their own reports, including conditional delivery.
    # Their acknowledgement must never produce a duplicate Telegram message.
    if isinstance(payload, dict) and "delivered" in payload:
        return ""
    target = clean(environ.get("OPENCLAW_TELEGRAM_CHAT_ID"))
    if text and target:
        try:
            runner([*openclaw_command(environ), "message", "send", "--channel", "telegram",
                    "--target", target, "--message", text],
                   capture_output=True, text=True, check=True, timeout=90, env=dict(environ))
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigurationError(f"{hook.key}: Telegram delivery failed") from exc
    return text


CRON_TIMEZONE = "Europe/Vienna"
CRON_NAME_PREFIX = "openclaw-ephemeral-webhook-"
LEGACY_CRON_NAME_PREFIX = "openclaw-ephemeral-repositories-"
CRON_CLI_TIMEOUT_SECONDS = 30
CRON_COMMAND_TIMEOUT_SECONDS = 3_600
TIME_SPEC = re.compile(
    r"^(?:(?:CET|CEST|Europe/Vienna)\s+)?"
    r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)$",
    re.IGNORECASE,
)
PAIR_CURRENT_DEVICE_SOURCE = r"""
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";

const stateDir = process.env.OPENCLAW_STATE_DIR
  || process.env.OPENCLAW_CONFIG_DIR
  || path.join(process.env.HOME || "/root", ".openclaw");
const modulePath = process.argv[1];
const bootstrap = await import(
  modulePath.startsWith("file:") ? modulePath : pathToFileURL(modulePath).href
);
let deviceId;
if (process.env.OPENCLAW_DEVICE_IDENTITY) {
  deviceId = JSON.parse(fs.readFileSync(process.env.OPENCLAW_DEVICE_IDENTITY, "utf8")).deviceId;
} else {
  // v2026.9.2 stores the CLI identity in SQLite. Read only its public id.
  const database = new DatabaseSync(path.join(stateDir, "state", "openclaw.sqlite"), { readOnly: true });
  try {
    deviceId = database.prepare("SELECT device_id FROM device_identities WHERE identity_key = 'primary'").get()?.device_id;
  } finally {
    database.close();
  }
}
if (!deviceId) throw new Error("The current OpenClaw device identity is missing");
const { pending } = await bootstrap.listDevicePairing();
for (const request of pending.filter((item) => item.deviceId === deviceId)) {
  await bootstrap.approveDevicePairing(request.requestId, {
    callerScopes: [
      "operator.admin",
      "operator.pairing",
      "operator.read",
      "operator.write",
    ],
  });
}
""".strip()


@dataclass(frozen=True)
class LocalTime:
    hour: int
    minute: int

    @property
    def expression(self) -> str:
        return f"{self.minute} {self.hour} * * *"

    @property
    def label(self) -> str:
        return f"{self.hour:02d}{self.minute:02d}"


@dataclass(frozen=True)
class ScheduledWebhook:
    hook: Webhook
    times: tuple[LocalTime, ...]


@dataclass(frozen=True)
class SchedulePlan:
    webhooks: tuple[ScheduledWebhook, ...]


@dataclass(frozen=True)
class ScheduleResult:
    kept_jobs: int
    removed_jobs: int
    added_jobs: int
    initialized_webhooks: tuple[str, ...]
    outputs: tuple[str, ...] = ()


def crontab_times(raw: str) -> tuple[LocalTime, ...]:
    """Parse Vienna wall-clock values; the IANA zone provides CET/CEST switching."""

    value = clean(raw)
    if not value:
        return ()
    parsed: list[LocalTime] = []
    seen: set[tuple[int, int]] = set()
    for item in value.split(","):
        candidate = item.strip()
        match = TIME_SPEC.fullmatch(candidate)
        if match is None:
            raise ConfigurationError(
                f"WEBHOOK_TIMES entry {candidate!r} must be HH:MM, "
                "CET HH:MM, CEST HH:MM, or Europe/Vienna HH:MM"
            )
        local_time = LocalTime(
            hour=int(match.group("hour"), 10),
            minute=int(match.group("minute"), 10),
        )
        key = (local_time.hour, local_time.minute)
        if key not in seen:
            seen.add(key)
            parsed.append(local_time)
    return tuple(parsed)


def build_schedule_plan(environ: Mapping[str, str]) -> SchedulePlan:
    return SchedulePlan(tuple(ScheduledWebhook(hook, crontab_times(hook.times))
                              for hook in discover_webhooks(environ)))


def scheduling_requested(environ: Mapping[str, str]) -> bool:
    return any(key.startswith("WEBHOOK_URL") for key in environ)


def ephemeral_command() -> list[str]:
    return ["/usr/local/bin/openclaw-ephemeral.py"]


def _gateway_auth(environ: Mapping[str, str]) -> list[str]:
    port = integer(
        environ,
        "OPENCLAW_GATEWAY_PORT",
        default=18_789,
        maximum=65_535,
    )
    arguments = ["--url", f"ws://127.0.0.1:{port}"]
    token = clean(environ.get("OPENCLAW_GATEWAY_TOKEN"))
    if token:
        arguments.extend(("--token", token))
    return arguments


def _json_stdout(result: Any) -> Any:
    output = getattr(result, "stdout", "")
    if not isinstance(output, str):
        return {}
    output = output.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(output):
            if character not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            return payload
    return {}


def _approve_current_device(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
) -> None:
    explicit = clean(environ.get("OPENCLAW_DEVICE_BOOTSTRAP_MODULE"))
    if explicit:
        module_path = Path(explicit)
        if not module_path.is_absolute() or not module_path.is_file():
            raise OSError(
                "OPENCLAW_DEVICE_BOOTSTRAP_MODULE must be an existing absolute file"
            )
    else:
        command = openclaw_command(environ)[0]
        executable = which(command, path=environ.get("PATH"))
        if executable is None:
            raise OSError(f"cannot resolve OpenClaw executable: {command}")
        resolved = Path(executable).resolve()
        candidates = (
            resolved.parent / "dist/plugin-sdk/device-bootstrap.js",
            resolved.parent.parent / "dist/plugin-sdk/device-bootstrap.js",
        )
        module_path = next((path for path in candidates if path.is_file()), None)
        if module_path is None:
            raise OSError(
                f"cannot resolve device-bootstrap.js from {resolved}"
            )
    runner(
        [
            "node",
            "--input-type=module",
            "--eval",
            PAIR_CURRENT_DEVICE_SOURCE,
            str(module_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=CRON_CLI_TIMEOUT_SECONDS,
        env=dict(environ),
    )


def _list_cron_jobs(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
) -> tuple[Mapping[str, Any], ...]:
    try:
        _approve_current_device(environ, runner=runner)
    except (OSError, subprocess.SubprocessError):
        # Pairing is only needed for a fresh local CLI identity. The single
        # authoritative cron call below decides whether scheduling can proceed.
        pass
    arguments = [
        *openclaw_command(environ),
        "cron",
        "list",
        "--all",
        *_gateway_auth(environ),
        "--json",
    ]
    try:
        result = runner(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=CRON_CLI_TIMEOUT_SECONDS,
            env=dict(environ),
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError(
            f"OpenClaw cron list timed out after {CRON_CLI_TIMEOUT_SECONDS} seconds"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise ConfigurationError(
            f"OpenClaw cron list process error ({type(exc).__name__})"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(f"OpenClaw cron list failed: {exc}") from exc

    if getattr(result, "returncode", 1) != 0:
        stderr = clean(getattr(result, "stderr", ""))
        stdout = clean(getattr(result, "stdout", ""))
        detail = stderr or stdout or "OpenClaw cron list failed"
        raise ConfigurationError(f"OpenClaw cron list failed: {detail}")

    payload = _json_stdout(result)
    jobs = payload.get("jobs") if isinstance(payload, Mapping) else None
    if not isinstance(jobs, list):
        raise ConfigurationError("OpenClaw cron list returned invalid JSON")
    return tuple(job for job in jobs if isinstance(job, Mapping))


def _job_matches(
    job: Mapping[str, Any],
    *,
    local_time: LocalTime,
    argv: Sequence[str],
) -> bool:
    schedule = job.get("schedule")
    payload = job.get("payload")
    return (
        job.get("enabled") is True
        and job.get("agentId") == "main"
        and job.get("sessionTarget") == "isolated"
        and job.get("wakeMode") == "now"
        and isinstance(schedule, Mapping)
        and schedule.get("kind") == "cron"
        and schedule.get("expr") == local_time.expression
        and schedule.get("tz") == CRON_TIMEZONE
        and schedule.get("staggerMs", 0) == 0
        and isinstance(payload, Mapping)
        and payload.get("kind") == "command"
        and payload.get("argv") == list(argv)
        and payload.get("timeoutSeconds") == CRON_COMMAND_TIMEOUT_SECONDS
        and isinstance(job.get("delivery"), Mapping)
        and job["delivery"].get("mode") == "none"
    )


def _run_checked(
    arguments: Sequence[str],
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
    operation: str,
) -> None:
    try:
        runner(
            list(arguments),
            check=True,
            capture_output=True,
            text=True,
            timeout=CRON_CLI_TIMEOUT_SECONDS,
            env=dict(environ),
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError(
            f"OpenClaw cron {operation} timed out after "
            f"{CRON_CLI_TIMEOUT_SECONDS} seconds"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ConfigurationError(
            f"OpenClaw cron {operation} failed with status {exc.returncode}: "
            f"{' '.join(arguments[:4])}"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise ConfigurationError(
            f"OpenClaw cron {operation} process error ({type(exc).__name__}): "
            f"{' '.join(arguments[:4])}"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"OpenClaw cron {operation} failed: "
            f"{' '.join(arguments[:4])}: {exc}"
        ) from exc


def _remove_cron_job(
    job_id: str,
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
) -> None:
    _run_checked(
        [
            *openclaw_command(environ),
            "cron",
            "rm",
            job_id,
            *_gateway_auth(environ),
            "--json",
        ],
        environ,
        runner=runner,
        operation="rm",
    )


def _add_cron_job(
    local_time: LocalTime,
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
    name: str | None = None,
) -> None:
    arguments = [
        *openclaw_command(environ),
        "cron",
        "add",
        "--cron",
        local_time.expression,
        "--name",
        name or f"{CRON_NAME_PREFIX}{local_time.label}",
        "--agent",
        "main",
        "--session",
        "isolated",
        "--tz",
        CRON_TIMEZONE,
        "--exact",
        "--command-argv",
        json.dumps(list(argv), separators=(",", ":")),
        "--timeout-seconds",
        str(CRON_COMMAND_TIMEOUT_SECONDS),
        "--no-deliver",
        *_gateway_auth(environ),
        "--json",
    ]
    _run_checked(
        arguments,
        environ,
        runner=runner,
        operation="add",
    )


def reconcile_cron_jobs(
    plan: SchedulePlan,
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[int, int, int]:
    """Reconcile only owned webhook jobs and remove the replaced Fullrun jobs."""
    jobs = _list_cron_jobs(environ, runner=runner)
    desired = {}
    for configured in plan.webhooks:
        hook = configured.hook
        suffix = hook.key.removeprefix("WEBHOOK_URL").removeprefix("_") or "01"
        argv = [*ephemeral_command(), "webhook", "--webhook", hook.key]
        for local_time in configured.times:
            desired[f"{CRON_NAME_PREFIX}{suffix}-{local_time.label}"] = (local_time, argv)
    kept_names = set()
    removed = 0
    for job in jobs:
        name = job.get("name", "")
        if not isinstance(name, str) or not name.startswith((CRON_NAME_PREFIX, LEGACY_CRON_NAME_PREFIX)):
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ConfigurationError(f"owned OpenClaw cron job {name!r} has no id")
        target = desired.get(name)
        if (target and name not in kept_names
                and _job_matches(job, local_time=target[0], argv=target[1])):
            kept_names.add(name)
            continue
        _remove_cron_job(job_id, environ, runner=runner)
        removed += 1
    added = 0
    for name, (local_time, argv) in desired.items():
        if name not in kept_names:
            _add_cron_job(local_time, argv, environ, runner=runner, name=name)
            added += 1
    return len(kept_names), removed, added


def schedule(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> ScheduleResult:
    """Reconcile native cron jobs, then fire INIT hooks sequentially in group order."""
    plan = build_schedule_plan(environ)
    kept, removed, added = reconcile_cron_jobs(plan, environ, runner=runner)
    initialized = []
    outputs = []
    errors = []
    for configured in plan.webhooks:
        if not configured.hook.init:
            continue
        try:
            text = dispatch_webhook(configured.hook, environ, runner=runner)
            initialized.append(configured.hook.key)
            if text:
                outputs.append(text)
        except ConfigurationError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigurationError("; ".join(errors))
    return ScheduleResult(kept, removed, added, tuple(initialized), tuple(outputs))
