#!/bin/bash
# launchd wrapper: consumes pending PWA bets from Cloudflare KV
# and appends them to results/ledger.csv.
# Triggered every 2 minutes by com.sportsbrain.consume-pending-bets.plist
# Also acts as the carrier for the health-aggregator (no separate plist needed).

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/consume_pending_bets.log"
HEALTH_LOG="$SPORTSBRAIN_DIR/results/health_aggregate.log"

cd "$SPORTSBRAIN_DIR" || { echo "ERROR: could not cd to $SPORTSBRAIN_DIR"; exit 1; }

# Load env vars (SIGNALS_CLOUD_URL, SIGNALS_API_TOKEN) for both consume and aggregator.
if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
  set -a
  . "$SPORTSBRAIN_DIR/.env"
  set +a
fi

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"

# --- consume pending bets ---
health_start "consume_pending_bets"
out=$(python3 -m scripts.consume_pending_bets 2>&1)
EXIT_CODE=$?
# Only log lines when something interesting happens (skip silent "no pending")
if [ -n "$out" ] && [ "$out" != "[consume] no pending bets" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $out" >> "$LOG"
fi
# Always finish — let aggregator see "alive" signals every 2 min.
health_finish "consume_pending_bets" "$EXIT_CODE" "" "$LOG"

# --- piggy-back: aggregate health every tick ---
health_start "aggregate_health"
python3 -m src.monitoring.aggregate_health --quiet >> "$HEALTH_LOG" 2>&1
AGG_EXIT=$?
health_finish "aggregate_health" "$AGG_EXIT" "" "$HEALTH_LOG"
