"""Provider-neutral redundancy and quality contracts for Top-5 shadow odds.

This module is deliberately a contract and planning boundary.  It normalizes
injected source payloads, audits the sources that already exist in SportsBrain,
and plans possible request paths without making a provider call.  It never
selects an authority, writes runtime state, invokes a scheduler, publishes an
artifact, or activates a Top-5 league.

The existing provider modules remain the owners of their network behavior.  The
adapters here accept their already-observed payload shape (or an explicitly
enriched injected fixture) and fail closed when required identity or timing
metadata is absent.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Any, TypeVar

from src.football.production_contracts import ProductionContractError
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_validation import (
    EvidenceProvenance,
    ProviderEvidence,
    SafetyAssertions,
    ShadowEvidenceBundle,
    ShadowObservationEvidence,
)
from src.football.top5_shadow_validation import (
    ProviderOutcome as ValidationProviderOutcome,
)


class ShadowProviderContractError(ProductionContractError):
    """Invalid provider input, identity, planning assumption, or provenance."""


class SourceNormalizationError(ShadowProviderContractError):
    """An injected source payload cannot be normalized safely."""

    def __init__(self, error: SourceError, message: str):
        self.error = SourceError(error)
        super().__init__(message)


class CapabilityKind(str, Enum):
    FIXTURES = "fixtures"
    ODDS = "odds"
    RESULTS = "results"
    CLOSING_BENCHMARK = "closing_benchmark"


class CapabilityStatus(str, Enum):
    IMPLEMENTED_CANDIDATE = "implemented_candidate"
    CANDIDATE_ONLY = "candidate_only"
    RESULT_ONLY = "result_only"
    HISTORICAL_ONLY = "historical_only"
    CACHE_ONLY = "cache_only"
    UNSUPPORTED = "unsupported"
    REJECTED = "rejected"


class MarketRole(str, Enum):
    SIGNAL_TIME = "signal_time"
    CLOSING_BENCHMARK = "closing_benchmark"


class CompletenessState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class SourceError(str, Enum):
    NONE = "none"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    MALFORMED_RESPONSE = "malformed_response"
    PARTIAL_MARKET = "partial_market"
    WRONG_FIXTURE = "wrong_fixture"
    WRONG_LEAGUE = "wrong_league"
    WRONG_MARKET = "wrong_market"
    TEAM_ALIAS_MISMATCH = "team_alias_mismatch"
    KICKOFF_MISMATCH = "kickoff_mismatch"
    INVERTED_HOME_AWAY = "inverted_home_away"
    MISSING_DRAW = "missing_draw"
    ODDS_SANITY = "odds_sanity"
    STALE_ODDS = "stale_odds"
    DUPLICATE_SNAPSHOT = "duplicate_snapshot"
    MISSING_PROVENANCE = "missing_provenance"
    MISSING_TIMESTAMP = "missing_timestamp"
    CLOSING_LEAKAGE = "closing_leakage"


class AdapterStatus(str, Enum):
    CANDIDATE_ONLY = "candidate_only"
    HISTORICAL_ONLY = "historical_only"
    CACHE_ONLY = "cache_only"


class OddsApiRequestKind(str, Enum):
    AUTHENTICATION = "authentication"
    SPORT_DISCOVERY = "sport_discovery"
    ODDS = "odds"
    RESULTS = "results"
    CLOSING_BENCHMARK = "closing_benchmark"


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

TOP5_LEAGUE_CODES = ("BL1", "EPL", "LL", "SA", "L1")
TOP5_SPORT_KEYS = MappingProxyType(
    {
        "BL1": "soccer_germany_bundesliga",
        "EPL": "soccer_epl",
        "LL": "soccer_spain_la_liga",
        "SA": "soccer_italy_serie_a",
        "L1": "soccer_france_ligue_1",
    }
)
TOP5_LEAGUE_NAMES = MappingProxyType(
    {
        "BL1": "Bundesliga",
        "EPL": "Premier League",
        "LL": "La Liga",
        "SA": "Serie A",
        "L1": "Ligue 1",
    }
)

_LEAGUE_ALIASES = MappingProxyType(
    {
        **{code.lower(): code for code in TOP5_LEAGUE_CODES},
        **{key: code for code, key in TOP5_SPORT_KEYS.items()},
        **{name.lower(): code for code, name in TOP5_LEAGUE_NAMES.items()},
    }
)


def normalize_league(value: object) -> str:
    """Resolve only exact known Top-5 aliases; unknown leagues fail closed."""

    if not isinstance(value, str) or not value.strip():
        raise ShadowProviderContractError("league identity is required")
    code = _LEAGUE_ALIASES.get(value.strip().lower())
    if code is None:
        raise ShadowProviderContractError(
            f"league is outside the Top-5 scope: {value!r}"
        )
    return code


_TEAM_ALIASES = MappingProxyType(
    {
        "man utd": "manchester united",
        "man united": "manchester united",
        "manchester utd": "manchester united",
        "psg": "paris saint germain",
        "paris sg": "paris saint germain",
        "bayern munchen": "bayern munich",
        "inter milan": "internazionale",
        "inter": "internazionale",
        "atletico madrid": "atletico de madrid",
    }
)


def normalize_team_name(value: object) -> str:
    """Normalize a known spelling without fuzzy or substring matching."""

    if not isinstance(value, str) or not value.strip():
        raise ShadowProviderContractError("team identity is required")
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    tokens = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_value.lower()).split()
    normalized = " ".join(tokens)
    return _TEAM_ALIASES.get(normalized, normalized)


def _utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ShadowProviderContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field_name)
    if isinstance(value, str):
        try:
            return _utc(
                datetime.fromisoformat(value.replace("Z", "+00:00")), field_name
            )
        except ValueError as exc:
            raise ShadowProviderContractError(
                f"{field_name} is not a valid timestamp"
            ) from exc
    raise ShadowProviderContractError(f"{field_name} is required")


def _optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, field_name)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowProviderContractError(f"{field_name} is required")
    return value.strip()


def _sha(value: object, field_name: str) -> str:
    text = _required_text(value, field_name)
    if _SHA_RE.fullmatch(text) is None:
        raise ShadowProviderContractError(f"{field_name} must be a 40-64 character SHA")
    return text.lower()


def _number(value: object, field_name: str, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ShadowProviderContractError(f"{field_name} must be numeric") from exc
    if not isfinite(result) or (minimum is not None and result < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise ShadowProviderContractError(f"{field_name} must be finite{suffix}")
    return result


def make_fixture_key(
    league: object, home_team: object, away_team: object, kickoff: object
) -> str:
    """Return the canonical internal fixture identity."""

    league_code = normalize_league(league)
    home = normalize_team_name(home_team)
    away = normalize_team_name(away_team)
    kickoff_utc = _utc(kickoff, "kickoff")
    if home == away:
        raise ShadowProviderContractError("fixture teams must be distinct")
    return f"{league_code}|{home}|{away}|{kickoff_utc.isoformat()}"


@dataclass(frozen=True)
class CapabilityRecord:
    """One source capability row; each capability is classified separately."""

    source: str
    capability: CapabilityKind
    status: CapabilityStatus
    implementation_location: str
    league_coverage: tuple[str, ...]
    supports_1x2: bool
    timestamps: str
    bookmaker_identity: str
    freshness: str
    authentication: str
    rate_limits: str
    failure_modes: tuple[str, ...]
    bulk_capability: str
    usable_at_signal_time: bool
    usable_historically: bool
    closing_benchmark_only: bool
    authoritative_approved: bool = False
    notes: str = ""

    def validate(self) -> None:
        _required_text(self.source, "source")
        _required_text(self.implementation_location, "implementation_location")
        CapabilityKind(self.capability)
        CapabilityStatus(self.status)
        if any(
            not isinstance(code, str) or not code.strip()
            for code in self.league_coverage
        ):
            raise ShadowProviderContractError(
                "capability league coverage contains a blank"
            )
        if self.authoritative_approved:
            raise ShadowProviderContractError(
                "no provider authority is approved by this workstream"
            )
        if self.closing_benchmark_only and self.usable_at_signal_time:
            raise ShadowProviderContractError(
                "closing benchmark cannot be signal-time usable"
            )

    def as_row(self) -> dict[str, object]:
        self.validate()
        return {
            "source": self.source,
            "capability": self.capability.value,
            "status": self.status.value,
            "implementation_location": self.implementation_location,
            "league_coverage": list(self.league_coverage),
            "supports_1x2": self.supports_1x2,
            "timestamps": self.timestamps,
            "bookmaker_identity": self.bookmaker_identity,
            "freshness": self.freshness,
            "authentication": self.authentication,
            "rate_limits": self.rate_limits,
            "failure_modes": list(self.failure_modes),
            "bulk_capability": self.bulk_capability,
            "usable_at_signal_time": self.usable_at_signal_time,
            "usable_historically": self.usable_historically,
            "closing_benchmark_only": self.closing_benchmark_only,
            "authoritative_approved": False,
            "notes": self.notes,
        }


def _cap(
    source: str,
    capability: CapabilityKind,
    status: CapabilityStatus,
    location: str,
    coverage: tuple[str, ...],
    *,
    one_x_two: bool,
    timestamps: str,
    bookmaker: str,
    freshness: str,
    auth: str,
    rate_limits: str,
    failures: tuple[str, ...],
    bulk: str,
    signal_time: bool,
    historical: bool,
    closing_only: bool,
    notes: str = "",
) -> CapabilityRecord:
    record = CapabilityRecord(
        source=source,
        capability=capability,
        status=status,
        implementation_location=location,
        league_coverage=coverage,
        supports_1x2=one_x_two,
        timestamps=timestamps,
        bookmaker_identity=bookmaker,
        freshness=freshness,
        authentication=auth,
        rate_limits=rate_limits,
        failure_modes=failures,
        bulk_capability=bulk,
        usable_at_signal_time=signal_time,
        usable_historically=historical,
        closing_benchmark_only=closing_only,
        notes=notes,
    )
    record.validate()
    return record


def provider_inventory() -> tuple[CapabilityRecord, ...]:
    """Return the audited football source inventory in deterministic order."""

    top5 = TOP5_LEAGUE_CODES
    return (
        _cap(
            "the_odds_api",
            CapabilityKind.FIXTURES,
            CapabilityStatus.IMPLEMENTED_CANDIDATE,
            "src/data/football_discovery.py; src/data/odds_api.py",
            top5,
            one_x_two=False,
            timestamps="commence_time from event payload",
            bookmaker="not a fixture field",
            freshness="one-hour discovery cache; live endpoint when uncached",
            auth="ODDS_API_KEY",
            rate_limits="credit quota; current checkout reports 0 remaining",
            failures=("401/403", "429", "422", "timeout", "stale cache"),
            bulk="/sports discovery and bulk /odds; single-event fallback",
            signal_time=True,
            historical=False,
            closing_only=False,
            notes="Valid implementation candidate; no authority selected.",
        ),
        _cap(
            "the_odds_api",
            CapabilityKind.ODDS,
            CapabilityStatus.IMPLEMENTED_CANDIDATE,
            "src/football/odds/the_odds_api.py; src/data/odds_api.py",
            top5,
            one_x_two=True,
            timestamps="bookmaker last_update when supplied; event payload otherwise",
            bookmaker="bookmaker key/title in raw payload; consensus in legacy quote",
            freshness="live response or stale disk cache in legacy path",
            auth="ODDS_API_KEY",
            rate_limits="credit quota; 500 used / 0 remaining current state",
            failures=(
                "401",
                "403",
                "429",
                "422",
                "timeout",
                "malformed",
                "stale cache",
            ),
            bulk="one sport-key request can cover multiple fixtures/bookmakers",
            signal_time=True,
            historical=False,
            closing_only=False,
            notes="Only usable after quota preflight; never auto-retried when exhausted.",
        ),
        _cap(
            "the_odds_api",
            CapabilityKind.RESULTS,
            CapabilityStatus.IMPLEMENTED_CANDIDATE,
            "src/data/results_router.py; src/data/odds_api.py",
            ("BL1", "EPL"),
            one_x_two=False,
            timestamps="scores endpoint event timestamps",
            bookmaker="not applicable",
            freshness="live/recent scores window",
            auth="ODDS_API_KEY",
            rate_limits="credit quota; one request per sport fallback",
            failures=("missing sport mapping", "401/403", "429", "timeout", "empty"),
            bulk="sport-level /scores",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Results fallback is not coverage for LL/SA/L1 in current router.",
        ),
        _cap(
            "the_odds_api",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.UNSUPPORTED,
            "src/data/odds_api.py",
            (),
            one_x_two=False,
            timestamps="no historical closing contract",
            bookmaker="not exposed as a closing observation",
            freshness="not applicable",
            auth="ODDS_API_KEY",
            rate_limits="credit quota",
            failures=("no historical endpoint contract",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Not a closing source in this repository.",
        ),
        _cap(
            "betfair",
            CapabilityKind.FIXTURES,
            CapabilityStatus.CANDIDATE_ONLY,
            "src/football/odds/betfair.py",
            (),
            one_x_two=False,
            timestamps="raw catalogue may contain event data; legacy quote drops kickoff",
            bookmaker="exchange identity only",
            freshness="5-minute in-memory bulk cache",
            auth="BETFAIR_APP_KEY + username/password",
            rate_limits="vendor/API limits not declared in repository",
            failures=(
                "missing credentials",
                "login failure",
                "HTTP error",
                "empty catalogue",
                "event identity mismatch",
            ),
            bulk="catalogue + market-book bulk calls",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Competition mapping and safe kickoff/source timestamp are absent from current Top-5 path.",
        ),
        _cap(
            "betfair",
            CapabilityKind.ODDS,
            CapabilityStatus.CANDIDATE_ONLY,
            "src/football/odds/betfair.py",
            (),
            one_x_two=True,
            timestamps="legacy normalized quote has no source timestamp",
            bookmaker="exchange",
            freshness="5-minute in-memory bulk cache",
            auth="BETFAIR_APP_KEY + username/password",
            rate_limits="vendor/API limits not declared in repository",
            failures=(
                "missing credentials",
                "login failure",
                "HTTP error",
                "three-runner mismatch",
                "sanity rejection",
            ),
            bulk="market catalogue then market-book prices",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Can be normalized only from an injected enriched payload; not an approved authority.",
        ),
        _cap(
            "betfair",
            CapabilityKind.RESULTS,
            CapabilityStatus.UNSUPPORTED,
            "src/football/odds/betfair.py",
            (),
            one_x_two=False,
            timestamps="not implemented",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="Betfair credentials",
            rate_limits="not declared",
            failures=("no result loader",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "betfair",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.UNSUPPORTED,
            "src/football/odds/betfair.py",
            (),
            one_x_two=False,
            timestamps="not implemented",
            bookmaker="not implemented",
            freshness="not applicable",
            auth="Betfair credentials",
            rate_limits="not declared",
            failures=("no historical closing capture",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "oddsportal",
            CapabilityKind.FIXTURES,
            CapabilityStatus.CANDIDATE_ONLY,
            "src/football/odds/oddsportal.py",
            (),
            one_x_two=False,
            timestamps="legacy day page drops kickoff",
            bookmaker="aggregate page, not bookmaker identity",
            freshness="15-minute in-memory day cache",
            auth="none declared; Cloudflare may challenge",
            rate_limits="not declared; observed 403 path",
            failures=(
                "403",
                "non-200",
                "HTML selector drift",
                "wrong day",
                "ambiguous fixture",
            ),
            bulk="one day overview request",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Current scraper does not establish Top-5 league or exact kickoff identity.",
        ),
        _cap(
            "oddsportal",
            CapabilityKind.ODDS,
            CapabilityStatus.CANDIDATE_ONLY,
            "src/football/odds/oddsportal.py",
            (),
            one_x_two=True,
            timestamps="legacy quote has no source timestamp",
            bookmaker="oddsportal aggregate only; no bookmaker identity",
            freshness="15-minute in-memory day cache",
            auth="none declared; Cloudflare may challenge",
            rate_limits="not declared; observed 403 path",
            failures=("403", "HTML drift", "partial 1X2", "stale", "wrong fixture"),
            bulk="day-level overview",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Candidate-only; enriched metadata is mandatory before safe use.",
        ),
        _cap(
            "oddsportal",
            CapabilityKind.RESULTS,
            CapabilityStatus.UNSUPPORTED,
            "src/football/odds/oddsportal.py",
            (),
            one_x_two=False,
            timestamps="not implemented",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="none declared",
            rate_limits="not declared",
            failures=("no result parser",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "oddsportal",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.REJECTED,
            "src/football/odds/oddsportal.py",
            (),
            one_x_two=True,
            timestamps="no capture/closing timestamp contract",
            bookmaker="aggregate only",
            freshness="unbounded without source timestamp",
            auth="none declared",
            rate_limits="not declared",
            failures=(
                "closing time ambiguity",
                "selector drift",
                "historical identity ambiguity",
            ),
            bulk="day page only",
            signal_time=False,
            historical=False,
            closing_only=True,
            notes="Rejected until a distinct, timestamped closing capture is implemented.",
        ),
        _cap(
            "football_data",
            CapabilityKind.FIXTURES,
            CapabilityStatus.HISTORICAL_ONLY,
            "src/data/football_data.py",
            top5,
            one_x_two=False,
            timestamps="date only; no exact kickoff",
            bookmaker="not a fixture field",
            freshness="30-day local cache; season CSV historical",
            auth="none",
            rate_limits="public CSV availability; retry helper",
            failures=("missing season CSV", "CSV schema drift", "date parse failure"),
            bulk="one season CSV per league",
            signal_time=False,
            historical=True,
            closing_only=False,
            notes="Historical fixture/result rows only; not event-relative signal-time discovery.",
        ),
        _cap(
            "football_data",
            CapabilityKind.ODDS,
            CapabilityStatus.HISTORICAL_ONLY,
            "src/data/football_data.py",
            top5,
            one_x_two=True,
            timestamps="date only; PSH/PSD/PSA and PSCH/PSCD/PSCA columns",
            bookmaker="Pinnacle closing columns; opening bookmaker label implicit",
            freshness="season file; local 30-day cache",
            auth="none",
            rate_limits="public CSV availability",
            failures=("missing columns", "malformed odds", "CSV unavailable"),
            bulk="one league-season file",
            signal_time=False,
            historical=True,
            closing_only=True,
            notes="Opening/closing historical values; never signal-time input.",
        ),
        _cap(
            "football_data",
            CapabilityKind.RESULTS,
            CapabilityStatus.HISTORICAL_ONLY,
            "src/data/football_data.py; src/data/results_router.py",
            top5,
            one_x_two=False,
            timestamps="date only; final score columns FTHG/FTAG",
            bookmaker="not applicable",
            freshness="season file; local 30-day cache",
            auth="none",
            rate_limits="public CSV availability",
            failures=(
                "season not published",
                "missing final score",
                "team alias mismatch",
            ),
            bulk="one league-season CSV",
            signal_time=False,
            historical=True,
            closing_only=False,
            notes="Current results router supports all Top-5 codes through football-data mapping.",
        ),
        _cap(
            "football_data",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.HISTORICAL_ONLY,
            "src/data/football_data.py",
            top5,
            one_x_two=True,
            timestamps="PSCH/PSCD/PSCA are historical closing columns without capture time",
            bookmaker="Pinnacle closing proxy",
            freshness="historical season file",
            auth="none",
            rate_limits="public CSV availability",
            failures=("missing closing columns", "malformed odds", "wrong fixture"),
            bulk="one league-season file",
            signal_time=False,
            historical=True,
            closing_only=True,
            notes="Legitimate benchmark only; separate from prediction input.",
        ),
        _cap(
            "espn",
            CapabilityKind.FIXTURES,
            CapabilityStatus.RESULT_ONLY,
            "src/data/football_live.py; scripts/bundesliga2_scan.py",
            ("BL1", "EPL"),
            one_x_two=False,
            timestamps="event date and last_update",
            bookmaker="not applicable",
            freshness="public scoreboard; documented ~30-60s result lag",
            auth="none",
            rate_limits="public endpoint; retry helper",
            failures=(
                "missing mapping",
                "HTTP/network failure",
                "missing competitors",
                "name mismatch",
            ),
            bulk="one scoreboard request per mapped league",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Current Top-5 mapping is only BL1/EPL; no odds.",
        ),
        _cap(
            "espn",
            CapabilityKind.ODDS,
            CapabilityStatus.UNSUPPORTED,
            "src/data/football_live.py",
            (),
            one_x_two=False,
            timestamps="not applicable",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="none",
            rate_limits="public endpoint",
            failures=("no odds fields",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "espn",
            CapabilityKind.RESULTS,
            CapabilityStatus.RESULT_ONLY,
            "src/data/football_live.py; scripts/settle_bets.py",
            ("BL1", "EPL"),
            one_x_two=False,
            timestamps="event date and last_update",
            bookmaker="not applicable",
            freshness="public scoreboard; possible lag",
            auth="none",
            rate_limits="public endpoint",
            failures=("network failure", "incomplete score", "alias mismatch"),
            bulk="one scoreboard request per league",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="No authoritative Top-5 result decision is made here.",
        ),
        _cap(
            "espn",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.UNSUPPORTED,
            "src/data/football_live.py",
            (),
            one_x_two=False,
            timestamps="not applicable",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="none",
            rate_limits="public endpoint",
            failures=("no odds data",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "cache",
            CapabilityKind.FIXTURES,
            CapabilityStatus.CACHE_ONLY,
            "src/data/cache.py; data/cache/football_active_sports.json",
            (),
            one_x_two=False,
            timestamps="cache mtime only unless payload carries event timestamp",
            bookmaker="inherited/unknown",
            freshness="TTL-dependent; stale possible",
            auth="inherited",
            rate_limits="zero network requests when hit",
            failures=("missing cache", "expired cache", "corrupt cache"),
            bulk="cached prior source payload",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Not an independent source or authority.",
        ),
        _cap(
            "cache",
            CapabilityKind.ODDS,
            CapabilityStatus.CACHE_ONLY,
            "src/data/cache.py; data/cache/odds_api_upcoming_wide.pkl",
            (),
            one_x_two=True,
            timestamps="only safe if source timestamp is retained in payload",
            bookmaker="inherited from cached source",
            freshness="legacy path can return arbitrarily stale cache",
            auth="inherited",
            rate_limits="zero network requests on cache hit",
            failures=("stale cache", "corrupt pickle", "source identity missing"),
            bulk="cached bulk response",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="May keep a run alive but cannot replace a validated live source.",
        ),
        _cap(
            "cache",
            CapabilityKind.RESULTS,
            CapabilityStatus.UNSUPPORTED,
            "src/data/cache.py",
            (),
            one_x_two=False,
            timestamps="not a result source",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="inherited",
            rate_limits="none",
            failures=("no result contract",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "cache",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.UNSUPPORTED,
            "src/data/cache.py",
            (),
            one_x_two=False,
            timestamps="not a closing source",
            bookmaker="inherited/unknown",
            freshness="not applicable",
            auth="inherited",
            rate_limits="none",
            failures=("no closing semantics",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "pinnacle",
            CapabilityKind.FIXTURES,
            CapabilityStatus.REJECTED,
            "src/football/odds/pinnacle.py",
            (),
            one_x_two=False,
            timestamps="legacy matchup payload; BL2 keyword filter only",
            bookmaker="Pinnacle guest API",
            freshness="5-minute in-memory cache",
            auth="guest endpoint",
            rate_limits="vendor endpoint; retry helper",
            failures=("BL2-only mapping", "network", "schema drift"),
            bulk="leagues then matchups then markets",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Current implementation is Bundesliga-2-specific, outside Top-5 scope.",
        ),
        _cap(
            "pinnacle",
            CapabilityKind.ODDS,
            CapabilityStatus.REJECTED,
            "src/football/odds/pinnacle.py",
            (),
            one_x_two=True,
            timestamps="legacy quote has no source timestamp",
            bookmaker="Pinnacle",
            freshness="5-minute in-memory cache",
            auth="guest endpoint",
            rate_limits="retry helper; vendor endpoint",
            failures=("BL2-only", "missing draw", "sanity rejection", "network"),
            bulk="league/matchup/market calls",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="No Top-5 competition mapping exists in current code.",
        ),
        _cap(
            "pinnacle",
            CapabilityKind.RESULTS,
            CapabilityStatus.UNSUPPORTED,
            "src/football/odds/pinnacle.py",
            (),
            one_x_two=False,
            timestamps="not implemented",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="guest endpoint",
            rate_limits="not declared",
            failures=("no result loader",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "pinnacle",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.UNSUPPORTED,
            "src/football/odds/pinnacle.py",
            (),
            one_x_two=False,
            timestamps="no historical closing capture",
            bookmaker="not implemented",
            freshness="not applicable",
            auth="guest endpoint",
            rate_limits="not declared",
            failures=("no closing path",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "websearch",
            CapabilityKind.FIXTURES,
            CapabilityStatus.REJECTED,
            "src/football/odds/websearch.py",
            (),
            one_x_two=False,
            timestamps="no fixture timestamp",
            bookmaker="not available",
            freshness="unbounded search snippets",
            auth="DDGS dependency",
            rate_limits="search-engine dependent",
            failures=("ambiguous search", "stale snippet", "wrong league"),
            bulk="one query per search",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Not a fixture authority.",
        ),
        _cap(
            "websearch",
            CapabilityKind.ODDS,
            CapabilityStatus.REJECTED,
            "src/football/odds/websearch.py",
            (),
            one_x_two=True,
            timestamps="no source timestamp",
            bookmaker="no bookmaker identity",
            freshness="snippet age unknown",
            auth="DDGS dependency",
            rate_limits="search-engine dependent",
            failures=(
                "single-source quote",
                "wrong fixture",
                "wrong league",
                "stale snippet",
                "parse ambiguity",
            ),
            bulk="three broad queries per match",
            signal_time=False,
            historical=False,
            closing_only=False,
            notes="Legacy path is no-bet/display-only and cannot satisfy this source contract.",
        ),
        _cap(
            "websearch",
            CapabilityKind.RESULTS,
            CapabilityStatus.UNSUPPORTED,
            "src/football/odds/websearch.py",
            (),
            one_x_two=False,
            timestamps="not implemented",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="DDGS dependency",
            rate_limits="search-engine dependent",
            failures=("no result contract",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "websearch",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.REJECTED,
            "src/football/odds/websearch.py",
            (),
            one_x_two=True,
            timestamps="no closing timestamp",
            bookmaker="no identity",
            freshness="unknown",
            auth="DDGS dependency",
            rate_limits="search-engine dependent",
            failures=("closing leakage risk", "source ambiguity"),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=True,
        ),
        _cap(
            "betexplorer",
            CapabilityKind.FIXTURES,
            CapabilityStatus.HISTORICAL_ONLY,
            "src/data/betexplorer.py",
            (),
            one_x_two=False,
            timestamps="tournament result page; no Top-5 club coverage",
            bookmaker="default aggregate",
            freshness="historical scrape",
            auth="Playwright/browser dependency",
            rate_limits="site/browser dependent",
            failures=("JS rendering", "slug ambiguity", "page drift"),
            bulk="tournament results page",
            signal_time=False,
            historical=True,
            closing_only=False,
            notes="World Cup/Euro/Copa historical tournaments, not Top-5 leagues.",
        ),
        _cap(
            "betexplorer",
            CapabilityKind.ODDS,
            CapabilityStatus.HISTORICAL_ONLY,
            "src/data/betexplorer.py",
            (),
            one_x_two=True,
            timestamps="historical row; no signal-time capture",
            bookmaker="betexplorer_default",
            freshness="historical scrape",
            auth="Playwright/browser dependency",
            rate_limits="site/browser dependent",
            failures=("JS rendering", "missing odds cell", "slug ambiguity"),
            bulk="one tournament page",
            signal_time=False,
            historical=True,
            closing_only=False,
            notes="Not a Top-5 signal-time candidate.",
        ),
        _cap(
            "betexplorer",
            CapabilityKind.RESULTS,
            CapabilityStatus.UNSUPPORTED,
            "src/data/betexplorer.py",
            (),
            one_x_two=False,
            timestamps="not used as result loader",
            bookmaker="not applicable",
            freshness="not applicable",
            auth="Playwright/browser dependency",
            rate_limits="site/browser dependent",
            failures=("no result router",),
            bulk="not implemented",
            signal_time=False,
            historical=False,
            closing_only=False,
        ),
        _cap(
            "betexplorer",
            CapabilityKind.CLOSING_BENCHMARK,
            CapabilityStatus.REJECTED,
            "src/data/betexplorer.py",
            (),
            one_x_two=True,
            timestamps="no explicit closing capture",
            bookmaker="default aggregate",
            freshness="historical only",
            auth="Playwright/browser dependency",
            rate_limits="site/browser dependent",
            failures=("closing semantics absent", "historical league mismatch"),
            bulk="tournament page",
            signal_time=False,
            historical=True,
            closing_only=True,
        ),
    )


def capability_matrix() -> tuple[CapabilityRecord, ...]:
    """Compatibility alias for report callers."""

    return provider_inventory()


@dataclass(frozen=True)
class SourceProvenance:
    source_uri: str
    raw_record_id: str
    retrieved_at: datetime
    adapter_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at")
        )

    def validate(self) -> None:
        _required_text(self.source_uri, "source_uri")
        if _SCHEME_RE.match(self.source_uri) is None:
            raise ShadowProviderContractError("source_uri must have an explicit scheme")
        _required_text(self.raw_record_id, "raw_record_id")
        _required_text(self.adapter_version, "adapter_version")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "source_uri": self.source_uri,
            "raw_record_id": self.raw_record_id,
            "retrieved_at": self.retrieved_at.isoformat(),
            "adapter_version": self.adapter_version,
        }


@dataclass(frozen=True)
class ExpectedFixture:
    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "league", normalize_league(self.league))
        object.__setattr__(self, "kickoff", _utc(self.kickoff, "kickoff"))

    def validate(self) -> None:
        expected_key = make_fixture_key(
            self.league, self.home_team, self.away_team, self.kickoff
        )
        if self.fixture_key != expected_key:
            raise ShadowProviderContractError("expected fixture_key is not canonical")
        if normalize_team_name(self.home_team) == normalize_team_name(self.away_team):
            raise ShadowProviderContractError("expected fixture teams must be distinct")


@dataclass(frozen=True)
class ShadowSourceObservation:
    """One normalized provider observation, including rejected observations."""

    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime
    provider_identity: str
    bookmaker_identity: str | None
    market_type: str
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    capture_timestamp: datetime
    source_timestamp: datetime | None
    request_identity: str
    source_provenance: SourceProvenance | None
    completeness: CompletenessState = CompletenessState.COMPLETE
    confidence: float = 1.0
    error_classification: SourceError = SourceError.NONE
    market_role: MarketRole = MarketRole.SIGNAL_TIME
    request_latency_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "league", normalize_league(self.league))
        object.__setattr__(self, "kickoff", _utc(self.kickoff, "kickoff"))
        object.__setattr__(
            self, "capture_timestamp", _utc(self.capture_timestamp, "capture_timestamp")
        )
        if self.source_timestamp is not None:
            object.__setattr__(
                self,
                "source_timestamp",
                _utc(self.source_timestamp, "source_timestamp"),
            )

    @property
    def odds_age_seconds(self) -> float | None:
        if self.source_timestamp is None:
            return None
        return (self.capture_timestamp - self.source_timestamp).total_seconds()

    @property
    def signal_time_input_allowed(self) -> bool:
        return self.market_role is MarketRole.SIGNAL_TIME

    def validate(self) -> None:
        expected_key = make_fixture_key(
            self.league, self.home_team, self.away_team, self.kickoff
        )
        if self.fixture_key != expected_key:
            raise ShadowProviderContractError(
                "observation fixture_key is not canonical for its identity"
            )
        for field_name, value in (
            ("provider_identity", self.provider_identity),
            ("market_type", self.market_type),
            ("request_identity", self.request_identity),
        ):
            _required_text(value, field_name)
        if self.bookmaker_identity is not None:
            _required_text(self.bookmaker_identity, "bookmaker_identity")
        CompletenessState(self.completeness)
        MarketRole(self.market_role)
        SourceError(self.error_classification)
        for name, value in (
            ("home_odds", self.home_odds),
            ("draw_odds", self.draw_odds),
            ("away_odds", self.away_odds),
        ):
            if value is not None:
                _number(value, name)
        _number(self.confidence, "confidence")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ShadowProviderContractError("confidence must be in [0, 1]")
        if (
            not isinstance(self.request_latency_ms, int)
            or isinstance(self.request_latency_ms, bool)
            or self.request_latency_ms < 0
        ):
            raise ShadowProviderContractError("request_latency_ms must be non-negative")
        if (
            self.source_timestamp is not None
            and self.source_timestamp > self.capture_timestamp
        ):
            raise ShadowProviderContractError(
                "source_timestamp cannot be after capture_timestamp"
            )
        if self.source_provenance is None:
            raise ShadowProviderContractError("source provenance is required")
        self.source_provenance.validate()
        if self.completeness is CompletenessState.COMPLETE:
            if any(
                value is None
                for value in (self.home_odds, self.draw_odds, self.away_odds)
            ):
                raise ShadowProviderContractError(
                    "complete observation requires all 1X2 odds"
                )
            if self.source_timestamp is None:
                raise ShadowProviderContractError(
                    "complete observation requires source_timestamp"
                )
        if (
            self.error_classification is SourceError.NONE
            and self.completeness is not CompletenessState.COMPLETE
        ):
            raise ShadowProviderContractError(
                "incomplete observation requires an error classification"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": self.kickoff.isoformat(),
            "provider_identity": self.provider_identity,
            "bookmaker_identity": self.bookmaker_identity,
            "market_type": self.market_type,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "capture_timestamp": self.capture_timestamp.isoformat(),
            "source_timestamp": self.source_timestamp.isoformat()
            if self.source_timestamp
            else None,
            "odds_age_seconds": self.odds_age_seconds,
            "request_identity": self.request_identity,
            "source_provenance": self.source_provenance.as_payload()
            if self.source_provenance
            else None,
            "completeness": self.completeness.value,
            "confidence": self.confidence,
            "error_classification": self.error_classification.value,
            "market_role": self.market_role.value,
            "request_latency_ms": self.request_latency_ms,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ShadowSourceObservation:
        if not isinstance(payload, Mapping):
            raise ShadowProviderContractError(
                "source observation payload must be a mapping"
            )
        provenance = payload.get("source_provenance")
        if not isinstance(provenance, Mapping):
            raise ShadowProviderContractError("source_provenance is required")
        return cls(
            league=payload.get("league", ""),
            fixture_key=payload.get("fixture_key", ""),
            home_team=payload.get("home_team", ""),
            away_team=payload.get("away_team", ""),
            kickoff=_parse_timestamp(payload.get("kickoff"), "kickoff"),
            provider_identity=payload.get("provider_identity", ""),
            bookmaker_identity=payload.get("bookmaker_identity"),
            market_type=payload.get("market_type", ""),
            home_odds=payload.get("home_odds"),
            draw_odds=payload.get("draw_odds"),
            away_odds=payload.get("away_odds"),
            capture_timestamp=_parse_timestamp(
                payload.get("capture_timestamp"), "capture_timestamp"
            ),
            source_timestamp=_optional_timestamp(
                payload.get("source_timestamp"), "source_timestamp"
            ),
            request_identity=payload.get("request_identity", ""),
            source_provenance=SourceProvenance(
                source_uri=provenance.get("source_uri", ""),
                raw_record_id=provenance.get("raw_record_id", ""),
                retrieved_at=_parse_timestamp(
                    provenance.get("retrieved_at"), "retrieved_at"
                ),
                adapter_version=provenance.get("adapter_version", ""),
            ),
            completeness=CompletenessState(
                payload.get("completeness", CompletenessState.COMPLETE.value)
            ),
            confidence=float(payload.get("confidence", 1.0)),
            error_classification=SourceError(
                payload.get("error_classification", SourceError.NONE.value)
            ),
            market_role=MarketRole(
                payload.get("market_role", MarketRole.SIGNAL_TIME.value)
            ),
            request_latency_ms=int(payload.get("request_latency_ms", 0)),
        )


@dataclass(frozen=True)
class SourceQualityPolicy:
    maximum_odds_age_seconds: int = 900
    kickoff_tolerance_seconds: int = 300
    required_market_type: str = "h2h_1x2"
    require_bookmaker_identity: bool = True

    def validate(self) -> None:
        if self.maximum_odds_age_seconds <= 0 or self.kickoff_tolerance_seconds < 0:
            raise ShadowProviderContractError("source quality timing policy is invalid")
        _required_text(self.required_market_type, "required_market_type")


@dataclass(frozen=True)
class SourceQualityReport:
    provider_identity: str
    candidate_status: AdapterStatus | None
    accepted: bool
    prediction_input_allowed: bool
    authority_approved: bool
    errors: tuple[SourceError, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.accepted

    def validate(self) -> None:
        _required_text(self.provider_identity, "provider_identity")
        if self.authority_approved:
            raise ShadowProviderContractError("source quality cannot approve authority")
        if self.accepted and self.errors:
            raise ShadowProviderContractError(
                "accepted source quality cannot contain errors"
            )
        if self.prediction_input_allowed and not self.accepted:
            raise ShadowProviderContractError(
                "rejected observation cannot enter prediction input"
            )


def _append_error(errors: list[SourceError], error: SourceError) -> None:
    if error is not SourceError.NONE and error not in errors:
        errors.append(error)


def validate_source_observation(
    observation: ShadowSourceObservation,
    expected: ExpectedFixture,
    policy: SourceQualityPolicy | None = None,
) -> SourceQualityReport:
    """Apply deterministic identity, freshness, market, and provenance gates."""

    expected.validate()
    policy = policy or SourceQualityPolicy()
    policy.validate()
    errors: list[SourceError] = []
    warnings: list[str] = []
    try:
        observation.validate()
    except ShadowProviderContractError as exc:
        warnings.append(str(exc))

    try:
        observed_league = normalize_league(observation.league)
        if observed_league != expected.league:
            _append_error(errors, SourceError.WRONG_LEAGUE)
    except ShadowProviderContractError:
        _append_error(errors, SourceError.WRONG_LEAGUE)

    if observation.fixture_key != expected.fixture_key:
        _append_error(errors, SourceError.WRONG_FIXTURE)

    try:
        observed_home = normalize_team_name(observation.home_team)
        observed_away = normalize_team_name(observation.away_team)
        expected_home = normalize_team_name(expected.home_team)
        expected_away = normalize_team_name(expected.away_team)
        if observed_home == expected_away and observed_away == expected_home:
            _append_error(errors, SourceError.INVERTED_HOME_AWAY)
        elif (observed_home, observed_away) != (expected_home, expected_away):
            _append_error(errors, SourceError.TEAM_ALIAS_MISMATCH)
    except ShadowProviderContractError:
        _append_error(errors, SourceError.TEAM_ALIAS_MISMATCH)

    try:
        kickoff_delta = abs(
            (_utc(observation.kickoff, "kickoff") - expected.kickoff).total_seconds()
        )
        if kickoff_delta > policy.kickoff_tolerance_seconds:
            _append_error(errors, SourceError.KICKOFF_MISMATCH)
    except ShadowProviderContractError:
        _append_error(errors, SourceError.KICKOFF_MISMATCH)

    if observation.market_type != policy.required_market_type:
        _append_error(errors, SourceError.WRONG_MARKET)
    if policy.require_bookmaker_identity and not observation.bookmaker_identity:
        _append_error(errors, SourceError.MISSING_PROVENANCE)

    if observation.source_provenance is None:
        _append_error(errors, SourceError.MISSING_PROVENANCE)
    else:
        try:
            observation.source_provenance.validate()
        except ShadowProviderContractError:
            _append_error(errors, SourceError.MISSING_PROVENANCE)

    age = observation.odds_age_seconds
    if age is None or age < 0:
        _append_error(errors, SourceError.MISSING_TIMESTAMP)
    elif age > policy.maximum_odds_age_seconds:
        _append_error(errors, SourceError.STALE_ODDS)

    odds = (observation.home_odds, observation.draw_odds, observation.away_odds)
    if any(value is None for value in odds):
        _append_error(
            errors,
            SourceError.MISSING_DRAW
            if observation.draw_odds is None
            else SourceError.PARTIAL_MARKET,
        )
    else:
        try:
            values = tuple(
                _number(value, name, minimum=1.01)
                for name, value in zip(
                    ("home_odds", "draw_odds", "away_odds"), odds, strict=True
                )
            )
            implied = sum(1.0 / value for value in values)
            if not (1.0 < implied <= 1.20):
                _append_error(errors, SourceError.ODDS_SANITY)
        except ShadowProviderContractError:
            _append_error(errors, SourceError.ODDS_SANITY)

    _append_error(errors, SourceError(observation.error_classification))
    if observation.market_role is MarketRole.CLOSING_BENCHMARK:
        _append_error(errors, SourceError.CLOSING_LEAKAGE)
        warnings.append(
            "closing benchmark is never admissible as signal-time prediction input"
        )

    status = adapter_status_for(observation.provider_identity)
    accepted = not errors
    report = SourceQualityReport(
        provider_identity=observation.provider_identity,
        candidate_status=status,
        accepted=accepted,
        prediction_input_allowed=accepted
        and observation.market_role is MarketRole.SIGNAL_TIME,
        authority_approved=False,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    report.validate()
    return report


def validate_observation_batch(
    observations: Sequence[ShadowSourceObservation],
    expected_by_fixture: Mapping[str, ExpectedFixture],
    policy: SourceQualityPolicy | None = None,
) -> tuple[SourceQualityReport, ...]:
    """Validate observations and reject duplicate source snapshots."""

    policy = policy or SourceQualityPolicy()
    seen: set[tuple[str, str, str, str, str | None]] = set()
    reports: list[SourceQualityReport] = []
    for observation in observations:
        expected = expected_by_fixture.get(observation.fixture_key)
        if expected is None:
            report = SourceQualityReport(
                provider_identity=observation.provider_identity,
                candidate_status=adapter_status_for(observation.provider_identity),
                accepted=False,
                prediction_input_allowed=False,
                authority_approved=False,
                errors=(SourceError.WRONG_FIXTURE,),
            )
        else:
            report = validate_source_observation(observation, expected, policy)
        snapshot_key = (
            observation.provider_identity,
            observation.fixture_key,
            observation.market_type,
            observation.bookmaker_identity or "",
            observation.source_timestamp.isoformat()
            if observation.source_timestamp
            else None,
        )
        if snapshot_key in seen:
            report = SourceQualityReport(
                provider_identity=report.provider_identity,
                candidate_status=report.candidate_status,
                accepted=False,
                prediction_input_allowed=False,
                authority_approved=False,
                errors=(*report.errors, SourceError.DUPLICATE_SNAPSHOT)
                if SourceError.DUPLICATE_SNAPSHOT not in report.errors
                else report.errors,
                warnings=report.warnings,
            )
        seen.add(snapshot_key)
        report.validate()
        reports.append(report)
    return tuple(reports)


@dataclass(frozen=True)
class AdapterDescriptor:
    provider_identity: str
    status: AdapterStatus
    implementation_locations: tuple[str, ...]
    signal_time_capable: bool
    closing_benchmark_capable: bool


_ADAPTERS = MappingProxyType(
    {
        "the_odds_api": AdapterDescriptor(
            "the_odds_api",
            AdapterStatus.CANDIDATE_ONLY,
            ("src/football/odds/the_odds_api.py", "src/data/odds_api.py"),
            True,
            False,
        ),
        "betfair": AdapterDescriptor(
            "betfair",
            AdapterStatus.CANDIDATE_ONLY,
            ("src/football/odds/betfair.py",),
            True,
            False,
        ),
        "oddsportal": AdapterDescriptor(
            "oddsportal",
            AdapterStatus.CANDIDATE_ONLY,
            ("src/football/odds/oddsportal.py",),
            True,
            False,
        ),
        "football_data": AdapterDescriptor(
            "football_data",
            AdapterStatus.HISTORICAL_ONLY,
            ("src/data/football_data.py",),
            False,
            True,
        ),
    }
)


def adapter_status_for(provider_identity: str) -> AdapterStatus | None:
    descriptor = _ADAPTERS.get(provider_identity)
    return descriptor.status if descriptor else None


def supported_candidate_adapters() -> tuple[str, ...]:
    """Return adapters that can normalize injected payloads without network calls."""

    return tuple(
        provider
        for provider, descriptor in _ADAPTERS.items()
        if descriptor.status is AdapterStatus.CANDIDATE_ONLY
    )


def _source_provenance(
    provider: str, record_id: str, retrieved_at: datetime, version: str
) -> SourceProvenance:
    uri = {
        "the_odds_api": "https://api.the-odds-api.com/v4/sports/{sport}/odds",
        "betfair": "https://api.betfair.com/exchange/betting/rest/v1.0/listMarketBook/",
        "oddsportal": "https://www.oddsportal.com/matches/football/{date}/",
        "football_data": "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv",
    }[provider]
    return SourceProvenance(uri, record_id, retrieved_at, version)


def _event_identity(
    payload: Mapping[str, object], expected: ExpectedFixture
) -> tuple[str, str, str, datetime, str]:
    try:
        home = _required_text(payload.get("home_team"), "home_team")
        away = _required_text(payload.get("away_team"), "away_team")
        kickoff = _parse_timestamp(
            payload.get("commence_time", payload.get("kickoff")), "commence_time"
        )
        league = normalize_league(payload.get("league", payload.get("sport_key", "")))
    except (ShadowProviderContractError, KeyError) as exc:
        raise SourceNormalizationError(
            SourceError.MALFORMED_RESPONSE, str(exc)
        ) from exc
    record_id = _required_text(
        payload.get("id", payload.get("event_id", "")), "event_id"
    )
    if league != expected.league:
        raise SourceNormalizationError(
            SourceError.WRONG_LEAGUE, "source event belongs to another league"
        )
    return home, away, league, kickoff, record_id


def _observation_from_prices(
    *,
    provider: str,
    payload: Mapping[str, object],
    expected: ExpectedFixture,
    capture_timestamp: datetime,
    request_identity: str,
    bookmaker_identity: str | None,
    prices: Mapping[str, object],
    source_timestamp: datetime | None,
    market_role: MarketRole,
    adapter_version: str,
    record_id: str,
) -> ShadowSourceObservation:
    home, away, league, kickoff, _ = _event_identity(payload, expected)
    home_key, away_key = normalize_team_name(home), normalize_team_name(away)
    values: dict[str, float | None] = {"home": None, "draw": None, "away": None}
    for key, raw in prices.items():
        normalized = str(key).strip().lower()
        if normalized in {home_key, "home"}:
            target = "home"
        elif normalized in {away_key, "away"}:
            target = "away"
        elif normalized in {"draw", "x", "tie"}:
            target = "draw"
        else:
            continue
        try:
            values[target] = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            values[target] = None
    missing = [name for name, value in values.items() if value is None]
    completeness = (
        CompletenessState.COMPLETE if not missing else CompletenessState.PARTIAL
    )
    error = SourceError.NONE
    if missing:
        error = (
            SourceError.MISSING_DRAW
            if "draw" in missing
            else SourceError.PARTIAL_MARKET
        )
    return ShadowSourceObservation(
        league=league,
        fixture_key=make_fixture_key(league, home, away, kickoff),
        home_team=home,
        away_team=away,
        kickoff=kickoff,
        provider_identity=provider,
        bookmaker_identity=bookmaker_identity,
        market_type="h2h_1x2",
        home_odds=values["home"],
        draw_odds=values["draw"],
        away_odds=values["away"],
        capture_timestamp=_utc(capture_timestamp, "capture_timestamp"),
        source_timestamp=source_timestamp,
        request_identity=_required_text(request_identity, "request_identity"),
        source_provenance=_source_provenance(
            provider,
            f"{record_id}:{bookmaker_identity or 'aggregate'}",
            _utc(capture_timestamp, "capture_timestamp"),
            adapter_version,
        ),
        completeness=completeness,
        confidence=1.0,
        error_classification=error,
        market_role=market_role,
    )


class TheOddsAPIAdapter:
    """Normalize injected The Odds API event/bookmaker payloads only."""

    provider_identity = "the_odds_api"
    status = AdapterStatus.CANDIDATE_ONLY
    adapter_version = "top5-shadow-source-v1/the-odds-api"

    @classmethod
    def normalize(
        cls,
        payload: Mapping[str, object],
        expected: ExpectedFixture,
        *,
        capture_timestamp: datetime,
        request_identity: str,
        market_role: MarketRole = MarketRole.SIGNAL_TIME,
    ) -> tuple[ShadowSourceObservation, ...]:
        expected.validate()
        if not isinstance(payload, Mapping):
            raise SourceNormalizationError(
                SourceError.MALFORMED_RESPONSE, "The Odds API event must be a mapping"
            )
        home, away, league, kickoff, record_id = _event_identity(payload, expected)
        del home, away, league, kickoff
        bookmakers = payload.get("bookmakers")
        if not isinstance(bookmakers, Sequence) or isinstance(bookmakers, (str, bytes)):
            raise SourceNormalizationError(
                SourceError.MALFORMED_RESPONSE,
                "The Odds API event requires a bookmakers sequence",
            )
        observations: list[ShadowSourceObservation] = []
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, Mapping):
                raise SourceNormalizationError(
                    SourceError.MALFORMED_RESPONSE, "bookmaker entry must be a mapping"
                )
            bookmaker_identity = bookmaker.get("key") or bookmaker.get("title")
            if (
                not isinstance(bookmaker_identity, str)
                or not bookmaker_identity.strip()
            ):
                raise SourceNormalizationError(
                    SourceError.MISSING_PROVENANCE, "bookmaker identity is missing"
                )
            markets = bookmaker.get("markets")
            if not isinstance(markets, Sequence) or isinstance(markets, (str, bytes)):
                raise SourceNormalizationError(
                    SourceError.MALFORMED_RESPONSE,
                    "bookmaker markets must be a sequence",
                )
            h2h: Mapping[str, object] | None = None
            for market in markets:
                if not isinstance(market, Mapping):
                    raise SourceNormalizationError(
                        SourceError.MALFORMED_RESPONSE, "market entry must be a mapping"
                    )
                if market.get("key") == "h2h":
                    outcomes = market.get("outcomes")
                    if not isinstance(outcomes, Sequence) or isinstance(
                        outcomes, (str, bytes)
                    ):
                        raise SourceNormalizationError(
                            SourceError.MALFORMED_RESPONSE,
                            "h2h outcomes must be a sequence",
                        )
                    prices: dict[str, object] = {}
                    for outcome in outcomes:
                        if isinstance(outcome, Mapping) and "name" in outcome:
                            prices[str(outcome["name"])] = outcome.get("price")
                    h2h = prices
                    break
            if h2h is None:
                h2h = {}
            source_timestamp = _optional_timestamp(
                bookmaker.get("last_update", payload.get("last_update")),
                "last_update",
            )
            observations.append(
                _observation_from_prices(
                    provider=cls.provider_identity,
                    payload=payload,
                    expected=expected,
                    capture_timestamp=capture_timestamp,
                    request_identity=request_identity,
                    bookmaker_identity=str(bookmaker_identity).strip(),
                    prices=h2h,
                    source_timestamp=source_timestamp,
                    market_role=market_role,
                    adapter_version=cls.adapter_version,
                    record_id=record_id,
                )
            )
        if not observations:
            raise SourceNormalizationError(
                SourceError.PROVIDER_UNAVAILABLE,
                "The Odds API returned no bookmaker observations",
            )
        return tuple(observations)


class BetfairAdapter:
    """Normalize an enriched Betfair market payload without logging in."""

    provider_identity = "betfair"
    status = AdapterStatus.CANDIDATE_ONLY
    adapter_version = "top5-shadow-source-v1/betfair"

    @classmethod
    def normalize(
        cls,
        payload: Mapping[str, object],
        expected: ExpectedFixture,
        *,
        capture_timestamp: datetime,
        request_identity: str,
        market_role: MarketRole = MarketRole.SIGNAL_TIME,
    ) -> tuple[ShadowSourceObservation, ...]:
        expected.validate()
        if not isinstance(payload, Mapping):
            raise SourceNormalizationError(
                SourceError.MALFORMED_RESPONSE, "Betfair market must be a mapping"
            )
        if "market_id" not in payload:
            raise SourceNormalizationError(
                SourceError.MISSING_PROVENANCE, "Betfair market_id is required"
            )
        event = payload.get("event")
        event_data = dict(event) if isinstance(event, Mapping) else {}
        merged = dict(payload)
        for key in (
            "home_team",
            "away_team",
            "kickoff",
            "commence_time",
            "league",
            "sport_key",
        ):
            if key not in merged and key in event_data:
                merged[key] = event_data[key]
        merged.setdefault("id", payload.get("market_id"))
        try:
            source_timestamp = _parse_timestamp(
                payload.get(
                    "source_timestamp",
                    payload.get("last_update", event_data.get("updated_at")),
                ),
                "source_timestamp",
            )
        except ShadowProviderContractError as exc:
            raise SourceNormalizationError(
                SourceError.MISSING_TIMESTAMP, str(exc)
            ) from exc
        runners = payload.get("runners")
        if not isinstance(runners, Sequence) or isinstance(runners, (str, bytes)):
            raise SourceNormalizationError(
                SourceError.MALFORMED_RESPONSE, "Betfair runners must be a sequence"
            )
        prices: dict[str, object] = {}
        for runner in runners:
            if not isinstance(runner, Mapping):
                raise SourceNormalizationError(
                    SourceError.MALFORMED_RESPONSE, "Betfair runner must be a mapping"
                )
            label = (
                runner.get("selection")
                or runner.get("runnerName")
                or runner.get("name")
            )
            price = runner.get("price")
            if label is not None:
                prices[str(label)] = price
        if len(prices) < 3:
            raise SourceNormalizationError(
                SourceError.PARTIAL_MARKET,
                "Betfair MATCH_ODDS requires home, draw, and away runners",
            )
        observation = _observation_from_prices(
            provider=cls.provider_identity,
            payload=merged,
            expected=expected,
            capture_timestamp=capture_timestamp,
            request_identity=request_identity,
            bookmaker_identity="betfair_exchange",
            prices=prices,
            source_timestamp=source_timestamp,
            market_role=market_role,
            adapter_version=cls.adapter_version,
            record_id=str(payload["market_id"]),
        )
        return (observation,)


class OddsPortalAdapter:
    """Normalize only explicitly enriched OddsPortal rows.

    The current scraper's ``home/away/h/d/a`` row intentionally lacks league,
    kickoff, and source timestamp.  Passing that row therefore raises instead
    of inventing metadata.
    """

    provider_identity = "oddsportal"
    status = AdapterStatus.CANDIDATE_ONLY
    adapter_version = "top5-shadow-source-v1/oddsportal"

    @classmethod
    def normalize(
        cls,
        payload: Mapping[str, object],
        expected: ExpectedFixture,
        *,
        capture_timestamp: datetime,
        request_identity: str,
        market_role: MarketRole = MarketRole.SIGNAL_TIME,
    ) -> tuple[ShadowSourceObservation, ...]:
        expected.validate()
        if not isinstance(payload, Mapping):
            raise SourceNormalizationError(
                SourceError.MALFORMED_RESPONSE, "OddsPortal row must be a mapping"
            )
        required = ("league", "home", "away", "kickoff", "source_timestamp")
        if any(key not in payload for key in required):
            raise SourceNormalizationError(
                SourceError.MISSING_PROVENANCE,
                "OddsPortal row lacks explicit league, kickoff, or source timestamp",
            )
        source_timestamp = _parse_timestamp(
            payload.get("source_timestamp"), "source_timestamp"
        )
        event = {
            "league": payload.get("league"),
            "home_team": payload.get("home"),
            "away_team": payload.get("away"),
            "kickoff": payload.get("kickoff"),
            "id": payload.get("match_id", payload.get("fixture_id", "")),
        }
        if not event["id"]:
            raise SourceNormalizationError(
                SourceError.MISSING_PROVENANCE, "OddsPortal row requires match_id"
            )
        prices = {
            "home": payload.get("h", payload.get("home_odds")),
            "draw": payload.get("d", payload.get("draw_odds")),
            "away": payload.get("a", payload.get("away_odds")),
        }
        observation = _observation_from_prices(
            provider=cls.provider_identity,
            payload=event,
            expected=expected,
            capture_timestamp=capture_timestamp,
            request_identity=request_identity,
            bookmaker_identity=str(
                payload.get("bookmaker_identity") or "oddsportal_aggregate"
            ),
            prices=prices,
            source_timestamp=source_timestamp,
            market_role=market_role,
            adapter_version=cls.adapter_version,
            record_id=str(event["id"]),
        )
        return (observation,)


class FootballDataClosingAdapter:
    """Normalize only historical Football-Data closing rows."""

    provider_identity = "football_data"
    status = AdapterStatus.HISTORICAL_ONLY
    adapter_version = "top5-shadow-source-v1/football-data-closing"

    @classmethod
    def normalize(
        cls,
        row: Mapping[str, object],
        expected: ExpectedFixture,
        *,
        capture_timestamp: datetime,
        request_identity: str,
        source_timestamp: datetime | None = None,
    ) -> tuple[ShadowSourceObservation, ...]:
        expected.validate()
        if not isinstance(row, Mapping):
            raise SourceNormalizationError(
                SourceError.MALFORMED_RESPONSE, "Football-Data row must be a mapping"
            )
        kickoff = row.get("kickoff")
        if kickoff is None:
            raise SourceNormalizationError(
                SourceError.MISSING_TIMESTAMP,
                "Football-Data date-only rows cannot be promoted to signal-time fixtures",
            )
        close = {
            "home": row.get("ps_close_home", row.get("PSCH")),
            "draw": row.get("ps_close_draw", row.get("PSCD")),
            "away": row.get("ps_close_away", row.get("PSCA")),
        }
        if any(value is None for value in close.values()):
            raise SourceNormalizationError(
                SourceError.PARTIAL_MARKET,
                "Football-Data row lacks complete Pinnacle closing 1X2",
            )
        event = {
            "league": row.get("league", expected.league),
            "home_team": row.get("home_team", row.get("HomeTeam")),
            "away_team": row.get("away_team", row.get("AwayTeam")),
            "kickoff": kickoff,
            "id": row.get("match_id", f"{expected.league}:{expected.fixture_key}"),
        }
        observation = _observation_from_prices(
            provider=cls.provider_identity,
            payload=event,
            expected=expected,
            capture_timestamp=capture_timestamp,
            request_identity=request_identity,
            bookmaker_identity="pinnacle_closing",
            prices=close,
            source_timestamp=source_timestamp
            or _parse_timestamp(row.get("source_timestamp"), "source_timestamp"),
            market_role=MarketRole.CLOSING_BENCHMARK,
            adapter_version=cls.adapter_version,
            record_id=str(event["id"]),
        )
        return (observation,)


@dataclass(frozen=True)
class EvidenceIdentity:
    """Explicit identity supplied by the evidence producer; never inferred here."""

    evidence_id: str
    artifact_id: str
    artifact_sha: str
    source_sha: str
    candidate_id: str
    model_identity: str
    research_sha: str = FROZEN_RESEARCH_SHA

    def validate(self) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("artifact_id", self.artifact_id),
            ("candidate_id", self.candidate_id),
            ("model_identity", self.model_identity),
        ):
            _required_text(value, name)
        _sha(self.artifact_sha, "artifact_sha")
        _sha(self.source_sha, "source_sha")
        if _sha(self.research_sha, "research_sha") != FROZEN_RESEARCH_SHA.lower():
            raise ShadowProviderContractError(
                "provider evidence references an unfrozen Research SHA"
            )


def to_shadow_observation_evidence(
    observation: ShadowSourceObservation,
    expected: ExpectedFixture,
    identity: EvidenceIdentity,
    *,
    policy: SourceQualityPolicy | None = None,
) -> ShadowObservationEvidence:
    """Bridge one accepted/rejected source observation into v1 evidence."""

    identity.validate()
    if observation.market_role is MarketRole.CLOSING_BENCHMARK:
        raise SourceNormalizationError(
            SourceError.CLOSING_LEAKAGE,
            "closing benchmark cannot become prediction observation evidence",
        )
    report = validate_source_observation(observation, expected, policy)
    provenance = EvidenceProvenance(
        evidence_id=identity.evidence_id,
        artifact_id=identity.artifact_id,
        artifact_sha=identity.artifact_sha,
        source_sha=identity.source_sha,
        research_sha=identity.research_sha,
        league_code=observation.league,
        candidate_id=identity.candidate_id,
        model_identity=identity.model_identity,
        generated_at=observation.capture_timestamp,
        fixture_key=observation.fixture_key,
    )
    evidence = ShadowObservationEvidence(
        provenance=provenance,
        discovered=True,
        eligible=report.accepted,
        valid_odds=report.accepted,
        prediction_id=None,
        rejected=not report.accepted,
        rejected_reason=";".join(error.value for error in report.errors)
        if report.errors
        else None,
        provider_covered=report.accepted,
        stale=SourceError.STALE_ODDS in report.errors,
        fallback_used=False,
        error=not report.accepted,
        duplicate_suppressed=SourceError.DUPLICATE_SNAPSHOT in report.errors,
        no_bet=True,
        publication_enabled=False,
    )
    evidence.validate()
    return evidence


def build_shadow_evidence_bundle(
    observations: Sequence[ShadowSourceObservation],
    expected_by_fixture: Mapping[str, ExpectedFixture],
    identity_by_fixture: Mapping[str, EvidenceIdentity],
    *,
    window_start: datetime,
    window_end: datetime,
    policy: SourceQualityPolicy | None = None,
) -> ShadowEvidenceBundle:
    """Build a read-only v1 bundle; caller supplies all evidence identities."""

    start = _utc(window_start, "window_start")
    end = _utc(window_end, "window_end")
    if start >= end:
        raise ShadowProviderContractError("evidence window must be ordered")
    records = []
    for observation in observations:
        expected = expected_by_fixture.get(observation.fixture_key)
        identity = identity_by_fixture.get(observation.fixture_key)
        if expected is None or identity is None:
            raise ShadowProviderContractError(
                "every observation requires explicit expected fixture and evidence identity"
            )
        records.append(
            to_shadow_observation_evidence(
                observation, expected, identity, policy=policy
            )
        )
    bundle = ShadowEvidenceBundle(
        evidence_window_start=start,
        evidence_window_end=end,
        safety=SafetyAssertions(
            no_bet=True,
            publication_enabled=False,
            real_bet_created=False,
            ledger_mutated=False,
            sealed_data_accessed=False,
            research_mutated=False,
            production_activation=False,
        ),
        observations=tuple(records),
    )
    bundle.validate()
    return bundle


def _provider_outcome(error: SourceError) -> ValidationProviderOutcome:
    mapping = {
        SourceError.NONE: ValidationProviderOutcome.SUCCESS,
        SourceError.TIMEOUT: ValidationProviderOutcome.TIMEOUT,
        SourceError.HTTP_403: ValidationProviderOutcome.HTTP_403,
        SourceError.HTTP_429: ValidationProviderOutcome.HTTP_429,
        SourceError.STALE_ODDS: ValidationProviderOutcome.STALE,
        SourceError.WRONG_FIXTURE: ValidationProviderOutcome.WRONG_FIXTURE,
        SourceError.WRONG_MARKET: ValidationProviderOutcome.WRONG_MARKET,
        SourceError.PARTIAL_MARKET: ValidationProviderOutcome.PARTIAL_RESPONSE,
        SourceError.MISSING_DRAW: ValidationProviderOutcome.PARTIAL_RESPONSE,
    }
    return mapping.get(error, ValidationProviderOutcome.MALFORMED_RESPONSE)


def to_shadow_provider_evidence(
    observation: ShadowSourceObservation,
    expected: ExpectedFixture,
    identity: EvidenceIdentity,
    *,
    policy: SourceQualityPolicy | None = None,
) -> ProviderEvidence:
    """Bridge one provider result into the existing v1 provider evidence type."""

    identity.validate()
    report = validate_source_observation(observation, expected, policy)
    provenance = EvidenceProvenance(
        evidence_id=identity.evidence_id,
        artifact_id=identity.artifact_id,
        artifact_sha=identity.artifact_sha,
        source_sha=identity.source_sha,
        research_sha=identity.research_sha,
        league_code=observation.league,
        candidate_id=identity.candidate_id,
        model_identity=identity.model_identity,
        generated_at=observation.capture_timestamp,
        fixture_key=observation.fixture_key,
    )
    max_age = float((policy or SourceQualityPolicy()).maximum_odds_age_seconds)
    error = report.errors[0] if report.errors else SourceError.NONE
    provider_evidence = ProviderEvidence(
        provenance=provenance,
        provider_name=observation.provider_identity,
        outcome=_provider_outcome(error),
        requested_fixture_count=1,
        covered_fixture_count=1 if report.accepted else 0,
        availability=report.accepted,
        latency_ms=observation.request_latency_ms,
        odds_age_seconds=observation.odds_age_seconds,
        maximum_odds_age_seconds=max_age,
        bulk_requests=1,
        fallback_requests=0,
        retry_count=0,
        bulk_reused=False,
        stale_rejections=int(SourceError.STALE_ODDS in report.errors),
        wrong_market_rejections=int(SourceError.WRONG_MARKET in report.errors),
        wrong_fixture_rejections=int(SourceError.WRONG_FIXTURE in report.errors),
    )
    provider_evidence.validate()
    return provider_evidence


@dataclass(frozen=True)
class OddsApiQuotaSnapshot:
    authenticated: bool
    quota_used: int
    quota_remaining: int

    def validate(self) -> None:
        if not isinstance(self.authenticated, bool):
            raise ShadowProviderContractError("authenticated must be boolean")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (self.quota_used, self.quota_remaining)
        ):
            raise ShadowProviderContractError(
                "Odds API quota values must be non-negative integers"
            )


CURRENT_EXHAUSTED_ODDS_API = OddsApiQuotaSnapshot(
    authenticated=True,
    quota_used=500,
    quota_remaining=0,
)


@dataclass(frozen=True)
class OddsApiPreflight:
    request_kind: OddsApiRequestKind
    allowed: bool
    network_may_execute: bool
    estimated_cost_units: float
    reason: str
    retry_count: int
    fallback_fanout_allowed: bool


def authorize_odds_api_request(
    quota: OddsApiQuotaSnapshot,
    request_kind: OddsApiRequestKind,
    *,
    estimated_cost_units: float = 1.0,
) -> OddsApiPreflight:
    """Preflight a request; a rejected paid call must not reach the network."""

    quota.validate()
    kind = OddsApiRequestKind(request_kind)
    cost = _number(estimated_cost_units, "estimated_cost_units", minimum=0.0)
    if kind is OddsApiRequestKind.AUTHENTICATION:
        if not quota.authenticated:
            return OddsApiPreflight(
                kind, False, False, 0.0, "authentication_missing", 0, False
            )
        return OddsApiPreflight(
            kind, True, True, 0.0, "zero_cost_authentication_endpoint", 0, False
        )
    if not quota.authenticated:
        return OddsApiPreflight(
            kind, False, False, cost, "authentication_missing", 0, False
        )
    if quota.quota_remaining < cost:
        return OddsApiPreflight(kind, False, False, cost, "quota_exhausted", 0, False)
    return OddsApiPreflight(kind, True, True, cost, "quota_available", 0, False)


T = TypeVar("T")


@dataclass(frozen=True)
class GuardedCallResult:
    preflight: OddsApiPreflight
    network_called: bool
    result: Any = None
    error: str | None = None


def guarded_odds_api_call(
    quota: OddsApiQuotaSnapshot,
    request_kind: OddsApiRequestKind,
    call: Callable[[], T],
    *,
    estimated_cost_units: float = 1.0,
) -> GuardedCallResult:
    """Execute an injected callable only after a successful preflight."""

    preflight = authorize_odds_api_request(
        quota,
        request_kind,
        estimated_cost_units=estimated_cost_units,
    )
    if not preflight.allowed:
        return GuardedCallResult(
            preflight, network_called=False, error=preflight.reason
        )
    try:
        return GuardedCallResult(preflight, network_called=True, result=call())
    except Exception as exc:  # noqa: BLE001 - injected call result is evidence, not a retry trigger
        return GuardedCallResult(
            preflight, network_called=True, error=f"{type(exc).__name__}: {exc}"
        )


@dataclass(frozen=True)
class ProviderPlanningRequest:
    league: str
    window_start: datetime
    window_end: datetime
    required_market: str
    freshness_requirement_seconds: int
    allowed_sources: tuple[str, ...]
    fixture_count: int
    remaining_quota: Mapping[str, int | OddsApiQuotaSnapshot] = field(
        default_factory=dict
    )
    credentials_available: Mapping[str, bool] = field(default_factory=dict)
    cached_fixture_count: Mapping[str, int] = field(default_factory=dict)
    cached_max_age_seconds: Mapping[str, float] = field(default_factory=dict)
    cost_units_per_request: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "league", normalize_league(self.league))
        object.__setattr__(
            self, "window_start", _utc(self.window_start, "window_start")
        )
        object.__setattr__(self, "window_end", _utc(self.window_end, "window_end"))

    def validate(self) -> None:
        if self.window_start >= self.window_end:
            raise ShadowProviderContractError(
                "provider planning window must be ordered"
            )
        if self.required_market != "h2h_1x2":
            raise ShadowProviderContractError(
                "Top-5 provider planning requires h2h_1x2"
            )
        if self.freshness_requirement_seconds <= 0:
            raise ShadowProviderContractError("freshness requirement must be positive")
        if (
            not isinstance(self.fixture_count, int)
            or isinstance(self.fixture_count, bool)
            or self.fixture_count < 0
        ):
            raise ShadowProviderContractError(
                "fixture_count must be a non-negative integer"
            )
        if not self.allowed_sources or len(set(self.allowed_sources)) != len(
            self.allowed_sources
        ):
            raise ShadowProviderContractError(
                "allowed_sources must be non-empty and unique"
            )
        if any(
            not isinstance(source, str) or not source.strip()
            for source in self.allowed_sources
        ):
            raise ShadowProviderContractError("allowed_sources contains a blank source")
        for source, quota in self.remaining_quota.items():
            if isinstance(quota, OddsApiQuotaSnapshot):
                quota.validate()
            elif not isinstance(quota, int) or isinstance(quota, bool) or quota < 0:
                raise ShadowProviderContractError(
                    f"remaining quota for {source} is invalid"
                )
        for name, values in (("cached_fixture_count", self.cached_fixture_count),):
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in values.values()
            ):
                raise ShadowProviderContractError(f"{name} contains an invalid count")
        for value in self.cached_max_age_seconds.values():
            _number(value, "cached_max_age_seconds", minimum=0.0)
        for value in self.cost_units_per_request.values():
            _number(value, "cost_units_per_request", minimum=0.0)


@dataclass(frozen=True)
class ProviderPathPlan:
    source: str
    status: CapabilityStatus
    operationally_possible: bool
    estimated_requests: int
    estimated_provider_cost_units: float
    expected_fixture_coverage: float
    fallback_capability: str
    reason: str
    signal_time_usable: bool
    authority_approved: bool = False

    def validate(self) -> None:
        _required_text(self.source, "source")
        CapabilityStatus(self.status)
        if self.authority_approved:
            raise ShadowProviderContractError(
                "provider planning cannot approve authority"
            )
        if (
            self.estimated_requests < 0
            or not isfinite(float(self.estimated_provider_cost_units))
            or self.estimated_provider_cost_units < 0
        ):
            raise ShadowProviderContractError(
                "provider plan request/cost values are invalid"
            )
        if not 0.0 <= self.expected_fixture_coverage <= 1.0:
            raise ShadowProviderContractError(
                "expected fixture coverage must be in [0, 1]"
            )


@dataclass(frozen=True)
class ProviderPlan:
    paths: tuple[ProviderPathPlan, ...]
    selected_source: str | None = None
    automatic_fallback_enabled: bool = False
    automatic_fanout_requests: int = 0

    def validate(self) -> None:
        if self.selected_source is not None:
            raise ShadowProviderContractError(
                "provider planner must not select an authority"
            )
        if self.automatic_fallback_enabled or self.automatic_fanout_requests != 0:
            raise ShadowProviderContractError(
                "provider planner cannot auto-fan-out after exhaustion"
            )
        seen: set[str] = set()
        for path in self.paths:
            path.validate()
            if path.source in seen:
                raise ShadowProviderContractError(
                    "provider plan contains duplicate sources"
                )
            seen.add(path.source)

    @property
    def quota_independent_paths(self) -> tuple[ProviderPathPlan, ...]:
        return tuple(
            path
            for path in self.paths
            if path.source != "the_odds_api" and path.operationally_possible
        )


_PLANNER_METADATA = MappingProxyType(
    {
        "the_odds_api": (
            CapabilityStatus.IMPLEMENTED_CANDIDATE,
            True,
            1,
            "independent alternative paths are listed but never auto-triggered",
        ),
        "betfair": (
            CapabilityStatus.CANDIDATE_ONLY,
            True,
            2,
            "independent of The Odds API; requires credentials and enriched identity",
        ),
        "oddsportal": (
            CapabilityStatus.CANDIDATE_ONLY,
            True,
            1,
            "independent of The Odds API; candidate-only day request",
        ),
        "cache": (
            CapabilityStatus.CACHE_ONLY,
            True,
            0,
            "not an independent provider; only usable with fresh retained metadata",
        ),
        "football_data": (
            CapabilityStatus.HISTORICAL_ONLY,
            False,
            0,
            "historical/closing benchmark only; not signal-time",
        ),
        "espn": (
            CapabilityStatus.RESULT_ONLY,
            False,
            0,
            "results/fixture scoreboard only; no 1X2 odds",
        ),
        "pinnacle": (
            CapabilityStatus.REJECTED,
            False,
            0,
            "current implementation is Bundesliga-2-only",
        ),
        "websearch": (
            CapabilityStatus.REJECTED,
            False,
            0,
            "no reliable source timestamp or bookmaker identity",
        ),
    }
)


def _quota_remaining(request: ProviderPlanningRequest, source: str) -> int | None:
    value = request.remaining_quota.get(source)
    if isinstance(value, OddsApiQuotaSnapshot):
        return value.quota_remaining
    return value


def plan_provider_paths(request: ProviderPlanningRequest) -> ProviderPlan:
    """Plan all possible paths without selecting one or dispatching any call."""

    request.validate()
    paths: list[ProviderPathPlan] = []
    for source in sorted(request.allowed_sources):
        metadata = _PLANNER_METADATA.get(source)
        if metadata is None:
            paths.append(
                ProviderPathPlan(
                    source=source,
                    status=CapabilityStatus.UNSUPPORTED,
                    operationally_possible=False,
                    estimated_requests=0,
                    estimated_provider_cost_units=0.0,
                    expected_fixture_coverage=0.0,
                    fallback_capability="none",
                    reason="source is not implemented in SportsBrain",
                    signal_time_usable=False,
                )
            )
            continue
        status, signal_capable, request_count, fallback = metadata
        unit_cost = float(request.cost_units_per_request.get(source, 1.0))
        estimated_cost = request_count * unit_cost
        coverage = 1.0 if request.fixture_count else 0.0
        possible = True
        reason = "candidate path is operationally possible; authority remains unset"
        if not signal_capable:
            possible = False
            reason = fallback
        elif source == "the_odds_api":
            quota = _quota_remaining(request, source)
            credential = request.credentials_available.get(source)
            if quota is None or credential is not True:
                possible = False
                reason = (
                    "authentication and remaining quota must be explicitly supplied"
                )
            elif quota < estimated_cost:
                possible = False
                reason = (
                    "quota_exhausted: paid request rejected before network execution"
                )
        elif (
            source == "betfair"
            and request.credentials_available.get(source) is not True
        ):
            possible = False
            reason = "Betfair credentials are not declared"
        elif source == "cache":
            cached = request.cached_fixture_count.get(source, 0)
            cached_age = request.cached_max_age_seconds.get(source)
            if (
                cached <= 0
                or cached_age is None
                or cached_age > request.freshness_requirement_seconds
            ):
                possible = False
                reason = "no cache snapshot satisfies the freshness requirement"
            else:
                coverage = (
                    min(1.0, cached / request.fixture_count)
                    if request.fixture_count
                    else 0.0
                )
        paths.append(
            ProviderPathPlan(
                source=source,
                status=status,
                operationally_possible=possible,
                estimated_requests=request_count,
                estimated_provider_cost_units=estimated_cost,
                expected_fixture_coverage=coverage if possible else 0.0,
                fallback_capability=fallback,
                reason=reason,
                signal_time_usable=signal_capable and possible,
            )
        )
    plan = ProviderPlan(tuple(paths))
    plan.validate()
    return plan


def canonical_observation_digest(observation: ShadowSourceObservation) -> str:
    """Stable digest for injected observation identity, not an authority choice."""

    payload = json.dumps(
        observation.as_payload(), sort_keys=True, separators=(",", ":"), default=str
    )
    return sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CURRENT_EXHAUSTED_ODDS_API",
    "TOP5_LEAGUE_CODES",
    "TOP5_LEAGUE_NAMES",
    "TOP5_SPORT_KEYS",
    "AdapterDescriptor",
    "AdapterStatus",
    "BetfairAdapter",
    "CapabilityKind",
    "CapabilityRecord",
    "CapabilityStatus",
    "CompletenessState",
    "EvidenceIdentity",
    "ExpectedFixture",
    "FootballDataClosingAdapter",
    "GuardedCallResult",
    "MarketRole",
    "OddsApiPreflight",
    "OddsApiQuotaSnapshot",
    "OddsApiRequestKind",
    "OddsPortalAdapter",
    "ProviderPathPlan",
    "ProviderPlan",
    "ProviderPlanningRequest",
    "ShadowProviderContractError",
    "ShadowSourceObservation",
    "SourceError",
    "SourceNormalizationError",
    "SourceProvenance",
    "SourceQualityPolicy",
    "SourceQualityReport",
    "TheOddsAPIAdapter",
    "adapter_status_for",
    "authorize_odds_api_request",
    "build_shadow_evidence_bundle",
    "canonical_observation_digest",
    "capability_matrix",
    "guarded_odds_api_call",
    "make_fixture_key",
    "normalize_league",
    "normalize_team_name",
    "plan_provider_paths",
    "provider_inventory",
    "supported_candidate_adapters",
    "to_shadow_observation_evidence",
    "to_shadow_provider_evidence",
    "validate_observation_batch",
    "validate_source_observation",
]
