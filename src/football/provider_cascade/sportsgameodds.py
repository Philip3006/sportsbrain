"""Experimental SportsGameOdds adapter for football shadow evaluation.

SportsGameOdds is deliberately not part of ``FOOTBALL_PROVIDER_REPERTOIRE``.
This module reuses the provider-cascade request, response, error, and
``NormalizedOddsObservation`` contracts without creating a second odds model.
It can be used by injected experiments or the bounded diagnostic only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.adapters import (
    AdapterResult,
    HttpTransport,
    ProviderRequest,
    RawProviderResponse,
    _accepted_result,
    _BaseAdapter,
    requests_transport,
)
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    CascadeTimingPolicy,
    NormalizedOddsObservation,
    ProviderConfig,
    ProviderState,
    TransportCapability,
)

SPORTSGAMEODDS_PROVIDER = "sportsgameodds"
SPORTSGAMEODDS_API_URL = "https://api.sportsgameodds.com/v2"
SPORTSGAMEODDS_LEAGUE_ID = "UEFA_CHAMPIONS_LEAGUE"
SPORTSGAMEODDS_INTERNAL_LEAGUE = "CL"
SPORTSGAMEODDS_REGULATION_MARKET_IDS = (
    "points-home-reg-ml3way-home",
    "points-all-reg-ml3way-draw",
    "points-away-reg-ml3way-away",
)


class SportsGameOddsError(ProductionContractError):
    """Safe, non-secret failure at the experimental provider boundary."""

    def __init__(self, reason: str, state: ProviderState = ProviderState.MALFORMED):
        super().__init__(reason)
        self.reason = reason
        self.state = state


@dataclass(frozen=True)
class SportsGameOddsExperimentConfig:
    """Explicit experiment parameters; no production activation is implied."""

    league_code: str = SPORTSGAMEODDS_INTERNAL_LEAGUE
    league_id: str = SPORTSGAMEODDS_LEAGUE_ID
    maximum_odds_age_seconds: int = 900
    kickoff_tolerance_seconds: int = 90
    adapter_version: str = "sportsgameodds-v2-experimental-1"

    def validate(self) -> None:
        if self.league_code != SPORTSGAMEODDS_INTERNAL_LEAGUE:
            raise SportsGameOddsError(
                "unsupported_internal_league", ProviderState.UNSUPPORTED_LEAGUE
            )
        if self.league_id != SPORTSGAMEODDS_LEAGUE_ID:
            raise SportsGameOddsError(
                "unsupported_provider_league", ProviderState.UNSUPPORTED_LEAGUE
            )
        if self.maximum_odds_age_seconds <= 0 or self.kickoff_tolerance_seconds < 0:
            raise SportsGameOddsError(
                "invalid_experiment_timing", ProviderState.MALFORMED
            )
        if not self.adapter_version.strip():
            raise SportsGameOddsError(
                "missing_adapter_version", ProviderState.MALFORMED
            )


def build_sportsgameodds_provider_config(
    *, enabled: bool = False, adapter_version: str = "sportsgameodds-v2-experimental-1"
) -> ProviderConfig:
    """Build a disabled, candidate-only cascade config for offline experiments."""

    config = ProviderConfig(
        name=SPORTSGAMEODDS_PROVIDER,
        enabled=enabled,
        league_allowlist=frozenset({SPORTSGAMEODDS_INTERNAL_LEAGUE}),
        market_allowlist=(MARKET_PREMATCH_1X2,),
        candidate_only=True,
        quality_eligible=False,
        shadow_only=True,
        credentials_required=True,
        credential_env=("SPORTSGAMEODDS_API_KEY",),
        adapter_version=adapter_version,
        source_timestamp_required=True,
    )
    config.validate()
    return config


class SportsGameOddsAdapter(_BaseAdapter):
    """Candidate-only provider adapter using the existing cascade contracts."""

    name = SPORTSGAMEODDS_PROVIDER
    credential_names = ("SPORTSGAMEODDS_API_KEY",)
    transport_capability = TransportCapability.NETWORK_CAPABLE
    league_ids: ClassVar[dict[str, str]] = {
        SPORTSGAMEODDS_INTERNAL_LEAGUE: SPORTSGAMEODDS_LEAGUE_ID
    }

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        aliases: Mapping[str, str] | None = None,
        experiment: SportsGameOddsExperimentConfig | None = None,
    ) -> None:
        super().__init__(transport=transport or requests_transport, aliases=aliases)
        self.experiment = experiment or SportsGameOddsExperimentConfig()
        self.experiment.validate()

    def normalize_event(
        self,
        fixture: Fixture,
        event: Mapping[str, object],
        *,
        config: ProviderConfig | None = None,
        request_identity: str = "injected-sportsgameodds-request",
        requested_at: datetime,
        captured_at: datetime,
        provider_priority: int = 0,
        evidence_kind: str = "TEST_INJECTED",
        kickoff_tolerance_seconds: int | None = None,
    ) -> tuple[NormalizedOddsObservation, ...]:
        """Normalize one injected event without network or publication effects."""

        from src.football.provider_cascade.sportsgameodds_normalization import (
            observations_for_event,
        )

        active_config = config or build_sportsgameodds_provider_config(enabled=True)
        if active_config.name != self.name:
            raise SportsGameOddsError("provider_config_identity_mismatch")
        active_config.validate()
        raw_started = _utc(requested_at, "requested_at")
        raw_completed = _utc(captured_at, "captured_at")
        response = RawProviderResponse(
            200,
            event,
            {},
            raw_started,
            raw_completed,
            max(0, round((raw_completed - raw_started).total_seconds() * 1000)),
        )
        return observations_for_event(
            fixture,
            event,
            config=active_config,
            experiment=self.experiment,
            request_identity=request_identity,
            requested_at=raw_started,
            provider_priority=provider_priority,
            captured_at=raw_completed,
            aliases=self._aliases,
            kickoff_tolerance_seconds=(
                self.experiment.kickoff_tolerance_seconds
                if kickoff_tolerance_seconds is None
                else kickoff_tolerance_seconds
            ),
            evidence_kind=evidence_kind,
            raw_response=response,
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
        timing_policy: CascadeTimingPolicy | None = None,
        identity_resolution: Any = None,
        authorization: Any = None,
    ) -> AdapterResult:
        """Fetch one event for an explicitly authorized, candidate-only run."""

        from src.football.provider_cascade.sportsgameodds_normalization import (
            observations_for_event,
        )

        if config.name != self.name:
            return AdapterResult(
                ProviderState.CONFIG_DISABLED,
                "provider_config_identity_mismatch",
                network_called=False,
            )
        config.validate()
        if not config.enabled:
            return AdapterResult(
                ProviderState.CONFIG_DISABLED,
                "experimental_provider_disabled",
                network_called=False,
            )
        if not config.candidate_only or config.quality_eligible:
            return AdapterResult(
                ProviderState.CONFIG_DISABLED,
                "experimental_provider_must_remain_candidate_only",
                network_called=False,
            )
        if (
            config.initial_quota.remaining is not None
            and config.initial_quota.remaining <= config.quota_reserve
        ):
            return AdapterResult(
                ProviderState.QUOTA_EXHAUSTED,
                "quota_exhausted_or_reserve",
                network_called=False,
            )
        if timing_policy is None:
            return AdapterResult(
                ProviderState.MALFORMED,
                "explicit_experiment_timing_required",
                network_called=False,
            )
        timing_policy.validate()
        if authorization is None or not authorization.permits(self.name):
            return AdapterResult(
                ProviderState.HEALTH_UNKNOWN,
                "network_authorization_required",
                network_called=False,
            )
        credentials = self._credentials(config)
        if credentials is None:
            return self._credential_result()
        if fixture.league_code not in self.league_ids:
            return AdapterResult(
                ProviderState.UNSUPPORTED_LEAGUE,
                "unsupported_league",
                network_called=False,
            )
        requested_at = _utc(requested_at, "requested_at")
        request = ProviderRequest(
            self.name,
            f"{SPORTSGAMEODDS_API_URL}/events",
            {
                "leagueID": self.league_ids[fixture.league_code],
                "type": "match",
                "oddsAvailable": "true",
                "live": "false",
                "started": "false",
                "oddID": ",".join(SPORTSGAMEODDS_REGULATION_MARKET_IDS),
                "includeOpposingOdds": "true",
                "limit": "100",
            },
            {"X-Api-Key": credentials[0], "Accept": "application/json"},
        )
        try:
            response = self._request(request, config, authorization=authorization)
        except ProductionContractError:
            return AdapterResult(
                ProviderState.HEALTH_UNKNOWN,
                "network_authorization_required",
                network_called=False,
            )
        classified = self._classify_response(response)
        if classified is not None:
            return classified
        if (
            not isinstance(response.payload, Mapping)
            or response.payload.get("success") is False
        ):
            return self._base_result(
                response, ProviderState.MALFORMED, "api_body_error"
            )
        events = response.payload.get("data")
        if not isinstance(events, list):
            return self._base_result(
                response, ProviderState.MALFORMED, "payload_data_not_list"
            )
        for event in events:
            if not isinstance(event, Mapping):
                continue
            if (
                provider_fixture_id
                and str(event.get("eventID", "")).strip() != provider_fixture_id
            ):
                continue
            try:
                observations = observations_for_event(
                    fixture,
                    event,
                    config=config,
                    experiment=self.experiment,
                    request_identity=request_identity,
                    requested_at=requested_at,
                    provider_priority=provider_priority,
                    captured_at=response.completed_at,
                    aliases=self._aliases,
                    kickoff_tolerance_seconds=timing_policy.kickoff_tolerance_seconds,
                    evidence_kind="NETWORK_OBSERVED_CANDIDATE",
                    raw_response=response,
                )
            except SportsGameOddsError as exc:
                return self._base_result(response, exc.state, exc.reason)
            return _accepted_result(
                response, observations[0], "accepted_candidate_only"
            )
        return self._base_result(
            response,
            ProviderState.UNSUPPORTED_FIXTURE,
            "fixture_not_found_or_identity_mismatch",
        )


__all__ = [
    "SPORTSGAMEODDS_API_URL",
    "SPORTSGAMEODDS_INTERNAL_LEAGUE",
    "SPORTSGAMEODDS_LEAGUE_ID",
    "SPORTSGAMEODDS_PROVIDER",
    "SPORTSGAMEODDS_REGULATION_MARKET_IDS",
    "SportsGameOddsAdapter",
    "SportsGameOddsError",
    "SportsGameOddsExperimentConfig",
    "build_sportsgameodds_provider_config",
]
