#!/bin/bash
# launchd entrypoint: load the protected runtime environment before refresh.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SPORTSBRAIN_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$SPORTSBRAIN_DIR"

set -a
. "$SPORTSBRAIN_DIR/.env"
set +a

exec /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 scripts/refresh_odds.py
