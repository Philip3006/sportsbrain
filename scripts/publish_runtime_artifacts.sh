#!/bin/bash
# Isolated publisher for local runtime artifacts. Never mutates the active checkout.

_canonical_github_remote() {
  case "$1" in
    https://github.com/Philip3006/sportsbrain|https://github.com/Philip3006/sportsbrain.git|\
    git@github.com:Philip3006/sportsbrain|git@github.com:Philip3006/sportsbrain.git|\
    ssh://git@github.com/Philip3006/sportsbrain|ssh://git@github.com/Philip3006/sportsbrain.git)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

_canonical_origin_transport() {
  local checkout="$1"
  local remote
  remote="$(git -C "$checkout" remote get-url origin 2>/dev/null)" || return 1
  _canonical_github_remote "$remote" || return 1
  printf '%s\n' "$remote"
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
  local expected_transport="$2"
  local remote
  git -C "$checkout" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  remote="$(_canonical_origin_transport "$checkout")" || return 1
  [ "$remote" = "$expected_transport" ] || return 1
}

_publish_config_path() {
  printf '%s/.runtime-publish.env\n' "$1"
}

_publish_release_owned_lock() {
  local lock_dir="$1"
  local owner_file="$2"
  local owner_pid="$3"
  local log="$4"
  if [ -f "$owner_file" ] && [ "$(cat "$owner_file" 2>/dev/null)" = "$owner_pid" ]; then
    rm -f -- "$owner_file"
    rmdir -- "$lock_dir" 2>/dev/null || echo "[runtime-publish] owned lock cleanup deferred" >> "$log"
  fi
}

_publish_resolve_dir() {
  local source_dir="$1"
  local log="$2"
  local config value count
  if [ -n "${SPORTSBRAIN_PUBLISH_DIR:-}" ]; then
    return 0
  fi
  config="$(_publish_config_path "$source_dir")"
  if [ ! -f "$config" ] || [ -L "$config" ]; then
    echo "[runtime-publish] missing SPORTSBRAIN_PUBLISH_DIR and protected runtime config" >> "$log"
    return 1
  fi
  count="$(grep -c '^SPORTSBRAIN_PUBLISH_DIR=' "$config" 2>/dev/null)"
  value="$(sed -n 's/^SPORTSBRAIN_PUBLISH_DIR=//p' "$config")"
  if [ "$count" != "1" ] || [ -z "$value" ] || [ "${value#/}" = "$value" ] || \
     printf '%s' "$value" | grep -q '[[:space:]]'; then
    echo "[runtime-publish] invalid runtime publish config" >> "$log"
    return 1
  fi
  SPORTSBRAIN_PUBLISH_DIR="$value"
  export SPORTSBRAIN_PUBLISH_DIR
}

runtime_publish_configure() {
  local source_dir="$1"
  local publish_dir="$2"
  local config tmp
  if [ -z "$publish_dir" ] || [ "$publish_dir" = "$source_dir" ] || [ "${publish_dir#/}" = "$publish_dir" ]; then
    echo "runtime-publish: unsafe publish directory" >&2
    return 1
  fi
  _canonical_origin_transport "$source_dir" >/dev/null || {
    echo "runtime-publish: active checkout origin is not canonical" >&2
    return 1
  }
  config="$(_publish_config_path "$source_dir")"
  tmp="${config}.tmp.$$"
  (umask 077 && printf 'SPORTSBRAIN_PUBLISH_DIR=%s\n' "$publish_dir" > "$tmp") || return 1
  mv "$tmp" "$config" || return 1
  chmod 600 "$config"
}

runtime_publish_setup() {
  local source_dir="$1"
  local log="$2"
  local publish_dir expected_transport

  _publish_resolve_dir "$source_dir" "$log" || return 1
  publish_dir="$SPORTSBRAIN_PUBLISH_DIR"
  expected_transport="$(_canonical_origin_transport "$source_dir")" || {
    echo "[runtime-publish] active checkout origin is not canonical" >> "$log"
    return 1
  }

  if [ -z "$publish_dir" ] || [ "$publish_dir" = "$source_dir" ]; then
    echo "[runtime-publish] missing or unsafe SPORTSBRAIN_PUBLISH_DIR" >> "$log"
    return 1
  fi
  if [ ! -e "$publish_dir" ]; then
    if ! git clone --no-checkout "$expected_transport" "$publish_dir" >> "$log" 2>&1 || \
       ! git -C "$publish_dir" checkout -b runtime-publish origin/main >> "$log" 2>&1; then
      echo "[runtime-publish] unable to provision publisher checkout" >> "$log"
      return 1
    fi
  fi
  if ! _publish_validate_checkout "$publish_dir" "$expected_transport" || \
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

  local publish_dir
  _publish_resolve_dir "$source_dir" "$log" || return 1
  publish_dir="$SPORTSBRAIN_PUBLISH_DIR"
  if [ -z "$publish_dir" ] || [ "$publish_dir" = "$source_dir" ]; then
    echo "[runtime-publish] missing or unsafe SPORTSBRAIN_PUBLISH_DIR" >> "$log"
    return 1
  fi

  (
    # The sibling lock also serializes first-run publisher provisioning.
    local lock_dir="${publish_dir}.sportsbrain-runtime-publish.lock.d"
    local owner_file="$lock_dir/owner.pid"
    local owner_pid="$BASHPID"
    local waited=0
    local lock_timeout="${SPORTSBRAIN_PUBLISH_LOCK_TIMEOUT_SECONDS:-30}"
    while ! mkdir "$lock_dir" 2>/dev/null; do
      if [ "$waited" -ge "$lock_timeout" ]; then
        echo "[runtime-publish] publisher lock timeout" >> "$log"
        exit 1
      fi
      sleep 2
      waited=$((waited + 2))
    done
    if ! printf '%s\n' "$owner_pid" > "$owner_file"; then
      echo "[runtime-publish] unable to record lock ownership" >> "$log"
      exit 1
    fi
    trap "_publish_release_owned_lock '$lock_dir' '$owner_file' '$owner_pid' '$log'" EXIT
    trap "_publish_release_owned_lock '$lock_dir' '$owner_file' '$owner_pid' '$log'; exit 130" INT
    trap "_publish_release_owned_lock '$lock_dir' '$owner_file' '$owner_pid' '$log'; exit 143" TERM

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
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if [ "${1:-}" = "configure" ] && [ -n "${2:-}" ] && [ -n "${3:-}" ]; then
    runtime_publish_configure "$2" "$3"
  elif [ "${1:-}" = "setup" ] && [ -n "${2:-}" ]; then
    runtime_publish_setup "$2" "${3:-/dev/stderr}"
  else
    echo "usage: $0 configure <active-checkout> <publish-dir> | setup <active-checkout> [log-path]" >&2
    exit 2
  fi
fi
