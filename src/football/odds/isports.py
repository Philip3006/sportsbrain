"""Shared iSports football catalog, schedule, and 1X2 odds adapter.

The adapter normalizes provider evidence for Top-5 and Champions League shadow
and qualification use.  It is not registered in the production odds merger,
does not select an authoritative provider, and performs no side effects beyond
the explicitly invoked HTTP operation.
"""

from __future__ import annotations

import os
import re
import statistics
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from math import isfinite
from typing import TYPE_CHECKING, Protocol

import requests

from src.betting.odds_utils import remove_margin_shin
from src.football.odds.base import sanity_1x2
from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    _utc,
)
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
    digest_record,
)
from src.football.top5_shadow_provider_redundancy import (
    make_fixture_key,
    normalize_team_name,
)

if TYPE_CHECKING:
    from src.football.champions_league_model import ChampionsLeagueFixture

ISPORTS_PROVIDER_IDENTITY = "isports_api"
ISPORTS_BASE_URL = "https://api.isportsapi.com"
ISPORTS_ADAPTER_VERSION = "isports-football-shared-v1"
ISPORTS_TOP5_LEAGUES = ("EPL", "BL1", "LL", "SA", "L1")
ISPORTS_COMPETITIONS = (*ISPORTS_TOP5_LEAGUES, "UCL")
ISPORTS_ENDPOINTS = {
    "catalog": "/sport/football/league/basic",
    "schedule": "/sport/football/schedule/basic",
    "main_odds": "/sport/football/odds/main",
    "european_odds": "/sport/football/odds/european/all",
}
ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS = {
    ISPORTS_ENDPOINTS["catalog"]: 1800,
    ISPORTS_ENDPOINTS["schedule"]: 60,
    ISPORTS_ENDPOINTS["main_odds"]: 10,
    ISPORTS_ENDPOINTS["european_odds"]: 60,
}
_ENDPOINT_PARAMETERS = {
    ISPORTS_ENDPOINTS["catalog"]: frozenset(),
    ISPORTS_ENDPOINTS["schedule"]: frozenset({"leagueId"}),
    ISPORTS_ENDPOINTS["main_odds"]: frozenset({"matchId"}),
    ISPORTS_ENDPOINTS["european_odds"]: frozenset({"matchId"}),
}
MAX_SOURCE_AGE_SECONDS = 900
MAX_BULK_MATCH_IDS = 100
_SAFE_HEADERS = frozenset(
    {
        "x-rate-limit",
        "x-ratelimit-limit",
        "x-rate-limit-remaining",
        "x-ratelimit-remaining",
        "x-rate-limit-reset",
        "x-ratelimit-reset",
        "x-quota-limit",
        "x-quota-remaining",
        "x-requests-remaining",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
        "retry-after",
    }
)


