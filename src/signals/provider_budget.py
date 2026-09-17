"""Provider quota tracking and circuit-breaker for odds refresh.

Provider quota state is stored in operator-owned runtime state, seeded from the
legacy checkout cache when necessary. The refresher checks this before dispatching
any API call. A provider with circuit_open=True is skipped until the circuit resets.

The Odds API is a monthly quota provider. A zero-credit observation is therefore
held until an explicit, provider-appropriate next-month boundary. A legacy usage
file without freshness/reset evidence remains fail-closed and never receives a
speculative automatic probe.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

_log = logging.getLogger("sportsbrain.signals.provider_budget")

_BUDGET_PATH: Path | None = None
_API_USAGE_PATH: Path | None = None


def _api_usage_path() -> Path:
    """Return the external usage path unless a test explicitly overrides it."""
    if _API_USAGE_PATH is not None:
        return _API_USAGE_PATH
    return runtime_state_path("data/cache/api_usage.json", require_external=True)


def _budget_path() -> Path:
    if _BUDGET_PATH is not None:
        return _BUDGET_PATH
    return runtime_state_path("data/cache/provider_budget.json", require_external=True)


def _load() -> dict[str, dict]:
    budget_path = _budget_path()
    if not budget_path.exists():
        return {}
    try:
        raw = json.loads(budget_path.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _save(state: dict[str, dict]) -> None:
    budget_path = _budget_path()
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(budget_path, state)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_api_usage() -> dict[str, object] | None:
    path = _api_usage_path()
    if not path.exists():
        return None
    try:
        usage = json.loads(path.read_text())
    except (OSError, TypeError, ValueError):
        return None
    return usage if isinstance(usage, dict) else None


def odds_api_quota_state() -> dict[str, object] | None:
    """Return redacted The Odds API quota evidence, if available."""
    usage = _load_api_usage()
    if usage is None:
        return None
    result: dict[str, object] = {}
    for key in (
        "requests_used", "requests_remaining", "observed_at", "reset_at", "state", "source"
    ):
        if key in usage:
            result[key] = usage[key]
    return result


def the_odds_api_quota_revalidation_eligible(*, now: datetime | None = None) -> bool:
    """Return whether a future authorized monthly-reset probe is eligible."""
    usage = _load_api_usage()
    if usage is None:
        return False
    try:
        remaining = int(usage.get("requests_remaining", -1))
    except (TypeError, ValueError):
        return False
    if remaining != 0:
        return False
    observed_at = _parse_timestamp(usage.get("observed_at"))
    reset_at = _parse_timestamp(usage.get("reset_at"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (
        observed_at is not None
        and reset_at is not None
        and reset_at > observed_at
        and observed_at <= current
        and current >= reset_at
    )


def _the_odds_api_exhausted(*, now: datetime | None = None) -> bool:
    """Check monthly quota without blocking forever after its known reset."""
    usage = _load_api_usage()
    if usage is None:
        return False
    try:
        remaining = int(usage.get("requests_remaining", 1))
    except (TypeError, ValueError):
        return False
    if remaining > 0:
        return False
    return not the_odds_api_quota_revalidation_eligible(now=now)


def is_provider_available(
    name: str,
    *,
    allow_quota_revalidation: bool = False,
    now: datetime | None = None,
) -> bool:
    """Return False if the provider circuit is open or not authorized to revalidate."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reset_eligible = (
        name == "the_odds_api"
        and the_odds_api_quota_revalidation_eligible(now=current)
    )
    if reset_eligible and not allow_quota_revalidation:
        usage = _load_api_usage() or {}
        _open_circuit(
            name,
            reason="quota_revalidation_requires_explicit_opt_in",
            error_code=429,
            reset_at=_parse_timestamp(usage.get("reset_at")),
        )
        return False
    if reset_eligible and allow_quota_revalidation:
        # A legacy zero-quota entry may have opened a circuit before reset
        # metadata existed.  Explicit revalidation is the only boundary that
        # may clear that stale gate for the caller's bounded probe.
        entry = _load().get(name)
        if entry and entry.get("circuit_open"):
            _close_circuit(name)
    if name == "the_odds_api" and _the_odds_api_exhausted(now=current):
        usage = _load_api_usage() or {}
        _open_circuit(
            name,
            reason="quota_exhausted",
            error_code=429,
            reset_at=_parse_timestamp(usage.get("reset_at")),
        )
        return False

    state = _load()
    entry = state.get(name)
    if not entry:
        return True

    if not entry.get("circuit_open", False):
        return True

    reset_at_str = entry.get("circuit_reset_at", "")
    if reset_at_str:
        try:
            reset_at = datetime.fromisoformat(reset_at_str.replace("Z", "+00:00"))
            if current >= reset_at:
                _close_circuit(name)
                return True
        except ValueError:
            pass

    return False


def _open_circuit(
    name: str,
    *,
    reason: str,
    error_code: int | None = None,
    reset_at: datetime | None = None,
) -> None:
    state = _load()
    entry = state.get(name, {})
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if reset_at is None and not (
        name == "the_odds_api" and reason == "quota_exhausted"
    ):
        # Daily circuit reset is retained for transient legacy providers.
        reset_at = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
    entry.update({
        "circuit_open": True,
        "last_error_ts": now_str,
        "last_error_code": error_code,
        "fallback_reason": reason,
        "circuit_reset_at": reset_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if reset_at is not None
        else "",
    })
    state[name] = entry
    _save(state)
    _log.warning("[provider_budget] circuit OPEN for %s: %s", name, reason)


def _close_circuit(name: str) -> None:
    state = _load()
    entry = state.get(name, {})
    entry["circuit_open"] = False
    entry["circuit_reset_at"] = ""
    state[name] = entry
    _save(state)
    _log.info("[provider_budget] circuit CLOSED for %s (quota reset)", name)


def record_success(name: str, quota_remaining: int | None = None) -> None:
    """Call after a successful provider fetch."""
    state = _load()
    entry = state.get(name, {})
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["last_success_ts"] = now_str
    entry["circuit_open"] = False
    if quota_remaining is not None:
        entry["quota_remaining"] = quota_remaining
    state[name] = entry
    _save(state)


def record_error(name: str, error_code: int, *, open_circuit: bool = False) -> None:
    """Call after a provider error. Set open_circuit=True for quota/auth failures."""
    state = _load()
    entry = state.get(name, {})
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["last_error_ts"] = now_str
    entry["last_error_code"] = error_code
    state[name] = entry
    _save(state)
    if open_circuit:
        _open_circuit(name, reason=f"http_{error_code}", error_code=error_code)


def get_budget_snapshot() -> dict[str, dict]:
    """Return full budget state for health dashboards."""
    return _load()
