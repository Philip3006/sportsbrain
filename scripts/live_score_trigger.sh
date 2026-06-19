#!/bin/bash
# Triggers live_score_push.yml on GitHub Actions every 5 min via launchd.
# Falls der GH Actions cron nicht zuverlässig feuert (neu hinzugefügte Workflows).
set -uo pipefail  # no -e: we want to record the exit code in health, not die

GH=/opt/homebrew/bin/gh
REPO="Philip3006/sportsbrain"
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/launchd_live_score_push.log"

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "live_score_push"

timestamp() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

{
    echo "--- $(timestamp) ---"
    "$GH" workflow run live_score_push.yml --repo "$REPO" && \
        echo "Workflow getriggert." || \
        echo "WARNUNG: gh workflow run fehlgeschlagen."
} >> "$LOG" 2>&1
EXIT_CODE=$?

health_finish "live_score_push" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
