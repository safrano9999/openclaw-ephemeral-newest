#!/usr/bin/env bash
set -euo pipefail

# Synchronize the canonical host approval store, then normalize the requested config
# policy in one validated write. The generated config intentionally starts in
# the preset-compatible security/ask form so these are the only two CLI starts.
openclaw exec-policy preset yolo
openclaw config patch --stdin <<'JSON'
{
  "tools": {
    "exec": {
      "mode": "full",
      "security": null,
      "ask": null
    }
  }
}
JSON
