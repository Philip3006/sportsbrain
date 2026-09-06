#!/bin/bash
# launchd entrypoint: load the protected runtime environment before refresh.
set -euo pipefail

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
cd "$SPORTSBRAIN_DIR"

set -a
. "$SPORTSBRAIN_DIR/.env"
set +a

exec /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 scripts/refresh_odds.py
