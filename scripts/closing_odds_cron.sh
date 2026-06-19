#!/bin/bash
# Wrapper for launchd: updates closing odds for open bets.
# Runs silently if no open bets or outside tournament period.
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/closing_odds_cron.log"
cd "$SPORTSBRAIN_DIR" || exit 1
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] closing_odds_cron started ---" >> "$LOG" 2>&1
python3 scripts/update_closing_odds.py >> "$LOG" 2>&1

# Push signals.json to GitHub Pages (safe push: rebase first)
git add docs/data/signals.json >> "$LOG" 2>&1
git commit -m "auto: closing odds $(date '+%Y-%m-%d %H:%M')" >> "$LOG" 2>&1 || true
# shellcheck source=./_git_safe_push.sh
source "$SPORTSBRAIN_DIR/scripts/_git_safe_push.sh"
git_safe_push "$LOG"
