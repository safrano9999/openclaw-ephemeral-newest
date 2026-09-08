"""Discover and register OpenClaw plugins contributed by the final image."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .environment import ConfigurationError, clean, state_dir_path, workspace_path


MANIFEST_NAME = "openclaw.plugin.json"
PLUGIN_ROOTS_ENV = "OPENCLAW_PLUGIN_ROOTS"
IMAGE_PLUGIN_INSTALLS = Path("/usr/local/share/openclaw/image-plugin-installs.json")
SKIPPED_TREE_NAMES = frozenset(
    {".git", ".venv", "__pycache__", "node_modules"}
)
ROUTE_LITERAL = re.compile(
    r"\bpath\s*:\s*['\"](?P<path>/[^'\"]*)['\"]"
)
ROUTE_IDENTIFIER = re.compile(
    r"\bpath\s*:\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*[,}]"
)


@dataclass(frozen=True)
class OpenClawPlugin:
    """One validated plugin manifest and its optional runnable HTTP hook."""

    repository: str
    plugin_id: str
    path: Path
    manifest: Mapping[str, Any]
    hook_path: str | None


def restore_image_plugin_installs(
    environ: Mapping[str, str],
    *,
    destination: Path,
    seed_path: Path = IMAGE_PLUGIN_INSTALLS,
) -> None:
    """Reconcile image plugin records after mounting an older persistent ledger."""

    database_path = state_dir_path(environ, destination) / "state" / "openclaw.sqlite"
    if not seed_path.is_file() or not database_path.is_file():
        return
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        if seed["schemaVersion"] != 1 or not isinstance(seed["installRecords"], dict):
            raise ValueError("invalid image plugin records")
        matching: dict[str, Any] = {}
        for plugin_id, record in seed["installRecords"].items():
            path = Path(record["installPath"])
            if not (path / MANIFEST_NAME).is_file() or not (path / "package.json").is_file():
                continue
            package = json.loads((path / "package.json").read_text(encoding="utf-8"))
            if package.get("version") == record.get("version"):
                matching[plugin_id] = record
        if not matching:
            return
        with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True)) as database:
            if not database.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'config_machine_state'"
            ).fetchone():
                return
            with database:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT value_json FROM config_machine_state WHERE state_key = ?",
                    ("plugins.installedIndex",),
                ).fetchone()
                if row is None:
                    return
                ledger = json.loads(row[0])
                records = ledger["index"]["installRecords"]
                changed = False
                for plugin_id, record in matching.items():
                    previous = records.get(plugin_id)
                    # Keep an operator's installation at a different path.
                    if previous and previous.get("installPath") != record["installPath"]:
                        continue
                    if previous != record:
                        records[plugin_id] = record
                        changed = True
                if changed:
                    ledger["revision"] += 1
                    database.execute(
                        "UPDATE config_machine_state SET value_json = ? WHERE state_key = ?",
                        (json.dumps(ledger), "plugins.installedIndex"),
                    )
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigurationError("cannot reconcile image plugin install records") from exc


def _path_list(raw: str) -> tuple[str, ...]:
    """Parse a path-list value while accepting commas for env-file ergonomics."""

    values: list[str] = []
    for comma_group in raw.split(","):
        for item in comma_group.split(os.pathsep):
            item = item.strip()
            if item and item not in values:
                values.append(item)
    return tuple(values)


def _managed_plugin_roots(state_dir: Path) -> tuple[str, ...]:
    """Read exact install paths from OpenClaw's canonical public plugin ledger."""

    database_path = state_dir / "state" / "openclaw.sqlite"
    if not database_path.is_file():
        return ()
    try:
        with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            if not database.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'config_machine_state'"
            ).fetchone():
                return ()
            row = database.execute(
                "SELECT value_json FROM config_machine_state WHERE state_key = ?",
                ("plugins.installedIndex",),
            ).fetchone()
        if row is None:
            return ()
        records = json.loads(row[0])["index"]["installRecords"]
        if not isinstance(records, dict):
            raise ValueError("expected an install record map")
        roots: list[str] = []
        for record in records.values():
            if not isinstance(record, dict):
                raise ValueError("expected an install record")
            install_path = record.get("installPath")
            if install_path is not None:
                if not isinstance(install_path, str) or not install_path.strip():
                    raise ValueError("expected an install path")
                roots.append(install_path)
        return tuple(roots)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise ConfigurationError("cannot read OpenClaw installed-plugin metadata") from exc


