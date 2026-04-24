#!/usr/bin/env bash

set -euo pipefail

# cron runs with a minimal PATH (/usr/bin:/bin) that omits ~/.local/bin,
# where poetry typically lives — without this, the poetry check below fails
# silently under cron (mail output is discarded when no MTA is installed).
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

unset VIRTUAL_ENV

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config.yaml> [--prefix NAME] [--force]" >&2
  exit 64
fi

if ! command -v poetry >/dev/null 2>&1; then
  echo "ERROR: poetry is not installed. Install it first: https://python-poetry.org/docs/#installation" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

poetry install --no-interaction --no-root >/dev/null
exec poetry run python -m autocommit_tray.cron "$@"
