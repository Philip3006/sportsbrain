#!/bin/bash
# Lokaler Live-Push-Trigger (alle 2 Min via launchd).
# Läuft tennis_live_push.py direkt — kein GH-Actions-Dispatch mehr.
set -uo pipefail  # no -e: we want to record the exit code in health, not die

PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/launchd_live_score_push.log"

source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "live_score_push"

timestamp() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

{
    echo "--- $(timestamp) ---"
    cd "$SPORTSBRAIN_DIR"
    "$PYTHON" scripts/tennis_live_push.py
} >> "$LOG" 2>&1
EXIT_CODE=$?

health_finish "live_score_push" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
