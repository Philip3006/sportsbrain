#!/bin/bash
# Isolated publisher for local runtime artifacts. Never mutates the active checkout.

_publish_remote_url() {
  printf '%s\n' 'https://github.com/Philip3006/sportsbrain.git'
}

_publish_remote() {
  local remote
  remote="$(_publish_remote_url)"
  printf '%s\n' "${remote%/}" | sed 's/\.git$//'
}

_publish_permitted() {
  case "$1" in
    docs/data/signals.json|docs/data/signals_philip.json|docs/data/tennis_live_scores.json|\
    data/cache/tennis_live_scores.json|data/cache/tennis_suspended.json)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

_publish_validate_checkout() {
  local checkout="$1"
  local expected="$2"
  git -C "$checkout" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  [ "$(_publish_remote)" = "$expected" ] || return 1
  [ "$(git -C "$checkout" remote get-url origin | sed 's#/$##; s/\.git$//')" = "$expected" ] || return 1
}

runtime_publish_setup() {
  local source_dir="$1"
  local log="$2"
  local publish_dir="${SPORTSBRAIN_PUBLISH_DIR:-}"
  local expected
  expected="$(_publish_remote)"

  if [ -z "$publish_dir" ] || [ "$publish_dir" = "$source_dir" ]; then
    echo "[runtime-publish] missing or unsafe SPORTSBRAIN_PUBLISH_DIR" >> "$log"
    return 1
  fi
  if ! _publish_validate_checkout "$source_dir" "$expected"; then
    echo "[runtime-publish] active checkout origin is not canonical" >> "$log"
    return 1
  fi
  if [ ! -e "$publish_dir" ]; then
    if ! git clone --no-checkout "$(_publish_remote_url)" "$publish_dir" >> "$log" 2>&1 || \
       ! git -C "$publish_dir" checkout -b runtime-publish origin/main >> "$log" 2>&1; then
      echo "[runtime-publish] unable to provision publisher checkout" >> "$log"
      return 1
    fi
  fi
  if ! _publish_validate_checkout "$publish_dir" "$expected" || \
     [ "$(git -C "$publish_dir" branch --show-current)" != "runtime-publish" ]; then
    echo "[runtime-publish] publisher must be canonical runtime-publish checkout" >> "$log"
    return 1
  fi
  return 0
}

runtime_publish_artifacts() {
  local source_dir="$1"
  local log="$2"
  local message="$3"
  shift 3

  local publish_dir="${SPORTSBRAIN_PUBLISH_DIR:-}"
  if [ -z "$publish_dir" ] || [ "$publish_dir" = "$source_dir" ]; then
    echo "[runtime-publish] missing or unsafe SPORTSBRAIN_PUBLISH_DIR" >> "$log"
    return 1
  fi

  # The sibling lock also serializes first-run publisher provisioning.
  local lock_dir="${publish_dir}.sportsbrain-runtime-publish.lock.d"
  local waited=0
  local lock_timeout="${SPORTSBRAIN_PUBLISH_LOCK_TIMEOUT_SECONDS:-30}"
  while ! mkdir "$lock_dir" 2>/dev/null; do
    if [ "$waited" -ge "$lock_timeout" ]; then
      echo "[runtime-publish] publisher lock timeout" >> "$log"
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done

  local publish_result
  (
    if ! runtime_publish_setup "$source_dir" "$log"; then
      exit 1
    fi
    if [ -n "$(git -C "$publish_dir" status --porcelain)" ]; then
      echo "[runtime-publish] publisher has unexplained local state" >> "$log"
      exit 1
    fi

    local path
    for path in "$@"; do
      if ! _publish_permitted "$path" || [ ! -f "$source_dir/$path" ]; then
        echo "[runtime-publish] forbidden or missing artifact: $path" >> "$log"
        exit 1
      fi
    done

    if ! git -C "$publish_dir" fetch origin main >> "$log" 2>&1 || \
       ! git -C "$publish_dir" merge --ff-only origin/main >> "$log" 2>&1; then
      echo "[runtime-publish] unable to align publisher with origin/main" >> "$log"
      exit 1
    fi
    if ! git -C "$publish_dir" merge-base --is-ancestor HEAD origin/main; then
      echo "[runtime-publish] publisher has uncontained history" >> "$log"
      exit 1
    fi

    for path in "$@"; do
      mkdir -p "$publish_dir/$(dirname "$path")"
      cp "$source_dir/$path" "$publish_dir/$path"
      git -C "$publish_dir" add -- "$path"
    done
    if git -C "$publish_dir" diff --cached --quiet; then
      exit 0
    fi
    if ! git -C "$publish_dir" diff --cached --name-only | while IFS= read -r path; do
      _publish_permitted "$path"
    done; then
      echo "[runtime-publish] staged path outside allowlist" >> "$log"
      git -C "$publish_dir" reset >> "$log" 2>&1
      exit 1
    fi
    if ! git -C "$publish_dir" -c user.name="SportsBrain Bot" -c user.email="bot@sportsbrain" \
        commit -m "$message" >> "$log" 2>&1; then
      echo "[runtime-publish] commit failed" >> "$log"
      exit 1
    fi

    local attempt
    for attempt in 1 2 3; do
      if git -C "$publish_dir" push origin HEAD:main >> "$log" 2>&1; then
        git -C "$publish_dir" fetch origin main >> "$log" 2>&1
        git -C "$publish_dir" merge-base --is-ancestor HEAD origin/main
        exit 0
      fi
      git -C "$publish_dir" fetch origin main >> "$log" 2>&1
      if ! git -C "$publish_dir" rebase origin/main >> "$log" 2>&1; then
        git -C "$publish_dir" rebase --abort >> "$log" 2>&1 || true
        echo "[runtime-publish] conflict; publication failed closed" >> "$log"
        exit 1
      fi
    done
    echo "[runtime-publish] push failed after bounded retries" >> "$log"
    exit 1
  )
  publish_result=$?
  # The lock is created by this invocation and contains no user data.
  rmdir "$lock_dir" 2>/dev/null || rm -rf "$lock_dir"
  return "$publish_result"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if [ "${1:-}" = "setup" ] && [ -n "${2:-}" ]; then
    runtime_publish_setup "$2" "${3:-/dev/stderr}"
  else
    echo "usage: $0 setup <active-checkout> [log-path]" >&2
    exit 2
  fi
fi
