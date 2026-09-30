#!/bin/bash
# Shared helper: pull --rebase before push to avoid race conditions
# between local cron jobs and GitHub Actions pushing to the same branch.
#
# Usage in a wrapper:
#   source scripts/_git_safe_push.sh
#   git_safe_push "<log_path>"
#
# Strategy:
#   - Acquire process-level lock (/tmp/sportsbrain_push.lock) so concurrent
#     local cron jobs are serialised instead of all racing to push at once.
#   - Inside the lock: fetch + rebase with --strategy-option=theirs, then push.
#   - 3 retries handle GH-Actions pushes that may arrive while we hold the lock.
#   - Lock timeout: 90 s (any push takes <30 s normally; 90 s is generous).
#
# Note: during `git pull --rebase`, "theirs" = the LOCAL commit being replayed
# (not remote). Bots should only touch permitted runtime-data paths (see
# _bot_permitted below). Source-file conflicts cause fail-closed.

_GIT_SAFE_PUSH_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# === Source-file guard ===
# Permitted paths for bot commits. Anything outside this set is a source file;
# conflicts or staged changes in source files cause fail-closed.
_bot_permitted() {
  local f="$1"
  case "$f" in
    docs/data/*|data/cache/*|data/live_scores.json|data/odds_history/*|\
    results/audits/continuous_model_lifecycle_registry.json|\
    results/research/nations_league_v1_1_live_prediction_store.jsonl|\
    results/health/*|results/scans/*|\
    results/tennis_live_signals.json|results/tennis_scan_*|\
    results/tennis_cal_stats.json|\
    models/dc_bundesliga2/*|models/tennis_lgbm*|\
    models/tennis/*|models/tennis_calibrators/*)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

# Public: call before any bot `git commit`.
# Returns 1 (and logs) if any staged file is outside permitted bot paths.
bot_assert_staged_safe() {
  local LOG="${1:-/dev/stderr}"
  local TS; TS="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  local bad=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if ! _bot_permitted "$f"; then
      echo "[$TS] bot_assert_staged_safe: FORBIDDEN staged file: $f" >> "$LOG"
      bad=1
    fi
  done < <(git diff --staged --name-only 2>/dev/null)
  [ "$bad" -eq 0 ] || return 1
}

# Pre-flight: resolve any stuck unmerged files left by a previous failed autostash apply.
# This happens when git pull --rebase --autostash hits a conflict during stash pop:
# the rebase itself finishes but the autostash apply leaves unmerged index entries,
# blocking all subsequent git operations. We resolve by taking the working-tree version
# (which is already the merged result from the failed apply) and staging it.
# Signals-specific merge: try to preserve the union of football/tennis signals
# instead of blindly taking --theirs, which has repeatedly wiped locally-scanned
# signals during the incident window (2026-07-06 audit: only 7 match_ids left).
# Requires jq — falls back to caller's default resolution otherwise.
_git_signals_json_merge() {
  local f="$1"
  local LOG="$2"
  local TS
  TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  if ! command -v jq >/dev/null 2>&1; then
    echo "[$TS] git_safe_push: jq missing — skipping signals merge for $f" >> "$LOG"
    return 1
  fi
  local ours_tmp theirs_tmp merged_tmp
  ours_tmp="$(mktemp)"; theirs_tmp="$(mktemp)"; merged_tmp="$(mktemp)"
  if ! git show ":2:$f" > "$ours_tmp" 2>>"$LOG" || ! git show ":3:$f" > "$theirs_tmp" 2>>"$LOG"; then
    echo "[$TS] git_safe_push: cannot extract stages for $f" >> "$LOG"
    rm -f "$ours_tmp" "$theirs_tmp" "$merged_tmp"
    return 1
  fi
  # Base = theirs (remote); union in ours' football/tennis signals by (match_id, market).
  # Ours' all_odds and model_tips also unioned (theirs wins on key collision — remote is newer).
  if jq -s '
    def by_key(k1;k2): [.[] | {key: ((.[k1] // "") + "|" + (.[k2] // "")), value: .}] | from_entries;
    .[1] as $theirs | .[0] as $ours |
    ($theirs.football // []) as $tf | ($ours.football // []) as $of |
    ($tf | by_key("match_id";"market")) as $tfm |
    ($of | by_key("match_id";"market")) as $ofm |
    ($theirs.tennis // []) as $tt | ($ours.tennis // []) as $ot |
    ($tt | by_key("match_id";"market")) as $ttm |
    ($ot | by_key("match_id";"market")) as $otm |
    $theirs
      | .football = (($ofm + $tfm) | to_entries | map(.value))
      | .tennis   = (($otm + $ttm) | to_entries | map(.value))
      | .all_odds   = (($ours.all_odds   // {}) + ($theirs.all_odds   // {}))
      | .model_tips = (($ours.model_tips // {}) + ($theirs.model_tips // {}))
  ' "$ours_tmp" "$theirs_tmp" > "$merged_tmp" 2>>"$LOG"; then
    # Sanity check: merged must have same or more football+tennis entries than theirs
    local n_ours n_theirs n_merged
    n_ours=$(jq '((.football // []) | length) + ((.tennis // []) | length)' "$ours_tmp" 2>/dev/null || echo 0)
    n_theirs=$(jq '((.football // []) | length) + ((.tennis // []) | length)' "$theirs_tmp" 2>/dev/null || echo 0)
    n_merged=$(jq '((.football // []) | length) + ((.tennis // []) | length)' "$merged_tmp" 2>/dev/null || echo 0)
    if [ "$n_merged" -ge "$n_theirs" ] && [ "$n_merged" -ge "$n_ours" ]; then
      if ! python3 "$_GIT_SAFE_PUSH_SCRIPT_DIR/merge_nations_league_public.py" \
          --target "$merged_tmp" --base "$merged_tmp" \
          --candidate-file "$ours_tmp" --candidate-file "$theirs_tmp" >> "$LOG" 2>&1; then
        echo "[$TS] git_safe_push: Nations League snapshot merge failed closed for $f" >> "$LOG"
        rm -f "$ours_tmp" "$theirs_tmp" "$merged_tmp"
        return 1
      fi
      mv "$merged_tmp" "$f"
      echo "[$TS] git_safe_push: signals merged (ours=$n_ours theirs=$n_theirs → merged=$n_merged) for $f" >> "$LOG"
      rm -f "$ours_tmp" "$theirs_tmp"
      return 0
    fi
    echo "[$TS] git_safe_push: signals merge sanity failed (ours=$n_ours theirs=$n_theirs merged=$n_merged)" >> "$LOG"
  fi
  rm -f "$ours_tmp" "$theirs_tmp" "$merged_tmp"
  return 1
}

# A successful rebase with -Xtheirs can silently choose the bot's older full
# signals snapshot over a newer main snapshot. Reconcile changed signal files
# against the freshly fetched main version before pushing.
_git_preserve_nations_league_after_rebase() {
  local LOG="$1"
  local TS; TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  local f remote_tmp changed=0
  local -a changed_paths=()
  while IFS= read -r f; do
    case "$f" in
      docs/data/signals.json|docs/data/signals_*.json) ;;
      *) continue ;;
    esac
    remote_tmp="$(mktemp)" || return 1
    if git show "origin/main:$f" > "$remote_tmp" 2>/dev/null; then
      if ! python3 "$_GIT_SAFE_PUSH_SCRIPT_DIR/merge_nations_league_public.py" \
          --target "$f" --base "$f" --candidate-file "$remote_tmp" >> "$LOG" 2>&1; then
        rm -f "$remote_tmp"
        echo "[$TS] git_safe_push: post-rebase Nations League merge failed for $f" >> "$LOG"
        return 1
      fi
      if ! git diff --quiet -- "$f"; then
        git add -- "$f" >> "$LOG" 2>&1 || { rm -f "$remote_tmp"; return 1; }
        changed_paths+=("$f")
        changed=1
      fi
    fi
    rm -f "$remote_tmp"
  done < <(git diff --name-only origin/main...HEAD -- docs/data)

  if [ "$changed" -eq 1 ]; then
    if ! git commit -m "auto: retain validated Nations League public snapshot" \
        -- "${changed_paths[@]}" >> "$LOG" 2>&1; then
      echo "[$TS] git_safe_push: unable to commit reconciled Nations League snapshot" >> "$LOG"
      return 1
    fi
  fi
}

_git_clear_unmerged() {
  local LOG="$1"
  local TS; TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"

  local unmerged_files
  unmerged_files=$(git ls-files --unmerged | awk '{print $4}' | sort -u)
  [ -z "$unmerged_files" ] && return 0

  echo "[$TS] git_safe_push: unmerged files detected — checking permissions" >> "$LOG"

  # Fail closed immediately if any unmerged file is outside permitted bot paths.
  # Source-file conflicts must never be auto-resolved; they need human review.
  while IFS= read -r f; do
    if ! _bot_permitted "$f"; then
      echo "[$TS] git_safe_push: FAIL CLOSED — source file conflict requires human review: $f" >> "$LOG"
      return 1
    fi
  done <<< "$unmerged_files"

  echo "[$TS] git_safe_push: resolving unmerged data files (all permitted)" >> "$LOG"

  # Use while<<<var instead of pipe to keep everything in the current shell
  # (pipe creates a subshell; git-add effects persist but variable changes don't).
  while IFS= read -r f; do
    # signals.json: jq-based union merge to preserve both sides' rows.
    case "$f" in
      docs/data/signals.json|docs/data/signals_*.json)
        if _git_signals_json_merge "$f" "$LOG"; then
          git add -- "$f" >> "$LOG" 2>&1 || true
          echo "[$TS] git_safe_push: staged merged $f" >> "$LOG"
          continue
        fi
        echo "[$TS] git_safe_push: FAIL CLOSED — signals conflict merge failed: $f" >> "$LOG"
        return 1
        ;;
    esac

    # Generic data file: try to take the local version (freshly generated by bot).
    # During `git pull --rebase`, "theirs" = local replay commit = the fresh data.
    git checkout --theirs -- "$f" >> "$LOG" 2>&1 || true

    # If markers remain (e.g. no stage-3 in autostash scenario), try --ours.
    if grep -qE '^(<{7}|={7}|>{7})' "$f" 2>/dev/null; then
      git checkout --ours -- "$f" >> "$LOG" 2>&1 || true
    fi
    # Last resort: reset to HEAD (stale but marker-free; next tick regenerates it).
    if grep -qE '^(<{7}|={7}|>{7})' "$f" 2>/dev/null; then
      echo "[$TS] git_safe_push: WARN $f still has markers — resetting to HEAD" >> "$LOG"
      git checkout HEAD -- "$f" >> "$LOG" 2>&1 || true
    fi
    # Never commit a file with conflict markers.
    if grep -qE '^(<{7}|={7}|>{7})' "$f" 2>/dev/null; then
      echo "[$TS] git_safe_push: ERROR $f STILL has markers — skipping stage" >> "$LOG"
      continue
    fi

    git add -- "$f" >> "$LOG" 2>&1 || true
    echo "[$TS] git_safe_push: staged $f" >> "$LOG"
  done <<< "$unmerged_files"

  # Safety gate: verify the resolution commit touches only permitted files.
  # This catches edge cases where a prior checkout accidentally staged something unexpected.
  if ! bot_assert_staged_safe "$LOG"; then
    echo "[$TS] git_safe_push: FAIL CLOSED — aborting resolution commit (forbidden staged file)" >> "$LOG"
    git reset HEAD >> "$LOG" 2>&1 || true
    return 1
  fi

  git commit -m "auto: resolve unmerged data files" --no-edit >> "$LOG" 2>&1 || true
}

# Internal: the actual push logic (called while holding the process lock).
_git_safe_push_body() {
  local LOG="$1"
  local TS
  TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"

  # Recover from any stuck unmerged state before attempting git operations
  _git_clear_unmerged "$LOG" || return 1

  git fetch origin main >> "$LOG" 2>&1

  if ! git pull --rebase --autostash --strategy-option=theirs origin main >> "$LOG" 2>&1; then
    echo "[$TS] git_safe_push: rebase conflict, aborting" >> "$LOG"
    git rebase --abort >> "$LOG" 2>&1 || true
    # Second chance: clear any unmerged state left by autostash and retry once
    _git_clear_unmerged "$LOG" || return 1
    git fetch origin main >> "$LOG" 2>&1
    if ! git pull --rebase --autostash --strategy-option=theirs origin main >> "$LOG" 2>&1; then
      echo "[$TS] git_safe_push: rebase failed after unmerged-clear, giving up" >> "$LOG"
      git rebase --abort >> "$LOG" 2>&1 || true
      return 1
    fi
  fi
  _git_preserve_nations_league_after_rebase "$LOG" || return 1

  if git push origin main >> "$LOG" 2>&1; then
    echo "[$TS] git_safe_push: push ok (attempt 1) — $(git log origin/main -1 --oneline 2>/dev/null)" >> "$LOG"
    return 0
  fi

  echo "[$TS] git_safe_push: first push rejected, retrying" >> "$LOG"
  git fetch origin main >> "$LOG" 2>&1
  if ! git pull --rebase --autostash --strategy-option=theirs origin main >> "$LOG" 2>&1; then
    echo "[$TS] git_safe_push: retry rebase conflict" >> "$LOG"
    git rebase --abort >> "$LOG" 2>&1 || true
    return 1
  fi
  _git_preserve_nations_league_after_rebase "$LOG" || return 1
  if git push origin main >> "$LOG" 2>&1; then
    echo "[$TS] git_safe_push: push ok (attempt 2) — $(git log origin/main -1 --oneline 2>/dev/null)" >> "$LOG"
    return 0
  fi

  echo "[$TS] git_safe_push: second push rejected, final attempt" >> "$LOG"
  git fetch origin main >> "$LOG" 2>&1
  if ! git pull --rebase --autostash --strategy-option=theirs origin main >> "$LOG" 2>&1; then
    echo "[$TS] git_safe_push: final rebase conflict" >> "$LOG"
    git rebase --abort >> "$LOG" 2>&1 || true
    return 1
  fi
  _git_preserve_nations_league_after_rebase "$LOG" || return 1
  if git push origin main >> "$LOG" 2>&1; then
    echo "[$TS] git_safe_push: push ok (attempt 3) — $(git log origin/main -1 --oneline 2>/dev/null)" >> "$LOG"
    return 0
  fi

  echo "[$TS] git_safe_push: push FAILED after 3 attempts — HEAD=$(git log --oneline -1 2>/dev/null)" >> "$LOG"
  return 1
}

git_safe_push() {
  local LOG="${1:-/dev/stderr}"
  local LOCKFILE="/tmp/sportsbrain_push.lock"
  local TS
  TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"

  if command -v flock >/dev/null 2>&1; then
    # flock available (Linux or macOS+Homebrew util-linux): exclusive lock, 90s timeout
    (
      flock -w 90 200 || {
        echo "[$TS] git_safe_push: lock timeout (flock)" >> "$LOG"
        exit 1
      }
      _git_safe_push_body "$LOG"
    ) 200>"$LOCKFILE"
    return $?
  else
    # mkdir-based spin lock — atomic on POSIX, portable macOS fallback
    local LOCKDIR="${LOCKFILE}.d"
    local waited=0
    while ! mkdir "$LOCKDIR" 2>/dev/null; do
      if [ $waited -ge 90 ]; then
        echo "[$TS] git_safe_push: lock timeout (mkdir)" >> "$LOG"
        return 1
      fi
      sleep 3
      waited=$((waited + 3))
    done
    echo $$ > "$LOCKDIR/pid"
    _git_safe_push_body "$LOG"
    local ret=$?
    rm -rf "$LOCKDIR"
    return $ret
  fi
}