def plugin_roots(
    environ: Mapping[str, str],
    *,
    destination: Path,
) -> tuple[Path, ...]:
    """Return explicit roots followed by the conventional image/state roots."""

    raw_roots: list[str] = list(_path_list(clean(environ.get(PLUGIN_ROOTS_ENV))))
    plugins_dir = clean(environ.get("OPENCLAW_PLUGINS_DIR"))
    if plugins_dir:
        raw_roots.append(plugins_dir)

    state_extensions = state_dir_path(environ, destination) / "extensions"
    workspace_extensions = (
        workspace_path(environ, destination) / ".openclaw" / "extensions"
    )
    raw_roots.extend(
        (
            "/opt/safrano9999",
            str(state_extensions),
            str(workspace_extensions),
        )
    )
    raw_roots.extend(_managed_plugin_roots(state_dir_path(environ, destination)))

    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_roots:
        if raw == "~" or raw.startswith("~/"):
            home = clean(environ.get("HOME"))
            if not home:
                raise ConfigurationError("HOME is required to expand a plugin root")
            raw = home + raw[1:]
        path = Path(raw).resolve()
        if path in seen:
            continue
        seen.add(path)
        roots.append(path)
    return tuple(roots)


def _manifest_paths(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return (root,) if root.name == MANIFEST_NAME else ()
    if not root.is_dir():
        return ()

    manifests: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIPPED_TREE_NAMES and not name.startswith(".")
        )
        if MANIFEST_NAME in files:
            manifests.append(Path(current) / MANIFEST_NAME)
    return tuple(sorted(manifests, key=lambda path: str(path)))


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read OpenClaw plugin manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid OpenClaw plugin manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(
            f"invalid OpenClaw plugin manifest {path}: expected an object"
        )
    return payload


def _plugin_id(path: Path, manifest: Mapping[str, Any]) -> str:
    value = manifest.get("id")
    plugin_id = value.strip() if isinstance(value, str) else ""
    if not plugin_id:
        raise ConfigurationError(f"OpenClaw plugin manifest has no id: {path}")
    return plugin_id


def _config_properties(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    schema = manifest.get("configSchema")
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _valid_hook_path(value: Any) -> str | None:
    path = value.strip() if isinstance(value, str) else ""
    if not path:
        return None
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in path)
    ):
        return None
    return path


def _manifest_hook_path(manifest: Mapping[str, Any]) -> str | None:
    properties = _config_properties(manifest)
    webhook = properties.get("webhook")
    if not isinstance(webhook, Mapping):
        return None
    webhook_properties = webhook.get("properties")
    if not isinstance(webhook_properties, Mapping):
        return None
    path_property = webhook_properties.get("path")
    if not isinstance(path_property, Mapping):
        return None
    return _valid_hook_path(path_property.get("default"))


def _source_hook_path(plugin_path: Path) -> str | None:
    """Read a statically declared first HTTP route without executing plugin code."""

    for filename in ("index.js", "index.mjs", "index.ts"):
        source_path = plugin_path / filename
        try:
            source = source_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return None

        for match in re.finditer(r"\bregisterHttpRoute\s*\(", source):
            snippet = source[match.end() : match.end() + 4_096]
            literal = ROUTE_LITERAL.search(snippet)
            if literal is not None:
                path = _valid_hook_path(literal.group("path"))
                if path:
                    return path
            identifier = ROUTE_IDENTIFIER.search(snippet)
            if identifier is None:
                continue
            name = re.escape(identifier.group("name"))
            assignment = re.search(
                rf"\b(?:const|let|var)\s+{name}\s*=\s*['\"](?P<path>/[^'\"]*)['\"]",
                source,
            )
            if assignment is not None:
                path = _valid_hook_path(assignment.group("path"))
                if path:
                    return path
    return None


def _repository_name(root: Path, plugin_path: Path) -> str:
    try:
        relative = plugin_path.relative_to(root)
    except ValueError:
        return plugin_path.name
    return relative.parts[0] if relative.parts else plugin_path.name


