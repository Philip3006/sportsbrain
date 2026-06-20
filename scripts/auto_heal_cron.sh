#!/bin/bash
# Layer 1 auto-healer: detects job errors and retries infrequent jobs once per
# error event. Runs every 10 min via com.sportsbrain.auto-heal.plist (24/7).
#
# Handled automatically:
#   - Job retry on error (with per-job cooldown to prevent retry storms)
#   - Git push repair (if local commits are not pushed to origin)
#
# NOT handled here (requires Layer 2 / Claude CronCreate):
#   - Code bugs, API key issues, novel errors that need diagnosis+code-edit

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/auto_heal.log"
STATE_FILE="$SPORTSBRAIN_DIR/results/auto_heal_state.json"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"

cd "$SPORTSBRAIN_DIR" || exit 1

if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
  set -a; . "$SPORTSBRAIN_DIR/.env"; set +a
fi

NOW_EPOCH=$(date -u +%s)

_log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] [auto_heal] $*" >> "$LOG"
}

# Read a value from the JSON state file (returns "" if missing)
_state_get() {
  local key="$1"
  [ -f "$STATE_FILE" ] || { echo ""; return; }
  "$PYTHON" -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('$key', ''))
except Exception:
    print('')
" 2>/dev/null
}

# Write a key=value pair to the JSON state file
_state_set() {
  local key="$1"
  local value="$2"
  "$PYTHON" - <<PYEOF 2>/dev/null
import json, os
path = '$STATE_FILE'
d = {}
if os.path.exists(path):
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
d['$key'] = '$value'
json.dump(d, open(path, 'w'), indent=2)
PYEOF
}

# Returns 0 (true) if job should be retried: status=error AND cooldown elapsed
_should_retry() {
  local job="$1"
  local cooldown_s="$2"
  local health_file="$SPORTSBRAIN_DIR/results/health/${job}.json"

  [ -f "$health_file" ] || return 1

  local status
  status=$("$PYTHON" -c "
import json
try:
    d = json.load(open('$health_file'))
    print(d.get('status', ''))
except Exception:
    print('')
" 2>/dev/null)

  [ "$status" = "error" ] || return 1

  local last_retry
  last_retry=$(_state_get "${job}_last_retry_epoch")

  if [ -z "$last_retry" ] || [ "$last_retry" = "0" ]; then
    return 0  # Never retried for this error event
  fi

  local elapsed=$(( NOW_EPOCH - last_retry ))
  [ "$elapsed" -ge "$cooldown_s" ]
}

# Run a retry of the given job via its cron wrapper
_retry_job() {
  local job="$1"
  local script="$2"
  _log "$job error detected — retrying via $script"
  _state_set "${job}_last_retry_epoch" "$NOW_EPOCH"
  bash "$SPORTSBRAIN_DIR/$script" >> "$LOG" 2>&1
  local exit_code=$?
  _log "$job retry finished (exit $exit_code)"
}

# ── Job retry checks ────────────────────────────────────────────────────────

# settle: hourly job — retry after 30 min cooldown
if _should_retry "settle" 1800; then
  _retry_job "settle" "scripts/settle_cron.sh"
fi

# daily_scan: daily job — retry after 2h cooldown
if _should_retry "daily_scan" 7200; then
  _retry_job "daily_scan" "scripts/scan_cron.sh"
fi

# auto_retrain: 12h job — retry after 3h cooldown
if _should_retry "auto_retrain" 10800; then
  _retry_job "auto_retrain" "scripts/auto_retrain_cron.sh"
fi

# closing_odds: 12h job — retry after 2h cooldown
if _should_retry "closing_odds" 7200; then
  _retry_job "closing_odds" "scripts/closing_odds_cron.sh"
fi

# consume_pending_bets / live_score_push: skip — they retry every 2 min already

# ── Git push repair ─────────────────────────────────────────────────────────
LOCAL_AHEAD=$(git -C "$SPORTSBRAIN_DIR" rev-list --count origin/main..main 2>/dev/null || echo "0")
if [ "$LOCAL_AHEAD" -gt 0 ]; then
  _log "$LOCAL_AHEAD commit(s) not pushed — running _git_safe_push.sh"
  bash "$SPORTSBRAIN_DIR/scripts/_git_safe_push.sh" >> "$LOG" 2>&1
fi

exit 0
