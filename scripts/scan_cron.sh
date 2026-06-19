#!/bin/bash
# Wrapper for launchd: runs SportsBrain daily scan with auto-logging.
# Triggered at 07:00 UTC (09:00 CET) by com.sportsbrain.daily-scan.plist

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/scan_cron.log"

cd "$SPORTSBRAIN_DIR" || { echo "ERROR: could not cd to $SPORTSBRAIN_DIR"; exit 1; }

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

EXIT_CODE=$?

# 4. Push signals.json to GitHub Pages (safe push: rebase first)
echo "--- Git push ---" >> "$LOG"
git add docs/data/signals.json >> "$LOG" 2>&1
git commit -m "auto: scan $(date '+%Y-%m-%d')" >> "$LOG" 2>&1
# shellcheck source=./_git_safe_push.sh
source "$SPORTSBRAIN_DIR/scripts/_git_safe_push.sh"
git_safe_push "$LOG"
PUSH_EXIT=$?
echo "--- Git push done (exit $PUSH_EXIT) ---" >> "$LOG"

echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] scan_cron finished (exit $EXIT_CODE) ---" >> "$LOG"
