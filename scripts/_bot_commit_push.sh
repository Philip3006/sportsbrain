#!/bin/bash
# P0D-001 — Bot commit + path-validated push.
#
# Usage: bash scripts/_bot_commit_push.sh "<commit message>" [log_path]
#
# Call AFTER staging (git add ...). This script:
#   1. Skips cleanly if nothing is staged.
#   2. Validates every staged path against _bot_permitted() — FAIL-CLOSED if any
#      staged path is outside the allowlist (source files, secrets, etc.).
#   3. Commits with the provided message + writer provenance annotation.
#   4. Pushes to origin/main with up to 5 fetch+rebase+push retries.
#
# Provenance: the git commit message includes the GITHUB_RUN_ID (when running in
# GitHub Actions) so that every runtime data commit is traceable to a workflow run.
#
# Exit codes:
#   0 — nothing staged (clean), OR commit+push succeeded
#   1 — forbidden staged path (fail-closed), OR push exhausted retries

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_git_safe_push.sh
source "$SCRIPT_DIR/_git_safe_push.sh"

MSG="${1:-auto: runtime data update $(date -u +%Y-%m-%dT%H:%M:%SZ)}"
LOG="${2:-/dev/stderr}"
TS() { date '+%Y-%m-%d %H:%M:%S %Z'; }

# Append run identity to commit message when inside GitHub Actions.
if [ -n "${GITHUB_RUN_ID:-}" ]; then
  MSG="$MSG [run=$GITHUB_RUN_ID]"
fi

# Step 1: Nothing staged → clean exit.
if git diff --staged --quiet 2>/dev/null; then
  echo "[$(TS)] _bot_commit_push: nothing staged — skip" >> "$LOG"
  exit 0
fi

# Step 2: Validate staged paths (fail-closed).
if ! bot_assert_staged_safe "$LOG"; then
  echo "[$(TS)] _bot_commit_push: FAIL CLOSED — forbidden staged path aborts commit" >> "$LOG"
  exit 1
fi

# Step 3: Commit.
git commit -m "$MSG" >> "$LOG" 2>&1 || {
  echo "[$(TS)] _bot_commit_push: git commit failed" >> "$LOG"
  exit 1
}

# Step 4: Push with retry (mirrors the pattern used by all standard runtime workflows).
for i in 1 2 3 4 5; do
  git fetch origin main >> "$LOG" 2>&1
  if ! git rebase --autostash --strategy-option=theirs origin/main >> "$LOG" 2>&1; then
    git rebase --abort >> "$LOG" 2>&1 || true
    echo "[$(TS)] _bot_commit_push: rebase conflict attempt $i" >> "$LOG"
    continue
  fi
  if git push origin main >> "$LOG" 2>&1; then
    echo "[$(TS)] _bot_commit_push: push ok (attempt $i) — $(git log origin/main -1 --oneline 2>/dev/null)" >> "$LOG"
    exit 0
  fi
  echo "[$(TS)] _bot_commit_push: push failed attempt $i, backoff..." >> "$LOG"
  sleep $((RANDOM % 20 + 10))
done

echo "[$(TS)] _bot_commit_push: push FAILED after 5 attempts" >> "$LOG"
exit 1
