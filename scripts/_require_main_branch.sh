#!/bin/bash
# Shared helper: fail-closed guard for launchd bots.
#
# Prevents launchd cron jobs from committing/pushing when the primary local
# working copy is not on `main`. Incident 2026-08-09: developer switched local
# checkout to phase-1/dev; 3 tennis_live_push commits were diverted to that
# branch instead of main, causing ~7 min of stale live-score data on the PWA
# and contaminating the Phase-1 dev branch.
#
# Root cause: bot trigger scripts have no branch assertion. `git commit` writes
# to whatever HEAD points at.
#
# Contract:
#   source scripts/_require_main_branch.sh
#   require_main_branch "<job_name>" "<log_path>"  || exit 42
#
# Return codes:
#   0  = on main, continue
#   42 = NOT on main, fail-closed (caller MUST exit non-zero for health monitor)
#
# Side effects when off-main:
#   - Appends [BRANCH-GUARD] line to log
#   - Writes health event via `python3 -m scripts._branch_guard_alert`
#     (soft-fails if python unavailable — logging is primary signal)

require_main_branch() {
  local job="${1:-unknown}"
  local log="${2:-/dev/stderr}"
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"

  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    echo "[$ts] [BRANCH-GUARD] $job: cannot determine branch (detached HEAD?) — fail-closed" >> "$log"
    _branch_guard_emit_health "$job" "detached_head" "$branch" 2>/dev/null || true
    return 42
  fi

  if [ "$branch" != "main" ]; then
    echo "[$ts] [BRANCH-GUARD] $job: refusing to run on non-main branch '$branch' — fail-closed" >> "$log"
    _branch_guard_emit_health "$job" "non_main_branch" "$branch" 2>/dev/null || true
    return 42
  fi

  return 0
}

_branch_guard_emit_health() {
  local job="$1"
  local reason="$2"
  local branch="$3"
  # Best-effort health event — use existing python-based health writer if present.
  # We do NOT create the whole health-json here; we let the caller's health_finish
  # produce a normal failure record. This helper additionally writes a
  # dedicated `branch_guard_last.json` marker so the aggregator can surface it.
  local PY
  PY="$(command -v python3 || true)"
  [ -z "$PY" ] && return 0
  local dir="results/health"
  mkdir -p "$dir" 2>/dev/null || true
  local out="$dir/branch_guard_last.json"
  "$PY" - <<PYEOF > "$out" 2>/dev/null || true
import json, os, datetime
print(json.dumps({
  "job": "$job",
  "reason": "$reason",
  "branch": "$branch",
  "ts_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
  "action": "fail_closed",
}, indent=2))
PYEOF
}
