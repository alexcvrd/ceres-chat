#!/usr/bin/env bash
# Venv for ceres-chat. Idempotent, uv-based, frozen: the lock decides.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "uv not found (expected on PATH or at ~/.local/bin/uv)"; exit 1; }
[ -d .venv ] || "$UV" venv .venv --python 3.12
[ -f uv.lock ] || "$UV" lock
"$UV" sync --frozen --extra dev
[ -f requirements.lock ] || "$UV" export --frozen --extra dev --no-emit-project --no-hashes \
  --format requirements.txt -o requirements.lock
.venv/bin/python smoke_test.py
