"""Repository-hook dispatch and idempotent OpenClaw cron reconciliation."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .environment import ConfigurationError, clean, config_path, integer, openclaw_command
from .plugins import (
    OpenClawPlugin,
    discover_openclaw_plugins,
    select_plugin_hooks,
)


CRONTAB_TIME_ENV = "OPENCLAW_CRONTAB_TIME"
CRONTAB_REPOS_ENV = "OPENCLAW_CRONTAB_REPOS"
START_INIT_ENV = "OPENCLAW_REPOS_START_INIT"
CRON_TIMEZONE = "Europe/Vienna"
CRON_NAME_PREFIX = "openclaw-ephemeral-repositories-europe-vienna-"
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
class SchedulePlan:
    times: tuple[LocalTime, ...]
    cron_plugins: tuple[OpenClawPlugin, ...]
    init_plugins: tuple[OpenClawPlugin, ...]


@dataclass(frozen=True)
class ScheduleResult:
    kept_jobs: int
    removed_jobs: int
    added_jobs: int
    initialized_repositories: tuple[str, ...]


def repository_csv(raw: str, *, name: str) -> tuple[str, ...]:
    """Parse a strict comma-separated repository list with stable de-duplication."""

    value = clean(raw)
    if not value:
        return ()
    repositories: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        repository = item.strip()
        if not repository:
            raise ConfigurationError(f"{name} contains an empty repository name")
        folded = repository.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        repositories.append(repository)
    return tuple(repositories)


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
                f"{CRONTAB_TIME_ENV} entry {candidate!r} must be HH:MM, "
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


def build_schedule_plan(
    environ: Mapping[str, str],
    plugins: Sequence[OpenClawPlugin],
) -> SchedulePlan:
    """Validate all scheduling selectors against the final discovered image."""

    times = crontab_times(environ.get(CRONTAB_TIME_ENV, ""))
    cron_names = repository_csv(
        environ.get(CRONTAB_REPOS_ENV, ""),
        name=CRONTAB_REPOS_ENV,
    )
    init_names = repository_csv(
        environ.get(START_INIT_ENV, ""),
        name=START_INIT_ENV,
    )
    return SchedulePlan(
        times=times,
        cron_plugins=select_plugin_hooks(plugins, cron_names),
        init_plugins=select_plugin_hooks(plugins, init_names),
    )


def scheduling_requested(environ: Mapping[str, str]) -> bool:
    return any(
        clean(environ.get(name))
        for name in (CRONTAB_TIME_ENV, CRONTAB_REPOS_ENV, START_INIT_ENV)
    )


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


def _dispatch_argv(
    environ: Mapping[str, str],
    plugins: Sequence[OpenClawPlugin],
) -> list[str]:
    repositories = ",".join(plugin.repository for plugin in plugins)
    return [
        *ephemeral_command(),
        "dispatch",
        "--repos",
        repositories,
    ]


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
) -> None:
    arguments = [
        *openclaw_command(environ),
        "cron",
        "add",
        "--cron",
        local_time.expression,
        "--name",
        f"{CRON_NAME_PREFIX}{local_time.label}",
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
    """Keep exact jobs, remove stale/duplicate owned jobs, and add missing jobs."""

    jobs = _list_cron_jobs(environ, runner=runner)
    argv = _dispatch_argv(environ, plan.cron_plugins)
    desired = {
        f"{CRON_NAME_PREFIX}{local_time.label}": local_time
        for local_time in plan.times
        if plan.cron_plugins
    }
    kept_names: set[str] = set()
    kept = 0
    removed = 0
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str) or not name.startswith(CRON_NAME_PREFIX):
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ConfigurationError(f"owned OpenClaw cron job {name!r} has no id")
        local_time = desired.get(name)
        if (
            local_time is not None
            and name not in kept_names
            and _job_matches(job, local_time=local_time, argv=argv)
        ):
            kept_names.add(name)
            kept += 1
            continue
        _remove_cron_job(job_id, environ, runner=runner)
        removed += 1

    added = 0
    for name, local_time in desired.items():
        if name in kept_names:
            continue
        _add_cron_job(local_time, argv, environ, runner=runner)
        added += 1
    return kept, removed, added


def dispatch_plugins(
    plugins: Sequence[OpenClawPlugin],
    environ: Mapping[str, str],
    *,
    opener: Callable[..., Any] = urlopen,
) -> tuple[str, ...]:
    """POST each selected plugin-owned hook through the local authenticated gateway."""

    if not plugins:
        return ()
    port = integer(
        environ,
        "OPENCLAW_GATEWAY_PORT",
        default=18_789,
        maximum=65_535,
    )
    token = clean(environ.get("OPENCLAW_GATEWAY_TOKEN"))
    dispatched: list[str] = []
    for plugin in plugins:
        if plugin.hook_path is None:
            raise ConfigurationError(
                f"OpenClaw plugin repository {plugin.repository!r} has no "
                "schedulable HTTP hook"
            )
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"http://127.0.0.1:{port}{plugin.hook_path}",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        try:
            response = opener(request, timeout=300)
            read = getattr(response, "read", None)
            if callable(read):
                read()
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except (HTTPError, URLError, OSError) as exc:
            raise ConfigurationError(
                f"OpenClaw plugin hook failed for {plugin.repository!r} "
                f"at {plugin.hook_path}: {exc}"
            ) from exc
        dispatched.append(plugin.repository)
    return tuple(dispatched)


def dispatch_repositories(
    environ: Mapping[str, str],
    repository_names: Sequence[str],
    *,
    opener: Callable[..., Any] = urlopen,
) -> tuple[str, ...]:
    destination = config_path(environ)
    plugins, _warnings = discover_openclaw_plugins(
        environ,
        destination=destination,
    )
    selected = select_plugin_hooks(plugins, repository_names)
    return dispatch_plugins(selected, environ, opener=opener)


def schedule(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any] = subprocess.run,
    opener: Callable[..., Any] = urlopen,
) -> ScheduleResult:
    """Reconcile managed cron jobs, then run explicitly selected init hooks once."""

    if not scheduling_requested(environ):
        return ScheduleResult(0, 0, 0, ())
    destination = config_path(environ)
    plugins, _warnings = discover_openclaw_plugins(
        environ,
        destination=destination,
    )
    plan = build_schedule_plan(environ, plugins)
    kept, removed, added = reconcile_cron_jobs(
        plan,
        environ,
        runner=runner,
    )
    initialized = dispatch_plugins(plan.init_plugins, environ, opener=opener)
    return ScheduleResult(kept, removed, added, initialized)
