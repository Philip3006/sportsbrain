#!/bin/bash
# Wrapper for launchd: runs SportsBrain daily scan with auto-logging.
# Triggered at 07:00 UTC (09:00 CET) by com.sportsbrain.daily-scan.plist

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/scan_cron.log"

cd "$SPORTSBRAIN_DIR" || { echo "ERROR: could not cd to $SPORTSBRAIN_DIR"; exit 1; }

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
# shellcheck source=./_require_main_branch.sh
source "$SPORTSBRAIN_DIR/scripts/_require_main_branch.sh"
health_start "daily_scan"

# Fail-closed branch guard (incident 2026-08-09).
if ! require_main_branch "daily_scan" "$LOG"; then
    health_finish "daily_scan" 42 "" "$LOG"
    exit 42
fi

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] scan_cron started ---" >> "$LOG"
echo "========================================" >> "$LOG"

# 1. Auto-settle completed matches
echo "--- Settle bets ---" >> "$LOG"
python3 scripts/settle_bets.py >> "$LOG" 2>&1

# 2. Refresh injury news (DDG search, all 48 teams)
echo "--- Injury refresh ---" >> "$LOG"
python3 scripts/refresh_injuries.py >> "$LOG" 2>&1

# 3. Daily value scan — bankroll from weekly snapshot (tier-aware stakes)
BANKROLL=$(python3 -c "
from src.betting.ledger import get_bankroll_snapshot
print(get_bankroll_snapshot())
" 2>/dev/null || echo "100")
echo "--- Bankroll (snapshot): €$BANKROLL ---" >> "$LOG"
echo "--- Daily scan ---" >> "$LOG"
python3 scripts/daily_scan.py --bankroll "$BANKROLL" --retrain >> "$LOG" 2>&1

JOB_EXIT=$?
PUBLISH_EXIT=0

# 4. Publish generated output through the isolated publisher checkout.
source "$SPORTSBRAIN_DIR/scripts/publish_runtime_artifacts.sh"
runtime_publish_artifacts "$SPORTSBRAIN_DIR" "$LOG" \
    "auto: scan $(date '+%Y-%m-%d')" docs/data/signals.json
PUBLISH_EXIT=$?
EXIT_CODE=$JOB_EXIT
if [ "$EXIT_CODE" -eq 0 ] && [ "$PUBLISH_EXIT" -ne 0 ]; then
    EXIT_CODE=42
fi
echo "--- Publish done (job=$JOB_EXIT publish=$PUBLISH_EXIT final=$EXIT_CODE) ---" >> "$LOG"

echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] scan_cron finished (exit $EXIT_CODE) ---" >> "$LOG"

# Health-status: track exit + auto-detect fallback markers in the log tail.
health_finish "daily_scan" "$EXIT_CODE" "" "$LOG"
