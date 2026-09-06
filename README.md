# openclaw-ephemeral

Environment-driven Python runtime for OpenClaw. It rebuilds the complete
`openclaw.json` from the current process environment without reading or merging
an older JSON file.

The repository is centered on the executable `openclaw-ephemeral.py`, the
`openclaw_ephemeral` package, and their tests. Container packaging is optional
and isolated below `container/`.

## Python runtime

```text
openclaw-ephemeral.py configure
openclaw-ephemeral.py run
openclaw-ephemeral.py restart
openclaw-ephemeral.py schedule
openclaw-ephemeral.py dispatch --repos WELCOME,NEXTCLOUD
```

- `configure` writes a fresh configuration and applies the trusted runtime
  policy. The policy uses one synchronized exec-approval preset and one
  validated config patch instead of a sequence of path-based writes.
- `run` configures OpenClaw, executes the lifecycle hooks, and starts the
  gateway.
- `restart` configures OpenClaw and requests a gateway restart.
- `schedule` reconciles the runtime-owned OpenClaw cron jobs and then runs the
  selected one-time init hooks.
- `dispatch` posts to the plugin-owned HTTP hooks of explicit repository names.

Every configuration rebuild scans available `openclaw.plugin.json` manifests
under the conventional `/opt/safrano9999`, global state-extension, and
workspace-extension roots. Additional roots can be supplied through the
path-list/CSV variable `OPENCLAW_PLUGIN_ROOTS`; the established single-root
`OPENCLAW_PLUGINS_DIR` is also honored. Missing roots and an image with no
contributed plugins are valid. Discovered plugins are enabled through explicit
load paths and entries; duplicate plugin ids keep the first root in discovery
order. This final-image scan also sees plugins contributed by the topmost image
layer.

On OpenClaw 2026.9.2, managed npm/ClawHub plugin paths are also read from the
SQLite installed-plugin ledger. Only its recorded package roots are scanned;
unrelated `node_modules` trees are not searched. The ledger is not rewritten or
copied into `openclaw.json`. Native provider discovery carries the discovered
provider plugin paths into its isolated temporary configuration.

Existing state must complete OpenClaw's explicit Doctor migrations before
startup. In particular, retired `exec-approvals.json` blocks approval commands
until `openclaw doctor --fix` imports or reconciles it with SQLite. This runtime
does not run Doctor automatically. The trusted policy command continues to
update OpenClaw's canonical approval store; inspect its effective behavior with
`openclaw exec-policy show --json`.

Only `run` scans runtime hooks:

```text
/usr/local/share/openclaw-ephemeral/runtime.d/pre-config.d   before configure
/usr/local/share/openclaw-ephemeral/runtime.d/post-config.d  after configure and trusted policy
/usr/local/share/openclaw-ephemeral/runtime.d/pre-gateway.d  immediately before gateway exec
```

Entries run in lexical filename order. Names must match
`[A-Za-z0-9][A-Za-z0-9._-]*`. Symlinks and non-regular entries abort startup,
non-executable regular files are ignored, and an unsuccessful hook prevents all
later phases and gateway execution.

## Environment contract

Recognized model variables include:

```text
OPENCLAW_MODEL
OPENCLAW_OPENAI_V1_DEFAULT_LLM
OPENAI_V1_PROVIDER
OPENAI_V1_URL
OPENAI_V1_PORT
OPENAI_V1_KEY
OPENAI_V1_API_KEY_ALIAS
OPENAI_V1_STREAM
OPENAI_V1_DISCOVERY_HEADERS
OPENAI_V1_MODELS
```

Numbered OpenAI-v1 groups use `_2`, `_3`, and subsequent suffixes. Native
OpenClaw providers continue to use their established `*_API_KEY` variables.
`OPENAI_V1_DISCOVERY_HEADERS` is an optional JSON object for endpoint-specific
catalog headers; it cannot replace the generated bearer authorization.
`OPENAI_V1_MODELS` accepts a JSON array or comma-separated model ids and is
merged with discovery results, or used as a fallback when `/models` is not
available. Neither option depends on a provider name.
Gateway and Telegram configuration use `OPENCLAW_GATEWAY_*` and
`OPENCLAW_TELEGRAMTOKEN`. `OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS` supplies the
fixed base for `gateway.controlUi.allowedOrigins`; it accepts comma-separated
exact HTTP(S) origins without paths or wildcards. For example:

```text
OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS=http://127.0.0.1:18789,http://localhost:18789,http://127.0.0.1:20789
```

The example configuration is the sole source of that fixed preset; the runtime
does not carry a second hard-coded list. The example also presets
`OPENCLAW_CONTROL_UI_ALLOWED_ORIGINS_AUTO=1`. At `1`, the runtime appends a
missing Cloudflare origin derived from `CITADEL_CLOUDFLARE_DOMAIN` when
Cloudflare is enabled, plus current Tailscale DNS/IP origins for the internal
and published gateway ports. At `0`, the CSV list remains exact.
Unavailable or invalid automatic sources are silently skipped.

Optional global HTTP MCP servers use repeatable groups:

```text
MCP_SERVER_NAME=
MCP_SERVER_URL=
MCP_SERVER_BEARER=
MCP_SERVER_ALLOW_PRIVATE=0

MCP_SERVER_NAME_02=
MCP_SERVER_URL_02=
MCP_SERVER_BEARER_02=
MCP_SERVER_ALLOW_PRIVATE_02=0
```