def discover_openclaw_plugins(
    environ: Mapping[str, str],
    *,
    destination: Path,
) -> tuple[tuple[OpenClawPlugin, ...], tuple[str, ...]]:
    """Scan every available manifest under configured and conventional roots."""

    plugins: list[OpenClawPlugin] = []
    warnings: list[str] = []
    seen_paths: set[Path] = set()
    seen_ids: dict[str, OpenClawPlugin] = {}

    for root in plugin_roots(environ, destination=destination):
        for manifest_path in _manifest_paths(root):
            plugin_path = manifest_path.parent.resolve()
            if plugin_path in seen_paths:
                continue
            seen_paths.add(plugin_path)
            manifest = _load_manifest(manifest_path)
            plugin_id = _plugin_id(manifest_path, manifest)
            repository = _repository_name(root, plugin_path)
            descriptor = OpenClawPlugin(
                repository=repository,
                plugin_id=plugin_id,
                path=plugin_path,
                manifest=manifest,
                hook_path=(
                    _manifest_hook_path(manifest)
                    or _source_hook_path(plugin_path)
                ),
            )
            folded_id = plugin_id.casefold()
            previous = seen_ids.get(folded_id)
            if previous is not None:
                warnings.append(
                    f"duplicate OpenClaw plugin id {plugin_id!r} at {plugin_path}; "
                    f"using {previous.path}"
                )
                continue
            seen_ids[folded_id] = descriptor
            plugins.append(descriptor)

    return tuple(plugins), tuple(warnings)


def _merge_plugin_config(entry: dict[str, Any], values: Mapping[str, Any]) -> None:
    config = entry.setdefault("config", {})
    if not isinstance(config, dict):
        config = {}
        entry["config"] = config
    for key, value in values.items():
        if isinstance(value, Mapping) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value


def register_openclaw_plugins(
    config: dict[str, Any],
    plugins: Sequence[OpenClawPlugin],
    *,
    environ: Mapping[str, str],
    destination: Path,
) -> tuple[str, ...]:
    """Add discovered plugins to explicit load paths and enabled entries."""

    if not plugins:
        return ()

    plugin_config = config.setdefault("plugins", {})
    load = plugin_config.setdefault("load", {})
    paths = load.setdefault("paths", [])
    entries = plugin_config.setdefault("entries", {})
    if not isinstance(paths, list) or not isinstance(entries, dict):
        raise ConfigurationError("generated plugins configuration has an invalid shape")

    telegram_target = clean(environ.get("OPENCLAW_TELEGRAM_CHAT_ID"))
    extensions_dir = destination.parent / "extensions"
    registered: list[str] = []
    for plugin in plugins:
        path_text = str(plugin.path)
        # Codex needs its bundled/global origin for native compaction and /codex.
        # An explicit load path changes that origin to "config".
        if plugin.plugin_id == "codex":
            paths[:] = [existing for existing in paths if existing != path_text]
        elif path_text not in paths:
            paths.append(path_text)
        entry = entries.setdefault(plugin.plugin_id, {})
        if not isinstance(entry, dict):
            entry = {}
            entries[plugin.plugin_id] = entry
        entry["enabled"] = True
        registered.append(plugin.plugin_id)

        properties = _config_properties(plugin.manifest)
        if "autoSetupPython" in properties:
            _merge_plugin_config(entry, {"autoSetupPython": False})
        if "configPath" in properties:
            for config_root in (extensions_dir / plugin.plugin_id, plugin.path):
                selected = next(
                    (
                        config_root / filename
                        for filename in ("config.conf", "config.json")
                        if (config_root / filename).is_file()
                    ),
                    None,
                )
                if selected is not None:
                    _merge_plugin_config(entry, {"configPath": str(selected)})
                    break

        if telegram_target:
            delivery = {"channel": "telegram", "target": telegram_target}
            if "delivery" in properties:
                _merge_plugin_config(entry, {"delivery": dict(delivery)})
            if "statusDelivery" in properties:
                _merge_plugin_config(entry, {"statusDelivery": dict(delivery)})

    return tuple(registered)


def select_plugin_hooks(
    plugins: Iterable[OpenClawPlugin],
    repository_names: Sequence[str],
) -> tuple[OpenClawPlugin, ...]:
    """Resolve explicit repository selectors and reject unknown or hookless repos."""

    by_repository: dict[str, list[OpenClawPlugin]] = {}
    for plugin in plugins:
        by_repository.setdefault(plugin.repository.casefold(), []).append(plugin)

    selected: list[OpenClawPlugin] = []
    for repository in repository_names:
        matches = by_repository.get(repository.casefold(), [])
        if not matches:
            available = ", ".join(plugin.repository for plugin in plugins) or "none"
            raise ConfigurationError(
                f"unknown OpenClaw plugin repository {repository!r}; "
                f"discovered repositories: {available}"
            )
        if len(matches) > 1:
            raise ConfigurationError(
                f"ambiguous OpenClaw plugin repository {repository!r}"
            )
        plugin = matches[0]
        if plugin.hook_path is None:
            raise ConfigurationError(
                f"OpenClaw plugin repository {plugin.repository!r} has no "
                "schedulable HTTP hook"
            )
        if plugin not in selected:
            selected.append(plugin)
    return tuple(selected)
