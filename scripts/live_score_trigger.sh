#!/bin/bash
# Lokaler Live-Push-Trigger (alle 2 Min via launchd).
# Läuft tennis_live_push.py direkt + pusht docs/data/tennis_live_scores.json.
set -uo pipefail  # no -e: we want to record the exit code in health, not die

PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="/Users/philiprassillier/Library/Logs/sportsbrain_live_score_push.log"
RUNTIME_STATE_DIR="/Users/philiprassillier/Library/Application Support/SportsBrain/runtime-state"
mkdir -p "/Users/philiprassillier/Library/Caches/SportsBrain" "$RUNTIME_STATE_DIR" || exit 1
RUNTIME_STAGE_DIR="$(mktemp -d /Users/philiprassillier/Library/Caches/SportsBrain/live-score-push.XXXXXX)" || exit 1
if [ -f "$SPORTSBRAIN_DIR/.env" ]; then
    set -a
    . "$SPORTSBRAIN_DIR/.env"
    set +a
fi
export SPORTSBRAIN_RUNTIME_STATE_DIR="$RUNTIME_STATE_DIR"
export SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR="$RUNTIME_STAGE_DIR"

source "$SPORTSBRAIN_DIR/scripts/_health.sh"
source "$SPORTSBRAIN_DIR/scripts/_require_main_branch.sh"
health_start "live_score_push"

# Fail-closed branch guard (incident 2026-08-09 — bot commits diverted to phase-1/dev).
if ! require_main_branch "live_score_push" "$LOG"; then
    health_finish "live_score_push" 42 "" "$LOG"
    exit 42
fi

timestamp() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

{
    echo "--- $(timestamp) ---"
    cd "$SPORTSBRAIN_DIR"

    JOB_EXIT=0
    # ── Generate fresh data ───────────────────────────────────────────────────
    "$PYTHON" scripts/live_score_push.py 2>&1 | grep -v "^$"
    LIVE_EXIT=${PIPESTATUS[0]}
    "$PYTHON" scripts/tennis_live_push.py
    TENNIS_EXIT=$?
    JOB_EXIT=0
    if [ "$LIVE_EXIT" -ne 0 ]; then
        JOB_EXIT=$LIVE_EXIT
    elif [ "$TENNIS_EXIT" -ne 0 ]; then
        JOB_EXIT=$TENNIS_EXIT
    fi
    PUBLISH_EXIT=0
    source "$SPORTSBRAIN_DIR/scripts/publish_runtime_artifacts.sh"
    PUBLISH_PATHS=(
        docs/data/live_scores.json
        docs/data/tennis_live_scores.json
        data/cache/tennis_live_scores.json
    )
    if [ -f "$RUNTIME_STAGE_DIR/data/cache/tennis_suspended.json" ]; then
        PUBLISH_PATHS+=(data/cache/tennis_suspended.json)
    fi
    runtime_publish_staged_artifacts "$SPORTSBRAIN_DIR" "$RUNTIME_STAGE_DIR" "$LOG" \
        "auto: live scores $(date -u +%H:%M)" \
        "${PUBLISH_PATHS[@]}"
    PUBLISH_EXIT=$?
    if [ "$JOB_EXIT" -eq 0 ] && [ "$PUBLISH_EXIT" -ne 0 ]; then
        JOB_EXIT=42
    fi
    echo "--- Publish done (job=$JOB_EXIT publish=$PUBLISH_EXIT final=$JOB_EXIT) ---"
} >> "$LOG" 2>&1
EXIT_CODE=$JOB_EXIT

health_finish "live_score_push" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
