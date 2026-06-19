#!/bin/bash
# Shared helper: tracks per-job health for SportsBrain cron jobs.
# All wrapper scripts source this and call health_start/finish around their work.
#
# Usage:
#   source scripts/_health.sh
#   health_start "daily_scan"
#   python3 scripts/daily_scan.py
#   EXIT=$?
#   health_finish "daily_scan" $EXIT          # writes results/health/daily_scan.json
#   # optional: health_finish "daily_scan" $EXIT "espn"   (degraded with fallback)
#
# Auto-detects degraded status when:
#   - exit_code == 0 AND the script log contains the marker "USED STALE CACHE"
#     or "ESPN-Fallback aktiv" or "WebSearch-Fallback" → status=degraded
#
# Writes via Python module to keep schema in one place.

# -- internal state, set by health_start --
_HEALTH_START_TS=""
_HEALTH_RUN_ID=""

health_start() {
  local job="$1"
  _HEALTH_START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  _HEALTH_RUN_ID="${job}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  export HEALTH_RUN_ID="$_HEALTH_RUN_ID"
}

# health_finish <job> <exit_code> [explicit_fallback] [log_path]
# When log_path is supplied, the log tail is grepped for fallback markers and
# error text to auto-fill the status/error fields.
health_finish() {
  local job="$1"
  local exit_code="$2"
  local fallback="${3:-}"
  local log_path="${4:-}"
  local start_ts="${_HEALTH_START_TS:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  local run_id="${_HEALTH_RUN_ID:-${job}-manual}"

  # Duration in seconds (best-effort — works on macOS / GNU date).
  local duration_s=""
  if [ -n "$_HEALTH_START_TS" ]; then
    local started_epoch finished_epoch
    started_epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$_HEALTH_START_TS" "+%s" 2>/dev/null || echo "")
    finished_epoch=$(date -u +%s)
    if [ -n "$started_epoch" ]; then
      duration_s=$(( finished_epoch - started_epoch ))
    fi
  fi

  # Default status from exit code.
  local status="ok"
  if [ "$exit_code" -ne 0 ]; then
    status="error"
  fi

  # Auto-detect degraded from log markers (only when exit==0).
  local detected_fallback=""
  if [ -n "$log_path" ] && [ -f "$log_path" ] && [ "$exit_code" -eq 0 ]; then
    local tail_text
    tail_text=$(tail -n 200 "$log_path" 2>/dev/null)
    if echo "$tail_text" | grep -q -i "USED STALE CACHE"; then
      detected_fallback="stale_cache"
      status="degraded"
    elif echo "$tail_text" | grep -q -i "ESPN-Fallback aktiv"; then
      detected_fallback="espn"
      status="degraded"
    elif echo "$tail_text" | grep -q -i "WebSearch-Fallback"; then
      detected_fallback="websearch"
      status="degraded"
    fi
  fi

  # Explicit fallback wins.
  if [ -n "$fallback" ]; then
    detected_fallback="$fallback"
    if [ "$exit_code" -eq 0 ]; then
      status="degraded"
    fi
  fi

  # Extract error tail when status=error.
  local err_msg=""
  if [ "$status" = "error" ] && [ -n "$log_path" ] && [ -f "$log_path" ]; then
    err_msg=$(grep -E -i "error|exception|traceback|timeout|rejected" "$log_path" 2>/dev/null \
      | tail -n 3 \
      | tr '\n' ' ' \
      | cut -c1-500)
    if [ -z "$err_msg" ]; then
      err_msg="exit_code=$exit_code (no error keyword in log tail)"
    fi
  fi

  # Build python args.
  local args=(
    --job "$job"
    --status "$status"
    --exit-code "$exit_code"
    --started-at "$start_ts"
    --run-id "$run_id"
  )
  if [ -n "$duration_s" ]; then
    args+=(--duration "$duration_s")
  fi
  if [ -n "$detected_fallback" ]; then
    args+=(--fallback "$detected_fallback")
  fi
  if [ -n "$err_msg" ]; then
    args+=(--error "$err_msg")
  fi

  python3 -m src.monitoring.health_writer "${args[@]}" >/dev/null 2>&1 || true

  # Reset state to allow nested usage in the same shell.
  _HEALTH_START_TS=""
  _HEALTH_RUN_ID=""

  # Hook for Phase C: push notifications on failure are inserted here later.
  if [ "$status" = "error" ]; then
    # Stub — Phase C wires this up.
    if command -v health_push_on_fail >/dev/null 2>&1; then
      health_push_on_fail "$job" "$err_msg" || true
    fi
  fi
}
