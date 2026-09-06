#!/usr/bin/env bash
set -euo pipefail

# OpenClaw 2026.9.2 synchronizes the host approval store and canonical exec.mode
# in this one command; the generator already uses that config form.
openclaw exec-policy preset yolo
