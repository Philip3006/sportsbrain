#!/bin/bash
# Wrapper for launchd com.sportsbrain.settle: runs settle_bets.py with
# health-tracking + .env-sourced credentials (so the ODDS_API_KEY in .env
# is authoritative rather than a stale value baked into the plist).
#
# Hourly schedule is preserved by the plist's StartCalendarInterval.

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/settle.log"

cd "$SPORTSBRAIN_DIR" || exit 1

# Use .env as the single source of truth for ODDS_API_KEY.
if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
  set -a
  . "$SPORTSBRAIN_DIR/.env"
  set +a
fi

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "settle"

echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] settle started ---" >> "$LOG"
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    scripts/settle_bets.py >> "$LOG" 2>&1
EXIT_CODE=$?
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] settle finished (exit $EXIT_CODE) ---" >> "$LOG"

health_finish "settle" "$EXIT_CODE" "" "$LOG"

# Refresh the Claude session report — runs hourly via this plist, so the
# report is always ≤1h old when a new Claude-Code session starts.
python3 scripts/generate_session_report.py >> "$LOG" 2>&1 || true

exit "$EXIT_CODE"
