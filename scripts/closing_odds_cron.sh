#!/bin/bash
# Wrapper for launchd: updates closing odds for open bets.
# Runs silently if no open bets or outside tournament period.
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="/Users/philiprassillier/Library/Logs/sportsbrain_closing_odds.log"
mkdir -p "/Users/philiprassillier/Library/Caches/SportsBrain" || exit 1
RUNTIME_STAGE_DIR="$(mktemp -d /Users/philiprassillier/Library/Caches/SportsBrain/closing-odds.XXXXXX)" || exit 1
export SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR="$RUNTIME_STAGE_DIR"
cd "$SPORTSBRAIN_DIR" || exit 1

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
# shellcheck source=./_require_main_branch.sh
source "$SPORTSBRAIN_DIR/scripts/_require_main_branch.sh"
health_start "closing_odds"

# Fail-closed branch guard (incident 2026-08-09).
if ! require_main_branch "closing_odds" "$LOG"; then
    health_finish "closing_odds" 42 "" "$LOG"
    exit 42
fi

echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] closing_odds_cron started ---" >> "$LOG" 2>&1
python3 scripts/update_closing_odds.py >> "$LOG" 2>&1
JOB_EXIT=$?
PUBLISH_EXIT=0

# Rebuild public signals from the private ledger into staging, then publish
# only the staged artifacts through the isolated checkout.
if [ "$JOB_EXIT" -eq 0 ]; then
    python3 -c 'from src.notifications.web_dashboard import write_signals_json_all_users; failed = write_signals_json_all_users(football=[], tennis=[]); raise SystemExit(1 if failed else 0)' >> "$LOG" 2>&1
    JOB_EXIT=$?
fi
source "$SPORTSBRAIN_DIR/scripts/publish_runtime_artifacts.sh"
runtime_publish_staged_artifacts "$SPORTSBRAIN_DIR" "$RUNTIME_STAGE_DIR" "$LOG" \
    "auto: closing odds $(date '+%Y-%m-%d %H:%M')" \
    docs/data/signals.json docs/data/signals_philip.json
PUBLISH_EXIT=$?
EXIT_CODE=$JOB_EXIT
if [ "$EXIT_CODE" -eq 0 ] && [ "$PUBLISH_EXIT" -ne 0 ]; then
    EXIT_CODE=42
fi
echo "--- Publish done (job=$JOB_EXIT publish=$PUBLISH_EXIT final=$EXIT_CODE) ---" >> "$LOG"

health_finish "closing_odds" "$EXIT_CODE" "" "$LOG"
