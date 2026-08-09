#!/bin/bash
# Lokaler Live-Push-Trigger (alle 2 Min via launchd).
# Läuft tennis_live_push.py direkt + pusht docs/data/tennis_live_scores.json.
set -uo pipefail  # no -e: we want to record the exit code in health, not die

PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
GIT=/usr/bin/git
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/launchd_live_score_push.log"

source "$SPORTSBRAIN_DIR/scripts/_health.sh"
source "$SPORTSBRAIN_DIR/scripts/_git_safe_push.sh"
source "$SPORTSBRAIN_DIR/scripts/_require_main_branch.sh"
health_start "live_score_push"

# Fail-closed branch guard (incident 2026-08-09 — bot commits diverted to phase-1/dev).
if ! require_main_branch "live_score_push" "$LOG"; then
    health_finish "live_score_push" 42 "" "$LOG"
    exit 42
fi

timestamp() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

{
    echo "--- $(timestamp) ---"
    cd "$SPORTSBRAIN_DIR"
    "$PYTHON" scripts/live_score_push.py 2>&1 | grep -v "^$" || true
    "$PYTHON" scripts/tennis_live_push.py

    # Live-Scores in PWA aktualisieren (kein Commit wenn keine Änderung)
    "$GIT" add -f docs/data/tennis_live_scores.json data/cache/tennis_live_scores.json data/cache/tennis_suspended.json 2>/dev/null || true
    if ! "$GIT" diff --staged --quiet; then
        "$GIT" -c user.name="SportsBrain Bot" -c user.email="bot@sportsbrain" \
            commit -m "auto: tennis live $(date -u +%H:%M)" --quiet
        git_safe_push "$LOG"
    fi
} >> "$LOG" 2>&1
EXIT_CODE=$?

health_finish "live_score_push" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
