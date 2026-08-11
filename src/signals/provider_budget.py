"""Provider quota tracking and circuit-breaker for odds refresh.

`data/cache/provider_budget.json` is the single source of truth for per-provider
quota state. The refresher checks this before dispatching any API call. A provider
with circuit_open=True is skipped entirely until the circuit resets.

Circuit reset policy:
  - Automatic: after circuit_reset_at has passed (default: midnight UTC = daily reset)
  - Manual: delete the entry or set circuit_open=False in the JSON

TheOddsAPI special case: api_usage.json is read as an additional signal.
If requests_remaining==0 there, the circuit for "the_odds_api" is opened.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.utils.atomic_io import atomic_write_json

_log = logging.getLogger("sportsbrain.signals.provider_budget")

ROOT = Path(__file__).resolve().parents[2]
_BUDGET_PATH = ROOT / "data" / "cache" / "provider_budget.json"
_API_USAGE_PATH = ROOT / "data" / "cache" / "api_usage.json"


def _load() -> dict[str, dict]:
    if not _BUDGET_PATH.exists():
        return {}
    try:
        raw = json.loads(_BUDGET_PATH.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save(state: dict[str, dict]) -> None:
    _BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_BUDGET_PATH, state)


def _the_odds_api_exhausted() -> bool:
    """Check api_usage.json for TheOddsAPI exhaustion."""
    if not _API_USAGE_PATH.exists():
        return False
    try:
        usage = json.loads(_API_USAGE_PATH.read_text())
        return int(usage.get("requests_remaining", 1)) <= 0
    except Exception:
        return False


def is_provider_available(name: str) -> bool:
    """Return False if the provider circuit is open (exhausted/erroring).

    Also auto-opens the TheOddsAPI circuit if api_usage.json shows quota=0.
    """
    if name == "the_odds_api" and _the_odds_api_exhausted():
        _open_circuit(name, reason="quota_exhausted", error_code=429)
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
            if datetime.now(timezone.utc) >= reset_at:
                _close_circuit(name)
                return True
        except ValueError:
            pass

    return False


def _open_circuit(name: str, *, reason: str, error_code: int | None = None) -> None:
    state = _load()
    entry = state.get(name, {})
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Reset at midnight UTC (daily quota reset for most providers)
    tomorrow = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    from datetime import timedelta
    tomorrow = tomorrow + timedelta(days=1)
    entry.update({
        "circuit_open": True,
        "last_error_ts": now_str,
        "last_error_code": error_code,
        "fallback_reason": reason,
        "circuit_reset_at": tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
