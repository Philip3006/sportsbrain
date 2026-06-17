#!/bin/bash
# launchd wrapper: consumes pending PWA bets from Cloudflare KV
# and appends them to results/ledger.csv.
# Triggered every 5 minutes by com.sportsbrain.consume-pending-bets.plist

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/consume_pending_bets.log"

cd "$SPORTSBRAIN_DIR" || { echo "ERROR: could not cd to $SPORTSBRAIN_DIR"; exit 1; }

# Load env vars (SIGNALS_CLOUD_URL, SIGNALS_API_TOKEN)
if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
  set -a
  . "$SPORTSBRAIN_DIR/.env"
  set +a
fi

# Log every run so the dashboard health layer can see the latest successful
# completion instead of a stale historical error.
out=$(python3 -m scripts.consume_pending_bets 2>&1)
rc=$?
if [ "$rc" -ne 0 ] || [ -n "$out" ]; then
  echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] consume_pending_bets start ---" >> "$LOG"
  echo "$out" >> "$LOG"
  if [ "$rc" -eq 0 ]; then
    echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] consume_pending_bets done ---" >> "$LOG"
  else
    echo "--- [$(date '+%Y-%m-%d %H:%M:%S %Z')] consume_pending_bets error exit $rc ---" >> "$LOG"
  fi
fi
exit "$rc"
