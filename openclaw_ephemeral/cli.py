"""Command-line lifecycle for the ephemeral OpenClaw runtime."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .configuration import ConfigurationResult, configure
from .environment import (
    ConfigurationError,
    expand_api_key_aliases,
    integer,
    openclaw_command,
)
from .scheduling import (
    discover_webhooks,
    dispatch_webhook,
    ephemeral_command,
    schedule,
    scheduling_requested,
)

TRUSTED_POLICY_SCRIPT = "/usr/local/bin/openclaw-ephemeral-yolo"
RUNTIME_HOOK_ROOT = Path("/usr/local/share/openclaw-ephemeral/runtime.d")
SAFE_RUNTIME_HOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RuntimeHookError(RuntimeError):
    """Raised when a runtime hook directory or executable is unsafe or fails."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclaw-ephemeral.py",
        description=(
            "Rebuild OpenClaw configuration entirely from the process environment."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "mode",
        choices=("configure", "run", "restart", "schedule", "webhook"),
        help=(
            "configure, run or restart the gateway, reconcile schedules, or "
            "call one configured webhook"
        ),
    )
    parser.add_argument(
        "--webhook",
        default="",
        help="URL environment variable of the webhook to call",
    )
    return parser


def _report(result: ConfigurationResult, stream: Any) -> None:
    print(f"OpenClaw config rebuilt atomically: {result.path}", file=stream)
    print(f"OpenClaw primary model: {result.primary_model}", file=stream)
    print(
        "OpenClaw models discovered: "
        f"{result.native_model_count} native, "
        f"{result.openai_v1_model_count} across "
        f"{result.openai_v1_provider_count} OpenAI-v1 provider(s)",
        file=stream,
    )
    print(
        f"OpenClaw MCP servers configured: {result.mcp_server_count}",
        file=stream,
    )
    print(f"OpenClaw plugins integrated: {result.plugin_count}", file=stream)
    if result.telegram_configured:
        print("OpenClaw Telegram configured from an environment reference", file=stream)
    if result.note_full_mode:
        print("OpenClaw NOTE full mode enabled", file=stream)
    for warning in result.warnings:
        print(f"Warning: {warning}", file=stream)


def _runtime_hook_paths(phase: str) -> tuple[Path, ...]:
    """Return validated executable hooks for one lifecycle phase."""

    directory = RUNTIME_HOOK_ROOT / f"{phase}.d"
    try:
        directory_mode = directory.lstat().st_mode
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise RuntimeHookError(f"cannot inspect {directory}: {exc}") from exc

    if stat.S_ISLNK(directory_mode):
        raise RuntimeHookError(f"hook directory must not be a symlink: {directory}")
    if not stat.S_ISDIR(directory_mode):
        raise RuntimeHookError(f"hook path is not a directory: {directory}")

    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise RuntimeHookError(f"cannot list {directory}: {exc}") from exc

    hooks: list[Path] = []
    for entry in entries:
        if SAFE_RUNTIME_HOOK_NAME.fullmatch(entry.name) is None:
            raise RuntimeHookError(f"unsafe hook name: {entry}")
        try:
            entry_mode = entry.lstat().st_mode
        except OSError as exc:
            raise RuntimeHookError(f"cannot inspect hook {entry}: {exc}") from exc
        if stat.S_ISLNK(entry_mode):
            raise RuntimeHookError(f"hook must not be a symlink: {entry}")
        if not stat.S_ISREG(entry_mode):
            raise RuntimeHookError(f"hook is not a regular file: {entry}")
        if entry_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            hooks.append(entry)
    return tuple(hooks)


def _run_runtime_hooks(
    phase: str,
    environ: Mapping[str, str],
    runner: Callable[..., Any],
) -> None:
    """Execute one validated hook phase directly with the runtime environment."""

    for hook in _runtime_hook_paths(phase):
        try:
            runner(
                [str(hook)],
                check=True,
                env=environ,
                shell=False,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeHookError(
                f"hook failed with status {exc.returncode}: {hook}"
            ) from exc
        except OSError as exc:
            raise RuntimeHookError(f"cannot execute hook {hook}: {exc}") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    spawner: Callable[..., Any] = subprocess.Popen,
    execvpe: Callable[..., Any] = os.execvpe,
    opener: Callable[..., Any] | None = None,
    stdout: Any = sys.stdout,
    stderr: Any = sys.stderr,
) -> int:
    """Run one lifecycle mode with injectable process primitives for tests."""

    args = _parser().parse_args(argv)
    injected = dict(os.environ if environ is None else environ)
    try:
        if args.mode == "webhook":
            hook = next((hook for hook in discover_webhooks(injected)
                         if hook.key == args.webhook), None)
            if hook is None:
                raise ConfigurationError("--webhook must name a configured WEBHOOK_URL variable")
            text = dispatch_webhook(hook, injected, runner=runner)
            if text:
                print(text, file=stdout)
            return 0
        if args.webhook:
            raise ConfigurationError("--webhook is only valid in webhook mode")
        if args.mode == "schedule":
            scheduled = schedule(injected, runner=runner)
            print("OpenClaw cron jobs reconciled: "
                  f"{scheduled.kept_jobs} kept, {scheduled.removed_jobs} removed, "
                  f"{scheduled.added_jobs} added", file=stdout)
            print("OpenClaw init webhooks dispatched: "
                  + (", ".join(scheduled.initialized_webhooks) or "none"), file=stdout)
            for text in scheduled.outputs:
                print(text, file=stdout)
            return 0

        if args.mode == "run":
            _run_runtime_hooks("pre-config", injected, runner)

        if opener is None:
            result = configure(injected, runner=runner)
        else:
            result = configure(injected, runner=runner, opener=opener)
        _report(result, stdout)

        flush = getattr(stdout, "flush", None)
        if callable(flush):
            flush()
        runtime_env = expand_api_key_aliases(injected)
        runner(
            [TRUSTED_POLICY_SCRIPT],
            check=True,
            env=runtime_env,
        )
        if args.mode == "run":
            _run_runtime_hooks("post-config", runtime_env, runner)
        if args.mode == "configure":
            return 0
        command = openclaw_command(runtime_env)
        if args.mode == "run":
            port = integer(
                runtime_env,
                "OPENCLAW_GATEWAY_PORT",
                default=18_789,
                maximum=65_535,
            )
            arguments = [
                *command,
                "gateway",
                "run",
                "--bind",
                "lan",
                "--port",
                str(port),
            ]
            if runtime_env.get("OPENCLAW_GATEWAY_TOKEN", "").strip():
                arguments.extend(("--auth", "token"))
            _run_runtime_hooks("pre-gateway", runtime_env, runner)
            if scheduling_requested(runtime_env):
                spawner(
                    [*ephemeral_command(), "schedule"],
                    env=runtime_env,
                )
            execvpe(arguments[0], arguments, runtime_env)
            raise RuntimeError("gateway exec unexpectedly returned")

        runner(
            [*command, "gateway", "restart"],
            check=True,
            env=runtime_env,
        )
        return 0
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=stderr)
        return 2
    except RuntimeHookError as exc:
        print(f"Runtime hook error: {exc}", file=stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
