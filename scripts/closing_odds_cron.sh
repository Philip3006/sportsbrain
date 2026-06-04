#!/bin/bash
# Wrapper for cron: updates closing odds for open bets.
# Runs silently if no open bets or outside tournament period.
cd /Users/philiprassillier/sportsbrain
python3 scripts/update_closing_odds.py >> /Users/philiprassillier/sportsbrain/results/closing_odds.log 2>&1
