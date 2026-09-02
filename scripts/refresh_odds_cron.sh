#!/bin/bash
# Wrapper for launchd com.sportsbrain.odds-refresh: runs refresh_odds.py with
# health-tracking + .env-sourced credentials, matching the pattern used by
# settle_cron.sh and other production job wrappers.
#
# Every-5-minute cadence is preserved by the plist's StartInterval.

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/launchd_odds_refresh.log"

cd "$SPORTSBRAIN_DIR" || exit 1

# Use .env as the single source of truth for ODDS_API_KEY.
if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
  set -a
  . "$SPORTSBRAIN_DIR/.env"
  set +a
fi

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "odds_refresh"

echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] odds_refresh started ---" >> "$LOG"
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    scripts/refresh_odds.py "$@" >> "$LOG" 2>&1
EXIT_CODE=$?
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] odds_refresh finished (exit $EXIT_CODE) ---" >> "$LOG"

health_finish "odds_refresh" "$EXIT_CODE" "" "$LOG"

exit "$EXIT_CODE"
