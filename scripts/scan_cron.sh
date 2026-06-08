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

python3 scripts/daily_scan.py --auto-log --bankroll 100 --retrain >> "$LOG" 2>&1

EXIT_CODE=$?
echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] scan_cron finished (exit $EXIT_CODE) ---" >> "$LOG"