class ISportsContractError(ProductionContractError):
    """Provider payload, identity, or timing cannot be accepted safely."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ISportsContractError(f"{name} is required")
    return value.strip()


def _provider_id(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ISportsContractError(f"{name} is required")
    normalized = str(value).strip()
    if not normalized:
        raise ISportsContractError(f"{name} is required")
    return normalized


def _canonical_name(value: object) -> str:
    name = _text(value, "competition name")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_name.casefold()).split())


_COMPETITION_NAMES: Mapping[str, frozenset[str]] = {
    "EPL": frozenset(
        {"premier league", "english premier league", "england premier league"}
    ),
    "BL1": frozenset({"bundesliga", "german bundesliga", "germany bundesliga"}),
    "LL": frozenset({"la liga", "spain la liga", "spanish la liga"}),
    "SA": frozenset({"serie a", "italy serie a", "italian serie a"}),
    "L1": frozenset({"ligue 1", "france ligue 1", "french ligue 1"}),
    "UCL": frozenset({"uefa champions league", "champions league"}),
}
_COMPETITION_SHORT_NAMES = {
    "eng pr": "EPL",
    "ger d1": "BL1",
    "spa d1": "LL",
    "ita d1": "SA",
    "fra d1": "L1",
    "uefa cl": "UCL",
}


@dataclass(frozen=True)
class ISportsCompetition:
    league_code: str
    provider_league_id: str
    provider_name: str
    short_name: str
    competition_type: int

    def validate(self) -> None:
        if self.league_code not in ISPORTS_COMPETITIONS:
            raise ISportsContractError("unsupported canonical competition")
        _text(self.provider_league_id, "provider league ID")
        if (
            _canonical_name(self.provider_name)
            not in _COMPETITION_NAMES[self.league_code]
        ):
            raise ISportsContractError("provider competition name mismatch")
        if not isinstance(self.competition_type, int) or isinstance(
            self.competition_type, bool
        ):
            raise ISportsContractError("provider competition type is invalid")
        expected_type = 2 if self.league_code == "UCL" else 1
        if self.competition_type != expected_type:
            raise ISportsContractError("provider competition type mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "league_code": self.league_code,
            "provider_league_id": self.provider_league_id,
            "provider_name": self.provider_name,
            "short_name": self.short_name,
            "competition_type": self.competition_type,
        }


def _response_rows(
    payload: object, *, field: str = "data"
) -> list[Mapping[str, object]]:
    """Read documented JSON list/envelope forms without accepting error payloads."""

    if isinstance(payload, Mapping):
        code = payload.get("code")
        if code is not None and str(code) not in {"0", "200", "success", "SUCCESS"}:
            raise ISportsContractError("provider returned a non-success response code")
        rows = payload.get(field)
        if rows is None and field == "data" and isinstance(payload.get("result"), list):
            rows = payload.get("result")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ISportsContractError(f"provider response {field} must be a list")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ISportsContractError(f"provider response {field} contains malformed rows")
    return rows  # type: ignore[return-value]


def resolve_competitions(payload: object) -> dict[str, ISportsCompetition]:
    """Resolve exactly the six explicitly requested competitions, never by guessed ID."""

    resolved: dict[str, ISportsCompetition] = {}
    ids: set[str] = set()
    for row in _response_rows(payload):
        name = row.get("name")
        short_name = row.get("shortName", "")
        candidates = {
            code
            for code, accepted_names in _COMPETITION_NAMES.items()
            if _canonical_name(name) in accepted_names
            or (
                isinstance(short_name, str)
                and _canonical_name(short_name) in accepted_names
            )
        }
        short_code = (
            _COMPETITION_SHORT_NAMES.get(_canonical_name(short_name))
            if isinstance(short_name, str) and short_name.strip()
            else None
        )
        if short_code is not None:
            candidates.add(short_code)
        if not candidates:
            continue
        if len(candidates) != 1:
            raise ISportsContractError("ambiguous provider competition identity")
        code = candidates.pop()
        provider_id = _provider_id(row.get("leagueId"), "provider league ID")
        if code in resolved or provider_id in ids:
            raise ISportsContractError("duplicate provider competition identity")
        raw_type = row.get("type")
        if not isinstance(raw_type, int) or isinstance(raw_type, bool):
            raise ISportsContractError("provider competition type is missing")
        competition = ISportsCompetition(
            league_code=code,
            provider_league_id=provider_id,
            provider_name=_text(name, "provider competition name"),
            short_name=str(short_name).strip(),
            competition_type=raw_type,
        )
        competition.validate()
        ids.add(provider_id)
        resolved[code] = competition
    if set(resolved) != set(ISPORTS_COMPETITIONS):
        missing = sorted(set(ISPORTS_COMPETITIONS) - set(resolved))
        raise ISportsContractError(
            "provider catalog is missing required competitions: " + ",".join(missing)
        )
    return {code: resolved[code] for code in ISPORTS_COMPETITIONS}


@dataclass(frozen=True)
class ISportsFixture:
    league_code: str
    provider_league_id: str
    provider_league_name: str
    provider_match_id: str
    kickoff_utc: datetime
    home_team_id: str
    away_team_id: str
    home_team: str
    away_team: str
    neutral: bool
    status: int
    fixture: Fixture

    def validate(self) -> None:
        if self.league_code not in ISPORTS_COMPETITIONS:
            raise ISportsContractError("fixture competition is unsupported")
        _text(self.provider_match_id, "provider match ID")
        _text(self.provider_league_id, "provider league ID")
        _text(self.home_team_id, "home team ID")
        _text(self.away_team_id, "away team ID")
        if self.home_team_id == self.away_team_id:
            raise ISportsContractError("fixture participant IDs are not distinct")
        _text(self.home_team, "home team")
        _text(self.away_team, "away team")
        if self.home_team.casefold() == self.away_team.casefold():
            raise ISportsContractError("fixture participants are not distinct")
        if not isinstance(self.neutral, bool):
            raise ISportsContractError("neutral flag must be boolean")
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise ISportsContractError("fixture status is invalid")
        if self.fixture.league_code != self.league_code:
            raise ISportsContractError("canonical fixture league mismatch")
        if (
            self.fixture.home_team != self.home_team
            or self.fixture.away_team != self.away_team
        ):
            raise ISportsContractError("canonical fixture participants mismatch")
        if self.fixture.kickoff != _utc(self.kickoff_utc, "kickoff"):
            raise ISportsContractError("canonical fixture kickoff mismatch")
        self.fixture.validate()

    @property
    def prematch_eligible(self) -> bool:
        """Convenience for live use; deterministic callers should pass ``now``."""

        return self.prematch_eligible_at(datetime.now(timezone.utc))

    def prematch_eligible_at(self, now: datetime) -> bool:
        return self.status == 0 and self.kickoff_utc > _utc(now, "eligibility time")

    def as_champions_league_fixture(self) -> ChampionsLeagueFixture:
        """Project a verified UCL schedule row, without approving a CL model."""

        if self.league_code != "UCL":
            raise ISportsContractError(
                "only a UCL fixture can be projected to the CL contract"
            )
        from src.football.champions_league_model import ChampionsLeagueFixture

        result = ChampionsLeagueFixture(
            fixture_key=f"UCL:{self.provider_match_id}",
            provider_event_id=self.provider_match_id,
            home_team_id=self.home_team_id,
            away_team_id=self.away_team_id,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=self.kickoff_utc,
            neutral_ground=self.neutral,
        )
        result.validate()
        return result

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "league_code": self.league_code,
            "provider_league_id": self.provider_league_id,
            "provider_league_name": self.provider_league_name,
            "provider_match_id": self.provider_match_id,
            "fixture_key": self.fixture.fixture_key,
            "kickoff_utc": self.kickoff_utc.isoformat(),
            "home_team_id": self.home_team_id,
            "away_team_id": self.away_team_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "neutral": self.neutral,
            "status": self.status,
        }


def _epoch_utc(value: object, field: str) -> datetime:
    if isinstance(value, bool):
        raise ISportsContractError(f"{field} must be a Unix timestamp")
    try:
        stamp = int(value)  # type: ignore[arg-type]
        if str(value).strip() != str(stamp):
            raise ValueError
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ISportsContractError(f"{field} must be a Unix timestamp") from exc


def normalize_schedule(
    payload: object,
    competition: ISportsCompetition,
) -> tuple[ISportsFixture, ...]:
    competition.validate()
    fixtures: list[ISportsFixture] = []
    match_ids: set[str] = set()
    fixture_keys: set[str] = set()
    for row in _response_rows(payload):
        provider_league_id = _provider_id(row.get("leagueId"), "schedule league ID")
        if provider_league_id != competition.provider_league_id:
            raise ISportsContractError("schedule league ID differs from catalog")
        provider_name = _text(row.get("leagueName"), "schedule league name")
        if (
            _canonical_name(provider_name)
            not in _COMPETITION_NAMES[competition.league_code]
        ):
            raise ISportsContractError("schedule league name differs from catalog")
        provider_match_id = _provider_id(row.get("matchId"), "provider match ID")
        if provider_match_id in match_ids:
            raise ISportsContractError("duplicate provider match ID")
        kickoff = _epoch_utc(row.get("matchTime"), "matchTime")
        home_name = _text(row.get("homeName"), "homeName")
        away_name = _text(row.get("awayName"), "awayName")
        if competition.league_code == "UCL":
            fixture_key = f"UCL:{provider_match_id}"
        else:
            fixture_key = make_fixture_key(
                competition.league_code, home_name, away_name, kickoff
            )
        if fixture_key in fixture_keys:
            raise ISportsContractError("duplicate canonical fixture")
        status = row.get("status")
        if not isinstance(status, int) or isinstance(status, bool):
            raise ISportsContractError("schedule status is invalid")
        neutral = row.get("neutral")
        if not isinstance(neutral, bool):
            raise ISportsContractError("schedule neutral flag is invalid")
        fixture = ISportsFixture(
            league_code=competition.league_code,
            provider_league_id=provider_league_id,
            provider_league_name=provider_name,
            provider_match_id=provider_match_id,
            kickoff_utc=kickoff,
            home_team_id=_provider_id(row.get("homeId"), "homeId"),
            away_team_id=_provider_id(row.get("awayId"), "awayId"),
            home_team=home_name,
            away_team=away_name,
            neutral=neutral,
            status=status,
            fixture=Fixture(
                fixture_key, competition.league_code, home_name, away_name, kickoff
            ),
        )
        fixture.validate()
        fixtures.append(fixture)
        match_ids.add(provider_match_id)
        fixture_keys.add(fixture_key)
    return tuple(
        sorted(fixtures, key=lambda item: (item.kickoff_utc, item.provider_match_id))
    )


@dataclass(frozen=True)
class ISportsHttpResponse:
    endpoint_path: str
    status_code: int | None
    payload: object | None
    safe_headers: Mapping[str, str]
    request_ordinal: int
    started_at: datetime
    completed_at: datetime
    transport_error_class: str | None = None
    request_parameters: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if self.endpoint_path not in ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS:
            raise ISportsContractError("unapproved iSports endpoint")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ISportsContractError("HTTP status is invalid")
        if self.request_ordinal < 1:
            raise ISportsContractError("request ordinal must be positive")
        start = _utc(self.started_at, "request start")
        end = _utc(self.completed_at, "request completion")
        if end < start:
            raise ISportsContractError("request timestamps are reversed")
        if set(self.safe_headers) - _SAFE_HEADERS:
            raise ISportsContractError("response contains an unsafe persisted header")
        if any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or name.casefold() == "api_key"
            for name, value in self.request_parameters.items()
        ):
            raise ISportsContractError("request provenance contains unsafe parameters")
        if self.transport_error_class and self.transport_error_class not in {
            "Timeout",
            "ConnectionError",
            "SSLError",
            "RequestException",
        }:
            raise ISportsContractError("transport error class is not normalized")

    @property
    def response_digest(self) -> str:
        if self.payload is None:
            material: object = {"status_code": self.status_code, "payload": None}
        else:
            material = self.payload
        return digest_record(material)

    def safe_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "operation": next(
                key
                for key, value in ISPORTS_ENDPOINTS.items()
                if value == self.endpoint_path
            ),
            "endpoint_path": self.endpoint_path,
            "http_status": self.status_code,
            "request_parameters": dict(sorted(self.request_parameters.items())),
            "request_shape_digest": digest_record(
                {
                    "provider": ISPORTS_PROVIDER_IDENTITY,
                    "operation": next(
                        key
                        for key, value in ISPORTS_ENDPOINTS.items()
                        if value == self.endpoint_path
                    ),
                    "endpoint_path": self.endpoint_path,
                    "parameters": dict(sorted(self.request_parameters.items())),
                }
            ),
            "request_ordinal": self.request_ordinal,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "safe_rate_quota_headers": dict(sorted(self.safe_headers.items())),
            "response_digest": self.response_digest,
            "transport_error_class": self.transport_error_class,
        }


class ISportsHttpTransport(Protocol):
    def __call__(
        self,
        endpoint_path: str,
        params: Mapping[str, str],
        api_key: str,
        timeout_seconds: float,
    ) -> tuple[
        int | None, object | None, Mapping[str, str], datetime, datetime, str | None
    ]: ...


def isports_requests_transport(
    endpoint_path: str,
    params: Mapping[str, str],
    api_key: str,
    timeout_seconds: float,
) -> tuple[
    int | None, object | None, Mapping[str, str], datetime, datetime, str | None
]:
    """One HTTPS request; auth query param remains transient and redirects are off."""

    started = datetime.now(timezone.utc)
    try:
        response = requests.get(
            ISPORTS_BASE_URL + endpoint_path,
            params={**params, "api_key": api_key},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.Timeout:
        ended = datetime.now(timezone.utc)
        return None, None, {}, started, ended, "Timeout"
    except requests.ConnectionError:
        ended = datetime.now(timezone.utc)
        return None, None, {}, started, ended, "ConnectionError"
    except requests.RequestException:
        ended = datetime.now(timezone.utc)
        return None, None, {}, started, ended, "RequestException"
    ended = datetime.now(timezone.utc)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    headers = {
        str(key).casefold(): str(value).strip()
        for key, value in response.headers.items()
        if str(key).casefold() in _SAFE_HEADERS
    }
    return response.status_code, payload, headers, started, ended, None


@dataclass(frozen=True)
class ISportsBookmakerQuote:
    source: str
    company_id: str
    company_name: str
    match_id: str
    home_decimal: float
    draw_decimal: float
    away_decimal: float
    change_time: datetime
    opening_home_decimal: float | None = None
    opening_draw_decimal: float | None = None
    opening_away_decimal: float | None = None

    @property
    def bookmaker_identity(self) -> str:
        return f"isports_api:{self.source}:{self.company_id}:{self.company_name}"

    def validate(self) -> None:
        if self.source not in {"main", "european"}:
            raise ISportsContractError("bookmaker source is invalid")
        _text(self.company_id, "bookmaker ID")
        _text(self.company_name, "bookmaker name")
        _text(self.match_id, "bookmaker match ID")
        _utc(self.change_time, "odds change time")
        values = (self.home_decimal, self.draw_decimal, self.away_decimal)
        if any(
            not isfinite(value) or value <= 1.0 or value > 100.0 for value in values
        ):
            raise ISportsContractError("decimal odds are out of bounds")
        if not sanity_1x2(*values, lo=1.0, hi=1.2):
            raise ISportsContractError("bookmaker 1X2 prices fail market sanity")


@dataclass(frozen=True)
class ISportsMarketSnapshot:
    fixture: ISportsFixture
    selected_source: str
    bookmaker_quotes: tuple[ISportsBookmakerQuote, ...]
    snapshot: MarketSnapshot
    source_timestamp: datetime
    captured_at: datetime
    raw_response_digests: Mapping[str, str]
    malformed_row_count: int

    def validate(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: int = MAX_SOURCE_AGE_SECONDS,
    ) -> None:
        self.fixture.validate()
        self.snapshot.validate()
        if self.selected_source not in {"main", "european"}:
            raise ISportsContractError("selected odds source is invalid")
        if not self.bookmaker_quotes:
            raise ISportsContractError("no valid bookmaker rows")
        if any(row.source != self.selected_source for row in self.bookmaker_quotes):
            raise ISportsContractError("bookmaker-ID namespaces cannot be mixed")
        if len({row.company_id for row in self.bookmaker_quotes}) != len(
            self.bookmaker_quotes
        ):
            raise ISportsContractError("duplicate bookmaker quote")
        source = _utc(self.source_timestamp, "snapshot source timestamp")
        captured = _utc(self.captured_at, "snapshot capture timestamp")
        if captured < source or captured >= self.fixture.kickoff_utc:
            raise ISportsContractError(
                "snapshot is not a prematch point-in-time record"
            )
        if (captured - source).total_seconds() > max_age_seconds:
            raise ISportsContractError("snapshot is stale")
        if now is not None and _utc(now, "validation now") < captured:
            raise ISportsContractError("snapshot capture timestamp is in the future")
        if self.snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ISportsContractError(
                "closing odds cannot be used as prediction input"
            )
        for digest in self.raw_response_digests.values():
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ISportsContractError("raw response digest is invalid")

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.captured_at, max_age_seconds=MAX_SOURCE_AGE_SECONDS)
        return {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "fixture": {
                "fixture_key": self.fixture.fixture.fixture_key,
                "league_code": self.fixture.league_code,
                "provider_league_id": self.fixture.provider_league_id,
                "provider_match_id": self.fixture.provider_match_id,
                "home_team_id": self.fixture.home_team_id,
                "away_team_id": self.fixture.away_team_id,
                "home_team": self.fixture.home_team,
                "away_team": self.fixture.away_team,
                "kickoff_utc": self.fixture.kickoff_utc.isoformat(),
                "neutral": self.fixture.neutral,
            },
            "source": self.selected_source,
            "snapshot": {
                "fixture_key": self.snapshot.fixture_key,
                "captured_at": self.snapshot.captured_at.isoformat(),
                "kind": self.snapshot.kind.value,
                "source": self.snapshot.source,
                "odds": dict(self.snapshot.odds),
                "snapshot_id": self.snapshot.snapshot_id,
            },
            "source_timestamp": self.source_timestamp.isoformat(),
            "captured_at": self.captured_at.isoformat(),
            "bookmaker_count": len(self.bookmaker_quotes),
            "bookmakers": [
                {
                    "namespace": row.source,
                    "company_id": row.company_id,
                    "company_name": row.company_name,
                    "bookmaker_identity": row.bookmaker_identity,
                    "current_decimal_1x2": [
                        row.home_decimal,
                        row.draw_decimal,
                        row.away_decimal,
                    ],
                    "opening_decimal_1x2": [
                        row.opening_home_decimal,
                        row.opening_draw_decimal,
                        row.opening_away_decimal,
                    ],
                    "change_time": row.change_time.isoformat(),
                }
                for row in sorted(
                    self.bookmaker_quotes,
                    key=lambda item: (item.company_id, item.company_name),
                )
            ],
            "raw_response_digests": dict(self.raw_response_digests),
            "malformed_row_count": self.malformed_row_count,
        }


_MAIN_COMPANIES = {
    "1": "Macauslot",
    "3": "Crown",
    "4": "Ladbrokes",
    "7": "SNAI",
    "8": "Bet365",
    "9": "William Hill",
    "12": "Easybets",
    "14": "Vcbet",
    "17": "Mansion88",
    "19": "Interwetten",
    "22": "10BET",
    "24": "12bet",
    "31": "Sbobet",
    "35": "Wewbet",
    "42": "18bet",
    "48": "HK Jockey Club",
    "49": "Bwin",
    "50": "1xbet",
}


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ISportsContractError(f"{name} is not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ISportsContractError(f"{name} is not numeric") from exc
    if not isfinite(result):
        raise ISportsContractError(f"{name} is not finite")
    return result


def _decimal_from_hk(value: object, name: str) -> float:
    hk = _finite_number(value, name)
    decimal = hk + 1.0
    if not isfinite(decimal) or decimal <= 1.0 or decimal > 100.0:
        raise ISportsContractError(f"{name} is outside decimal-price bounds")
    return decimal


def _main_entries(payload: object) -> tuple[list[Mapping[str, object]], int]:
    rows = _response_rows(payload)
    entries: list[Mapping[str, object]] = []
    malformed = 0
    for row in rows:
        raw = row.get("europeOdds", [])
        if not isinstance(raw, list):
            raise ISportsContractError("Main Odds europeOdds must be an array")
        for item in raw:
            if isinstance(item, Mapping):
                entries.append(item)
            elif isinstance(item, str):
                fields = [part.strip() for part in item.split(",")]
                if len(fields) < 11:
                    malformed += 1
                    continue
                entries.append(
                    {
                        "matchId": fields[0],
                        "companyId": fields[1],
                        "initialHome": fields[2],
                        "initialDraw": fields[3],
                        "initialAway": fields[4],
                        "instantHome": fields[5],
                        "instantDraw": fields[6],
                        "instantAway": fields[7],
                        "changeTime": fields[8],
                        "close": fields[9],
                        "oddsType": fields[10],
                    }
                )
            else:
                malformed += 1
    return entries, malformed


def _flag_is_true(value: object) -> bool:
    return (
        value is True
        or (isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"})
        or (isinstance(value, int) and not isinstance(value, bool) and value == 1)
    )


def parse_main_odds(
    payload: object,
    *,
    captured_at: datetime,
    expected_match_ids: Sequence[str] | None = None,
) -> tuple[dict[str, tuple[ISportsBookmakerQuote, ...]], int]:
    _utc(captured_at, "captured_at")
    result: dict[str, list[ISportsBookmakerQuote]] = {}
    rows, malformed = _main_entries(payload)
    expected = set(expected_match_ids) if expected_match_ids is not None else None
    for row in rows:
        try:
            if _flag_is_true(row.get("maintenance")):
                malformed += 1
                continue
            if _flag_is_true(row.get("inPlay")) or _flag_is_true(row.get("close")):
                continue
            odds_type = row.get("oddsType", row.get("type"))
            if odds_type is None:
                raise ISportsContractError("Main Odds stage is missing")
            if isinstance(odds_type, bool):
                raise ISportsContractError("Main Odds stage is invalid")
            if int(odds_type) != 1:
                if int(odds_type) == 0:
                    malformed += 1
                continue
            match_id = _provider_id(row.get("matchId"), "Main Odds match ID")
            if expected is not None and match_id not in expected:
                malformed += 1
                continue
            company_id = _provider_id(row.get("companyId"), "Main Odds company ID")
            name = _MAIN_COMPANIES.get(company_id)
            if name is None:
                raise ISportsContractError("Main Odds bookmaker ID is unknown")
            change = _epoch_utc(row.get("changeTime"), "Main Odds changeTime")
            quote = ISportsBookmakerQuote(
                source="main",
                company_id=company_id,
                company_name=name,
                match_id=match_id,
                home_decimal=_decimal_from_hk(row.get("instantHome"), "instantHome"),
                draw_decimal=_decimal_from_hk(row.get("instantDraw"), "instantDraw"),
                away_decimal=_decimal_from_hk(row.get("instantAway"), "instantAway"),
                change_time=change,
                opening_home_decimal=_decimal_from_hk(
                    row.get("initialHome"), "initialHome"
                ),
                opening_draw_decimal=_decimal_from_hk(
                    row.get("initialDraw"), "initialDraw"
                ),
                opening_away_decimal=_decimal_from_hk(
                    row.get("initialAway"), "initialAway"
                ),
            )
            quote.validate()
            result.setdefault(match_id, []).append(quote)
        except (ISportsContractError, TypeError, ValueError, OverflowError):
            malformed += 1
    return {
        key: tuple(sorted(values, key=lambda row: (row.company_id, row.company_name)))
        for key, values in result.items()
    }, malformed


def _european_detail_rows(raw: object) -> Sequence[object]:
    if not isinstance(raw, list):
        return ()
    return raw


def parse_european_odds(
    payload: object,
    *,
    captured_at: datetime,
    expected_fixtures: Mapping[str, ISportsFixture] | None = None,
) -> tuple[dict[str, tuple[ISportsBookmakerQuote, ...]], int]:
    _utc(captured_at, "captured_at")
    result: dict[str, list[ISportsBookmakerQuote]] = {}
    malformed = 0
    for match in _response_rows(payload):
        try:
            match_id = _provider_id(match.get("matchId"), "European match ID")
        except ISportsContractError:
            malformed += 1
            continue
        odds = match.get("odds", [])
        if not match_id or not isinstance(odds, list):
            malformed += 1
            continue
        if expected_fixtures is not None:
            fixture = expected_fixtures.get(match_id)
            try:
                identity_matches = (
                    fixture is not None
                    and _canonical_name(match.get("leagueName"))
                    in _COMPETITION_NAMES[fixture.league_code]
                    and normalize_team_name(match.get("homeName"))
                    == normalize_team_name(fixture.home_team)
                    and normalize_team_name(match.get("awayName"))
                    == normalize_team_name(fixture.away_team)
                    and _epoch_utc(match.get("matchTime"), "European matchTime")
                    == fixture.kickoff_utc
                )
            except (ISportsContractError, KeyError):
                identity_matches = False
            if not identity_matches:
                malformed += len(odds) if odds else 1
                continue
        for entry in odds:
            if not isinstance(entry, Mapping):
                malformed += 1
                continue
            if _flag_is_true(entry.get("inPlay")) or _flag_is_true(entry.get("close")):
                continue
            change_raw = entry.get("changeTime")
            details = _european_detail_rows(entry.get("oddsDetail"))
            for detail in details:
                try:
                    if isinstance(detail, Mapping):
                        fields = detail
                    elif isinstance(detail, str):
                        values = [part.strip() for part in detail.split(",")]
                        if len(values) < 8:
                            raise ISportsContractError(
                                "European Odds row is incomplete"
                            )
                        fields = {
                            "companyId": values[0],
                            "companyName": values[1],
                            "initialHome": values[2],
                            "initialDraw": values[3],
                            "initialAway": values[4],
                            "instantHome": values[5],
                            "instantDraw": values[6],
                            "instantAway": values[7],
                        }
                    else:
                        raise ISportsContractError("European Odds row is malformed")
                    company_id = _provider_id(
                        fields.get("companyId"), "European company ID"
                    )
                    company_name = _text(
                        fields.get("companyName"), "European company name"
                    )
                    row_change = fields.get("changeTime", change_raw)
                    quote = ISportsBookmakerQuote(
                        source="european",
                        company_id=company_id,
                        company_name=company_name,
                        match_id=match_id,
                        home_decimal=_decimal_from_hk(
                            fields.get("instantHome"), "instantHome"
                        ),
                        draw_decimal=_decimal_from_hk(
                            fields.get("instantDraw"), "instantDraw"
                        ),
                        away_decimal=_decimal_from_hk(
                            fields.get("instantAway"), "instantAway"
                        ),
                        change_time=_epoch_utc(row_change, "European changeTime"),
                        opening_home_decimal=_decimal_from_hk(
                            fields.get("initialHome"), "initialHome"
                        ),
                        opening_draw_decimal=_decimal_from_hk(
                            fields.get("initialDraw"), "initialDraw"
                        ),
                        opening_away_decimal=_decimal_from_hk(
                            fields.get("initialAway"), "initialAway"
                        ),
                    )
                    quote.validate()
                    result.setdefault(match_id, []).append(quote)
                except (ISportsContractError, TypeError, ValueError, OverflowError):
                    malformed += 1
    return {
        key: tuple(sorted(values, key=lambda row: (row.company_id, row.company_name)))
        for key, values in result.items()
    }, malformed


def aggregate_1x2(
    fixture: ISportsFixture,
    *,
    main_quotes: Sequence[ISportsBookmakerQuote] = (),
    european_quotes: Sequence[ISportsBookmakerQuote] = (),
    captured_at: datetime,
    main_response_digest: str = "",
    european_response_digest: str = "",
    malformed_row_count: int = 0,
    max_age_seconds: int = MAX_SOURCE_AGE_SECONDS,
) -> ISportsMarketSnapshot:
    """Prefer valid European Pro quotes; use Main only if none are valid.

    The two provider bookmaker-ID namespaces are never combined.  Prices are
    de-vigged per bookmaker via SportsBrain's existing Shin utility; the
    coordinate median is normalized and exposed as fair decimal market prices.
    """

    fixture.validate()
    captured = _utc(captured_at, "captured_at")
    if captured >= fixture.kickoff_utc:
        raise ISportsContractError("in-play/post-kickoff odds are not eligible")
    valid_by_source: dict[str, tuple[ISportsBookmakerQuote, ...]] = {}
    for source, values in (("european", european_quotes), ("main", main_quotes)):
        valid = tuple(
            sorted(
                (
                    item
                    for item in values
                    if item.match_id == fixture.provider_match_id
                    and item.change_time <= captured
                    and 0
                    <= (captured - item.change_time).total_seconds()
                    <= max_age_seconds
                ),
                key=lambda item: (item.company_id, item.company_name),
            )
        )
        if len({item.company_id for item in valid}) != len(valid):
            raise ISportsContractError("duplicate bookmaker identity within one source")
        valid_by_source[source] = valid
    selected_source = "european" if valid_by_source["european"] else "main"
    selected = valid_by_source[selected_source]
    if not selected:
        raise ISportsContractError("no fresh valid prematch 1X2 rows")
    probabilities = [
        remove_margin_shin((row.home_decimal, row.draw_decimal, row.away_decimal))
        for row in selected
    ]
    median_probs = tuple(
        statistics.median([row[index] for row in probabilities]) for index in range(3)
    )
    total = sum(median_probs)
    if not isfinite(total) or total <= 0:
        raise ISportsContractError("aggregate probability vector is invalid")
    fair_odds = {
        key: 1.0 / (value / total)
        for key, value in zip(("home", "draw", "away"), median_probs, strict=True)
    }
    snapshot_id = digest_record(
        {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "source": selected_source,
            "provider_match_id": fixture.provider_match_id,
            "captured_at": captured.isoformat(),
            "bookmakers": [
                {
                    "id": row.company_id,
                    "name": row.company_name,
                    "prices": [row.home_decimal, row.draw_decimal, row.away_decimal],
                    "change_time": row.change_time.isoformat(),
                }
                for row in selected
            ],
        }
    )
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture.fixture_key,
        captured_at=captured,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source=ISPORTS_PROVIDER_IDENTITY,
        odds=fair_odds,
        snapshot_id=snapshot_id,
    )
    result = ISportsMarketSnapshot(
        fixture=fixture,
        selected_source=selected_source,
        bookmaker_quotes=selected,
        snapshot=snapshot,
        source_timestamp=min(row.change_time for row in selected),
        captured_at=captured,
        raw_response_digests={
            key: value
            for key, value in (
                ("main", main_response_digest),
                ("european", european_response_digest),
            )
            if value
        },
        malformed_row_count=malformed_row_count,
    )
    result.validate(now=captured, max_age_seconds=max_age_seconds)
    return result


def normalized_observation(
    market: ISportsMarketSnapshot,
    *,
    request_identity: str,
    request_started_at: datetime,
    request_completed_at: datetime,
    adapter_source_sha: str,
    quota_before: QuotaSnapshot | None = None,
    quota_after: QuotaSnapshot | None = None,
    rate_limit: QuotaSnapshot | None = None,
) -> NormalizedOddsObservation:
    """Adapt a verified shared snapshot into the canonical cascade observation."""

    market.validate(now=request_completed_at)
    source_sha = _text(adapter_source_sha, "adapter source SHA")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", source_sha):
        raise ISportsContractError("adapter source SHA is invalid")
    raw_digest = market.raw_response_digests.get(market.selected_source)
    if raw_digest is None or not re.fullmatch(r"[0-9a-f]{64}", raw_digest):
        raise ISportsContractError("selected provider response digest is required")
    metadata = market.as_payload()
    metadata["configuration_digest"] = digest_record(
        {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "league": market.fixture.league_code,
            "odds_source": market.selected_source,
            "market": MARKET_PREMATCH_1X2,
            "maximum_source_age_seconds": MAX_SOURCE_AGE_SECONDS,
        }
    )
    metadata["adapter_source_sha"] = source_sha.lower()
    provider_record_digest = digest_record(
        {
            "provider": ISPORTS_PROVIDER_IDENTITY,
            "provider_match_id": market.fixture.provider_match_id,
            "source": market.selected_source,
            "bookmakers": metadata["bookmakers"],
            "source_timestamp": market.source_timestamp.isoformat(),
        }
    )
    metadata["provider_record_digest"] = provider_record_digest
    observation = NormalizedOddsObservation(
        league_code=market.fixture.league_code,
        fixture_key=market.fixture.fixture.fixture_key,
        provider_fixture_id=market.fixture.provider_match_id,
        home_team=market.fixture.home_team,
        away_team=market.fixture.away_team,
        kickoff_utc=market.fixture.kickoff_utc,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=market.snapshot.odds["home"],
        draw_odds=market.snapshot.odds["draw"],
        away_odds=market.snapshot.odds["away"],
        provider_identity=ISPORTS_PROVIDER_IDENTITY,
        bookmaker_identity=(
            "isports_api:"
            + market.selected_source
            + ":"
            + ",".join(row.company_id for row in market.bookmaker_quotes)
        ),
        source_timestamp=market.source_timestamp,
        captured_at=market.captured_at,
        request_identity=request_identity,
        request_started_at=_utc(request_started_at, "request_started_at"),
        request_completed_at=_utc(request_completed_at, "request_completed_at"),
        latency_ms=round(
            (
                _utc(request_completed_at, "request_completed_at")
                - _utc(request_started_at, "request_started_at")
            ).total_seconds()
            * 1000
        ),
        provider_priority=0,
        fallback_depth=0,
        quota_state_before=quota_before or QuotaSnapshot(),
        quota_state_after=quota_after or QuotaSnapshot(),
        rate_limit_state=rate_limit or QuotaSnapshot(),
        source_provenance=f"isports_api:{market.selected_source}:prematch_1x2",
        raw_record_digest=raw_digest,
        adapter_version=ISPORTS_ADAPTER_VERSION,
        completeness=ObservationCompleteness.COMPLETE,
        error_classification=ProviderState.AVAILABLE,
        candidate_only=True,
        delayed=False,
        delay_seconds=None,
        metadata=metadata,
        source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
    )
    observation.validate(require_fresh=False)
    metadata["normalized_record_digest"] = digest_record(observation.as_payload())
    observation = replace(observation, metadata=metadata)
    observation.validate(require_fresh=False)
    return observation


class ISportsClient:
    """Single-request client with explicit endpoint pacing, no retry/fallback."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: ISportsHttpTransport = isports_requests_transport,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 15.0,
        enforce_pacing: bool = True,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ISportsContractError("timeout must be finite and positive")
        self._api_key = api_key
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper
        self._timeout = timeout_seconds
        self._enforce_pacing = enforce_pacing
        self._last_request: dict[str, datetime] = {}
        self._responses: list[ISportsHttpResponse] = []
        self._request_count = 0
        self._credential_access_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def credential_access_count(self) -> int:
        return self._credential_access_count

    @property
    def pacing_enabled(self) -> bool:
        return self._enforce_pacing

    @property
    def responses(self) -> tuple[ISportsHttpResponse, ...]:
        """Safe response records from this client instance, in request order."""

        return tuple(self._responses)

    def _get_key(self) -> str:
        if self._api_key is not None:
            key = self._api_key
        else:
            self._credential_access_count += 1
            key = os.getenv("ISPORTS_API_KEY", "")
        if not isinstance(key, str) or not key.strip():
            raise ISportsContractError("ISPORTS_API_KEY is unavailable")
        return key.strip()

    def request(
        self, endpoint_path: str, params: Mapping[str, str] | None = None
    ) -> ISportsHttpResponse:
        if endpoint_path not in ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS:
            raise ISportsContractError(
                "endpoint is outside the reviewed iSports allowlist"
            )
        minimum = ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS[endpoint_path]
        now = _utc(self._clock(), "request clock")
        previous = self._last_request.get(endpoint_path)
        if previous is not None and self._enforce_pacing:
            delay = minimum - (now - previous).total_seconds()
            if delay > 0:
                self._sleeper(delay)
        safe_params = {str(name): str(value) for name, value in (params or {}).items()}
        if set(safe_params) - _ENDPOINT_PARAMETERS[endpoint_path]:
            raise ISportsContractError(
                "request contains parameters outside the endpoint allowlist"
            )
        api_key = self._get_key()
        # The key exists only in the transport call; it is never part of the
        # request object, evidence, response digest, or exception text.
        self._request_count += 1
        status, payload, headers, started, completed, error_class = self._transport(
            endpoint_path, safe_params, api_key, self._timeout
        )
        self._last_request[endpoint_path] = _utc(completed, "request completion")
        safe_headers = {
            str(name).casefold(): str(value).strip()
            for name, value in headers.items()
            if str(name).casefold() in _SAFE_HEADERS
        }
        response = ISportsHttpResponse(
            endpoint_path=endpoint_path,
            status_code=status,
            payload=payload,
            safe_headers=safe_headers,
            request_ordinal=self._request_count,
            started_at=started,
            completed_at=completed,
            transport_error_class=error_class,
            request_parameters=safe_params,
        )
        self._responses.append(response)
        response.validate()
        return response

    def catalog(self) -> tuple[dict[str, ISportsCompetition], ISportsHttpResponse]:
        response = self.request(ISPORTS_ENDPOINTS["catalog"])
        if response.status_code != 200 or response.transport_error_class:
            raise ISportsContractError("iSports competition catalog request failed")
        return resolve_competitions(response.payload), response

    def schedule(
        self, competition: ISportsCompetition
    ) -> tuple[tuple[ISportsFixture, ...], ISportsHttpResponse]:
        competition.validate()
        response = self.request(
            ISPORTS_ENDPOINTS["schedule"], {"leagueId": competition.provider_league_id}
        )
        if response.status_code != 200 or response.transport_error_class:
            raise ISportsContractError(
                f"iSports schedule request failed for {competition.league_code}"
            )
        return normalize_schedule(response.payload, competition), response

    def main_odds(
        self, match_ids: Sequence[str]
    ) -> tuple[dict[str, tuple[ISportsBookmakerQuote, ...]], int, ISportsHttpResponse]:
        ids = _validated_bulk_ids(match_ids)
        response = self.request(
            ISPORTS_ENDPOINTS["main_odds"], {"matchId": ",".join(ids)}
        )
        if response.status_code != 200 or response.transport_error_class:
            raise ISportsContractError("iSports Main Odds request failed")
        parsed, malformed = parse_main_odds(
            response.payload, captured_at=response.completed_at
        )
        return parsed, malformed, response

    def european_odds(
        self, match_ids: Sequence[str]
    ) -> tuple[dict[str, tuple[ISportsBookmakerQuote, ...]], int, ISportsHttpResponse]:
        ids = _validated_bulk_ids(match_ids)
        response = self.request(
            ISPORTS_ENDPOINTS["european_odds"], {"matchId": ",".join(ids)}
        )
        if response.status_code != 200 or response.transport_error_class:
            raise ISportsContractError("iSports European Odds request failed")
        parsed, malformed = parse_european_odds(
            response.payload, captured_at=response.completed_at
        )
        return parsed, malformed, response


