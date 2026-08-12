#!/bin/bash
# Lokaler Live-Push-Trigger (alle 2 Min via launchd).
# Läuft tennis_live_push.py direkt + pusht docs/data/tennis_live_scores.json.
set -uo pipefail  # no -e: we want to record the exit code in health, not die

PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
GIT=/usr/bin/git
SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LOG="$SPORTSBRAIN_DIR/results/launchd_live_score_push.log"

# Fail closed if local main is more than this many commits ahead of origin/main.
# Ephemeral live-score data is regenerated each run, so falling back to origin/main
# is always safe. A push failure that survives 3+ consecutive ticks signals a broken
# remote channel that needs human attention — not more commits on top of it.
MAX_DIVERGE=3

source "$SPORTSBRAIN_DIR/scripts/_health.sh"
source "$SPORTSBRAIN_DIR/scripts/_git_safe_push.sh"
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

    # ── Pre-commit sync ───────────────────────────────────────────────────────
    # Synchronize against origin/main BEFORE generating or committing runtime
    # state. Live-score files are ephemeral (regenerated every tick), so resetting
    # to origin/main is safe and prevents unbounded accumulation of stale commits.
    "$GIT" fetch origin main >> "$LOG" 2>&1 || \
        echo "[$(timestamp)] live_score_push: fetch failed (network?), continuing with local state"

    AHEAD=$("$GIT" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)

    if [ "$AHEAD" -gt "$MAX_DIVERGE" ]; then
        echo "[$(timestamp)] live_score_push: FAIL CLOSED — local main is $AHEAD commits ahead of origin/main (limit=$MAX_DIVERGE). Push channel broken; not creating more commits."
        health_finish "live_score_push" 42 "diverged_${AHEAD}" "$LOG"
        exit 42
    fi

    if [ "$AHEAD" -gt 0 ]; then
        # Only reset if working tree is clean — never discard uncommitted human work.
        DIRTY=$("$GIT" status --porcelain | grep -v '^??' | wc -l | tr -d ' ')
        if [ "$DIRTY" -gt 0 ]; then
            echo "[$(timestamp)] live_score_push: SKIP reset — working tree has $DIRTY uncommitted change(s); proceeding without reset"
        else
            echo "[$(timestamp)] live_score_push: local main is $AHEAD commit(s) ahead — resetting to origin/main before generating fresh data"
            "$GIT" reset --hard origin/main >> "$LOG" 2>&1
        fi
    fi

    # ── Generate fresh data ───────────────────────────────────────────────────
    "$PYTHON" scripts/live_score_push.py 2>&1 | grep -v "^$" || true
    "$PYTHON" scripts/tennis_live_push.py

    # ── Commit and push (fail-closed on push failure) ─────────────────────────
    "$GIT" add -f docs/data/tennis_live_scores.json data/cache/tennis_live_scores.json data/cache/tennis_suspended.json 2>/dev/null || true
    if ! "$GIT" diff --staged --quiet; then
        "$GIT" -c user.name="SportsBrain Bot" -c user.email="bot@sportsbrain" \
            commit -m "auto: tennis live $(date -u +%H:%M)" --quiet
        if ! git_safe_push "$LOG"; then
            echo "[$(timestamp)] live_score_push: push FAILED after all retries — emitting health alert, not creating more commits"
            health_finish "live_score_push" 42 "push_failed" "$LOG"
            exit 42
        fi
    fi
} >> "$LOG" 2>&1
EXIT_CODE=$?

health_finish "live_score_push" "$EXIT_CODE" "" "$LOG"
exit "$EXIT_CODE"
