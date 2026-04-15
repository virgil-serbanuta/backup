#!/usr/bin/env bash

set -euo pipefail

if ! command -v poetry >/dev/null 2>&1; then
  echo "ERROR: poetry is not installed. Install it first: https://python-poetry.org/docs/#installation" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

poetry install --no-interaction --no-root
poetry run python autocommit_scan.py "$HOME"