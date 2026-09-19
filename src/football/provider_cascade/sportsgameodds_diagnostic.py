"""Bounded, redacted SportsGameOdds account and coverage diagnostic."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from src.football.production_contracts import _utc
from src.football.provider_cascade.adapters import (
    HttpTransport,
    ProviderRequest,
    _parse_timestamp,
    requests_transport,
)
from src.football.provider_cascade.sportsgameodds import (
    SPORTSGAMEODDS_API_URL,
    SPORTSGAMEODDS_LEAGUE_ID,
    SPORTSGAMEODDS_PROVIDER,
    SPORTSGAMEODDS_REGULATION_MARKET_IDS,
    SportsGameOddsError,
)
from src.football.provider_cascade.sportsgameodds_normalization import (
    _bookmaker_rows,
    _relevant_odds,
)


@dataclass(frozen=True)
class SportsGameOddsDiagnostic:
    """Redacted diagnostic summary safe for a review report."""

    status: str
    request_count: int
    endpoints: tuple[str, ...]
    league_available: bool | None = None
    market_available: bool | None = None
    fixture_count: int = 0
    regulation_1x2_events: int = 0
    bookmakers: tuple[str, ...] = ()
    freshest_age_seconds: float | None = None
    objects_returned: int = 0
    objects_consumed: int | None = None
    usage_before: Mapping[str, object] | None = None
    usage_after: Mapping[str, object] | None = None
    error: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "request_count": self.request_count,
            "endpoints": list(self.endpoints),
            "league_available": self.league_available,
            "market_available": self.market_available,
            "fixture_count": self.fixture_count,
            "regulation_1x2_events": self.regulation_1x2_events,
            "bookmakers": list(self.bookmakers),
            "freshest_age_seconds": self.freshest_age_seconds,
            "objects_returned": self.objects_returned,
            "objects_consumed": self.objects_consumed,
            "usage_before": self.usage_before,
            "usage_after": self.usage_after,
            "error": self.error,
        }


def _safe_usage(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, Mapping) or payload.get("success") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    result: dict[str, object] = {
        "tier": data.get("tier"),
        "is_active": data.get("isActive"),
    }
    limits = data.get("rateLimits")
    if isinstance(limits, Mapping):
        safe_limits: dict[str, object] = {}
        for interval, raw in limits.items():
            if not isinstance(raw, Mapping):
                continue
            safe_limits[str(interval)] = {
                key: raw.get(key)
                for key in (
                    "max-requests",
                    "current-requests",
                    "max-entities",
                    "current-entities",
                )
                if key in raw and isinstance(raw.get(key), (str, int, float))
            }
        result["rate_limits"] = safe_limits
    return result


def _usage_value(usage: Mapping[str, object] | None, field: str) -> int | None:
    limits = usage.get("rate_limits") if usage else None
    month = limits.get("per-month") if isinstance(limits, Mapping) else None
    value = month.get(field) if isinstance(month, Mapping) else None
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _summarize_events(events: object, now: datetime) -> dict[str, object]:
    if not isinstance(events, list):
        return {
            "fixture_count": 0,
            "regulation_1x2_events": 0,
            "bookmakers": [],
            "freshest_age_seconds": None,
        }
    bookmakers: set[str] = set()
    freshness: list[float] = []
    regulation_events = 0
    for event in events:
        if (
            not isinstance(event, Mapping)
            or str(event.get("leagueID", "")) != SPORTSGAMEODDS_LEAGUE_ID
        ):
            continue
        try:
            markets = _relevant_odds(event)
        except SportsGameOddsError:
            continue
        event_has_regulation = False
        for market in markets.values():
            for bookmaker_id, row in _bookmaker_rows(market.get("byBookmaker")):
                if row.get("available") is True:
                    bookmakers.add(bookmaker_id)
                    timestamp = _parse_timestamp(row.get("lastUpdatedAt"), now=now)
                    if timestamp is not None:
                        freshness.append((now - timestamp).total_seconds())
                    event_has_regulation = True
        if event_has_regulation:
            regulation_events += 1
    return {
        "fixture_count": len(events),
        "regulation_1x2_events": regulation_events,
        "bookmakers": sorted(bookmakers),
        "freshest_age_seconds": min(freshness) if freshness else None,
    }


class SportsGameOddsDiagnosticClient:
    """Five-request maximum account/league/market/events diagnostic."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: HttpTransport | None = None,
        timeout_seconds: float = 5.0,
        max_requests: int = 5,
    ) -> None:
        self._api_key = (
            api_key if api_key is not None else os.getenv("SPORTSGAMEODDS_API_KEY", "")
        )
        self._transport = transport or requests_transport
        self._timeout_seconds = timeout_seconds
        self._max_requests = min(max_requests, 5)

    def run(self, *, now: datetime | None = None) -> SportsGameOddsDiagnostic:
        if not self._api_key.strip():
            return SportsGameOddsDiagnostic("REAL_TEST_READY", 0, ())
        if self._max_requests < 5:
            raise SportsGameOddsError("diagnostic_requires_five_request_budget")
        captured_now = _utc(now or datetime.now(timezone.utc), "diagnostic now")
        calls: list[str] = []
        usage_before: dict[str, object] | None = None
        usage_after: dict[str, object] | None = None
        league_available: bool | None = None
        market_available: bool | None = None
        events: object = []
        error: str | None = None

        plan = (
            ("/account/usage", {}),
            ("/leagues", {"leagueID": SPORTSGAMEODDS_LEAGUE_ID}),
            (
                "/markets",
                {
                    "leagueID": SPORTSGAMEODDS_LEAGUE_ID,
                    "sportID": "SOCCER",
                    "isSupported": "true",
                    "limit": "10000",
                },
            ),
            (
                "/events",
                {
                    "leagueID": SPORTSGAMEODDS_LEAGUE_ID,
                    "type": "match",
                    "oddsAvailable": "true",
                    "live": "false",
                    "started": "false",
                    "oddID": ",".join(SPORTSGAMEODDS_REGULATION_MARKET_IDS),
                    "includeOpposingOdds": "true",
                    "limit": "100",
                },
            ),
            ("/account/usage", {}),
        )
        for path, params in plan:
            calls.append(path)
            response = self._transport(
                ProviderRequest(
                    SPORTSGAMEODDS_PROVIDER,
                    f"{SPORTSGAMEODDS_API_URL}{path}",
                    params,
                    {"X-Api-Key": self._api_key, "Accept": "application/json"},
                ),
                self._timeout_seconds,
            )
            if (
                response.status_code != 200
                or not isinstance(response.payload, Mapping)
                or response.payload.get("success") is False
            ):
                error = f"{path}:http_{response.status_code or 'no_status'}"
                break
            if path == "/account/usage":
                safe = _safe_usage(response.payload)
                if usage_before is None:
                    usage_before = safe
                else:
                    usage_after = safe
            elif path == "/leagues":
                data = response.payload.get("data")
                league_available = (
                    any(
                        isinstance(item, Mapping)
                        and item.get("leagueID") == SPORTSGAMEODDS_LEAGUE_ID
                        for item in data
                    )
                    if isinstance(data, list)
                    else False
                )
            elif path == "/markets":
                data = response.payload.get("data")
                market_available = (
                    any(
                        isinstance(item, Mapping)
                        and item.get("periodID") == "reg"
                        and item.get("betTypeID") == "ml3way"
                        and item.get("sideID") in {"home", "draw", "away"}
                        for item in data
                    )
                    if isinstance(data, list)
                    else False
                )
            elif path == "/events":
                events = response.payload.get("data", [])
        summary = _summarize_events(events, captured_now)
        consumed = None
        before_entities = _usage_value(usage_before, "current-entities")
        after_entities = _usage_value(usage_after, "current-entities")
        if before_entities is not None and after_entities is not None:
            consumed = max(0, after_entities - before_entities)
        return SportsGameOddsDiagnostic(
            "ERROR" if error else "COMPLETED",
            len(calls),
            tuple(calls),
            league_available=league_available,
            market_available=market_available,
            fixture_count=int(summary["fixture_count"]),
            regulation_1x2_events=int(summary["regulation_1x2_events"]),
            bookmakers=tuple(summary["bookmakers"]),
            freshest_age_seconds=summary["freshest_age_seconds"],
            objects_returned=int(summary["fixture_count"]),
            objects_consumed=consumed,
            usage_before=usage_before,
            usage_after=usage_after,
            error=error,
        )


__all__ = ["SportsGameOddsDiagnostic", "SportsGameOddsDiagnosticClient"]
