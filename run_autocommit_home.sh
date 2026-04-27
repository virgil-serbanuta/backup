#!/usr/bin/env bash

set -euo pipefail

unset VIRTUAL_ENV

if ! command -v poetry >/dev/null 2>&1; then
  echo "ERROR: poetry is not installed. Install it first: https://python-poetry.org/docs/#installation" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Skip `poetry install` when poetry.lock hasn't changed since last success.
# Avoids the spurious "Updating ..." messages that
# virtualenvs.options.system-site-packages = true triggers each run.
STAMP=".poetry-install-stamp"
if [[ ! -f "$STAMP" ]] || [[ poetry.lock -nt "$STAMP" ]]; then
  poetry install --no-interaction --no-root
  touch "$STAMP"
fi
poetry run python autocommit_scan.py "$HOME"