def _validated_bulk_ids(match_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(match_ids, (str, bytes)) or not isinstance(match_ids, Sequence):
        raise ISportsContractError("bulk match IDs must be a sequence")
    ids = tuple(_text(item, "provider match ID") for item in match_ids)
    if not ids or len(ids) > MAX_BULK_MATCH_IDS or len(set(ids)) != len(ids):
        raise ISportsContractError(
            "bulk match IDs must be unique and within provider limit"
        )
    return ids


@dataclass(frozen=True)
class CompetitionCapabilityResult:
    league_code: str
    provider_league_id: str
    provider_name: str
    upcoming_fixture_count: int
    selected_fixture: ISportsFixture | None
    main_bookmaker_count: int
    european_bookmaker_count: int
    selected_bookmaker_count: int
    newest_odds_change_time: datetime | None
    newest_odds_age_seconds: float | None
    malformed_row_count: int
    missing_market_fixture_count: int
    schedule_http_status: int | None
    market_http_statuses: Mapping[str, int | None]

    @property
    def market_coverage(self) -> bool:
        return self.selected_bookmaker_count > 0

    def as_payload(self) -> dict[str, object]:
        return {
            "league_code": self.league_code,
            "provider_league_id": self.provider_league_id,
            "canonical_competition_name": self.provider_name,
            "upcoming_fixture_count": self.upcoming_fixture_count,
            "provider_match_ids": [self.selected_fixture.provider_match_id]
            if self.selected_fixture
            else [],
            "selected_fixture": self.selected_fixture.as_payload()
            if self.selected_fixture
            else None,
            "schedule_coverage": self.selected_fixture is not None,
            "market_coverage": self.market_coverage,
            "main_odds_bookmaker_count": self.main_bookmaker_count,
            "european_odds_bookmaker_count": self.european_bookmaker_count,
            "selected_bookmaker_count": self.selected_bookmaker_count,
            "newest_odds_change_time": self.newest_odds_change_time.isoformat()
            if self.newest_odds_change_time
            else None,
            "newest_odds_age_seconds": self.newest_odds_age_seconds,
            "malformed_row_count": self.malformed_row_count,
            "missing_market_fixture_count": self.missing_market_fixture_count,
            "schedule_http_status": self.schedule_http_status,
            "market_http_statuses": dict(self.market_http_statuses),
        }


@dataclass(frozen=True)
class ISportsCapabilityReport:
    started_at: datetime
    completed_at: datetime
    competitions: tuple[CompetitionCapabilityResult, ...]
    request_evidence: tuple[Mapping[str, object], ...]
    request_count: int
    credential_access_count: int
    status: str = "COMPLETED"
    failure_classification: str | None = None
    failure_operation: str | None = None
    failure_http_status: int | None = None
    provider: str = ISPORTS_PROVIDER_IDENTITY
    retries: int = 0
    fallbacks: int = 0
    polling: int = 0
    production_authority: bool = False
    activation: bool = False
    publication: bool = False
    betting: bool = False
    ledger_mutation: bool = False

    def validate(self) -> None:
        if self.provider != ISPORTS_PROVIDER_IDENTITY:
            raise ISportsContractError("capability report provider identity mismatch")
        codes = tuple(item.league_code for item in self.competitions)
        if self.status == "COMPLETED" and codes != ISPORTS_COMPETITIONS:
            raise ISportsContractError("capability report competition order mismatch")
        if self.status == "FAILED_CLOSED" and codes != tuple(
            code for code in ISPORTS_COMPETITIONS if code in codes
        ):
            raise ISportsContractError("failed capability report order mismatch")
        if self.status not in {"COMPLETED", "FAILED_CLOSED"}:
            raise ISportsContractError("capability report status is invalid")
        if self.status == "FAILED_CLOSED" and not self.failure_classification:
            raise ISportsContractError("failed capability report lacks classification")
        if self.status == "COMPLETED" and any(
            (
                self.failure_classification,
                self.failure_operation,
                self.failure_http_status,
            )
        ):
            raise ISportsContractError("completed capability report has failure data")
        if not 0 <= self.request_count <= 9 or self.request_count != len(
            self.request_evidence
        ):
            raise ISportsContractError("capability report request count is invalid")
        if self.retries or self.fallbacks or self.polling:
            raise ISportsContractError(
                "capability report records a forbidden repeat request"
            )
        if any(
            (
                self.production_authority,
                self.activation,
                self.publication,
                self.betting,
                self.ledger_mutation,
            )
        ):
            raise ISportsContractError(
                "capability report crosses a production side-effect boundary"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": "isports-football-capability-report-v1",
            "provider": self.provider,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "status": self.status,
            "failure_classification": self.failure_classification,
            "failure_operation": self.failure_operation,
            "failure_http_status": self.failure_http_status,
            "request_count": self.request_count,
            "credential_access_count": self.credential_access_count,
            "retry_count": self.retries,
            "fallback_count": self.fallbacks,
            "polling_count": self.polling,
            "requests": [dict(item) for item in self.request_evidence],
            "competitions": [item.as_payload() for item in self.competitions],
            "production_authority": self.production_authority,
            "activation": self.activation,
            "publication": self.publication,
            "betting": self.betting,
            "ledger_mutation": self.ledger_mutation,
        }


def _failure_competition_results(
    responses: Sequence[ISportsHttpResponse], *, observed_at: datetime
) -> tuple[CompetitionCapabilityResult, ...]:
    """Retain safe schedule coverage when a later diagnostic endpoint fails."""

    catalog = next(
        (
            item
            for item in responses
            if item.endpoint_path == ISPORTS_ENDPOINTS["catalog"]
        ),
        None,
    )
    if catalog is None or catalog.status_code != 200 or catalog.transport_error_class:
        return ()
    try:
        competitions = resolve_competitions(catalog.payload)
    except (ISportsContractError, TypeError, ValueError, KeyError):
        return ()
    schedules = {
        item.request_parameters.get("leagueId"): item
        for item in responses
        if item.endpoint_path == ISPORTS_ENDPOINTS["schedule"]
    }
    market_statuses = {
        name: next(
            (
                item.status_code
                for item in reversed(responses)
                if item.endpoint_path == path
            ),
            None,
        )
        for name, path in (
            ("main", ISPORTS_ENDPOINTS["main_odds"]),
            ("european", ISPORTS_ENDPOINTS["european_odds"]),
        )
    }
    results: list[CompetitionCapabilityResult] = []
    for code in ISPORTS_COMPETITIONS:
        competition = competitions[code]
        response = schedules.get(competition.provider_league_id)
        fixtures: tuple[ISportsFixture, ...] = ()
        if (
            response is not None
            and response.status_code == 200
            and not response.transport_error_class
        ):
            try:
                fixtures = normalize_schedule(response.payload, competition)
            except (ISportsContractError, TypeError, ValueError, KeyError):
                fixtures = ()
        upcoming = tuple(
            item
            for item in fixtures
            if item.status == 0 and item.kickoff_utc > observed_at
        )
        selected = (
            min(upcoming, key=lambda item: (item.kickoff_utc, item.provider_match_id))
            if upcoming
            else None
        )
        results.append(
            CompetitionCapabilityResult(
                league_code=code,
                provider_league_id=competition.provider_league_id,
                provider_name=competition.provider_name,
                upcoming_fixture_count=len(upcoming),
                selected_fixture=selected,
                main_bookmaker_count=0,
                european_bookmaker_count=0,
                selected_bookmaker_count=0,
                newest_odds_change_time=None,
                newest_odds_age_seconds=None,
                malformed_row_count=0,
                missing_market_fixture_count=int(selected is not None),
                schedule_http_status=response.status_code if response else None,
                market_http_statuses=market_statuses,
            )
        )
    return tuple(results)


def run_capability_diagnostic(
    client: ISportsClient,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ISportsCapabilityReport:
    """Run the authorized 1+6+1+1 capability sample, without retry or polling."""

    started = _utc(now(), "diagnostic start")
    try:
        report = _run_capability_diagnostic(client, now=now, started=started)
    except (ISportsContractError, TypeError, ValueError, KeyError):
        # Do not include arbitrary exception text: the transport/provider may
        # embed request details. Preserve only normalized response facts.
        latest = client.responses[-1] if client.responses else None
        if latest is None:
            classification = "preflight_or_credential_failure"
            operation = None
            status_code = None
        elif latest.transport_error_class:
            classification = "transport_failure"
            operation = next(
                name
                for name, path in ISPORTS_ENDPOINTS.items()
                if path == latest.endpoint_path
            )
            status_code = latest.status_code
        elif latest.status_code != 200:
            classification = "http_failure"
            operation = next(
                name
                for name, path in ISPORTS_ENDPOINTS.items()
                if path == latest.endpoint_path
            )
            status_code = latest.status_code
        else:
            classification = "invalid_provider_response"
            operation = next(
                name
                for name, path in ISPORTS_ENDPOINTS.items()
                if path == latest.endpoint_path
            )
            status_code = latest.status_code
        failed = ISportsCapabilityReport(
            started_at=started,
            completed_at=_utc(now(), "diagnostic completion"),
            competitions=_failure_competition_results(
                client.responses, observed_at=_utc(now(), "failure observation time")
            ),
            request_evidence=tuple(item.safe_payload() for item in client.responses),
            request_count=client.request_count,
            credential_access_count=client.credential_access_count,
            status="FAILED_CLOSED",
            failure_classification=classification,
            failure_operation=operation,
            failure_http_status=status_code,
        )
        failed.validate()
        return failed
    return report


def _run_capability_diagnostic(
    client: ISportsClient,
    *,
    now: Callable[[], datetime],
    started: datetime,
) -> ISportsCapabilityReport:

    if client.request_count != 0:
        raise ISportsContractError("capability diagnostic requires a fresh client")
    if not client.pacing_enabled:
        raise ISportsContractError("capability diagnostic requires endpoint pacing")
    competitions, catalog_response = client.catalog()
    responses: list[ISportsHttpResponse] = [catalog_response]
    schedules: dict[str, tuple[ISportsFixture, ...]] = {}
    schedule_responses: dict[str, ISportsHttpResponse] = {}
    for code in ISPORTS_COMPETITIONS:
        items, response = client.schedule(competitions[code])
        schedules[code] = items
        schedule_responses[code] = response
        responses.append(response)
    observed_at = _utc(now(), "schedule observation time")
    selected: dict[str, ISportsFixture | None] = {}
    for code in ISPORTS_COMPETITIONS:
        future = [
            item
            for item in schedules[code]
            if item.status == 0 and item.kickoff_utc > observed_at
        ]
        selected[code] = (
            min(future, key=lambda item: (item.kickoff_utc, item.provider_match_id))
            if future
            else None
        )
    selected_fixtures = tuple(item for item in selected.values() if item is not None)
    match_ids = tuple(item.provider_match_id for item in selected_fixtures)
    if len(set(match_ids)) != len(match_ids):
        raise ISportsContractError("provider matchId is duplicated across competitions")
    main_data: dict[str, tuple[ISportsBookmakerQuote, ...]] = {}
    european_data: dict[str, tuple[ISportsBookmakerQuote, ...]] = {}
    main_malformed = european_malformed = 0
    main_response: ISportsHttpResponse | None = None
    european_response: ISportsHttpResponse | None = None
    if match_ids:
        main_response = client.request(
            ISPORTS_ENDPOINTS["main_odds"], {"matchId": ",".join(match_ids)}
        )
        responses.append(main_response)
        if main_response.status_code != 200 or main_response.transport_error_class:
            raise ISportsContractError("iSports Main Odds request failed")
        main_data, main_malformed = parse_main_odds(
            main_response.payload,
            captured_at=main_response.completed_at,
            expected_match_ids=match_ids,
        )
        european_response = client.request(
            ISPORTS_ENDPOINTS["european_odds"], {"matchId": ",".join(match_ids)}
        )
        responses.append(european_response)
        if (
            european_response.status_code != 200
            or european_response.transport_error_class
        ):
            raise ISportsContractError("iSports European Odds request failed")
        expected = {fixture.provider_match_id: fixture for fixture in selected_fixtures}
        european_data, european_malformed = parse_european_odds(
            european_response.payload,
            captured_at=european_response.completed_at,
            expected_fixtures=expected,
        )
    else:
        european_malformed = 0
    completed = _utc(now(), "diagnostic completion")
    results: list[CompetitionCapabilityResult] = []
    for code in ISPORTS_COMPETITIONS:
        fixture = selected[code]
        main_quotes = main_data.get(fixture.provider_match_id, ()) if fixture else ()
        european_quotes = (
            european_data.get(fixture.provider_match_id, ()) if fixture else ()
        )
        source_rows: tuple[ISportsBookmakerQuote, ...] = ()
        market_response_digests: dict[str, str] = {}
        if main_response is not None:
            market_response_digests["main"] = main_response.response_digest
        if european_response is not None:
            market_response_digests["european"] = european_response.response_digest
        if fixture is not None and (main_quotes or european_quotes):
            try:
                market = aggregate_1x2(
                    fixture,
                    main_quotes=main_quotes,
                    european_quotes=european_quotes,
                    captured_at=completed,
                    main_response_digest=market_response_digests.get("main", ""),
                    european_response_digest=market_response_digests.get(
                        "european", ""
                    ),
                    malformed_row_count=main_malformed + european_malformed,
                )
                source_rows = market.bookmaker_quotes
            except ISportsContractError:
                source_rows = ()
        newest = max((row.change_time for row in source_rows), default=None)
        age = (completed - newest).total_seconds() if newest else None
        results.append(
            CompetitionCapabilityResult(
                league_code=code,
                provider_league_id=competitions[code].provider_league_id,
                provider_name=competitions[code].provider_name,
                upcoming_fixture_count=sum(
                    1
                    for item in schedules[code]
                    if item.status == 0 and item.kickoff_utc > started
                ),
                selected_fixture=fixture,
                main_bookmaker_count=len(main_quotes),
                european_bookmaker_count=len(european_quotes),
                selected_bookmaker_count=len(source_rows),
                newest_odds_change_time=newest,
                newest_odds_age_seconds=age,
                malformed_row_count=main_malformed + european_malformed,
                missing_market_fixture_count=int(
                    fixture is not None and not source_rows
                ),
                schedule_http_status=schedule_responses[code].status_code,
                market_http_statuses={
                    "main": main_response.status_code if main_response else None,
                    "european": european_response.status_code
                    if european_response
                    else None,
                },
            )
        )
    report = ISportsCapabilityReport(
        started_at=started,
        completed_at=completed,
        competitions=tuple(results),
        request_evidence=tuple(item.safe_payload() for item in responses),
        request_count=client.request_count,
        credential_access_count=client.credential_access_count,
        status="COMPLETED",
    )
    report.validate()
    return report


__all__ = [
    "ISPORTS_ADAPTER_VERSION",
    "ISPORTS_BASE_URL",
    "ISPORTS_COMPETITIONS",
    "ISPORTS_ENDPOINTS",
    "ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS",
    "ISPORTS_PROVIDER_IDENTITY",
    "ISPORTS_TOP5_LEAGUES",
    "MAX_SOURCE_AGE_SECONDS",
    "CompetitionCapabilityResult",
    "ISportsBookmakerQuote",
    "ISportsCapabilityReport",
    "ISportsClient",
    "ISportsCompetition",
    "ISportsContractError",
    "ISportsFixture",
    "ISportsHttpResponse",
    "ISportsMarketSnapshot",
    "aggregate_1x2",
    "isports_requests_transport",
    "normalize_schedule",
    "normalized_observation",
    "parse_european_odds",
    "parse_main_odds",
    "resolve_competitions",
    "run_capability_diagnostic",
]
