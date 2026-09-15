"""Provider adapters for the Top-5 cascade.

Each adapter owns authentication, endpoint details, and payload parsing.  The
router only receives :class:`NormalizedOddsObservation` or a safe failure
classification.  The cascade router blocks the built-in transports unless a
controlled shadow run explicitly authorizes them; tests inject transports.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any, ClassVar, Protocol

import requests

from src.config import ODDS_API_URL
from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
    digest_record,
)
from src.football.top5_real_shadow_provider import (
    requests_transport as legacy_requests_transport,
)

ODDS_API_IO_URL = "https://api.odds-api.io/v3"
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
BETFAIR_BETTING_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"


@dataclass(frozen=True)
class ProviderRequest:
    """A request description.  It may contain credentials in memory only."""

    provider: str
    endpoint: str
    params: Mapping[str, str]
    headers: Mapping[str, str]
    json_body: object | None = None

    def safe_payload(self) -> dict[str, object]:
        """Return request metadata with all auth-bearing fields removed."""

        sensitive = {
            "apikey",
            "api_key",
            "x-application",
            "x-authentication",
            "authorization",
        }
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "params": {
                key: "[REDACTED]" if key.lower() in sensitive else value
                for key, value in self.params.items()
            },
            "headers": {
                key: "[REDACTED]" if key.lower() in sensitive else value
                for key, value in self.headers.items()
            },
            "json_body": "[REDACTED]" if self.json_body is not None else None,
        }


@dataclass(frozen=True)
class RawProviderResponse:
    status_code: int | None
    payload: object | None
    headers: Mapping[str, str]
    started_at: datetime
    completed_at: datetime
    latency_ms: int
    timeout: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "started_at", _utc(self.started_at, "request started_at")
        )
        object.__setattr__(
            self, "completed_at", _utc(self.completed_at, "request completed_at")
        )
        if self.completed_at < self.started_at or self.latency_ms < 0:
            raise ProductionContractError("provider response timing is invalid")


HttpTransport = Callable[[ProviderRequest, float], RawProviderResponse]
LegacyTransport = Callable[[str, tuple[str, ...], tuple[str, ...], str, float], Any]


def requests_transport(
    request: ProviderRequest, timeout_seconds: float
) -> RawProviderResponse:
    """Perform one request without logging URL parameters, headers, or body."""

    started_at = datetime.now(timezone.utc)
    try:
        response = requests.request(
            "POST" if request.json_body is not None else "GET",
            request.endpoint,
            params=dict(request.params),
            headers=dict(request.headers),
            json=request.json_body,
            timeout=timeout_seconds,
        )
    except requests.Timeout:
        completed_at = datetime.now(timezone.utc)
        return RawProviderResponse(
            None,
            None,
            {},
            started_at,
            completed_at,
            round((completed_at - started_at).total_seconds() * 1000),
            True,
            "timeout",
        )
    except requests.RequestException:
        completed_at = datetime.now(timezone.utc)
        return RawProviderResponse(
            None,
            None,
            {},
            started_at,
            completed_at,
            round((completed_at - started_at).total_seconds() * 1000),
            False,
            "request_error",
        )
    completed_at = datetime.now(timezone.utc)
    try:
        payload = response.json()
        error_code = None
    except ValueError:
        payload = None
        error_code = "malformed_json"
    return RawProviderResponse(
        response.status_code,
        payload,
        {str(key): str(value) for key, value in response.headers.items()},
        started_at,
        completed_at,
        round((completed_at - started_at).total_seconds() * 1000),
        False,
        error_code,
    )


@dataclass(frozen=True)
class AdapterResult:
    state: ProviderState
    reason: str
    observation: NormalizedOddsObservation | None = None
    status_code: int | None = None
    network_called: bool = True
    latency_ms: int = 0
    quota_after: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    rate_limit_state: QuotaSnapshot = field(default_factory=QuotaSnapshot)

    def validate(self) -> None:
        if not self.reason.strip() or self.latency_ms < 0:
            raise ProductionContractError("adapter result is missing safe fields")
        if self.observation is not None:
            self.observation.validate(require_fresh=False)
            if self.state is not ProviderState.AVAILABLE:
                raise ProductionContractError(
                    "failed adapter result cannot contain an observation"
                )


class OddsProviderAdapter(Protocol):
    name: str

    def fetch(
        self,
        fixture: Fixture,
        config: ProviderConfig,
        *,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        provider_fixture_id: str | None = None,
    ) -> AdapterResult: ...


class _BaseAdapter:
    name: str
    credential_names: tuple[str, ...]

    def __init__(
        self,
        *,
        transport: HttpTransport = requests_transport,
        aliases: Mapping[str, str] | None = None,
    ):
        self._transport = transport
        self._aliases = dict(aliases or {})

    def _credentials(self, config: ProviderConfig) -> tuple[str, ...] | None:
        names = config.credential_env or self.credential_names
        if not config.credentials_required:
            return tuple("" for _ in (names or ("injected",)))
        values = tuple(os.getenv(name, "") for name in names)
        if any(not value.strip() for value in values):
            return None
        return values

    def _request(
        self, request: ProviderRequest, config: ProviderConfig
    ) -> RawProviderResponse:
        return self._transport(request, config.timeout_seconds)

    def _base_result(
        self, response: RawProviderResponse, state: ProviderState, reason: str
    ) -> AdapterResult:
        response_quota = _quota_from_headers(response.headers)
        return AdapterResult(
            state=state,
            reason=reason,
            status_code=response.status_code,
            network_called=True,
            latency_ms=response.latency_ms,
            quota_after=response_quota,
            rate_limit_state=response_quota,
        )

    def _credential_result(self) -> AdapterResult:
        return AdapterResult(
            ProviderState.CREDENTIAL_MISSING, "credential_missing", network_called=False
        )

    def _classify_response(self, response: RawProviderResponse) -> AdapterResult | None:
        if response.timeout or response.error_code == "timeout":
            return self._base_result(
                response, ProviderState.TEMPORARILY_UNAVAILABLE, "timeout"
            )
        if response.error_code == "request_error":
            return self._base_result(
                response, ProviderState.TEMPORARILY_UNAVAILABLE, "network_error"
            )
        if response.error_code == "malformed_json":
            return self._base_result(
                response, ProviderState.MALFORMED, "malformed_json"
            )
        status = response.status_code
        if status in (401, 403):
            return self._base_result(
                response, ProviderState.AUTH_FAILED, f"http_{status}"
            )
        if status == 404:
            return self._base_result(
                response, ProviderState.UNSUPPORTED_FIXTURE, "http_404"
            )
        if status == 422:
            return self._base_result(
                response, ProviderState.UNSUPPORTED_MARKET, "http_422"
            )
        if status == 429:
            return self._base_result(response, ProviderState.RATE_LIMITED, "http_429")
        if status is not None and 500 <= status <= 599:
            return self._base_result(
                response, ProviderState.TEMPORARILY_UNAVAILABLE, f"http_{status}"
            )
        if status != 200:
            return self._base_result(
                response, ProviderState.MALFORMED, f"http_{status or 'no_status'}"
            )
        return None


class TheOddsAPIAdapter(_BaseAdapter):
    """The Odds API v4 adapter, reusing the existing SportsBrain transport."""

    name = "the_odds_api"
    credential_names = ("ODDS_API_KEY",)
    sport_keys: ClassVar[dict[str, str]] = {
        "BL1": "soccer_germany_bundesliga",
        "EPL": "soccer_epl",
        "LL": "soccer_spain_la_liga",
        "SA": "soccer_italy_serie_a",
        "L1": "soccer_france_ligue_1",
    }

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        legacy_transport: LegacyTransport | None = None,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(transport=transport or requests_transport, aliases=aliases)
        self._legacy_transport = (
            legacy_transport
            if legacy_transport is not None
            else (legacy_requests_transport if transport is None else None)
        )

    def fetch(
        self,
        fixture: Fixture,
        config: ProviderConfig,
        *,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        provider_fixture_id: str | None = None,
    ) -> AdapterResult:
        key_values = self._credentials(config)
        if key_values is None:
            return self._credential_result()
        sport_key = self.sport_keys.get(fixture.league_code)
        if sport_key is None:
            return AdapterResult(
                ProviderState.UNSUPPORTED_LEAGUE,
                "unsupported_league",
                network_called=False,
            )
        requested_at = _utc(requested_at, "requested_at")
        if self._legacy_transport is not None:
            legacy = self._legacy_transport(
                sport_key, ("h2h",), ("eu",), key_values[0], config.timeout_seconds
            )
            response = _legacy_to_raw(legacy, requested_at)
        else:
            request = ProviderRequest(
                self.name,
                f"{ODDS_API_URL}/sports/{sport_key}/odds",
                {
                    "apiKey": key_values[0],
                    "regions": "eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                {"Accept": "application/json"},
            )
            response = self._request(request, config)
        classified = self._classify_response(response)
        if classified is not None:
            return classified
        if not isinstance(response.payload, list):
            return self._base_result(
                response, ProviderState.MALFORMED, "payload_not_list"
            )
        for event in response.payload:
            if not isinstance(event, Mapping):
                continue
            if (
                provider_fixture_id
                and str(event.get("id", "")).strip() != provider_fixture_id
            ):
                continue
            identity = _event_identity(
                fixture,
                league_value=event.get("sport_key"),
                home=event.get("home_team"),
                away=event.get("away_team"),
                kickoff=event.get("commence_time"),
                aliases=self._aliases,
                expected_league=sport_key,
            )
            if identity != "match":
                continue
            observation = _the_odds_observation(
                fixture,
                event,
                request_identity=request_identity,
                requested_at=requested_at,
                provider_priority=provider_priority,
                config=config,
                response=response,
                aliases=self._aliases,
            )
            if observation is not None:
                return AdapterResult(
                    ProviderState.AVAILABLE,
                    "accepted",
                    observation,
                    response.status_code,
                    True,
                    response.latency_ms,
                    _quota_from_headers(response.headers),
                    _quota_from_headers(response.headers),
                )
            return self._base_result(
                response,
                ProviderState.QUALITY_REJECTED,
                "fixture_or_market_quality_rejected",
            )
        return self._base_result(
            response,
            ProviderState.UNSUPPORTED_FIXTURE,
            "fixture_not_found_or_identity_mismatch",
        )


class OddsApiIoAdapter(_BaseAdapter):
    """Odds-API.io v3 adapter using the documented multi-event odds endpoint."""

    name = "odds_api_io"
    credential_names = ("ODDS_API_IO_KEY",)

    def discovery_request(
        self, fixture: Fixture, config: ProviderConfig
    ) -> ProviderRequest | None:
        credentials = self._credentials(config)
        if credentials is None:
            return None
        return ProviderRequest(
            self.name,
            f"{ODDS_API_IO_URL}/events/search",
            {
                "apiKey": credentials[0],
                "query": f"{fixture.home_team} {fixture.away_team}",
                "status": "upcoming",
            },
            {"Accept": "application/json"},
        )

    def fetch(
        self,
        fixture: Fixture,
        config: ProviderConfig,
        *,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        provider_fixture_id: str | None = None,
    ) -> AdapterResult:
        credentials = self._credentials(config)
        if credentials is None:
            return self._credential_result()
        if not provider_fixture_id:
            return AdapterResult(
                ProviderState.UNSUPPORTED_FIXTURE,
                "provider_fixture_id_required_for_single_odds_call",
                network_called=False,
            )
        if not config.bookmakers:
            return AdapterResult(
                ProviderState.UNSUPPORTED_MARKET,
                "bookmaker_allowlist_required_for_odds_api_io",
                network_called=False,
            )
        request = ProviderRequest(
            self.name,
            f"{ODDS_API_IO_URL}/odds/multi",
            {
                "apiKey": credentials[0],
                "eventIds": provider_fixture_id,
                "bookmakers": ",".join(config.bookmakers),
                "markets": "ML",
            },
            {"Accept": "application/json"},
        )
        response = self._request(request, config)
        classified = self._classify_response(response)
        if classified is not None:
            return classified
        events = (
            response.payload
            if isinstance(response.payload, list)
            else [response.payload]
        )
        if not events:
            return self._base_result(
                response, ProviderState.UNSUPPORTED_FIXTURE, "empty_payload"
            )
        for event in events:
            if (
                not isinstance(event, Mapping)
                or str(event.get("id", "")).strip() != provider_fixture_id
            ):
                continue
            identity = _event_identity(
                fixture,
                home=event.get("home"),
                away=event.get("away"),
                kickoff=event.get("date"),
                aliases=self._aliases,
            )
            if identity != "match":
                return self._base_result(
                    response, _identity_state(identity), f"identity_{identity}"
                )
            observation = _odds_api_io_observation(
                fixture,
                event,
                request_identity=request_identity,
                requested_at=_utc(requested_at, "requested_at"),
                provider_priority=provider_priority,
                config=config,
                response=response,
                aliases=self._aliases,
            )
            if observation is not None:
                return AdapterResult(
                    ProviderState.AVAILABLE,
                    "accepted",
                    observation,
                    response.status_code,
                    True,
                    response.latency_ms,
                    _quota_from_headers(response.headers),
                    _quota_from_headers(response.headers),
                )
            return self._base_result(
                response,
                ProviderState.QUALITY_REJECTED,
                "fixture_or_market_quality_rejected",
            )
        return self._base_result(
            response, ProviderState.UNSUPPORTED_FIXTURE, "event_id_not_returned"
        )


class ApiFootballAdapter(_BaseAdapter):
    """API-Football v3 pre-match odds adapter.

    The public response contract does not guarantee a per-quote source update
    timestamp.  This adapter therefore rejects responses without one instead
    of treating HTTP capture time or kickoff time as odds freshness.
    """

    name = "api_football"
    credential_names = ("API_FOOTBALL_KEY",)
    league_ids: ClassVar[dict[str, str]] = {
        "BL1": "78",
        "EPL": "39",
        "LL": "140",
        "SA": "135",
        "L1": "61",
    }

    def discovery_request(
        self, fixture: Fixture, config: ProviderConfig
    ) -> ProviderRequest | None:
        credentials = self._credentials(config)
        league_id = self.league_ids.get(fixture.league_code)
        if credentials is None or league_id is None:
            return None
        return ProviderRequest(
            self.name,
            f"{API_FOOTBALL_URL}/fixtures",
            {
                "league": league_id,
                "season": str(fixture.kickoff.year),
                "date": fixture.kickoff.date().isoformat(),
            },
            {"x-apisports-key": credentials[0], "Accept": "application/json"},
        )

    def fetch(
        self,
        fixture: Fixture,
        config: ProviderConfig,
        *,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        provider_fixture_id: str | None = None,
    ) -> AdapterResult:
        credentials = self._credentials(config)
        if credentials is None:
            return self._credential_result()
        if not provider_fixture_id:
            return AdapterResult(
                ProviderState.UNSUPPORTED_FIXTURE,
                "provider_fixture_id_required_for_odds_call",
                network_called=False,
            )
        request = ProviderRequest(
            self.name,
            f"{API_FOOTBALL_URL}/odds",
            {"fixture": provider_fixture_id, "page": "1"},
            {"x-apisports-key": credentials[0], "Accept": "application/json"},
        )
        response = self._request(request, config)
        classified = self._classify_response(response)
        if classified is not None:
            return classified
        if not isinstance(response.payload, Mapping):
            return self._base_result(
                response, ProviderState.MALFORMED, "payload_not_object"
            )
        if _paging_total(response.payload) > 1:
            return self._base_result(
                response,
                ProviderState.PARTIAL,
                "odds_response_pagination_not_fully_read",
            )
        entries = response.payload.get("response")
        if not isinstance(entries, list) or not entries:
            return self._base_result(
                response, ProviderState.UNSUPPORTED_FIXTURE, "fixture_has_no_odds"
            )
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            identity = _api_football_identity(
                fixture, entry, provider_fixture_id, self._aliases
            )
            if identity != "match":
                return self._base_result(
                    response, _identity_state(identity), f"identity_{identity}"
                )
            observation = _api_football_observation(
                fixture,
                entry,
                request_identity=request_identity,
                requested_at=_utc(requested_at, "requested_at"),
                provider_priority=provider_priority,
                config=config,
                response=response,
                aliases=self._aliases,
            )
            if observation is not None:
                return AdapterResult(
                    ProviderState.AVAILABLE,
                    "accepted",
                    observation,
                    response.status_code,
                    True,
                    response.latency_ms,
                    _quota_from_headers(response.headers),
                    _quota_from_headers(response.headers),
                )
            return self._base_result(
                response, ProviderState.STALE, "missing_or_invalid_source_timestamp"
            )
        return self._base_result(
            response, ProviderState.UNSUPPORTED_FIXTURE, "fixture_not_found"
        )


class BetfairDelayedAdapter(_BaseAdapter):
    """Betfair Exchange API adapter restricted to the delayed app-key path."""

    name = "betfair_delayed"
    credential_names = ("BETFAIR_APP_KEY", "BETFAIR_SESSION_TOKEN")

    def discovery_request(
        self, fixture: Fixture, config: ProviderConfig
    ) -> ProviderRequest | None:
        credentials = self._credentials(config)
        if credentials is None:
            return None
        body = {
            "jsonrpc": "2.0",
            "method": "SportsAPING/v1.0/listMarketCatalogue",
            "params": {
                "filter": {"eventTypeIds": ["1"]},
                "marketProjection": [
                    "EVENT",
                    "COMPETITION",
                    "MARKET_START_TIME",
                    "RUNNER_DESCRIPTION",
                ],
                "maxResults": 100,
            },
            "id": 1,
        }
        return ProviderRequest(
            self.name,
            BETFAIR_BETTING_URL,
            {},
            {
                "X-Application": credentials[0],
                "X-Authentication": credentials[1],
                "Accept": "application/json",
            },
            body,
        )

    def fetch(
        self,
        fixture: Fixture,
        config: ProviderConfig,
        *,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        provider_fixture_id: str | None = None,
    ) -> AdapterResult:
        credentials = self._credentials(config)
        if credentials is None:
            return self._credential_result()
        if not provider_fixture_id:
            return AdapterResult(
                ProviderState.UNSUPPORTED_FIXTURE,
                "market_id_required_for_delayed_book_call",
                network_called=False,
            )
        body = {
            "jsonrpc": "2.0",
            "method": "SportsAPING/v1.0/listMarketBook",
            "params": {
                "marketIds": [provider_fixture_id],
                "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
            },
            "id": 1,
        }
        request = ProviderRequest(
            self.name,
            BETFAIR_BETTING_URL,
            {},
            {
                "X-Application": credentials[0],
                "X-Authentication": credentials[1],
                "Accept": "application/json",
            },
            body,
        )
        response = self._request(request, config)
        classified = self._classify_response(response)
        if classified is not None:
            return classified
        if not isinstance(response.payload, Mapping):
            return self._base_result(
                response, ProviderState.MALFORMED, "payload_not_object"
            )
        if response.payload.get("error") or response.payload.get("errorCode"):
            return self._base_result(
                response, ProviderState.AUTH_FAILED, "betfair_api_error"
            )
        result = response.payload.get("result")
        books = result if isinstance(result, list) else []
        for book in books:
            if (
                not isinstance(book, Mapping)
                or str(book.get("marketId", "")) != provider_fixture_id
            ):
                continue
            observation = _betfair_observation(
                fixture,
                book,
                request_identity=request_identity,
                requested_at=_utc(requested_at, "requested_at"),
                provider_priority=provider_priority,
                config=config,
                response=response,
                aliases=self._aliases,
            )
            if observation is not None:
                return AdapterResult(
                    ProviderState.AVAILABLE,
                    "accepted_delayed_observation",
                    observation,
                    response.status_code,
                    True,
                    response.latency_ms,
                    _quota_from_headers(response.headers),
                    _quota_from_headers(response.headers),
                )
            return self._base_result(
                response,
                ProviderState.QUALITY_REJECTED,
                "delayed_market_quality_rejected",
            )
        return self._base_result(
            response, ProviderState.UNSUPPORTED_FIXTURE, "market_id_not_returned"
        )


def _legacy_to_raw(response: Any, requested_at: datetime) -> RawProviderResponse:
    completed = _utc(
        getattr(response, "completed_at", requested_at), "legacy completed_at"
    )
    latency = int(getattr(response, "latency_ms", 0))
    return RawProviderResponse(
        getattr(response, "status_code", None),
        getattr(response, "payload", None),
        getattr(response, "headers", {}),
        requested_at,
        completed,
        latency,
        bool(getattr(response, "timeout", False)),
        getattr(response, "error_code", None),
    )


def _quota_from_headers(headers: Mapping[str, object]) -> QuotaSnapshot:
    lowered = {str(key).lower(): str(value).strip() for key, value in headers.items()}

    def integer(*names: str) -> int | None:
        for name in names:
            raw = lowered.get(name.lower())
            if raw is not None:
                try:
                    value = int(raw)
                except ValueError:
                    return None
                return value if value >= 0 else None
        return None

    return QuotaSnapshot(
        used=integer("x-requests-used", "x-ratelimit-requests-used"),
        remaining=integer(
            "x-requests-remaining",
            "x-ratelimit-requests-remaining",
            "x-ratelimit-remaining",
        ),
        rate_limit=integer("x-ratelimit-limit", "x-rate-limit-limit"),
        rate_remaining=integer("x-ratelimit-remaining", "x-rate-limit-remaining"),
    )


def _team_key(value: object, aliases: Mapping[str, str]) -> str:
    raw = str(value or "").strip()
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", raw).casefold()
        if not unicodedata.combining(char) and char.isalnum()
    )
    alias = aliases.get(raw) or aliases.get(normalized)
    if alias:
        normalized = "".join(char for char in alias.casefold() if char.isalnum())
    return normalized


def _parse_timestamp(value: object, *, now: datetime) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, (int, float)) and isfinite(float(value)):
        epoch_seconds = float(value)
        if epoch_seconds > 100_000_000_000:
            epoch_seconds /= 1000
        try:
            parsed = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return parsed if parsed <= now else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return parsed if parsed <= now else None


def _parse_kickoff(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and isfinite(float(value)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _event_identity(
    fixture: Fixture,
    *,
    league_value: object = None,
    home: object,
    away: object,
    kickoff: object,
    aliases: Mapping[str, str],
    expected_league: str | None = None,
) -> str:
    if (
        expected_league is not None
        and league_value is not None
        and str(league_value).strip() != expected_league
    ):
        return "wrong_league"
    home_key = _team_key(home, aliases)
    away_key = _team_key(away, aliases)
    expected_home = _team_key(fixture.home_team, aliases)
    expected_away = _team_key(fixture.away_team, aliases)
    if not home_key or not away_key:
        return "malformed"
    if home_key == expected_away and away_key == expected_home:
        return "swapped_home_away"
    if (home_key, away_key) != (expected_home, expected_away):
        return "wrong_fixture"
    parsed_kickoff = _parse_kickoff(kickoff)
    if parsed_kickoff is None:
        return "malformed"
    if abs((parsed_kickoff - fixture.kickoff).total_seconds()) > 90:
        return "kickoff_mismatch"
    return "match"


def _identity_state(identity: str) -> ProviderState:
    return {
        "wrong_league": ProviderState.UNSUPPORTED_LEAGUE,
        "swapped_home_away": ProviderState.QUALITY_REJECTED,
        "kickoff_mismatch": ProviderState.QUALITY_REJECTED,
        "malformed": ProviderState.MALFORMED,
        "wrong_fixture": ProviderState.UNSUPPORTED_FIXTURE,
    }.get(identity, ProviderState.QUALITY_REJECTED)


def _source_timestamp(event: Mapping[str, object], *, now: datetime) -> datetime | None:
    bookmakers = event.get("bookmakers")
    if not isinstance(bookmakers, list):
        return None
    timestamps: list[datetime] = []
    for bookmaker in bookmakers:
        if not isinstance(bookmaker, Mapping):
            continue
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if isinstance(market, Mapping) and market.get("key") == "h2h":
                timestamp = _parse_timestamp(
                    market.get("last_update") or bookmaker.get("last_update"), now=now
                )
                if timestamp is not None:
                    timestamps.append(timestamp)
    return min(timestamps) if timestamps else None


def _decimal(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) and result > 1.0 else None


def _the_odds_observation(
    fixture: Fixture,
    event: Mapping[str, object],
    *,
    request_identity: str,
    requested_at: datetime,
    provider_priority: int,
    config: ProviderConfig,
    response: RawProviderResponse,
    aliases: Mapping[str, str],
) -> NormalizedOddsObservation | None:
    captured = response.completed_at
    bookmakers = event.get("bookmakers")
    if not isinstance(bookmakers, list):
        return None
    selected: tuple[str, dict[str, float], datetime] | None = None
    expected = {
        _team_key(fixture.home_team, aliases): "home",
        _team_key(fixture.away_team, aliases): "away",
        "draw": "draw",
    }
    for bookmaker in sorted(
        (bookmaker for bookmaker in bookmakers if isinstance(bookmaker, Mapping)),
        key=lambda value: str(value.get("key", "")),
    ):
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, Mapping) or market.get("key") != "h2h":
                continue
            source = _parse_timestamp(
                market.get("last_update") or bookmaker.get("last_update"), now=captured
            )
            outcomes = market.get("outcomes")
            if source is None or not isinstance(outcomes, list):
                continue
            odds: dict[str, float] = {}
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    continue
                label = _team_key(outcome.get("name", ""), aliases)
                key = expected.get(label)
                price = _decimal(outcome.get("price"))
                if key is not None and price is not None:
                    odds[key] = price
            if set(odds) == {"home", "draw", "away"}:
                selected = (
                    str(bookmaker.get("key") or bookmaker.get("title") or "unknown"),
                    odds,
                    source,
                )
                break
        if selected is not None:
            break
    if selected is None:
        return None
    bookmaker, odds, source_timestamp = selected
    return _observation(
        fixture,
        provider_fixture_id=str(event.get("id", "")),
        provider_identity="the_odds_api",
        bookmaker_identity=bookmaker,
        odds=odds,
        source_timestamp=source_timestamp,
        captured_at=captured,
        request_identity=request_identity,
        requested_at=requested_at,
        provider_priority=provider_priority,
        config=config,
        response=response,
        source_provenance="the_odds_api:v4:/sports/{sport}/odds; bookmaker.last_update/market.last_update",
        record=event,
    )


def _odds_api_io_observation(
    fixture: Fixture,
    event: Mapping[str, object],
    *,
    request_identity: str,
    requested_at: datetime,
    provider_priority: int,
    config: ProviderConfig,
    response: RawProviderResponse,
    aliases: Mapping[str, str],
) -> NormalizedOddsObservation | None:
    bookmakers = event.get("bookmakers")
    if not isinstance(bookmakers, Mapping):
        return None
    for bookmaker_name in sorted(str(value) for value in bookmakers):
        markets = bookmakers.get(bookmaker_name)
        if not isinstance(markets, list):
            continue
        for market in markets:
            if (
                not isinstance(market, Mapping)
                or str(market.get("name", "")).casefold() != "ml"
            ):
                continue
            odds_rows = market.get("odds")
            if (
                not isinstance(odds_rows, list)
                or not odds_rows
                or not isinstance(odds_rows[0], Mapping)
            ):
                continue
            row = odds_rows[0]
            odds = {key: _decimal(row.get(key)) for key in ("home", "draw", "away")}
            source = _parse_timestamp(
                market.get("updatedAt"), now=response.completed_at
            )
            if source is None or any(value is None for value in odds.values()):
                continue
            return _observation(
                fixture,
                provider_fixture_id=str(event.get("id", "")),
                provider_identity="odds_api_io",
                bookmaker_identity=bookmaker_name,
                odds={key: value for key, value in odds.items() if value is not None},
                source_timestamp=source,
                captured_at=response.completed_at,
                request_identity=request_identity,
                requested_at=requested_at,
                provider_priority=provider_priority,
                config=config,
                response=response,
                source_provenance="odds_api_io:v3:/odds/multi; market.updatedAt",
                record=event,
            )
    return None


def _paging_total(payload: Mapping[str, object]) -> int:
    paging = payload.get("paging")
    if not isinstance(paging, Mapping):
        return 1
    try:
        return max(1, int(paging.get("total", 1)))
    except (TypeError, ValueError):
        return 1


def _api_football_identity(
    fixture: Fixture,
    entry: Mapping[str, object],
    provider_fixture_id: str,
    aliases: Mapping[str, str],
) -> str:
    fixture_meta = entry.get("fixture")
    teams = entry.get("teams")
    if (
        not isinstance(fixture_meta, Mapping)
        or str(fixture_meta.get("id", "")) != provider_fixture_id
    ):
        return "wrong_fixture"
    if not isinstance(teams, Mapping):
        return "malformed"
    home = teams.get("home")
    away = teams.get("away")
    if not isinstance(home, Mapping) or not isinstance(away, Mapping):
        return "malformed"
    return _event_identity(
        fixture,
        home=home.get("name"),
        away=away.get("name"),
        kickoff=fixture_meta.get("date"),
        aliases=aliases,
    )


def _api_football_observation(
    fixture: Fixture,
    entry: Mapping[str, object],
    *,
    request_identity: str,
    requested_at: datetime,
    provider_priority: int,
    config: ProviderConfig,
    response: RawProviderResponse,
    aliases: Mapping[str, str],
) -> NormalizedOddsObservation | None:
    bookmakers = entry.get("bookmakers")
    if not isinstance(bookmakers, list):
        return None
    for bookmaker in sorted(
        (value for value in bookmakers if isinstance(value, Mapping)),
        key=lambda value: (str(value.get("id", "")), str(value.get("name", ""))),
    ):
        bets = bookmaker.get("bets")
        if not isinstance(bets, list):
            continue
        for bet in bets:
            if not isinstance(bet, Mapping):
                continue
            name = str(bet.get("name", "")).casefold()
            if str(bet.get("id", "")) != "1" and name not in {"match winner", "1x2"}:
                continue
            values = bet.get("values")
            if not isinstance(values, list):
                continue
            odds: dict[str, float] = {}
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                label = _team_key(value.get("value", ""), aliases)
                key = (
                    "home"
                    if label in {_team_key(fixture.home_team, aliases), "home"}
                    else (
                        "away"
                        if label in {_team_key(fixture.away_team, aliases), "away"}
                        else ("draw" if label == "draw" else None)
                    )
                )
                price = _decimal(value.get("odd"))
                if key is not None and price is not None:
                    odds[key] = price
            source_value = (
                entry.get("updatedAt")
                or entry.get("lastUpdate")
                or bookmaker.get("updatedAt")
                or bookmaker.get("last_update")
            )
            source = _parse_timestamp(source_value, now=response.completed_at)
            if source is None or set(odds) != {"home", "draw", "away"}:
                continue
            return _observation(
                fixture,
                provider_fixture_id=str(entry.get("fixture", {}).get("id", "")),
                provider_identity="api_football",
                bookmaker_identity=str(
                    bookmaker.get("name") or bookmaker.get("id") or "unknown"
                ),
                odds=odds,
                source_timestamp=source,
                captured_at=response.completed_at,
                request_identity=request_identity,
                requested_at=requested_at,
                provider_priority=provider_priority,
                config=config,
                response=response,
                source_provenance="api_football:v3:/odds; explicit updatedAt/lastUpdate only",
                record=entry,
            )
    return None


def _betfair_observation(
    fixture: Fixture,
    book: Mapping[str, object],
    *,
    request_identity: str,
    requested_at: datetime,
    provider_priority: int,
    config: ProviderConfig,
    response: RawProviderResponse,
    aliases: Mapping[str, str],
) -> NormalizedOddsObservation | None:
    if book.get("isMarketDataDelayed") is not True:
        return None
    definition = book.get("marketDefinition")
    definition_runners = (
        definition.get("runners") if isinstance(definition, Mapping) else None
    )
    runners = book.get("runners")
    if not isinstance(definition_runners, list) or not isinstance(runners, list):
        return None
    names: dict[str, str] = {}
    for runner in definition_runners:
        if isinstance(runner, Mapping):
            names[str(runner.get("id"))] = str(runner.get("name", ""))
    odds: dict[str, float] = {}
    expected_home = _team_key(fixture.home_team, aliases)
    expected_away = _team_key(fixture.away_team, aliases)
    for runner in runners:
        if not isinstance(runner, Mapping):
            continue
        label = names.get(str(runner.get("selectionId")), str(runner.get("name", "")))
        normalized = _team_key(label, aliases)
        key = (
            "home"
            if normalized == expected_home
            else "away"
            if normalized == expected_away
            else "draw"
            if normalized == "draw"
            else None
        )
        ex = runner.get("ex")
        available = ex.get("availableToBack") if isinstance(ex, Mapping) else None
        price = (
            _decimal(available[0].get("price"))
            if isinstance(available, list)
            and available
            and isinstance(available[0], Mapping)
            else None
        )
        if key is not None and price is not None:
            if key in odds:
                return None
            odds[key] = price
    source = _parse_timestamp(
        book.get("publishTime")
        or book.get("publish_time")
        or book.get("lastMatchTime"),
        now=response.completed_at,
    )
    if source is None or set(odds) != {"home", "draw", "away"}:
        return None
    delay_value = book.get("delaySeconds") or book.get("delay_seconds")
    try:
        delay_seconds = int(delay_value) if delay_value is not None else None
    except (TypeError, ValueError):
        delay_seconds = None
    return _observation(
        fixture,
        provider_fixture_id=str(book.get("marketId", "")),
        provider_identity="betfair_delayed",
        bookmaker_identity="betfair_exchange",
        odds=odds,
        source_timestamp=source,
        captured_at=response.completed_at,
        request_identity=request_identity,
        requested_at=requested_at,
        provider_priority=provider_priority,
        config=config,
        response=response,
        source_provenance="betfair_delayed:Betting API listMarketBook; publishTime; delayed app key",
        record=book,
        delayed=True,
        delay_seconds=delay_seconds,
        metadata={
            "delay_semantics": "Betfair Delayed App Key; 1-180 second snapshots per official contract"
        },
    )


def _observation(
    fixture: Fixture,
    *,
    provider_fixture_id: str,
    provider_identity: str,
    bookmaker_identity: str,
    odds: Mapping[str, float],
    source_timestamp: datetime,
    captured_at: datetime,
    request_identity: str,
    requested_at: datetime,
    provider_priority: int,
    config: ProviderConfig,
    response: RawProviderResponse,
    source_provenance: str,
    record: object,
    delayed: bool = False,
    delay_seconds: int | None = None,
    metadata: Mapping[str, object] | None = None,
) -> NormalizedOddsObservation:
    observation = NormalizedOddsObservation(
        league_code=fixture.league_code,
        fixture_key=fixture.fixture_key,
        provider_fixture_id=provider_fixture_id,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        kickoff_utc=fixture.kickoff,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=float(odds["home"]),
        draw_odds=float(odds["draw"]),
        away_odds=float(odds["away"]),
        provider_identity=provider_identity,
        bookmaker_identity=bookmaker_identity,
        source_timestamp=_utc(source_timestamp, "source_timestamp"),
        captured_at=_utc(captured_at, "captured_at"),
        request_identity=request_identity,
        request_started_at=_utc(requested_at, "request_started_at"),
        request_completed_at=_utc(response.completed_at, "request_completed_at"),
        latency_ms=response.latency_ms,
        provider_priority=provider_priority,
        fallback_depth=0,
        quota_state_after=_quota_from_headers(response.headers),
        rate_limit_state=_quota_from_headers(response.headers),
        source_provenance=source_provenance,
        raw_record_digest=digest_record(record),
        adapter_version=config.adapter_version,
        completeness=ObservationCompleteness.COMPLETE,
        error_classification=ProviderState.AVAILABLE,
        candidate_only=config.candidate_only or not config.quality_eligible,
        delayed=delayed,
        delay_seconds=delay_seconds,
        metadata=metadata or {},
    )
    observation.validate(require_fresh=False)
    return observation
