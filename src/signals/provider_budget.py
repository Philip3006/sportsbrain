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
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

_log = logging.getLogger("sportsbrain.signals.provider_budget")

_BUDGET_PATH: Path | None = None
_API_USAGE_PATH: Path | None = None
AUTH_REVALIDATION_SCHEMA_VERSION = "the-odds-api-auth-revalidation-v1"
AUTH_REVALIDATION_PROVIDER = "the_odds_api"
AUTH_FAILURE_CODES = frozenset({401, 403})
TOP5_QUOTA_EVIDENCE_MAX_AGE_SECONDS = 900
ODDS_API_QUOTA_EVIDENCE_SCHEMA_VERSION = "the-odds-api-quota-evidence-v1"
ODDS_API_QUOTA_EVIDENCE_PROVIDER = "the_odds_api"
ODDS_API_QUOTA_EVIDENCE_SOURCES = frozenset(
    {
        "the_odds_api_response_headers",
        "the_odds_api_auth_revalidation_response_headers",
    }
)


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


def _monthly_reset_boundary(observed_at: datetime) -> datetime:
    reset_at = observed_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if reset_at.month == 12:
        return reset_at.replace(year=reset_at.year + 1, month=1)
    return reset_at.replace(month=reset_at.month + 1)


def _load_api_usage() -> dict[str, object] | None:
    path = _api_usage_path()
    if not path.exists():
        return None
    try:
        usage = json.loads(path.read_text())
    except (OSError, TypeError, ValueError):
        return None
    return usage if isinstance(usage, dict) else None


def persist_odds_api_quota_usage(
    requests_used: int,
    requests_remaining: int,
    *,
    source: str,
    observed_at: datetime | None = None,
    path: Path | None = None,
) -> dict[str, object]:
    """Persist and verify canonical The Odds API quota evidence."""

    if (
        isinstance(requests_used, bool)
        or not isinstance(requests_used, int)
        or requests_used < 0
    ):
        raise ValueError("requests_used must be a non-negative integer")
    if (
        isinstance(requests_remaining, bool)
        or not isinstance(requests_remaining, int)
        or requests_remaining < 0
    ):
        raise ValueError("requests_remaining must be a non-negative integer")
    if not isinstance(source, str) or source not in ODDS_API_QUOTA_EVIDENCE_SOURCES:
        raise ValueError("quota evidence source is not canonical for The Odds API")

    observed = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reset_at = _monthly_reset_boundary(observed)
    usage: dict[str, object] = {
        "schema_version": ODDS_API_QUOTA_EVIDENCE_SCHEMA_VERSION,
        "provider": ODDS_API_QUOTA_EVIDENCE_PROVIDER,
        "requests_used": requests_used,
        "requests_remaining": requests_remaining,
        "observed_at": observed.isoformat(),
        "reset_at": reset_at.isoformat(),
        "state": "QUOTA_EXHAUSTED" if requests_remaining == 0 else "AVAILABLE",
        "source": source,
    }
    usage_path = path or _api_usage_path()
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(usage_path, usage, sort_keys=True)

    try:
        persisted = json.loads(usage_path.read_text())
    except (OSError, TypeError, ValueError) as exc:
        raise OSError("quota evidence read-back failed") from exc
    if not isinstance(persisted, dict) or any(
        persisted.get(key) != value for key, value in usage.items()
    ):
        raise OSError("quota evidence read-back did not match the persisted record")
    return usage


