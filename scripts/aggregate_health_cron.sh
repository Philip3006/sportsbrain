#!/bin/bash
# Dedicated local health carrier. It must remain independent of financial intent handling.
set -uo pipefail

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/health_aggregate.log"

cd "$SPORTSBRAIN_DIR" || exit 1

# Health publication uses the same protected local configuration as the former carrier.
if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
  set -a
  . "$SPORTSBRAIN_DIR/.env"
  set +a
fi

# shellcheck source=./_health.sh
source "$SPORTSBRAIN_DIR/scripts/_health.sh"
health_start "aggregate_health"

# The active checkout is never a runtime artifact publisher.  Until the
# Cloudflare per-authority merge contract is deployed, fail closed rather than
# allowing an old local aggregate to replace newer cloud health.
python3 -m src.monitoring.aggregate_health --quiet --no-write --no-upload >> "$LOG" 2>&1
EXIT_CODE=$?

health_finish "aggregate_health" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
