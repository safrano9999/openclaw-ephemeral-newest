#!/usr/bin/env bash
set -euo pipefail

: "${OPENCLAW_VERSION:?OPENCLAW_VERSION is required}"
: "${OPENCLAW_DETERMINISTIC_TAG:?OPENCLAW_DETERMINISTIC_TAG is required}"
: "${OPENCLAW_DETERMINISTIC_ASSET:?OPENCLAW_DETERMINISTIC_ASSET is required}"
: "${OPENCLAW_DETERMINISTIC_SHA256:?OPENCLAW_DETERMINISTIC_SHA256 is required}"

release_url="https://github.com/safrano9999/openclaw-deterministic-latest/releases/download/${OPENCLAW_DETERMINISTIC_TAG}"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

current_version="$(openclaw --version)"
case "$current_version" in
    *"$OPENCLAW_VERSION"*) ;;
    *)
        printf 'OpenClaw version mismatch: expected %s, found %s\n' \
            "$OPENCLAW_VERSION" "$current_version" >&2
        exit 1
        ;;
esac

curl -fsSL --retry 3 \
    "${release_url}/${OPENCLAW_DETERMINISTIC_ASSET}" \
    -o "${temporary}/${OPENCLAW_DETERMINISTIC_ASSET}"

printf '%s  %s\n' \
    "$OPENCLAW_DETERMINISTIC_SHA256" \
    "$OPENCLAW_DETERMINISTIC_ASSET" \
    > "${temporary}/${OPENCLAW_DETERMINISTIC_ASSET}.sha256"
(cd "$temporary" && sha256sum -c "${OPENCLAW_DETERMINISTIC_ASSET}.sha256")

if [ -f /app/openclaw.mjs ]; then
    openclaw_root=/app
else
    openclaw_root="$(dirname "$(readlink -f "$(command -v openclaw)")")"
fi
[ -f "${openclaw_root}/openclaw.mjs" ] || {
    printf 'OpenClaw root not found: %s\n' "$openclaw_root" >&2
    exit 1
}

rm -rf "${openclaw_root}/dist"
tar -xzf "${temporary}/${OPENCLAW_DETERMINISTIC_ASSET}" -C "$openclaw_root"

patched_version="$(node "${openclaw_root}/openclaw.mjs" --version)"
case "$patched_version" in
    *"$OPENCLAW_VERSION"*) ;;
    *)
        printf 'Patched OpenClaw version mismatch: expected %s, found %s\n' \
            "$OPENCLAW_VERSION" "$patched_version" >&2
        exit 1
        ;;
esac

printf 'Installed openclaw-deterministic %s for OpenClaw %s\n' \
    "$OPENCLAW_DETERMINISTIC_TAG" "$OPENCLAW_VERSION"