def odds_api_quota_state() -> dict[str, object] | None:
    """Return redacted The Odds API quota evidence, if available."""
    usage = _load_api_usage()
    if usage is None:
        return None
    result: dict[str, object] = {}
    for key in (
        "schema_version",
        "provider",
        "requests_used",
        "requests_remaining",
        "observed_at",
        "reset_at",
        "state",
        "source",
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
        name == "the_odds_api" and the_odds_api_quota_revalidation_eligible(now=current)
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


def is_top5_provider_available(*, now: datetime | None = None) -> bool:
    """Require current canonical quota evidence before a Top-5 one-shot.

    The general provider gate retains its existing callers' behavior. The
    launch-specific one-shot additionally requires a positive, well-formed
    quota observation no older than the odds freshness ceiling. This prevents
    an elapsed circuit reset from making a legacy positive counter look fresh.
    It never performs an authentication or quota probe.
    """

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    usage = _load_api_usage()
    if usage is None:
        return False

    used = usage.get("requests_used")
    remaining = usage.get("requests_remaining")
    observed_at = _parse_timestamp(usage.get("observed_at"))
    reset_at = _parse_timestamp(usage.get("reset_at"))
    source = usage.get("source")
    if (
        usage.get("schema_version") != ODDS_API_QUOTA_EVIDENCE_SCHEMA_VERSION
        or usage.get("provider") != ODDS_API_QUOTA_EVIDENCE_PROVIDER
        or isinstance(used, bool)
        or not isinstance(used, int)
        or used < 0
        or isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or remaining <= 0
        or observed_at is None
        or reset_at is None
        or not isinstance(source, str)
        or source not in ODDS_API_QUOTA_EVIDENCE_SOURCES
        or usage.get("state") != "AVAILABLE"
        or observed_at > current
        or (current - observed_at).total_seconds() > TOP5_QUOTA_EVIDENCE_MAX_AGE_SECONDS
        or reset_at <= observed_at
        or reset_at <= current
        or reset_at != _monthly_reset_boundary(observed_at)
    ):
        return False

    return is_provider_available(
        "the_odds_api", allow_quota_revalidation=False, now=current
    )


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
    entry.update(
        {
            "circuit_open": True,
            "last_error_ts": now_str,
            "last_error_code": error_code,
            "fallback_reason": reason,
            "circuit_reset_at": reset_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if reset_at is not None
            else "",
        }
    )
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


def begin_auth_revalidation(
    name: str,
    *,
    explicit_opt_in: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Authorize one bounded authentication revalidation attempt.

    This is intentionally narrower than normal availability: only the Odds
    API's existing 401/403 circuit may enter it, and callers must opt in
    explicitly.  It never closes or weakens the circuit by itself.
    """

    if name != AUTH_REVALIDATION_PROVIDER:
        raise RuntimeError("auth revalidation provider is not permitted")
    if explicit_opt_in is not True:
        raise RuntimeError("auth revalidation requires explicit opt-in")
    state = _load()
    entry = state.get(name)
    if not isinstance(entry, Mapping) or entry.get("circuit_open") is not True:
        raise RuntimeError("auth revalidation requires an open provider circuit")
    try:
        error_code = int(entry.get("last_error_code"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "auth revalidation requires a recorded auth failure"
        ) from exc
    reason = str(entry.get("fallback_reason", ""))
    if error_code not in AUTH_FAILURE_CODES and reason not in {
        "http_401",
        "http_403",
    }:
        raise RuntimeError("auth revalidation is not permitted for this circuit reason")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "provider": name,
        "prior_error_code": error_code,
        "prior_reason": reason,
        "authorized_at": observed.isoformat(),
    }


def record_auth_revalidation(
    name: str,
    *,
    status_code: int | None,
    request_count: int,
    credential_access_count: int,
    safe_headers: Mapping[str, str],
    failure_class: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Persist one redacted auth-revalidation outcome and governed transition."""

    if name != AUTH_REVALIDATION_PROVIDER:
        raise RuntimeError("auth revalidation provider is not permitted")
    if request_count not in (0, 1) or credential_access_count not in (0, 1):
        raise RuntimeError("auth revalidation counts are invalid")
    normalized_headers = {
        str(key).casefold(): str(value) for key, value in safe_headers.items()
    }
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recorded_failure = failure_class
    quota_remaining_observed: int | None = None
    if request_count == 0 and credential_access_count == 1:
        transition = "remains_open"
        recorded_failure = "no_revalidation_request"
    elif (
        status_code is not None
        and 200 <= status_code < 300
        and request_count == 1
        and credential_access_count == 1
    ):
        transition = "remains_open"
        used_raw = normalized_headers.get("x-requests-used")
        remaining_raw = normalized_headers.get("x-requests-remaining")
        limit_raw = normalized_headers.get("x-requests-limit")
        try:
            if used_raw is None:
                raise ValueError("missing_x_requests_used")
            try:
                used_value = int(used_raw)
            except ValueError as exc:
                raise ValueError("invalid_x_requests_used") from exc
            if used_value < 0:
                raise ValueError("invalid_x_requests_used")
            if remaining_raw is None:
                raise ValueError("missing_x_requests_remaining")
            try:
                remaining_value = int(remaining_raw)
            except ValueError as exc:
                raise ValueError("invalid_x_requests_remaining") from exc
            if remaining_value < 0:
                raise ValueError("invalid_x_requests_remaining")
            if limit_raw is not None:
                try:
                    limit_value = int(limit_raw)
                except ValueError as exc:
                    raise ValueError("invalid_x_requests_limit") from exc
                if limit_value <= 0:
                    raise ValueError("invalid_x_requests_limit")
                if used_value > limit_value:
                    raise ValueError("x_requests_used_exceeds_limit")
                if remaining_value > limit_value:
                    raise ValueError("x_requests_remaining_exceeds_limit")
        except ValueError as exc:
            recorded_failure = str(exc)
        else:
            try:
                persist_odds_api_quota_usage(
                    used_value,
                    remaining_value,
                    source="the_odds_api_auth_revalidation_response_headers",
                    observed_at=observed,
                )
            except (OSError, TypeError, ValueError):
                recorded_failure = "quota_state_persistence_failed"
            else:
                quota_remaining_observed = remaining_value
                if remaining_value == 0:
                    recorded_failure = "quota_remaining_zero"
                else:
                    try:
                        record_success(name, quota_remaining=remaining_value)
                    except (OSError, TypeError, ValueError):
                        recorded_failure = "circuit_close_persistence_failed"
                    else:
                        transition = "closed"
    elif status_code is not None and 200 <= status_code < 300:
        transition = "remains_open"
        recorded_failure = "revalidation_request_counts_invalid"
    elif status_code in AUTH_FAILURE_CODES:
        record_error(name, int(status_code), open_circuit=True)
        transition = "reopened"
    else:
        if status_code is not None:
            record_error(name, int(status_code), open_circuit=True)
        transition = "remains_open"
    if status_code is not None and 200 <= status_code < 300 and transition != "closed":
        state = _load()
        entry = state.get(name, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["circuit_open"] = True
        state[name] = entry
        _save(state)
    audit = {
        "schema_version": AUTH_REVALIDATION_SCHEMA_VERSION,
        "observed_at": observed.isoformat(),
        "provider": name,
        "request_count": request_count,
        "credential_access_count": credential_access_count,
        "http_status": status_code,
        "safe_headers": normalized_headers,
        "failure_class": recorded_failure,
        "circuit_transition": transition,
    }
    state = _load()
    entry = state.get(name, {})
    if not isinstance(entry, dict):
        entry = {}
    entry["last_auth_revalidation"] = audit
    if quota_remaining_observed is not None:
        entry["quota_remaining"] = quota_remaining_observed
    state[name] = entry
    _save(state)
    return audit