The first group has no `_01` suffix; later groups use `_02`, `_03`, and so on.
Only the URL activates a group. The name and bearer are optional, with a
missing name derived from the URL hostname. Servers are available globally
without tool filters, advertise parallel tool calls, and use Codex approval
mode `approve`. Bearers are stored as environment placeholders such as
`Bearer ${MCP_SERVER_BEARER_02}`, never as resolved tokens.
Set the matching `MCP_SERVER_ALLOW_PRIVATE[_NN]` to `1` only for a trusted MCP
endpoint on loopback, a private network, or a link-local host alias.

Secret values are represented through environment references in the generated
configuration. The runtime does not serialize resolved credentials into its
status output.

Repository-hook scheduling uses exactly three variables:

```text
OPENCLAW_CRONTAB_TIME=CET 07:00,CET 19:00
OPENCLAW_CRONTAB_REPOS=WELCOME,NEXTCLOUD,KACHELMANN
OPENCLAW_REPOS_START_INIT=WELCOME,KACHELMANN
```

`OPENCLAW_CRONTAB_TIME` is interpreted as wall-clock time in
`Europe/Vienna`; the IANA timezone applies CET/CEST transitions automatically.
The other two values are CSV lists of repository directory names. Cron jobs use
native command payloads to call the deterministic `dispatch` mode, without an
agent/model turn. Unknown repository names and selected plugins without a
schedulable HTTP hook are configuration errors. When all three variables are
empty, `schedule` returns immediately. If the local cron CLI first requires
pairing, `schedule` approves only the pending request matching its own OpenClaw
device identity and then retries.

## Tests

The pairing-bridge test uses Node.js 24, matching the OpenClaw runtime.

```bash
python3 -m unittest discover -s tests -v
```

## Optional container image

[![GHCR](https://img.shields.io/badge/GHCR-openclaw--ephemeral--latest-0ea5e9)](https://github.com/users/safrano9999/packages/container/package/openclaw-ephemeral-latest)
[![Image tags](https://img.shields.io/badge/image-tags-2563eb)](https://github.com/users/safrano9999/packages/container/package/openclaw-ephemeral-latest)
[![Deterministic source](https://img.shields.io/badge/source-openclaw--deterministic--newest-111827)](https://github.com/safrano9999/openclaw-deterministic-newest)
[![Release overview](https://img.shields.io/badge/release-overview-7c3aed)](RELEASE.md)

The image definition and its build/runtime helpers live in `container/`. The
repository root remains the build context so the Containerfile can copy the
unchanged Python package and launcher.

The optional image target is
[`ghcr.io/safrano9999/openclaw-ephemeral-latest`](https://github.com/users/safrano9999/packages/container/package/openclaw-ephemeral-latest).
This migration does not build or publish it. Its pinned components are:

- `ghcr.io/openclaw/openclaw:2026.9.2`
- [`openclaw-deterministic-newest` release `2026.9.2-deterministic.2`](https://github.com/safrano9999/openclaw-deterministic-newest/releases/tag/2026.9.2-deterministic.2)
- [NOTE release ZIP `2026.7.36`](https://github.com/safrano9999/NOTE/releases/tag/2026.7.36)
- this repository's environment-driven Python runtime

The external archives are downloaded from their pinned releases and verified
by SHA-256. The build requires `OPENCLAW_DETERMINISTIC_SHA256` from the verified
new release; no previous release digest is reused. The archives are not
vendored here. The image defaults to `dummy/note`;
`dummy/dummy`, native providers, and OpenAI-v1 compatible providers remain
available.

The container runs as `root`, matching the existing Safrano and Fedora
OpenClaw images.

## Historical image proof

The preserved recording compares an unavailable normal model with the
`dummy/dummy` route. Click either screenshot to open the corresponding MP4.

<table>
  <thead>
    <tr>
      <th width="50%">Unavailable model</th>
      <th width="50%">Deterministic reply</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>
        <a href="https://github.com/safrano9999/openclaw-deterministic/releases/download/2026.7.1-deterministic.1/deterministic-before.mp4">
          <img src="https://github.com/safrano9999/openclaw-deterministic/releases/download/2026.7.1-deterministic.1/deterministic-before.png" alt="Telegram before the deterministic patch">
        </a>
      </td>
      <td>
        <a href="https://github.com/safrano9999/openclaw-deterministic/releases/download/2026.7.1-deterministic.1/deterministic-after.mp4">
          <img src="https://github.com/safrano9999/openclaw-deterministic/releases/download/2026.7.1-deterministic.1/deterministic-after.png" alt="Telegram using the deterministic route">
        </a>
      </td>
    </tr>
  </tbody>
</table>

The recordings predate the `2026.7.1` port and demonstrate the routing concept,
not byte-exact current reply text. With `dummy/note`, NOTE can claim and store
an ordinary non-command message without an LLM request and return it through
`/note show`.

![NOTE full mode in Telegram](https://raw.githubusercontent.com/safrano9999/NOTE/2026.7.36/docs/full-mode.jpg)

The NOTE screenshot was captured on OpenClaw `2026.6.11` and illustrates the
workflow rather than the current pinned build.

## Pull the optional image after publication

```bash
docker pull ghcr.io/safrano9999/openclaw-ephemeral-latest:latest
```

```bash
podman pull ghcr.io/safrano9999/openclaw-ephemeral-latest:latest
```
