#!/bin/bash
# Layer 1 auto-healer: detects job errors and retries infrequent non-financial
# jobs once per error event. Runs every 10 min via com.sportsbrain.auto-heal.plist.
#
# P0D-003 BOUNDARY:
#   Autonomous retry scope is restricted to non-financial, non-model, idempotent jobs.
#   Financial jobs (settle, closing_odds) and model jobs (auto_retrain) are REMOVED
#   from autonomous retry — failures log + escalate only.
#   Push repair is restricted to a healer-specific runtime-data allowlist; any commit
#   containing source, financial, model, workflow, or auth files is NOT pushed.
#
# Handled automatically:
#   - daily_scan retry on error (non-financial, idempotent)
#   - Git push repair for runtime-data-only ahead commits
#
# NOT handled here (requires human authorization):
#   - Code bugs, novel errors
#   - Financial job failures (settle, closing_odds, consume_pending_bets)
#   - Model retraining failures (auto_retrain)

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

# daily_scan: daily job — retry after 2h cooldown (non-financial, idempotent)
if _should_retry "daily_scan" 7200; then
  _retry_job "daily_scan" "scripts/scan_cron.sh"
fi

# P0D-003: settle, auto_retrain, closing_odds retries REMOVED.
# These jobs touch financial state or model artifacts and require human authorization.
# Failures for these jobs are handled by GH Actions and VAPID escalation only.
#
# consume_pending_bets / live_score_push: skip — they retry every 2 min already

# ── Healer push repair (runtime-data only) ──────────────────────────────────
# P0D-003: Before pushing, verify that ALL files in ahead commits are safe
# runtime-data paths. Any commit touching source, financial, model, workflow,
# auth, or security files MUST NOT be pushed by the healer. Log + skip.

# Returns 0 (safe to push) if all ahead files are healer-permitted runtime data.
# Healer push allowlist is STRICTER than the general _bot_permitted in _git_safe_push.sh:
#   - NO results/ledger* (now private-repo-owned, P0D-002)
#   - NO results/betting_journal.md (now private-repo-owned, P0D-002)
#   - NO models/* (model promotion is Tier 3)
#   - NO scripts/*, src/*, tests/*, .github/*, *.py, *.sh, *.yml (source)
_healer_push_safe() {
  local AHEAD_FILES
  AHEAD_FILES=$(git -C "$SPORTSBRAIN_DIR" diff --name-only origin/main..main 2>/dev/null)
  if [ -z "$AHEAD_FILES" ]; then
    return 0  # nothing ahead
  fi

  local unsafe=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      # Allowed healer push paths: runtime-generated, non-financial, non-model
      docs/data/*|data/cache/*|data/live_scores.json|data/odds_history/*|\
      results/health/*|results/scans/*|\
      results/tennis_live_signals.json|results/tennis_scan_*|\
      results/tennis_cal_stats.json|\
      results/auto_heal*.log|results/auto_heal_state.json)
        ;;  # permitted runtime data
      *)
        _log "healer push BLOCKED — commit contains non-runtime file: $f"
        unsafe=1
        ;;
    esac
  done <<< "$AHEAD_FILES"

  [ "$unsafe" -eq 0 ]
}

LOCAL_AHEAD=$(git -C "$SPORTSBRAIN_DIR" rev-list --count origin/main..main 2>/dev/null || echo "0")
if [ "$LOCAL_AHEAD" -gt 0 ]; then
  if _healer_push_safe; then
    _log "$LOCAL_AHEAD commit(s) not pushed — healer push-repair (runtime-data verified)"
    # shellcheck source=./_git_safe_push.sh
    source "$SPORTSBRAIN_DIR/scripts/_git_safe_push.sh" && git_safe_push "$LOG"
  else
    _log "$LOCAL_AHEAD ahead commit(s) contain non-runtime files — healer push SKIPPED, human action required"
  fi
fi

exit 0
