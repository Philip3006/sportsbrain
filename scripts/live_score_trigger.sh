#!/bin/bash
# Triggers live_score_push.yml on GitHub Actions every 5 min via launchd.
# Falls der GH Actions cron nicht zuverlässig feuert (neu hinzugefügte Workflows).
set -euo pipefail

GH=/opt/homebrew/bin/gh
REPO="Philip3006/sportsbrain"
LOG="/Users/philiprassillier/sportsbrain/results/launchd_live_score_push.log"

timestamp() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

{
    echo "--- $(timestamp) ---"
    "$GH" workflow run live_score_push.yml --repo "$REPO" && \
        echo "Workflow getriggert." || \
        echo "WARNUNG: gh workflow run fehlgeschlagen."
} >> "$LOG" 2>&1
