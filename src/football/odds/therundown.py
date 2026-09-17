"""Experimental TheRundown V2 adapter for UEFA Champions League odds.

This module deliberately sits outside the active provider cascade.  It uses
the existing provider-neutral observation/result contracts, but its provider
identity is candidate-only and it is not registered as Football authority.
The adapter can be exercised with an injected transport for deterministic
tests or with the existing HTTP transport after an explicit controlled-shadow
authorization is supplied by the caller.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import ClassVar

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.adapters import (
    AdapterResult,
    HttpTransport,
    ProviderRequest,
    RawProviderResponse,
    requests_transport,
)
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
    TransportCapability,
    digest_record,
)

THERUNDOWN_BASE_URL = "https://therundown.io/api/v2"
THERUNDOWN_PROVIDER_NAME = "therundown_experimental"
THERUNDOWN_CHAMPIONS_LEAGUE_SPORT_ID = 16
THERUNDOWN_CHAMPIONS_LEAGUE_CODE = "UEFA.CHAMP"
THERUNDOWN_MONEYLINE_MARKET_ID = 1
THERUNDOWN_ADAPTER_VERSION = "therundown-v2-experimental:1"
THERUNDOWN_CREDENTIAL_ENV = "THERUNDOWN_API_KEY"
THERUNDOWN_OFF_BOARD_SENTINEL = 0.0001

_CHAMPIONS_LEAGUE_NAMES = frozenset(
    {
        "championsleague",
        "uefachampionsleague",
        "uefachamp",
    }
)
_DRAW_NAMES = frozenset({"draw", "tie", "x"})


@dataclass(frozen=True)
class _NormalizationFailure(Exception):
    state: ProviderState
    reason: str


@dataclass(frozen=True)
class _TeamPair:
    home: Mapping[str, object]
    away: Mapping[str, object]


@dataclass(frozen=True)
class _PriceRow:
    decimal_price: float
    updated_at: datetime
    price_id: str
    source_id: str | None


def _team_key(value: object, aliases: Mapping[str, str]) -> str:
    raw = str(value or "").strip()
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", raw).casefold()
        if not unicodedata.combining(char) and char.isalnum()
    )
    alias = aliases.get(raw) or aliases.get(normalized)
    if alias:
        normalized = "".join(
            char
            for char in unicodedata.normalize("NFKD", alias).casefold()
            if not unicodedata.combining(char) and char.isalnum()
        )
    return normalized


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        try:
            return _utc(value, field_name)
        except ProductionContractError as exc:
            raise _NormalizationFailure(
                ProviderState.MALFORMED, f"{field_name}_invalid"
            ) from exc
    if value is None or not str(value).strip():
        raise _NormalizationFailure(ProviderState.MALFORMED, f"{field_name}_missing")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise _NormalizationFailure(
            ProviderState.MALFORMED, f"{field_name}_invalid"
        ) from exc


def _american_to_decimal(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        american = float(value)
    except (TypeError, ValueError):
        return None
    if (
        not isfinite(american)
        or american == THERUNDOWN_OFF_BOARD_SENTINEL
        or american == 0.0
        or abs(american) < 100.0
    ):
        return None
    decimal = 1.0 + american / 100.0 if american > 0 else 1.0 + 100.0 / abs(american)
    return decimal if isfinite(decimal) and decimal > 1.0 else None


def _quota_from_headers(headers: Mapping[str, object]) -> QuotaSnapshot:
    lowered = {
        str(key).casefold(): str(value).strip() for key, value in headers.items()
    }

    def integer(*names: str) -> int | None:
        for name in names:
            raw = lowered.get(name.casefold())
            if raw is None:
                continue
            try:
                value = int(raw)
            except ValueError:
                return None
            return value if value >= 0 else None
        return None

    return QuotaSnapshot(
        used=integer("x-datapoints-used"),
        remaining=integer("x-datapoints-remaining"),
        rate_limit=integer("x-rate-limit", "x-ratelimit-limit"),
        rate_remaining=integer("x-ratelimit-remaining", "x-rate-limit-remaining"),
    )


def _response_result(
    response: RawProviderResponse,
    state: ProviderState,
    reason: str,
) -> AdapterResult:
    quota = _quota_from_headers(response.headers)
    result = AdapterResult(
        state=state,
        reason=reason,
        status_code=response.status_code,
        network_called=True,
        latency_ms=response.latency_ms,
        quota_after=quota,
        rate_limit_state=quota,
        raw_response_digest=digest_record(response.payload),
    )
    result.validate()
    return result


def _accepted_result(
    response: RawProviderResponse,
    observation: NormalizedOddsObservation,
) -> AdapterResult:
    quota = _quota_from_headers(response.headers)
    result = AdapterResult(
        state=ProviderState.AVAILABLE,
        reason="accepted_candidate_only",
        observation=observation,
        status_code=response.status_code,
        network_called=True,
        latency_ms=response.latency_ms,
        quota_after=quota,
        rate_limit_state=quota,
        raw_response_digest=digest_record(response.payload),
        normalized_record_digest=digest_record(observation.as_payload()),
    )
    result.validate()
    return result


class TheRundownExperimentalAdapter:
    """Candidate-only TheRundown adapter; never active cascade authority."""

    name = THERUNDOWN_PROVIDER_NAME
    credential_names: ClassVar[tuple[str, ...]] = (THERUNDOWN_CREDENTIAL_ENV,)
    transport_capability = TransportCapability.NETWORK_CAPABLE

    def __init__(
        self,
        *,
        transport: HttpTransport = requests_transport,
        affiliate_ids: tuple[str, ...] = (),
        affiliate_names: Mapping[str, str] | None = None,
        aliases: Mapping[str, str] | None = None,
        expected_season_year: int | None = None,
    ) -> None:
        self._transport = transport
        self._affiliate_ids = tuple(str(value).strip() for value in affiliate_ids)
        if len(set(self._affiliate_ids)) != len(self._affiliate_ids) or any(
            not value.isdigit() or int(value) <= 0 for value in self._affiliate_ids
        ):
            raise ProductionContractError(
                "TheRundown affiliate IDs must be unique positive integers"
            )
        self._affiliate_names = {
            str(key): str(value).strip()
            for key, value in (affiliate_names or {}).items()
        }
        self._aliases = dict(aliases or {})
        if expected_season_year is not None and (
            isinstance(expected_season_year, bool) or expected_season_year < 1
        ):
            raise ProductionContractError("TheRundown expected season year is invalid")
        self._expected_season_year = expected_season_year

    def request_for_fixture(
        self, fixture: Fixture, config: ProviderConfig
    ) -> ProviderRequest | None:
        """Build one bounded V2 dated-event request without exposing the key."""

        fixture.validate()
        config.validate()
        if config.name != self.name:
            raise ProductionContractError(
                "TheRundown config identity does not match adapter"
            )
        credential_names = config.credential_env or self.credential_names
        credentials = tuple(os.getenv(name, "") for name in credential_names)
        if config.credentials_required and any(
            not value.strip() for value in credentials
        ):
            return None
        params: dict[str, str] = {
            "market_ids": str(THERUNDOWN_MONEYLINE_MARKET_ID),
            "main_line": "true",
            "hide_closed": "true",
            "hide_no_markets": "true",
        }
        if self._affiliate_ids:
            params["affiliate_ids"] = ",".join(self._affiliate_ids)
        return ProviderRequest(
            provider=self.name,
            endpoint=(
                f"{THERUNDOWN_BASE_URL}/sports/{THERUNDOWN_CHAMPIONS_LEAGUE_SPORT_ID}"
                f"/events/{fixture.kickoff.date().isoformat()}"
            ),
            params=params,
            headers={"Accept": "application/json", "X-TheRundown-Key": credentials[0]},
        )

    discovery_request = request_for_fixture

    def normalize_event(
        self,
        event: Mapping[str, object],
        fixture: Fixture,
        *,
        response: RawProviderResponse,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        config: ProviderConfig,
        timing_policy: CascadeTimingPolicy,
    ) -> tuple[NormalizedOddsObservation, ...]:
        """Normalize every complete main-line sportsbook deterministically."""

        fixture.validate()
        config.validate()
        timing_policy.validate()
        identity = self._event_identity(event, fixture, timing_policy=timing_policy)
        if identity != "match":
            state = {
                "wrong_league": ProviderState.UNSUPPORTED_LEAGUE,
                "wrong_season": ProviderState.UNSUPPORTED_LEAGUE,
                "swapped_home_away": ProviderState.QUALITY_REJECTED,
                "wrong_fixture": ProviderState.UNSUPPORTED_FIXTURE,
                "kickoff_mismatch": ProviderState.QUALITY_REJECTED,
                "malformed": ProviderState.MALFORMED,
            }.get(identity, ProviderState.QUALITY_REJECTED)
            raise _NormalizationFailure(state, f"identity_{identity}")
        teams = _extract_teams(event)
        markets = event.get("markets")
        if not isinstance(markets, list):
            raise _NormalizationFailure(ProviderState.MALFORMED, "markets_not_list")
        moneyline_markets = [
            market
            for market in markets
            if isinstance(market, Mapping)
            and _integer_field(market.get("market_id"))
            == THERUNDOWN_MONEYLINE_MARKET_ID
            and _integer_field(market.get("period_id")) == 0
            and _team_key(market.get("name"), {}) == "moneyline"
        ]
        if not moneyline_markets:
            raise _NormalizationFailure(
                ProviderState.UNSUPPORTED_MARKET, "missing_moneyline_market"
            )
        if len(moneyline_markets) != 1:
            raise _NormalizationFailure(
                ProviderState.QUALITY_REJECTED, "duplicate_moneyline_market"
            )
        participants = moneyline_markets[0].get("participants")
        if not isinstance(participants, list):
            raise _NormalizationFailure(
                ProviderState.MALFORMED, "participants_not_list"
            )
        participant_keys: dict[str, Mapping[str, object]] = {}
        home_id = str(teams.home.get("team_id", "")).strip()
        away_id = str(teams.away.get("team_id", "")).strip()
        for participant in participants:
            if not isinstance(participant, Mapping):
                continue
            participant_id = str(participant.get("id", "")).strip()
            label = _team_key(participant.get("name"), self._aliases)
            key = (
                "home"
                if participant_id == home_id
                or label == _team_key(fixture.home_team, self._aliases)
                else "away"
                if participant_id == away_id
                or label == _team_key(fixture.away_team, self._aliases)
                else "draw"
                if label in _DRAW_NAMES
                else ""
            )
            if not key or key in participant_keys:
                if key:
                    raise _NormalizationFailure(
                        ProviderState.QUALITY_REJECTED, "duplicate_market_participant"
                    )
                continue
            participant_keys[key] = participant
        if set(participant_keys) != {"home", "draw", "away"}:
            missing = sorted({"home", "draw", "away"} - set(participant_keys))
            raise _NormalizationFailure(
                ProviderState.QUALITY_REJECTED,
                "missing_" + "_".join(missing) + "_price",
            )

        rows: dict[str, dict[str, _PriceRow]] = {}
        for outcome, participant in sorted(participant_keys.items()):
            lines = participant.get("lines")
            if not isinstance(lines, list):
                continue
            for line in lines:
                if not isinstance(line, Mapping) or line.get("value") not in ("", None):
                    continue
                prices = line.get("prices")
                if not isinstance(prices, Mapping):
                    continue
                for affiliate_raw, price_raw in sorted(
                    prices.items(), key=lambda item: str(item[0])
                ):
                    affiliate_id = str(affiliate_raw).strip()
                    if self._affiliate_ids and affiliate_id not in self._affiliate_ids:
                        continue
                    if (
                        not isinstance(price_raw, Mapping)
                        or price_raw.get("is_main_line") is not True
                    ):
                        continue
                    converted = _american_to_decimal(price_raw.get("price"))
                    if converted is None:
                        continue
                    updated_at = _parse_datetime(
                        price_raw.get("updated_at"), field_name="price_updated_at"
                    )
                    if updated_at > response.completed_at:
                        raise _NormalizationFailure(
                            ProviderState.MALFORMED, "price_timestamp_in_future"
                        )
                    bucket = rows.setdefault(affiliate_id, {})
                    if outcome in bucket:
                        raise _NormalizationFailure(
                            ProviderState.QUALITY_REJECTED, "duplicate_observation"
                        )
                    bucket[outcome] = _PriceRow(
                        decimal_price=converted,
                        updated_at=updated_at,
                        price_id=str(price_raw.get("id", "")).strip(),
                        source_id=(
                            None
                            if price_raw.get("source_id") is None
                            else str(price_raw.get("source_id"))
                        ),
                    )

        fresh: list[NormalizedOddsObservation] = []
        stale_count = 0
        invalid_book_count = 0
        selected_affiliates = self._affiliate_ids or tuple(sorted(rows))
        for affiliate_id in selected_affiliates:
            bucket = rows.get(affiliate_id, {})
            if set(bucket) != {"home", "draw", "away"}:
                invalid_book_count += 1
                continue
            source_timestamp = min(row.updated_at for row in bucket.values())
            age_seconds = (response.completed_at - source_timestamp).total_seconds()
            if age_seconds < 0 or age_seconds > timing_policy.maximum_odds_age_seconds:
                stale_count += 1
                continue
            bookmaker = (
                self._affiliate_names.get(affiliate_id) or f"affiliate:{affiliate_id}"
            )
            source_id = next(
                (row.source_id for row in bucket.values() if row.source_id), None
            )
            metadata = {
                "experimental": True,
                "candidate_only": True,
                "competition_identity": THERUNDOWN_CHAMPIONS_LEAGUE_CODE,
                "therundown_sport_id": THERUNDOWN_CHAMPIONS_LEAGUE_SPORT_ID,
                "event_id": str(event.get("event_id")),
                "event_uuid": event.get("event_uuid"),
                "season_year": event.get("schedule", {}).get("season_year")
                if isinstance(event.get("schedule"), Mapping)
                else None,
                "season_type": event.get("schedule", {}).get("season_type")
                if isinstance(event.get("schedule"), Mapping)
                else None,
                "league_name": event.get("schedule", {}).get("league_name")
                if isinstance(event.get("schedule"), Mapping)
                else None,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "affiliate_id": affiliate_id,
                "affiliate_source_id": source_id,
                "market_id": THERUNDOWN_MONEYLINE_MARKET_ID,
                "period_id": 0,
                "odds_format": "american_to_decimal",
                "main_line_only": True,
                "event_status": (
                    event.get("score", {}).get("event_status")
                    if isinstance(event.get("score"), Mapping)
                    else None
                ),
                "neutral_venue": bool(
                    event.get("neutral_site")
                    or event.get("neutral_venue")
                    or event.get("is_neutral")
                ),
            }
            observation = NormalizedOddsObservation(
                league_code=fixture.league_code,
                fixture_key=fixture.fixture_key,
                provider_fixture_id=str(event.get("event_id", "")).strip(),
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                kickoff_utc=fixture.kickoff,
                market_type=MARKET_PREMATCH_1X2,
                home_odds=bucket["home"].decimal_price,
                draw_odds=bucket["draw"].decimal_price,
                away_odds=bucket["away"].decimal_price,
                provider_identity=self.name,
                bookmaker_identity=bookmaker,
                source_timestamp=source_timestamp,
                captured_at=response.completed_at,
                request_identity=request_identity,
                request_started_at=_utc(requested_at, "requested_at"),
                request_completed_at=response.completed_at,
                latency_ms=response.latency_ms,
                provider_priority=provider_priority,
                fallback_depth=0,
                quota_state_before=config.initial_quota,
                quota_state_after=_quota_from_headers(response.headers),
                rate_limit_state=_quota_from_headers(response.headers),
                source_provenance=(
                    "therundown:v2:/sports/16/events/{date};event_id="
                    f"{event.get('event_id')};market_id=1;affiliate_id={affiliate_id};"
                    "price.updated_at"
                ),
                raw_record_digest=digest_record(event),
                adapter_version=config.adapter_version,
                completeness=ObservationCompleteness.COMPLETE,
                candidate_only=True,
                metadata=metadata,
                source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
            )
            observation.validate(require_fresh=False)
            fresh.append(observation)
        if not fresh:
            if stale_count and stale_count == len(selected_affiliates):
                raise _NormalizationFailure(ProviderState.STALE, "all_bookmakers_stale")
            if invalid_book_count:
                raise _NormalizationFailure(
                    ProviderState.QUALITY_REJECTED, "bookmaker_1x2_incomplete"
                )
            raise _NormalizationFailure(
                ProviderState.QUALITY_REJECTED, "missing_bookmaker"
            )
        return tuple(fresh)

    def fetch(
        self,
        fixture: Fixture,
        config: ProviderConfig,
        *,
        request_identity: str,
        requested_at: datetime,
        provider_priority: int,
        provider_fixture_id: str | None = None,
        timing_policy: CascadeTimingPolicy | None = None,
        identity_resolution: object | None = None,
        authorization: NetworkAuthorizationContract | None = None,
    ) -> AdapterResult:
        if timing_policy is None:
            raise ProductionContractError(
                "explicit experiment timing policy is required"
            )
        timing_policy.validate()
        try:
            config.validate()
        except ProductionContractError:
            return AdapterResult(
                ProviderState.CONFIG_DISABLED,
                "invalid_provider_configuration",
                network_called=False,
            )
        if config.name != self.name:
            return AdapterResult(
                ProviderState.CONFIG_DISABLED,
                "provider_config_identity_mismatch",
                network_called=False,
            )
        if not config.enabled:
            return AdapterResult(
                ProviderState.CONFIG_DISABLED,
                "provider_disabled",
                network_called=False,
            )
        if (
            config.league_allowlist
            and fixture.league_code not in config.league_allowlist
        ):
            return AdapterResult(
                ProviderState.UNSUPPORTED_LEAGUE,
                "league_not_allowlisted",
                network_called=False,
            )
        if fixture.league_code != THERUNDOWN_CHAMPIONS_LEAGUE_CODE:
            return AdapterResult(
                ProviderState.UNSUPPORTED_LEAGUE,
                "champions_league_only",
                network_called=False,
            )
        if MARKET_PREMATCH_1X2 not in config.market_allowlist:
            return AdapterResult(
                ProviderState.UNSUPPORTED_MARKET,
                "prematch_1x2_not_allowlisted",
                network_called=False,
            )
        if authorization is None:
            return AdapterResult(
                ProviderState.HEALTH_UNKNOWN,
                "explicit_network_authorization_required",
                network_called=False,
            )
        try:
            authorization.validate()
        except ProductionContractError:
            return AdapterResult(
                ProviderState.HEALTH_UNKNOWN,
                "invalid_network_authorization",
                network_called=False,
            )
        if not authorization.permits(self.name):
            return AdapterResult(
                ProviderState.HEALTH_UNKNOWN,
                "network_authorization_does_not_permit_experimental_provider",
                network_called=False,
            )
        request = self.request_for_fixture(fixture, config)
        if request is None:
            return AdapterResult(
                ProviderState.CREDENTIAL_MISSING,
                "credential_missing",
                network_called=False,
            )
        requested_at = _utc(requested_at, "requested_at")
        try:
            response = self._transport(request, config.timeout_seconds)
        except Exception:  # noqa: BLE001 - transport boundary must fail closed
            return AdapterResult(
                ProviderState.TEMPORARILY_UNAVAILABLE,
                "transport_error",
                network_called=True,
            )
        classified = self._classify_response(response)
        if classified is not None:
            return classified
        if not isinstance(response.payload, Mapping):
            return _response_result(
                response, ProviderState.MALFORMED, "payload_not_object"
            )
        events = response.payload.get("events")
        if not isinstance(events, list):
            return _response_result(
                response, ProviderState.MALFORMED, "events_not_list"
            )
        event_ids = [
            str(event.get("event_id", "")).strip()
            for event in events
            if isinstance(event, Mapping) and str(event.get("event_id", "")).strip()
        ]
        if len(event_ids) != len(set(event_ids)):
            return _response_result(
                response, ProviderState.QUALITY_REJECTED, "duplicate_event_identity"
            )
        if provider_fixture_id is not None:
            candidates = [
                event
                for event in events
                if isinstance(event, Mapping)
                and str(event.get("event_id", "")).strip() == provider_fixture_id
            ]
        else:
            candidates = []
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                try:
                    identity = self._event_identity(
                        event, fixture, timing_policy=timing_policy
                    )
                except _NormalizationFailure:
                    identity = "malformed"
                if identity == "match":
                    candidates.append(event)
            # Preserve a useful strict classification for a single malformed,
            # wrong-league, or mismatched event response.
            if not candidates and len(events) == 1 and isinstance(events[0], Mapping):
                candidates = [events[0]]
        if not candidates:
            return _response_result(
                response, ProviderState.UNSUPPORTED_FIXTURE, "event_not_found"
            )
        if len(candidates) != 1:
            return _response_result(
                response,
                ProviderState.QUALITY_REJECTED,
                "duplicate_fixture_observation",
            )
        try:
            observations = self.normalize_event(
                candidates[0],
                fixture,
                response=response,
                request_identity=request_identity,
                requested_at=requested_at,
                provider_priority=provider_priority,
                config=config,
                timing_policy=timing_policy,
            )
        except _NormalizationFailure as failure:
            return _response_result(response, failure.state, failure.reason)
        return _accepted_result(response, observations[0])

    def _event_identity(
        self,
        event: Mapping[str, object],
        fixture: Fixture,
        *,
        timing_policy: CascadeTimingPolicy,
    ) -> str:
        if (
            _integer_field(event.get("sport_id"))
            != THERUNDOWN_CHAMPIONS_LEAGUE_SPORT_ID
        ):
            return "wrong_league"
        schedule = event.get("schedule")
        if schedule is not None and not isinstance(schedule, Mapping):
            return "malformed"
        if isinstance(schedule, Mapping):
            league_name = schedule.get("league_name")
            if (
                league_name is not None
                and _team_key(league_name, {}) not in _CHAMPIONS_LEAGUE_NAMES
            ):
                return "wrong_league"
            season_year = schedule.get("season_year")
            if (
                self._expected_season_year is not None
                and _integer_field(season_year) != self._expected_season_year
            ):
                return "wrong_season"
        if not str(event.get("event_id", "")).strip():
            return "malformed"
        try:
            kickoff = _parse_datetime(event.get("event_date"), field_name="event_date")
        except _NormalizationFailure:
            return "malformed"
        if (
            abs((kickoff - fixture.kickoff).total_seconds())
            > timing_policy.kickoff_tolerance_seconds
        ):
            return "kickoff_mismatch"
        try:
            teams = _extract_teams(event)
        except _NormalizationFailure:
            return "malformed"
        home_key = _team_key(teams.home.get("name"), self._aliases)
        away_key = _team_key(teams.away.get("name"), self._aliases)
        expected_home = _team_key(fixture.home_team, self._aliases)
        expected_away = _team_key(fixture.away_team, self._aliases)
        if not home_key or not away_key:
            return "malformed"
        if home_key == expected_away and away_key == expected_home:
            return "swapped_home_away"
        if (home_key, away_key) != (expected_home, expected_away):
            return "wrong_fixture"
        return "match"

    @staticmethod
    def _classify_response(response: RawProviderResponse) -> AdapterResult | None:
        if response.timeout or response.error_code == "timeout":
            return _response_result(
                response, ProviderState.TEMPORARILY_UNAVAILABLE, "timeout"
            )
        if response.error_code == "request_error":
            return _response_result(
                response, ProviderState.TEMPORARILY_UNAVAILABLE, "network_error"
            )
        if response.error_code == "malformed_json":
            return _response_result(response, ProviderState.MALFORMED, "malformed_json")
        status = response.status_code
        if status == 401:
            return _response_result(response, ProviderState.AUTH_FAILED, "http_401")
        if status == 403:
            return _response_result(
                response, ProviderState.AUTH_FAILED, "http_403_entitlement_or_history"
            )
        if status == 404:
            return _response_result(
                response, ProviderState.UNSUPPORTED_FIXTURE, "http_404"
            )
        if status == 429:
            headers = {
                str(key).casefold(): str(value).strip()
                for key, value in response.headers.items()
            }
            if headers.get("x-datapoints-remaining") == "0":
                return _response_result(
                    response, ProviderState.QUOTA_EXHAUSTED, "datapoint_quota_exhausted"
                )
            return _response_result(
                response, ProviderState.RATE_LIMITED, "rate_limited"
            )
        if status == 400:
            return _response_result(
                response, ProviderState.UNSUPPORTED_MARKET, "http_400_invalid_request"
            )
        if status is not None and 500 <= status <= 599:
            return _response_result(
                response, ProviderState.TEMPORARILY_UNAVAILABLE, f"http_{status}"
            )
        if status != 200:
            return _response_result(
                response, ProviderState.MALFORMED, f"http_{status or 'no_status'}"
            )
        return None


def _integer_field(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_teams(event: Mapping[str, object]) -> _TeamPair:
    teams = event.get("teams")
    if (
        not isinstance(teams, list)
        or len(teams) != 2
        or any(not isinstance(team, Mapping) for team in teams)
    ):
        raise _NormalizationFailure(ProviderState.MALFORMED, "teams_not_two_objects")
    typed = tuple(team for team in teams if isinstance(team, Mapping))
    home_marked = tuple(team for team in typed if team.get("is_home") is True)
    away_marked = tuple(team for team in typed if team.get("is_away") is True)
    if home_marked or away_marked:
        if (
            len(home_marked) != 1
            or len(away_marked) != 1
            or home_marked[0] is away_marked[0]
        ):
            raise _NormalizationFailure(
                ProviderState.MALFORMED, "team_home_away_markers_ambiguous"
            )
        return _TeamPair(home=home_marked[0], away=away_marked[0])
    # TheRundown V2 documents array order as [away_team, home_team].
    return _TeamPair(home=typed[1], away=typed[0])


__all__ = [
    "THERUNDOWN_ADAPTER_VERSION",
    "THERUNDOWN_BASE_URL",
    "THERUNDOWN_CHAMPIONS_LEAGUE_CODE",
    "THERUNDOWN_CHAMPIONS_LEAGUE_SPORT_ID",
    "THERUNDOWN_CREDENTIAL_ENV",
    "THERUNDOWN_MONEYLINE_MARKET_ID",
    "THERUNDOWN_OFF_BOARD_SENTINEL",
    "THERUNDOWN_PROVIDER_NAME",
    "TheRundownExperimentalAdapter",
]
