"""Bulk The Odds API transport and redacted response parsing for Top-5 shadow."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any

import requests

from src.config import ODDS_API_URL
from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    _utc,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS

REAL_SHADOW_MARKETS = ("h2h",)
REAL_SHADOW_REGIONS = ("eu",)
_QUOTA_HEADERS = frozenset({"x-requests-used", "x-requests-remaining", "x-requests-last"})


@dataclass(frozen=True)
class ProviderResponse:
    """Redacted transport result; response bodies never enter diagnostics."""

    status_code: int | None
    payload: object | None
    headers: Mapping[str, str]
    completed_at: datetime
    latency_ms: int
    timeout: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))
        if self.latency_ms < 0:
            raise ProductionContractError("provider latency must be non-negative")

    @property
    def quota_headers(self) -> dict[str, str]:
        return {
            key.lower(): str(value)
            for key, value in self.headers.items()
            if key.lower() in _QUOTA_HEADERS
        }


@dataclass(frozen=True)
class ProviderObservation:
    """One bounded bulk request and its non-secret coverage evidence."""

    league_code: str
    sport_key: str
    request_id: str
    requested_at: datetime
    completed_at: datetime
    status: str
    status_code: int | None
    latency_ms: int
    timeout: bool
    retry_count: int
    fallback_used: bool
    event_requests: int
    events_seen: int
    valid_fixture_count: int
    eligible_fixture_count: int
    snapshot_count: int
    outside_window_count: int
    unsupported_market_count: int
    malformed_event_count: int
    wrong_league_count: int
    failure_reason: str | None
    source_timestamp_missing_count: int = 0
    source_timestamp_malformed_count: int = 0
    quota_headers: Mapping[str, str] = field(default_factory=dict)
    fixtures: tuple[Fixture, ...] = ()
    snapshots: tuple[MarketSnapshot, ...] = ()
    valid_fixtures: tuple[Fixture, ...] = ()
    valid_fixture_keys: tuple[str, ...] = ()
    source_timestamps: tuple[datetime, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))
        if self.retry_count != 0 or self.fallback_used or self.event_requests != 0:
            raise ProductionContractError("real shadow provider must remain bulk-only")
        if self.snapshot_count != len(self.snapshots) or self.eligible_fixture_count != len(self.fixtures):
            raise ProductionContractError("provider observation coverage counts are inconsistent")
        if not (
            self.valid_fixture_count == len(self.valid_fixtures)
            == len(self.valid_fixture_keys)
            == len(self.source_timestamps)
        ):
            raise ProductionContractError("provider observation source coverage is inconsistent")
        if any(
            fixture.fixture_key != fixture_key
            or fixture.kickoff.tzinfo is None
            or timestamp.tzinfo is None
            for fixture, fixture_key, timestamp in zip(
                self.valid_fixtures,
                self.valid_fixture_keys,
                self.source_timestamps,
                strict=True,
            )
        ):
            raise ProductionContractError("provider observation source identity is inconsistent")

    def as_payload(self) -> dict[str, object]:
        denominator = self.valid_fixture_count
        return {
            "league": self.league_code,
            "sport_key": self.sport_key,
            "request_id": self.request_id,
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "status": self.status,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "timeout": self.timeout,
            "retry_count": self.retry_count,
            "fallback_used": self.fallback_used,
            "event_requests": self.event_requests,
            "events_seen": self.events_seen,
            "coverage": {
                "valid_fixture_count": denominator,
                "eligible_fixture_count": self.eligible_fixture_count,
                "signal_snapshot_count": self.snapshot_count,
                "eligible_rate": (
                    self.eligible_fixture_count / denominator if denominator else None
                ),
            },
            "outside_window_count": self.outside_window_count,
            "unsupported_market_count": self.unsupported_market_count,
            "malformed_event_count": self.malformed_event_count,
            "wrong_league_count": self.wrong_league_count,
            "source_timestamp_missing_count": self.source_timestamp_missing_count,
            "source_timestamp_malformed_count": self.source_timestamp_malformed_count,
            "source_timestamp_method": (
                "selected quote market.last_update, then bookmaker.last_update; "
                "composite age uses the oldest selected quote"
            ),
            "source_timestamps": [timestamp.isoformat() for timestamp in self.source_timestamps],
            "failure_reason": self.failure_reason,
            "quota_headers": dict(self.quota_headers),
            "results": "pending",
            "closing_capture": "not_captured",
        }


Transport = Callable[[str, tuple[str, ...], tuple[str, ...], str, float], ProviderResponse]


def requests_transport(
    sport_key: str,
    markets: tuple[str, ...],
    regions: tuple[str, ...],
    api_key: str,
    timeout_seconds: float,
) -> ProviderResponse:
    """Make one redacted provider request; no URL or exception is printed."""

    started = time.monotonic()
    completed_at = datetime.now(timezone.utc)
    params = {
        "apiKey": api_key,
        "regions": ",".join(regions),
        "markets": ",".join(markets),
        "oddsFormat": "decimal",
    }
    try:
        response = requests.get(
            f"{ODDS_API_URL}/sports/{sport_key}/odds",
            params=params,
            timeout=timeout_seconds,
        )
    except requests.Timeout:
        return ProviderResponse(
            None, None, {}, completed_at, round((time.monotonic() - started) * 1000), True, "timeout"
        )
    except requests.RequestException:
        return ProviderResponse(
            None, None, {}, completed_at, round((time.monotonic() - started) * 1000), False, "request_error"
        )
    completed_at = datetime.now(timezone.utc)
    payload = None
    error_code = None
    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            error_code = "malformed_json"
    return ProviderResponse(
        response.status_code,
        payload,
        response.headers,
        completed_at,
        round((time.monotonic() - started) * 1000),
        False,
        error_code,
    )


class RealTop5Provider:
    """The Odds API bulk adapter with no retries, fallback, or event fan-out."""

    def __init__(self, api_key: str, *, transport: Transport = requests_transport, timeout_seconds: float = 15.0):
        if not api_key.strip():
            raise ProductionContractError("ODDS_API_KEY is required")
        if timeout_seconds <= 0 or not isfinite(timeout_seconds):
            raise ProductionContractError("provider timeout must be a finite positive value")
        self._api_key = api_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def fetch_league(
        self,
        league_code: str,
        timing: Any,
        *,
        requested_at: datetime,
    ) -> ProviderObservation:
        if league_code not in TOP5_LEAGUE_ADAPTERS:
            raise ProductionContractError("real shadow requested an unknown league")
        requested_at = _utc(requested_at, "requested_at")
        sport_key = TOP5_LEAGUE_ADAPTERS[league_code].config.provider_sport_key
        request_id = _request_id(league_code, sport_key, requested_at)
        response = self._transport(
            sport_key,
            REAL_SHADOW_MARKETS,
            REAL_SHADOW_REGIONS,
            self._api_key,
            self._timeout_seconds,
        )
        return _build_observation(
            league_code,
            sport_key,
            request_id,
            requested_at,
            response,
            timing,
        )


def _build_observation(
    league_code: str,
    sport_key: str,
    request_id: str,
    requested_at: datetime,
    response: ProviderResponse,
    timing: Any,
) -> ProviderObservation:
    headers = response.quota_headers
    empty = {
        "league_code": league_code,
        "sport_key": sport_key,
        "request_id": request_id,
        "requested_at": requested_at,
        "completed_at": response.completed_at,
        "status": "timeout" if response.timeout else "http_error",
        "status_code": response.status_code,
        "latency_ms": response.latency_ms,
        "timeout": response.timeout,
        "retry_count": 0,
        "fallback_used": False,
        "event_requests": 0,
        "events_seen": 0,
        "valid_fixture_count": 0,
        "eligible_fixture_count": 0,
        "snapshot_count": 0,
        "outside_window_count": 0,
        "unsupported_market_count": 0,
        "malformed_event_count": 0,
        "wrong_league_count": 0,
        "failure_reason": response.error_code,
        "quota_headers": headers,
        "source_timestamp_missing_count": 0,
        "source_timestamp_malformed_count": 0,
    }
    if response.timeout or response.status_code != 200:
        if response.status_code == 401:
            empty["failure_reason"] = "authentication"
        elif response.status_code == 403:
            empty["failure_reason"] = "forbidden"
        elif response.status_code == 429:
            empty["failure_reason"] = "rate_limited"
        return ProviderObservation(**empty)
    if response.error_code == "malformed_json" or not isinstance(response.payload, list):
        empty["status"] = "malformed"
        empty["failure_reason"] = "malformed_payload"
        return ProviderObservation(**empty)
    if not response.payload:
        empty["status"] = "empty"
        empty["failure_reason"] = "empty_payload"
        return ProviderObservation(**empty)

    fixtures: list[Fixture] = []
    snapshots: list[MarketSnapshot] = []
    valid_fixtures: list[Fixture] = []
    valid_fixture_keys: list[str] = []
    source_timestamps: list[datetime] = []
    events_seen = len(response.payload)
    outside_window = unsupported = malformed = wrong_league = 0
    source_timestamp_missing = source_timestamp_malformed = 0
    valid_fixture_count = 0
    seen_ids: set[str] = set()
    for event in response.payload:
        if not isinstance(event, Mapping):
            malformed += 1
            continue
        event_id = str(event.get("id", "")).strip()
        if not event_id or event_id in seen_ids:
            malformed += 1
            continue
        seen_ids.add(event_id)
        if event.get("sport_key") not in (None, sport_key):
            wrong_league += 1
            continue
        try:
            fixture = _fixture_from_event(league_code, sport_key, event)
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        parsed_odds = _h2h_odds(event, as_of=response.completed_at)
        odds = parsed_odds.odds
        if odds is None:
            if parsed_odds.timestamp_state == "missing":
                source_timestamp_missing += 1
            elif parsed_odds.timestamp_state == "malformed":
                source_timestamp_malformed += 1
            else:
                unsupported += 1
            continue
        source_timestamp = parsed_odds.source_timestamp
        if source_timestamp is None:
            source_timestamp_missing += 1
            unsupported += 1
            continue
        valid_fixture_count += 1
        valid_fixtures.append(fixture)
        valid_fixture_keys.append(fixture.fixture_key)
        source_timestamps.append(source_timestamp)
        fixtures.append(fixture)
        if not timing.signal_time_contract.accepts(
            fixture.kickoff,
            source_timestamp,
            response.completed_at,
        ):
            outside_window += 1
            fixtures.pop()
            continue
        snapshots.append(MarketSnapshot(
            fixture_key=fixture.fixture_key,
            captured_at=source_timestamp,
            kind=MarketSnapshotKind.SIGNAL_TIME,
            source="the_odds_api:bulk:h2h:eu",
            odds=odds,
            snapshot_id=f"{request_id}:{event_id}",
        ))

    if snapshots:
        status = "success" if not (
            unsupported
            or malformed
            or wrong_league
            or source_timestamp_missing
            or source_timestamp_malformed
        ) else "partial"
        failure_reason = None if status == "success" else "partial_payload"
    elif valid_fixture_count == 0 and unsupported:
        status, failure_reason = "unsupported_market", "h2h_unavailable"
    elif outside_window:
        status, failure_reason = "no_eligible_fixtures", "outside_signal_window"
    elif source_timestamp_malformed:
        status, failure_reason = "freshness_unavailable", "malformed_source_timestamp"
    elif source_timestamp_missing:
        status, failure_reason = "freshness_unavailable", "missing_source_timestamp"
    else:
        status, failure_reason = "malformed", "no_valid_fixture"
    empty.update({
        "status": status,
        "status_code": response.status_code,
        "latency_ms": response.latency_ms,
        "timeout": False,
        "events_seen": events_seen,
        "valid_fixture_count": valid_fixture_count,
        "eligible_fixture_count": len(fixtures),
        "snapshot_count": len(snapshots),
        "outside_window_count": outside_window,
        "unsupported_market_count": unsupported,
        "malformed_event_count": malformed,
        "wrong_league_count": wrong_league,
        "source_timestamp_missing_count": source_timestamp_missing,
        "source_timestamp_malformed_count": source_timestamp_malformed,
        "failure_reason": failure_reason,
        "quota_headers": headers,
        "fixtures": tuple(fixtures),
        "snapshots": tuple(snapshots),
        "valid_fixtures": tuple(valid_fixtures),
        "valid_fixture_keys": tuple(valid_fixture_keys),
        "source_timestamps": tuple(source_timestamps),
    })
    return ProviderObservation(**empty)


def _fixture_from_event(league_code: str, sport_key: str, event: Mapping[str, object]) -> Fixture:
    event_id = str(event["id"]).strip()
    home = str(event["home_team"]).strip()
    away = str(event["away_team"]).strip()
    kickoff = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
    return Fixture(f"the_odds_api:{sport_key}:{event_id}", league_code, home, away, kickoff)


@dataclass(frozen=True)
class _ParsedH2HOdds:
    odds: dict[str, float] | None
    source_timestamp: datetime | None
    timestamp_state: str = "valid"


def _h2h_odds(event: Mapping[str, object], *, as_of: datetime) -> _ParsedH2HOdds:
    values: dict[str, tuple[float, datetime]] = {}
    missing_timestamps = malformed_timestamps = 0
    home = str(event.get("home_team", "")).strip().casefold()
    away = str(event.get("away_team", "")).strip().casefold()
    bookmakers = event.get("bookmakers")
    if not isinstance(bookmakers, list):
        return _ParsedH2HOdds(None, None, "invalid")
    for bookmaker in bookmakers:
        if not isinstance(bookmaker, Mapping):
            continue
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, Mapping) or market.get("key") != "h2h":
                continue
            source_timestamp, timestamp_state = _source_timestamp(market, bookmaker, as_of)
            if timestamp_state == "missing":
                missing_timestamps += 1
                continue
            if timestamp_state == "malformed":
                malformed_timestamps += 1
                continue
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list):
                continue
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    continue
                name = str(outcome.get("name", "")).strip()
                try:
                    price = float(outcome["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not isfinite(price) or price <= 1.0:
                    continue
                normalized = name.casefold()
                key = (
                    "home" if normalized == home
                    else "away" if normalized == away
                    else "draw" if normalized == "draw"
                    else None
                )
                if key is not None:
                    old = values.get(key)
                    if old is None or price > old[0] or (price == old[0] and source_timestamp < old[1]):
                        values[key] = (price, source_timestamp)
    if set(values) != {"home", "draw", "away"}:
        state = "malformed" if malformed_timestamps else "missing" if missing_timestamps else "invalid"
        return _ParsedH2HOdds(None, None, state)
    selected_timestamps = tuple(timestamp for _, timestamp in values.values())
    return _ParsedH2HOdds(
        {key: value for key, (value, _) in values.items()},
        min(selected_timestamps),
    )


def _source_timestamp(
    market: Mapping[str, object],
    bookmaker: Mapping[str, object],
    as_of: datetime,
) -> tuple[datetime | None, str]:
    raw = market.get("last_update")
    if raw is None or not str(raw).strip():
        raw = bookmaker.get("last_update")
    if raw is None or not str(raw).strip():
        return None, "missing"
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None, "malformed"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "malformed"
    parsed = parsed.astimezone(timezone.utc)
    if parsed > as_of:
        return None, "malformed"
    return parsed, "valid"


def _request_id(league_code: str, sport_key: str, requested_at: datetime) -> str:
    return _stable_id("provider-request", (league_code, sport_key, requested_at.isoformat()))


def _stable_id(prefix: str, fields: Sequence[str]) -> str:
    encoded = json.dumps(tuple(fields), separators=(",", ":")).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:32]}"
