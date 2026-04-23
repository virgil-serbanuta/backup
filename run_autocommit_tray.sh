#!/usr/bin/env bash

set -euo pipefail

unset VIRTUAL_ENV

# On Linux, force the AppIndicator backend; the default xorg backend renders
# a legacy tray icon that modern GNOME/Ubuntu shells accept but don't route
# clicks to. No effect on macOS (pystray uses the darwin backend there).
if [[ "$(uname -s)" == "Linux" ]]; then
  export PYSTRAY_BACKEND="${PYSTRAY_BACKEND:-appindicator}"
fi

if ! command -v poetry >/dev/null 2>&1; then
  echo "ERROR: poetry is not installed. Install it first: https://python-poetry.org/docs/#installation" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

poetry install --no-interaction --no-root >/dev/null
exec poetry run python -m autocommit_tray
