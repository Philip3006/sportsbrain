"""Offline-only Champions League provider-cascade shadow contracts.

This module is a readiness seam, not a provider client.  It records what the
currently checked-in source candidates can provide and validates replayed
observations before any future cascade integration.  No function here performs
I/O, selects an authority, spends quota, publishes a signal, or enables UCL in
the existing Top-5 router.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import ProductionContractError

CHAMPIONS_LEAGUE_CODE = "UCL"
CHAMPIONS_LEAGUE_SPORT_KEY = "soccer_uefa_champs_league"
REGULATION_1X2_MARKET = "football:pre_match:regulation_1x2"
CL_SHADOW_CONTRACT_VERSION = "cl-shadow-observation-v1"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_URI_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


class CLShadowContractError(ProductionContractError):
    """Invalid Champions League shadow or replay evidence."""


class CLReplayError(CLShadowContractError):
    """The deterministic replay input cannot be evaluated safely."""


class CLSourceStatus(str, Enum):
    IMPLEMENTED_CANDIDATE = "implemented_candidate"
    IMPLEMENTED_NOT_UCL_MAPPED = "implemented_not_ucl_mapped"
    DECOMMISSIONED = "decommissioned"
    DISABLED_LEGACY = "disabled_legacy"
    HISTORICAL_ONLY = "historical_only"
    RESULT_ONLY = "result_only"
    CACHE_ONLY = "cache_only"


class CLEventState(str, Enum):
    PRE_MATCH = "pre_match"
    IN_PLAY = "in_play"
    STALE = "stale"


class CLRejectionReason(str, Enum):
    UNKNOWN_FIXTURE = "unknown_fixture"
    FIXTURE_ID_MISMATCH = "fixture_id_mismatch"
    COMPETITION_MISMATCH = "competition_mismatch"
    PARTICIPANT_MISMATCH = "participant_mismatch"
    KICKOFF_MISMATCH = "kickoff_mismatch"
    INCOMPLETE_REGULATION_1X2 = "incomplete_regulation_1x2"
    WRONG_MARKET = "wrong_market"
    STALE_EVENT = "stale_event"
    IN_PLAY = "in_play"
    MISSING_TIMESTAMP = "missing_timestamp"
    INVALID_TIMESTAMP = "invalid_timestamp"
    MISSING_PROVENANCE = "missing_provenance"
    MISSING_QUOTA_METADATA = "missing_quota_metadata"
    INVALID_QUOTA_METADATA = "invalid_quota_metadata"
    INVALID_ODDS = "invalid_odds"
    DUPLICATE_FIXTURE = "duplicate_fixture"
    DUPLICATE_OBSERVATION = "duplicate_observation"


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CLShadowContractError(f"{field_name} is required")
    return value.strip()


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CLShadowContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _team(value: object) -> str:
    raw = _text(value, "team")
    ascii_value = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    normalized = " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", ascii_value.casefold()).split())
    aliases = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "paris sg": "paris saint germain",
        "psg": "paris saint germain",
        "inter": "internazionale",
        "inter milan": "internazionale",
        "bayern munchen": "bayern munich",
    }
    return aliases.get(normalized, normalized)


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CLSourceCompatibility:
    """One checked-in source candidate and its UCL shadow capabilities."""

    source: str
    status: CLSourceStatus
    implementation_locations: tuple[str, ...]
    competition_key: str
    exact_fixture_identity: bool
    regulation_1x2: bool
    multi_bookmaker_retention: str
    source_timestamps: str
    provenance: str
    quota_metadata: str
    in_play_rejection: bool
    notes: str = ""

    def validate(self) -> None:
        _text(self.source, "source")
        CLSourceStatus(self.status)
        if not self.implementation_locations or any(
            not isinstance(value, str) or not value.strip()
            for value in self.implementation_locations
        ):
            raise CLShadowContractError("implementation locations are required")
        if self.status is CLSourceStatus.IMPLEMENTED_CANDIDATE:
            if self.competition_key != CHAMPIONS_LEAGUE_SPORT_KEY:
                raise CLShadowContractError("UCL candidate has the wrong competition key")
            if not (self.exact_fixture_identity and self.regulation_1x2 and self.in_play_rejection):
                raise CLShadowContractError("UCL candidate is missing a hard shadow capability")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "source": self.source,
            "status": self.status.value,
            "implementation_locations": list(self.implementation_locations),
            "competition_key": self.competition_key,
            "exact_fixture_identity": self.exact_fixture_identity,
            "regulation_1x2": self.regulation_1x2,
            "multi_bookmaker_retention": self.multi_bookmaker_retention,
            "source_timestamps": self.source_timestamps,
            "provenance": self.provenance,
            "quota_metadata": self.quota_metadata,
            "in_play_rejection": self.in_play_rejection,
            "notes": self.notes,
        }


def _source(
    name: str,
    status: CLSourceStatus,
    locations: tuple[str, ...],
    competition_key: str,
    *,
    identity: bool,
    market: bool,
    bookmakers: str,
    timestamps: str,
    provenance: str,
    quota: str,
    in_play: bool,
    notes: str = "",
) -> CLSourceCompatibility:
    item = CLSourceCompatibility(
        name, status, locations, competition_key, identity, market, bookmakers,
        timestamps, provenance, quota, in_play, notes,
    )
    item.validate()
    return item


def compatibility_matrix() -> tuple[CLSourceCompatibility, ...]:
    """Return the deterministic audit of currently implemented source paths."""

    return (
        _source(
            "the_odds_api", CLSourceStatus.IMPLEMENTED_CANDIDATE,
            ("src/football/provider_cascade/adapters.py", "src/data/odds_api.py"),
            CHAMPIONS_LEAGUE_SPORT_KEY, identity=True, market=True,
            bookmakers="raw payload supports multiple; active cascade currently selects one",
            timestamps="bookmaker/market source timestamp",
            provenance="event id, bookmaker identity, request and raw-record digest",
            quota="response headers and replay metadata",
            in_play=True,
            notes="Only candidate with a configured UCL sport key; not enabled by the Top-5 router.",
        ),
        _source(
            "api_football", CLSourceStatus.IMPLEMENTED_NOT_UCL_MAPPED,
            ("src/football/provider_cascade/adapters.py",), "",
            identity=True, market=True, bookmakers="single normalized bookmaker",
            timestamps="capture-only; no guaranteed source timestamp",
            provenance="fixture id and adapter metadata", quota="response metadata",
            in_play=True, notes="Adapter league_ids covers domestic Top-5 only; no UCL mapping.",
        ),
        _source(
            "odds_api_io", CLSourceStatus.DECOMMISSIONED,
            ("src/football/provider_cascade/adapters.py",), "",
            identity=True, market=True, bookmakers="allow-list in request",
            timestamps="market.updatedAt", provenance="event/bookmaker metadata",
            quota="provider response", in_play=True,
            notes="Present for historical compatibility but prohibited by the cascade contract.",
        ),
        _source(
            "betfair_delayed", CLSourceStatus.DECOMMISSIONED,
            ("src/football/provider_cascade/adapters.py",), "",
            identity=True, market=True, bookmakers="exchange market, not bookmaker books",
            timestamps="delayed market capture", provenance="market catalogue/runner mapping",
            quota="provider response", in_play=True,
            notes="Delayed exchange path is not an approved pre-match UCL candidate.",
        ),
        _source(
            "football_data", CLSourceStatus.HISTORICAL_ONLY,
            ("src/data/football_data.py",), "",
            identity=False, market=True, bookmakers="implicit historical closing proxy",
            timestamps="date only", provenance="CSV row", quota="not applicable",
            in_play=False, notes="Useful only for historical benchmarks; no signal-time UCL contract.",
        ),
        _source(
            "espn", CLSourceStatus.RESULT_ONLY,
            ("src/data/football_live.py",), "",
            identity=False, market=False, bookmakers="not applicable",
            timestamps="scoreboard update", provenance="event payload", quota="not applicable",
            in_play=False, notes="No 1X2 odds path.",
        ),
        _source(
            "cache", CLSourceStatus.CACHE_ONLY,
            ("src/data/cache.py",), "",
            identity=False, market=True, bookmakers="inherited/unknown",
            timestamps="unsafe unless source timestamp is retained", provenance="inherited",
            quota="not authoritative", in_play=False,
            notes="Never an independent provider or authority.",
        ),
        _source(
            "oddsportal", CLSourceStatus.DISABLED_LEGACY,
            ("src/football/odds/oddsportal.py",), "",
            identity=False, market=True, bookmakers="aggregate only",
            timestamps="no explicit source timestamp", provenance="scrape page",
            quota="not governed", in_play=False,
            notes="Public fetch is permanently disabled and is not a cascade adapter.",
        ),
        _source(
            "websearch", CLSourceStatus.DISABLED_LEGACY,
            ("src/football/odds/websearch.py",), "",
            identity=False, market=False, bookmakers="search-derived aggregate",
            timestamps="not reliable", provenance="search snippets", quota="not applicable",
            in_play=False, notes="Not a football odds authority; public fetch is disabled.",
        ),
    )


CL_SOURCE_MATRIX = compatibility_matrix()
UCL_SOURCE_MATRIX = CL_SOURCE_MATRIX


@dataclass(frozen=True)
class CLFixture:
    fixture_key: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    competition_code: str = CHAMPIONS_LEAGUE_CODE
    provider_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff_utc", _utc(self.kickoff_utc, "kickoff_utc"))

    def validate(self) -> None:
        _text(self.fixture_key, "fixture_key")
        if self.provider_event_id is not None:
            _text(self.provider_event_id, "provider_event_id")
        if self.competition_code != CHAMPIONS_LEAGUE_CODE:
            raise CLShadowContractError("fixture is outside Champions League scope")
        if _team(self.home_team) == _team(self.away_team):
            raise CLShadowContractError("fixture participants must be distinct")
        _text(self.home_team, "home_team")
        _text(self.away_team, "away_team")


@dataclass(frozen=True)
class CLProvenance:
    source: str
    source_uri: str
    raw_record_id: str
    adapter_version: str
    payload_digest: str

    def validate(self) -> None:
        for name, value in (
            ("source", self.source), ("source_uri", self.source_uri),
            ("raw_record_id", self.raw_record_id), ("adapter_version", self.adapter_version),
        ):
            _text(value, name)
        if _URI_RE.match(self.source_uri) is None:
            raise CLShadowContractError("source_uri must have an explicit scheme")
        if _SHA256_RE.fullmatch(self.payload_digest) is None:
            raise CLShadowContractError("payload_digest must be a SHA-256 hex digest")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self.__dict__.copy()


@dataclass(frozen=True)
class CLQuotaMetadata:
    provider: str
    quota_before_remaining: int | None
    quota_after_remaining: int | None
    quota_used: int | None = None
    rate_limit_remaining: int | None = None
    request_cost_units: float = 0.0
    quota_reset_at: datetime | None = None
    network_request_count: int = 0

    def __post_init__(self) -> None:
        if self.quota_reset_at is not None:
            object.__setattr__(self, "quota_reset_at", _utc(self.quota_reset_at, "quota_reset_at"))

    @property
    def quota_remaining(self) -> int | None:
        return self.quota_after_remaining

    def validate(self) -> None:
        _text(self.provider, "quota provider")
        for name, value in (
            ("quota_before_remaining", self.quota_before_remaining),
            ("quota_after_remaining", self.quota_after_remaining),
            ("quota_used", self.quota_used),
            ("rate_limit_remaining", self.rate_limit_remaining),
        ):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise CLShadowContractError(f"{name} must be non-negative")
        if not isfinite(float(self.request_cost_units)) or self.request_cost_units < 0:
            raise CLShadowContractError("request_cost_units must be non-negative")
        if isinstance(self.network_request_count, bool) or self.network_request_count < 0:
            raise CLShadowContractError("network_request_count must be non-negative")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "quota_before_remaining": self.quota_before_remaining,
            "quota_after_remaining": self.quota_after_remaining,
            "quota_remaining": self.quota_remaining,
            "quota_used": self.quota_used,
            "rate_limit_remaining": self.rate_limit_remaining,
            "request_cost_units": self.request_cost_units,
            "quota_reset_at": self.quota_reset_at.isoformat() if self.quota_reset_at else None,
            "network_request_count": self.network_request_count,
        }


@dataclass(frozen=True)
class CLShadowPolicy:
    """Bounded engineering replay timing, not a production timing decision."""

    maximum_odds_age_seconds: int = 900
    kickoff_tolerance_seconds: int = 300
    require_quota_metadata: bool = True

    def validate(self) -> None:
        if (
            isinstance(self.maximum_odds_age_seconds, bool)
            or not isinstance(self.maximum_odds_age_seconds, int)
            or self.maximum_odds_age_seconds <= 0
        ):
            raise CLShadowContractError("maximum_odds_age_seconds must be positive")
        if (
            isinstance(self.kickoff_tolerance_seconds, bool)
            or not isinstance(self.kickoff_tolerance_seconds, int)
            or self.kickoff_tolerance_seconds < 0
        ):
            raise CLShadowContractError("kickoff_tolerance_seconds must be non-negative")
        if not isinstance(self.require_quota_metadata, bool):
            raise CLShadowContractError("require_quota_metadata must be boolean")


@dataclass(frozen=True)
class CLShadowObservation:
    fixture_key: str
    competition_code: str
    provider_event_id: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    market_type: str
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    bookmaker_identity: str
    source_timestamp: datetime | None
    captured_at: datetime
    provider_identity: str
    request_identity: str
    provenance: CLProvenance | None
    quota: CLQuotaMetadata | None
    event_state: CLEventState = CLEventState.PRE_MATCH
    in_play: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff_utc", _utc(self.kickoff_utc, "kickoff_utc"))
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        if self.source_timestamp is not None:
            object.__setattr__(self, "source_timestamp", _utc(self.source_timestamp, "source_timestamp"))

    def validate(self, *, require_quota_metadata: bool = True) -> None:
        for name, value in (
            ("fixture_key", self.fixture_key), ("provider_event_id", self.provider_event_id),
            ("home_team", self.home_team), ("away_team", self.away_team),
            ("provider_identity", self.provider_identity), ("request_identity", self.request_identity),
            ("bookmaker_identity", self.bookmaker_identity),
        ):
            _text(value, name)
        if self.competition_code != CHAMPIONS_LEAGUE_CODE:
            raise CLShadowContractError("observation is outside Champions League scope")
        if self.market_type != REGULATION_1X2_MARKET:
            raise CLShadowContractError("only pre-match regulation 1X2 is accepted")
        if _team(self.home_team) == _team(self.away_team):
            raise CLShadowContractError("observation participants must be distinct")
        try:
            CLEventState(self.event_state)
        except (TypeError, ValueError) as exc:
            raise CLShadowContractError("event_state is invalid") from exc
        if not isinstance(self.in_play, bool):
            raise CLShadowContractError("in_play must be boolean")
        if self.source_timestamp is None:
            raise CLShadowContractError("source_timestamp is required")
        if self.source_timestamp > self.captured_at:
            raise CLShadowContractError("source_timestamp cannot be after captured_at")
        odds = (self.home_odds, self.draw_odds, self.away_odds)
        if any(value is None for value in odds):
            raise CLShadowContractError("complete regulation 1X2 odds are required")
        if any(not isfinite(float(value)) or float(value) <= 1.0 for value in odds):
            raise CLShadowContractError("odds must be finite decimal values greater than 1")
        if self.provenance is None:
            raise CLShadowContractError("provenance is required")
        self.provenance.validate()
        if require_quota_metadata and self.quota is None:
            raise CLShadowContractError("quota metadata is required")
        if self.quota is not None:
            self.quota.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_version": CL_SHADOW_CONTRACT_VERSION,
            "fixture_key": self.fixture_key,
            "competition_code": self.competition_code,
            "provider_event_id": self.provider_event_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff_utc": self.kickoff_utc.isoformat(),
            "market_type": self.market_type,
            "odds": {"home": self.home_odds, "draw": self.draw_odds, "away": self.away_odds},
            "bookmaker_identity": self.bookmaker_identity,
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "captured_at": self.captured_at.isoformat(),
            "provider_identity": self.provider_identity,
            "request_identity": self.request_identity,
            "provenance": self.provenance.as_payload() if self.provenance else None,
            "quota": self.quota.as_payload() if self.quota else None,
            "event_state": CLEventState(self.event_state).value,
            "in_play": self.in_play,
        }

    def digest(self) -> str:
        return _digest(self.as_payload())


__all__ = [
    "CHAMPIONS_LEAGUE_CODE", "CHAMPIONS_LEAGUE_SPORT_KEY", "REGULATION_1X2_MARKET",
    "CL_SHADOW_CONTRACT_VERSION", "CLShadowContractError", "CLReplayError",
    "CLSourceStatus", "CLEventState", "CLRejectionReason", "CLSourceCompatibility",
    "CL_SOURCE_MATRIX", "UCL_SOURCE_MATRIX", "compatibility_matrix", "CLFixture",
    "CLProvenance", "CLQuotaMetadata", "CLShadowPolicy", "CLShadowObservation",
    "CLObservationDecision", "CLReplayRejection", "CLReplayResult",
    "evaluate_observation", "replay_cl_shadow",
]

from src.football.provider_cascade.champions_league_shadow_replay import (  # noqa: E402
    CLObservationDecision,
    CLReplayRejection,
    CLReplayResult,
    evaluate_observation,
    replay_cl_shadow,
)
