#!/bin/bash
# Auto-retrain cron wrapper — invoked by launchd every 12h during WM 2026.
# Fetches fresh martj42 CSV (force=True bypasses cache), retrains DC + LGBM
# when new WM 2026 matches are present.
set -uo pipefail   # no -e: we want to record the exit code in health

SPORTSBRAIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SPORTSBRAIN_DIR" || exit 1
LOG="$SPORTSBRAIN_DIR/results/launchd_auto_retrain.log"

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "auto_retrain"

TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $TS auto_retrain start =====" | tee -a "$LOG"

/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    scripts/auto_retrain.py 2>&1 | tee -a "$LOG"
EXIT_CODE=${PIPESTATUS[0]}

echo "===== $TS auto_retrain done =====" | tee -a "$LOG"

health_finish "auto_retrain" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
