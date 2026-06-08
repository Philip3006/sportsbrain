#!/bin/bash
# Wrapper for launchd: updates closing odds for open bets.
# Runs silently if no open bets or outside tournament period.
cd /Users/philiprassillier/sportsbrain
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] closing_odds_cron started ---" >> /Users/philiprassillier/sportsbrain/results/closing_odds_cron.log 2>&1
python3 scripts/update_closing_odds.py >> /Users/philiprassillier/sportsbrain/results/closing_odds_cron.log 2>&1
