#!/bin/bash
# Pre-match scanner: runs 45-90 min before any scheduled game.
# Triggered every 20 min by com.sportsbrain.prematch-scan.plist
# Skips if no game in window, or if cache is fresh (<25 min old).

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/prematch_scan_cron.log"
LOCKFILE="$SPORTSBRAIN_DIR/results/prematch_scan.lock"

cd "$SPORTSBRAIN_DIR" || exit 1

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "prematch_scan"

# Check if a game is starting in 45–90 minutes (pre-match window)
# OR it is 21:30–22:15 UTC and there are night games after 22:00 UTC (midnight Berlin = 22:00 UTC)
WINDOW_RESULT=$(python3 - <<'PYEOF'
import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

p = Path("docs/data/signals.json")
if not p.exists():
    sys.exit(1)

try:
    data = json.loads(p.read_text())
except Exception:
    sys.exit(1)

now = datetime.now(timezone.utc)

# --- Condition 1: game starts in 45–90 minutes ---
lo = now + timedelta(minutes=45)
hi = now + timedelta(minutes=90)
for g in data.get("schedule", []):
    ko = g.get("kickoff", "")
    if not ko:
        continue
    try:
        ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
        if lo <= ko_dt <= hi:
            print(f'{g["home"]} vs {g["away"]} @ {ko_dt.strftime("%H:%M UTC")} (pre-match)')
            sys.exit(0)
    except Exception:
        continue

# --- Condition 2: midnight-Berlin deadline (21:30–22:15 UTC) for night games ---
# Night games = kickoff after 22:00 UTC today through 08:00 UTC next day
midnight_window = (now.hour == 21 and now.minute >= 30) or (now.hour == 22 and now.minute <= 15)
if midnight_window:
    night_lo = now.replace(hour=22, minute=0, second=0, microsecond=0)
    night_hi = night_lo + timedelta(hours=10)  # up to 08:00 UTC next day
    for g in data.get("schedule", []):
        ko = g.get("kickoff", "")
        if not ko:
            continue
        try:
            ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
            if night_lo <= ko_dt <= night_hi:
                print(f'{g["home"]} vs {g["away"]} @ {ko_dt.strftime("%H:%M UTC")} (Nacht-Deadline)')
                sys.exit(0)
        except Exception:
            continue

sys.exit(1)
PYEOF
)

if [ $? -ne 0 ]; then
    # No game in window — silent noop, but report alive for health-tracking.
    health_finish "prematch_scan" 0 "" ""
    exit 0
fi

# Rate-limit: skip if we scanned in the last 25 minutes
if [ -f "$LOCKFILE" ]; then
    LAST=$(cat "$LOCKFILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    DIFF=$(( NOW - LAST ))
    if [ "$DIFF" -lt 1500 ]; then
        health_finish "prematch_scan" 0 "" ""
        exit 0
    fi
fi

# Write lock
date +%s > "$LOCKFILE"

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] prematch_scan gestartet ---" >> "$LOG"
echo "--- Spiel im Fenster: $WINDOW_RESULT ---" >> "$LOG"
echo "========================================" >> "$LOG"

# Get current bankroll from weekly snapshot (tier-aware stakes)
BANKROLL=$(python3 -c "
from src.betting.ledger import get_bankroll_snapshot
print(get_bankroll_snapshot())
" 2>/dev/null || echo "100")

echo "--- Bankroll: €$BANKROLL ---" >> "$LOG"

# Settle first
echo "--- Settle ---" >> "$LOG"
python3 scripts/settle_bets.py >> "$LOG" 2>&1

# Run scan
echo "--- Scan (--force, kein --auto-log) ---" >> "$LOG"
python3 scripts/daily_scan.py --bankroll "$BANKROLL" --force >> "$LOG" 2>&1

EXIT_CODE=$?
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] prematch_scan fertig (exit $EXIT_CODE) ---" >> "$LOG"

health_finish "prematch_scan" "$EXIT_CODE" "" "$LOG"
