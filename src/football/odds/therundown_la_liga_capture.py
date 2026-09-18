"""Bounded, candidate-only La Liga evidence capture for TheRundown.

This module is an evidence-capture boundary, not a provider registration or a
production transport.  It is deliberately limited to one LL/ESP1 date
discovery request followed by one event request.  The network path is inert
unless a caller supplies a valid :class:`LaLigaCaptureAuthorization` and an
explicit transport.  The command-line wrapper keeps the real transport behind
an additional explicit execution switch.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from src.football.odds.therundown import (
    THERUNDOWN_ADAPTER_VERSION,
    THERUNDOWN_BASE_URL,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
    TheRundownExperimentalAdapter,
    _event_prematch_state,
    _extract_teams,
    _NormalizationFailure,
    _parse_datetime,
)
from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.adapters import (
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
    ProviderConfig,
    QuotaSnapshot,
    digest_record,
)

LA_LIGA_CODE = "LL"
LA_LIGA_PROVIDER_LEAGUE = "ESP1"
LA_LIGA_PROVIDER_SPORT_ID = THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[LA_LIGA_CODE]
LA_LIGA_MAX_REQUESTS = 2
LA_LIGA_MAX_DATAPOINTS = 100
LA_LIGA_CAPTURE_SCHEMA = "top5-therundown-ll-capture-v1"


class LaLigaCaptureStatus(StrEnum):
    DISABLED = "DISABLED"
    READY = "READY"
    CAPTURED = "CAPTURED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class LaLigaCaptureAuthorization:
    """Caller-supplied, one-run envelope for the future real request."""

    provider: str
    league: str
    maximum_request_count: int
    maximum_datapoint_budget: int
    quota_before_used: int
    quota_before_remaining: int | None
    expires_at: datetime
    controlled_shadow_run_id: str
    ceo_authorization_id: str
    qualification_session_id: str
    no_bet: bool = True
    no_publication: bool = True
    no_activation: bool = True
    no_spend: bool = True

    def validate(self, *, now: datetime) -> None:
        now_utc = _utc(now, "authorization now")
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise ProductionContractError("authorization provider is not TheRundown")
        if self.league not in {LA_LIGA_CODE, LA_LIGA_PROVIDER_LEAGUE}:
            raise ProductionContractError("authorization league is not LL/ESP1")
        if (
            not isinstance(self.maximum_request_count, int)
            or isinstance(self.maximum_request_count, bool)
            or self.maximum_request_count != LA_LIGA_MAX_REQUESTS
        ):
            raise ProductionContractError(
                "La Liga capture requires exactly two requests"
            )
        if (
            not isinstance(self.maximum_datapoint_budget, int)
            or isinstance(self.maximum_datapoint_budget, bool)
            or self.maximum_datapoint_budget <= 0
            or self.maximum_datapoint_budget > LA_LIGA_MAX_DATAPOINTS
        ):
            raise ProductionContractError(
                "La Liga datapoint budget exceeds bounded cap"
            )
        if (
            not isinstance(self.quota_before_used, int)
            or isinstance(self.quota_before_used, bool)
            or self.quota_before_used < 0
            or (
                self.quota_before_remaining is not None
                and (
                    not isinstance(self.quota_before_remaining, int)
                    or isinstance(self.quota_before_remaining, bool)
                    or self.quota_before_remaining < 0
                )
            )
        ):
            raise ProductionContractError("quota-before evidence is invalid")
        expires = _utc(self.expires_at, "authorization expires_at")
        if expires <= now_utc:
            raise ProductionContractError("La Liga capture authorization is expired")
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("ceo_authorization_id", self.ceo_authorization_id),
            ("qualification_session_id", self.qualification_session_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ProductionContractError(f"{name} is required")
        for name, value in (
            ("no_bet", self.no_bet),
            ("no_publication", self.no_publication),
            ("no_activation", self.no_activation),
            ("no_spend", self.no_spend),
        ):
            if value is not True:
                raise ProductionContractError(f"{name} must remain true")

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "league": self.league,
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoint_budget": self.maximum_datapoint_budget,
            "quota_before_used": self.quota_before_used,
            "quota_before_remaining": self.quota_before_remaining,
            "expires_at": _utc(self.expires_at, "authorization expires_at").isoformat(),
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "qualification_session_id": self.qualification_session_id,
            "no_bet": True,
            "no_publication": True,
            "no_activation": True,
            "no_spend": True,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> LaLigaCaptureAuthorization:
        if not isinstance(raw, Mapping):
            raise ProductionContractError("capture authorization must be an object")
        try:
            expires_at = datetime.fromisoformat(
                str(raw.get("expires_at", "")).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ProductionContractError("authorization expiry is invalid") from exc
        return cls(
            provider=str(raw.get("provider", "")),
            league=str(raw.get("league", "")),
            maximum_request_count=raw.get("maximum_request_count", 0),
            maximum_datapoint_budget=raw.get("maximum_datapoint_budget", 0),
            quota_before_used=raw.get("quota_before_used", -1),
            quota_before_remaining=raw.get("quota_before_remaining"),
            expires_at=expires_at,
            controlled_shadow_run_id=str(raw.get("controlled_shadow_run_id", "")),
            ceo_authorization_id=str(raw.get("ceo_authorization_id", "")),
            qualification_session_id=str(raw.get("qualification_session_id", "")),
            no_bet=raw.get("no_bet") is True,
            no_publication=raw.get("no_publication") is True,
            no_activation=raw.get("no_activation") is True,
            no_spend=raw.get("no_spend") is True,
        )


@dataclass(frozen=True)
class LaLigaCaptureEvidence:
    status: LaLigaCaptureStatus
    reason: str
    authorization: LaLigaCaptureAuthorization | None = None
    selected_date: str | None = None
    fixture_key: str | None = None
    provider_event_id: str | None = None
    provider_request_id: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    kickoff: datetime | None = None
    participant_ids: Mapping[str, str] = field(default_factory=dict)
    participant_names: Mapping[str, str] = field(default_factory=dict)
    observations: tuple[NormalizedOddsObservation, ...] = ()
    source_timestamps: tuple[str, ...] = ()
    captured_at: datetime | None = None
    raw_response_digest: str = ""
    normalized_record_digests: tuple[str, ...] = ()
    quota_before: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    quota_after: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    rate_limit_state: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    quota_evidence: Mapping[str, object] = field(default_factory=dict)
    adapter_version: str = THERUNDOWN_ADAPTER_VERSION
    adapter_source_sha: str = ""
    requests_used: int = 0
    datapoints_consumed: int | None = None

    @property
    def b1_bridge_fields(self) -> dict[str, object]:
        """Return the immutable handoff fields B1/B2 need; no authority is issued."""

        return {
            "controlled_shadow_run_id": (
                self.authorization.controlled_shadow_run_id
                if self.authorization
                else None
            ),
            "ceo_authorization_id": (
                self.authorization.ceo_authorization_id if self.authorization else None
            ),
            "qualification_session_id": (
                self.authorization.qualification_session_id
                if self.authorization
                else None
            ),
            "provider_identity": THERUNDOWN_PROVIDER_NAME,
            "league": LA_LIGA_CODE,
            "fixture_key": self.fixture_key,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digests": list(self.normalized_record_digests),
            "source_timestamps": list(self.source_timestamps),
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "participant_ids": dict(self.participant_ids),
            "participant_names": dict(self.participant_names),
            "quota_before": self.quota_before.as_payload(),
            "quota_after": self.quota_after.as_payload(),
            "rate_limit_state": self.rate_limit_state.as_payload(),
            "quota_evidence": dict(self.quota_evidence),
            "network_execution": self.status is LaLigaCaptureStatus.CAPTURED,
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
        }

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": LA_LIGA_CAPTURE_SCHEMA,
            "status": self.status.value,
            "reason": self.reason,
            "authorization": self.authorization.as_payload()
            if self.authorization
            else None,
            "selected_date": self.selected_date,
            "fixture_key": self.fixture_key,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "participant_ids": dict(self.participant_ids),
            "participant_names": dict(self.participant_names),
            "observations": [item.as_payload() for item in self.observations],
            "source_timestamps": list(self.source_timestamps),
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digests": list(self.normalized_record_digests),
            "quota_before": self.quota_before.as_payload(),
            "quota_after": self.quota_after.as_payload(),
            "rate_limit_state": self.rate_limit_state.as_payload(),
            "quota_evidence": dict(self.quota_evidence),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "requests_used": self.requests_used,
            "datapoints_consumed": self.datapoints_consumed,
            "safety": {
                "candidate_only": True,
                "quality_eligible": False,
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "monetary_spend_authorized": False,
            },
        }


def adapter_source_sha() -> str:
    """Return a source digest suitable for the B1 bridge provenance field."""

    from src.football.odds import therundown

    return hashlib.sha256(Path(therundown.__file__).read_bytes()).hexdigest()


def _quota_snapshot(headers: Mapping[str, str]) -> QuotaSnapshot:
    lowered = {
        str(key).casefold(): str(value).strip() for key, value in headers.items()
    }

    def integer(*keys: str) -> int | None:
        for key in keys:
            raw = lowered.get(key)
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
        rate_remaining=integer("x-rate-limit-remaining", "x-ratelimit-remaining"),
    )


def _quota_evidence(headers: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "x-datapoints",
        "x-datapoints-used",
        "x-datapoints-remaining",
        "x-datapoints-limit",
        "x-rate-limit",
        "x-rate-limit-remaining",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-data-delay-seconds",
        "x-tier",
        "x-history-access",
        "x-live-odds-access",
        "x-websocket-access",
    }
    integer_keys = allowed - {
        "x-tier",
        "x-history-access",
        "x-live-odds-access",
        "x-websocket-access",
    }
    result: dict[str, object] = {}
    lowered = {
        str(key).casefold(): str(value).strip() for key, value in headers.items()
    }
    for key in sorted(allowed):
        value = lowered.get(key)
        if value is None:
            continue
        if key in integer_keys:
            try:
                result[key] = int(value)
            except ValueError:
                continue
        else:
            result[key] = value
    return result


def _datapoint_cost(headers: Mapping[str, str]) -> int | None:
    evidence = _quota_evidence(headers)
    cost = evidence.get("x-datapoints")
    if isinstance(cost, int):
        return cost
    return None


def _dates_request(
    adapter: TheRundownExperimentalAdapter, config: ProviderConfig
) -> ProviderRequest:
    credentials = tuple(
        os.getenv(name, "")
        for name in (config.credential_env or adapter.credential_names)
    )
    if config.credentials_required and any(not value.strip() for value in credentials):
        raise ProductionContractError(
            "THERUNDOWN_API_KEY is required for the real path"
        )
    return ProviderRequest(
        provider=THERUNDOWN_PROVIDER_NAME,
        endpoint=f"{THERUNDOWN_BASE_URL}/sports/{LA_LIGA_PROVIDER_SPORT_ID}/dates",
        params={},
        headers={"Accept": "application/json", "X-TheRundown-Key": credentials[0]},
    )


def _upcoming_date(payload: object, *, today: date) -> str | None:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("dates"), list):
        return None
    candidates: list[date] = []
    for value in payload["dates"]:
        try:
            parsed = date.fromisoformat(str(value).replace("Z", "")[:10])
        except ValueError:
            continue
        if parsed >= today:
            candidates.append(parsed)
    return min(candidates).isoformat() if candidates else None


def _event_fixture(
    event: Mapping[str, object], *, selected_date: str
) -> Fixture | None:
    if _event_prematch_state(event) != "accepted":
        return None
    if int(event.get("sport_id", -1)) != LA_LIGA_PROVIDER_SPORT_ID:
        return None
    event_id = str(event.get("event_id", "")).strip()
    if not event_id:
        return None
    try:
        kickoff = _parse_datetime(event.get("event_date"), field_name="event_date")
        if kickoff.date().isoformat() != selected_date:
            return None
        teams = _extract_teams(event)
    except (TypeError, ValueError, ProductionContractError, _NormalizationFailure):
        return None
    return Fixture(
        f"therundown:LL:{event_id}",
        LA_LIGA_CODE,
        str(teams.home.get("name", "")).strip(),
        str(teams.away.get("name", "")).strip(),
        kickoff,
    )


def _participant_identity(
    event: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str]]:
    teams = _extract_teams(event)
    ids = {
        "home": str(teams.home.get("team_id", "")).strip(),
        "away": str(teams.away.get("team_id", "")).strip(),
    }
    names = {
        "home": str(teams.home.get("name", "")).strip(),
        "away": str(teams.away.get("name", "")).strip(),
    }
    markets = event.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, Mapping) or str(market.get("market_id")) != "1":
                continue
            participants = market.get("participants")
            if not isinstance(participants, list):
                continue
            for participant in participants:
                if not isinstance(participant, Mapping):
                    continue
                label = str(participant.get("name", "")).strip().casefold()
                if label in {"draw", "tie", "x"}:
                    ids["draw"] = str(participant.get("id", "")).strip()
                    names["draw"] = str(participant.get("name", "")).strip()
    if set(ids) != {"home", "draw", "away"} or any(not value for value in ids.values()):
        raise ProductionContractError("participant identity provenance is incomplete")
    if set(names) != {"home", "draw", "away"} or any(
        not value for value in names.values()
    ):
        raise ProductionContractError("participant name provenance is incomplete")
    return ids, names


def _replay_transport(response: RawProviderResponse) -> HttpTransport:
    def transport(request: ProviderRequest, timeout: float) -> RawProviderResponse:
        del request, timeout
        return response

    return transport


def capture_la_liga(
    authorization: LaLigaCaptureAuthorization | None,
    *,
    now: datetime,
    adapter: TheRundownExperimentalAdapter | None = None,
    config: ProviderConfig | None = None,
    transport: HttpTransport | None = None,
    today: date | None = None,
) -> LaLigaCaptureEvidence:
    """Run the bounded two-request path with an injected or real transport."""

    if authorization is None:
        return LaLigaCaptureEvidence(
            LaLigaCaptureStatus.DISABLED,
            "explicit_capture_authorization_required",
            adapter_source_sha=adapter_source_sha(),
        )
    try:
        authorization.validate(now=now)
    except ProductionContractError as exc:
        return LaLigaCaptureEvidence(
            LaLigaCaptureStatus.REJECTED,
            str(exc),
            authorization=authorization,
            adapter_source_sha=adapter_source_sha(),
        )
    adapter = adapter or TheRundownExperimentalAdapter(
        transport=transport or requests_transport
    )
    if config is None:
        config = ProviderConfig(
            name=THERUNDOWN_PROVIDER_NAME,
            league_allowlist=frozenset({LA_LIGA_CODE}),
            market_allowlist=(MARKET_PREMATCH_1X2,),
            credentials_required=transport is None,
            credential_env=("THERUNDOWN_API_KEY",),
            candidate_only=True,
            quality_eligible=False,
            shadow_only=True,
            adapter_version=THERUNDOWN_ADAPTER_VERSION,
            initial_quota=QuotaSnapshot(
                used=authorization.quota_before_used,
                remaining=authorization.quota_before_remaining,
            ),
        )
    quota_before = config.initial_quota
    selected_date: str | None = None
    quota_after = quota_before
    rate_limit_state = quota_before
    quota_evidence: dict[str, object] = {}
    raw_response_digest = ""
    requests_used = 0
    datapoints_consumed: int | None = None
    try:
        config.validate()
        if (
            config.name != THERUNDOWN_PROVIDER_NAME
            or not config.candidate_only
            or config.quality_eligible
        ):
            raise ProductionContractError("capture configuration is not candidate-only")
        if config.league_allowlist != frozenset({LA_LIGA_CODE}):
            raise ProductionContractError("capture configuration must be LL-only")
        dates_request = _dates_request(adapter, config)
        request_transport = transport or adapter._transport
        dates_response = request_transport(dates_request, config.timeout_seconds)
        requests_used = 1
        dates_cost = _datapoint_cost(dates_response.headers)
        quota_after_dates = _quota_snapshot(dates_response.headers)
        quota_after = quota_after_dates
        rate_limit_state = quota_after_dates
        quota_evidence = _quota_evidence(dates_response.headers)
        raw_response_digest = digest_record(dates_response.payload)
        datapoints_consumed = dates_cost
        if dates_response.status_code != 200:
            return LaLigaCaptureEvidence(
                LaLigaCaptureStatus.REJECTED,
                f"dates_http_{dates_response.status_code or 'no_status'}",
                authorization=authorization,
                quota_before=quota_before,
                quota_after=quota_after_dates,
                rate_limit_state=quota_after_dates,
                quota_evidence=_quota_evidence(dates_response.headers),
                raw_response_digest=digest_record(dates_response.payload),
                requests_used=requests_used,
                datapoints_consumed=dates_cost,
                adapter_source_sha=adapter_source_sha(),
            )
        selected_date = _upcoming_date(
            dates_response.payload, today=today or now.date()
        )
        if selected_date is None:
            return LaLigaCaptureEvidence(
                LaLigaCaptureStatus.REJECTED,
                "no_upcoming_fixture_date",
                authorization=authorization,
                quota_before=quota_before,
                quota_after=quota_after_dates,
                rate_limit_state=quota_after_dates,
                quota_evidence=_quota_evidence(dates_response.headers),
                requests_used=requests_used,
                datapoints_consumed=dates_cost,
                adapter_source_sha=adapter_source_sha(),
            )
        if dates_cost is None or dates_cost > authorization.maximum_datapoint_budget:
            return LaLigaCaptureEvidence(
                LaLigaCaptureStatus.REJECTED,
                "datapoint_budget_exceeded_before_event_request",
                authorization=authorization,
                selected_date=selected_date,
                quota_before=quota_before,
                quota_after=quota_after_dates,
                rate_limit_state=quota_after_dates,
                quota_evidence=_quota_evidence(dates_response.headers),
                requests_used=requests_used,
                datapoints_consumed=dates_cost,
                adapter_source_sha=adapter_source_sha(),
            )

        events_request = ProviderRequest(
            provider=THERUNDOWN_PROVIDER_NAME,
            endpoint=f"{THERUNDOWN_BASE_URL}/sports/{LA_LIGA_PROVIDER_SPORT_ID}/events/{selected_date}",
            params={
                "market_ids": "1",
                "main_line": "true",
                "hide_closed": "true",
                "hide_no_markets": "true",
            },
            headers=dates_request.headers,
        )
        events_response = request_transport(events_request, config.timeout_seconds)
        requests_used += 1
        events_cost = _datapoint_cost(events_response.headers)
        quota_after = _quota_snapshot(events_response.headers)
        total_cost = (dates_cost or 0) + (events_cost or 0)
        evidence = _quota_evidence(events_response.headers)
        rate_limit_state = quota_after
        quota_evidence = evidence
        raw_response_digest = digest_record(events_response.payload)
        datapoints_consumed = total_cost
        if events_response.status_code != 200:
            return LaLigaCaptureEvidence(
                LaLigaCaptureStatus.REJECTED,
                f"events_http_{events_response.status_code or 'no_status'}",
                authorization=authorization,
                selected_date=selected_date,
                quota_before=quota_before,
                quota_after=quota_after,
                rate_limit_state=quota_after,
                quota_evidence=evidence,
                raw_response_digest=digest_record(events_response.payload),
                requests_used=requests_used,
                datapoints_consumed=total_cost,
                adapter_source_sha=adapter_source_sha(),
            )
        if dates_cost is None or events_cost is None:
            raise ProductionContractError("quota datapoint provenance is missing")
        if total_cost > authorization.maximum_datapoint_budget:
            raise ProductionContractError("datapoint budget exceeded")
        if not isinstance(events_response.payload, Mapping) or not isinstance(
            events_response.payload.get("events"), list
        ):
            raise ProductionContractError("events payload is not a list")
        candidates = [
            (event, _event_fixture(event, selected_date=selected_date))
            for event in events_response.payload["events"]
            if isinstance(event, Mapping)
        ]
        candidates = [
            (event, fixture) for event, fixture in candidates if fixture is not None
        ]
        if not candidates:
            raise ProductionContractError("no_upcoming_prematch_fixture")
        event, fixture = min(
            candidates, key=lambda item: (item[1].kickoff, item[1].fixture_key)
        )
        participant_ids, participant_names = _participant_identity(event)
        request_id = (
            f"therundown-ll:{authorization.controlled_shadow_run_id}:{selected_date}"
        )
        replay_adapter = TheRundownExperimentalAdapter(
            transport=_replay_transport(events_response),
        )
        observations = replay_adapter.fetch_observations(
            fixture,
            config,
            request_identity=request_id,
            requested_at=events_response.started_at,
            provider_priority=0,
            provider_fixture_id=str(event.get("event_id", "")).strip(),
            timing_policy=CascadeTimingPolicy(
                maximum_odds_age_seconds=900,
                kickoff_tolerance_seconds=60,
            ),
            authorization=NetworkAuthorizationContract(
                controlled_shadow_run_ref=authorization.controlled_shadow_run_id,
                authorized_providers=(THERUNDOWN_PROVIDER_NAME,),
            ),
        )
        normalized_digests = tuple(
            digest_record(item.as_payload()) for item in observations
        )
        source_timestamps = tuple(
            sorted(
                {
                    item.source_timestamp.isoformat()
                    for item in observations
                    if item.source_timestamp
                }
            )
        )
        if not observations or any(not item.raw_record_digest for item in observations):
            raise ProductionContractError("observation provenance is incomplete")
        return LaLigaCaptureEvidence(
            LaLigaCaptureStatus.CAPTURED,
            "candidate_only_capture_complete",
            authorization=authorization,
            selected_date=selected_date,
            fixture_key=fixture.fixture_key,
            provider_event_id=str(event.get("event_id", "")).strip(),
            provider_request_id=request_id,
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            kickoff=fixture.kickoff,
            participant_ids=participant_ids,
            participant_names=participant_names,
            observations=observations,
            source_timestamps=source_timestamps,
            captured_at=events_response.completed_at,
            raw_response_digest=digest_record(events_response.payload),
            normalized_record_digests=normalized_digests,
            quota_before=quota_before,
            quota_after=quota_after,
            rate_limit_state=quota_after,
            quota_evidence=evidence,
            adapter_version=THERUNDOWN_ADAPTER_VERSION,
            adapter_source_sha=adapter_source_sha(),
            requests_used=requests_used,
            datapoints_consumed=total_cost,
        )
    except (ProductionContractError, ValueError, TypeError, KeyError) as exc:
        return LaLigaCaptureEvidence(
            LaLigaCaptureStatus.REJECTED,
            str(exc),
            authorization=authorization,
            selected_date=selected_date,
            quota_before=quota_before,
            quota_after=quota_after,
            rate_limit_state=rate_limit_state,
            quota_evidence=quota_evidence,
            raw_response_digest=raw_response_digest,
            requests_used=requests_used,
            datapoints_consumed=datapoints_consumed,
            adapter_source_sha=adapter_source_sha(),
        )


__all__ = [
    "LA_LIGA_CAPTURE_SCHEMA",
    "LA_LIGA_CODE",
    "LA_LIGA_MAX_DATAPOINTS",
    "LA_LIGA_MAX_REQUESTS",
    "LA_LIGA_PROVIDER_LEAGUE",
    "LaLigaCaptureAuthorization",
    "LaLigaCaptureEvidence",
    "LaLigaCaptureStatus",
    "adapter_source_sha",
    "capture_la_liga",
]
