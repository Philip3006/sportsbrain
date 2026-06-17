#!/bin/bash
# Wrapper for launchd: updates closing odds for open bets.
# Runs silently if no open bets or outside tournament period.
cd /Users/philiprassillier/sportsbrain
LOG="/Users/philiprassillier/sportsbrain/results/closing_odds_cron.log"
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] closing_odds_cron started ---" >> "$LOG" 2>&1
python3 scripts/update_closing_odds.py >> "$LOG" 2>&1

# Refresh dashboard JSON/KV so CLV/open-bet state reflects the ledger.
python3 -c 'from src.notifications.web_dashboard import write_signals_json; write_signals_json()' >> "$LOG" 2>&1

# Push only repo-backed automation artifacts. Use autostash so unrelated local
# edits do not block a rebase, but never stage those unrelated edits.
git add results/ledger.csv docs/data/signals.json >> "$LOG" 2>&1
if ! git diff --cached --quiet -- results/ledger.csv docs/data/signals.json; then
  git commit -m "auto: closing odds $(date '+%Y-%m-%d %H:%M')" >> "$LOG" 2>&1 || true
  pushed=0
  for attempt in 1 2 3 4 5; do
    git pull --rebase --autostash origin main >> "$LOG" 2>&1 || continue
    if git push origin main >> "$LOG" 2>&1; then
      pushed=1
      break
    fi
    sleep $((attempt + 1))
  done
  if [ "$pushed" -ne 1 ]; then
    echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] closing_odds_cron error: push failed ---" >> "$LOG" 2>&1
    exit 1
  fi
fi

echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] closing_odds_cron done ---" >> "$LOG" 2>&1
