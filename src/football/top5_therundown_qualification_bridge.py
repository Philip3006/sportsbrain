"""No-network bridge from TheRundown observations to the APP-B1 gate."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite

from src.football.production_contracts import Fixture, ProductionContractError
from src.football.provider_cascade.adapters import AdapterResult
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    ProviderState,
    TimingProvenance,
    digest_record,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    TOP5_LEAGUES,
    ObservationEvidenceKind,
)
from src.football.top5_therundown_qualification import (
    EVIDENCE_SCHEMA_VERSION,
    THERUNDOWN_PROVIDER_IDENTITY,
)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_AUTHORIZATION_FIELDS = frozenset(
    {
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "provider_identity",
        "canonical_league",
        "fixture_key",
        "provider_event_id",
        "provider_request_id",
        "provider_scope",
        "league_scope",
        "fixture_scope",
        "network_execution",
        "no_bet",
        "publication_enabled",
        "monetary_spend_authorized",
    }
)


class TheRundownBridgeError(ProductionContractError):
    """The adapter result cannot be represented as trusted B1 evidence."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TheRundownBridgeError(f"{name} is required")
    return value


def _digest(value: object, name: str, *, source_sha: bool = False) -> str:
    value = _text(value, name)
    matcher = _SHA_RE if source_sha else _DIGEST_RE
    if matcher.fullmatch(value) is None:
        raise TheRundownBridgeError(f"{name} must be a hexadecimal digest")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TheRundownBridgeError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _scope(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TheRundownBridgeError(f"{name} must be a sequence")
    result = tuple(_text(item, f"{name} item") for item in value)
    if not result or len(result) != len(set(result)):
        raise TheRundownBridgeError(f"{name} must be non-empty and unique")
    return result


def _validate_authorization(
    metadata: Mapping[str, object],
    *,
    evidence_kind: ObservationEvidenceKind,
    network_request_count: int,
    observation: NormalizedOddsObservation,
    canonical_league: str,
) -> None:
    if set(metadata) != _AUTHORIZATION_FIELDS:
        raise TheRundownBridgeError("authorization metadata schema is incomplete")
    expected = {
        "provider_identity": THERUNDOWN_PROVIDER_IDENTITY,
        "canonical_league": canonical_league,
        "fixture_key": observation.fixture_key,
        "provider_event_id": observation.provider_fixture_id,
        "provider_request_id": observation.request_identity,
    }
    for name, value in expected.items():
        if metadata.get(name) != value:
            raise TheRundownBridgeError(f"authorization binding mismatch: {name}")
    for name in (
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
    ):
        _text(metadata.get(name), name)
    provider_scope = _scope(metadata.get("provider_scope"), "provider_scope")
    league_scope = _scope(metadata.get("league_scope"), "league_scope")
    fixture_scope = _scope(metadata.get("fixture_scope"), "fixture_scope")
    if THERUNDOWN_PROVIDER_IDENTITY not in provider_scope:
        raise TheRundownBridgeError(
            "authorization provider scope does not bind provider"
        )
    if canonical_league not in league_scope:
        raise TheRundownBridgeError("authorization league scope does not bind league")
    if observation.fixture_key not in fixture_scope:
        raise TheRundownBridgeError("authorization fixture scope does not bind fixture")
    safety = (
        ("network_execution", network_request_count == 1),
        ("no_bet", True),
        ("publication_enabled", False),
        ("monetary_spend_authorized", False),
    )
    if evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED:
        safety = tuple(
            (name, False if name == "network_execution" else expected_value)
            for name, expected_value in safety
        )
    for name, expected_value in safety:
        if metadata.get(name) is not expected_value:
            raise TheRundownBridgeError(f"unsafe authorization metadata: {name}")


def _validate_quota_consistency(
    observation: NormalizedOddsObservation,
    *,
    network_request_count: int,
    quota_cost_units: float,
) -> None:
    before = observation.quota_state_before
    after = observation.quota_state_after
    rate = observation.rate_limit_state
    if (
        (before.used is None and before.remaining is None)
        or (after.used is None and after.remaining is None)
        or (rate.rate_limit is None and rate.rate_remaining is None)
    ):
        raise TheRundownBridgeError("quota and rate-limit evidence is incomplete")
    if before.used is not None and after.used is not None and after.used < before.used:
        raise TheRundownBridgeError("quota usage moved backwards")
    if (
        before.remaining is not None
        and after.remaining is not None
        and after.remaining > before.remaining
    ):
        raise TheRundownBridgeError("quota remaining moved backwards")
    if (
        rate.rate_limit is not None
        and rate.rate_remaining is not None
        and rate.rate_remaining > rate.rate_limit
    ):
        raise TheRundownBridgeError("rate-limit remaining exceeds its limit")
    if (
        before.rate_remaining is not None
        and rate.rate_remaining is not None
        and rate.rate_remaining > before.rate_remaining
    ):
        raise TheRundownBridgeError("rate-limit remaining moved backwards")
    if (
        network_request_count == 1
        and before.used is not None
        and after.used is not None
        and float(after.used - before.used) != quota_cost_units
    ):
        raise TheRundownBridgeError("quota cost does not match quota usage")


def bridge_therundown_observation(
    result: AdapterResult,
    *,
    expected_fixture: Fixture,
    evidence_id: str,
    observation_id: str,
    provider_league_code: str,
    provider_league_identity_verified: bool,
    evidence_kind: ObservationEvidenceKind | str,
    synthetic_reconstruction: bool,
    network_request_count: int,
    maximum_odds_age_seconds: int,
    quota_cost_units: float,
    adapter_source_sha: str,
    authorization_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Project one adapter result into the strict APP-B1 evidence envelope.

    All identity and authority values that are absent from the provider-neutral
    observation are explicit required inputs. No value is inferred from a
    caller hint, and this function performs no provider or filesystem I/O.
    """

    if not isinstance(result, AdapterResult):
        raise TheRundownBridgeError("adapter result is required")
    if not isinstance(expected_fixture, Fixture):
        raise TheRundownBridgeError("expected fixture is required")
    expected_fixture.validate()
    _text(evidence_id, "evidence_id")
    _text(observation_id, "observation_id")
    _text(provider_league_code, "provider_league_code")
    _digest(adapter_source_sha, "adapter_source_sha", source_sha=True)
    if provider_league_identity_verified is not True:
        raise TheRundownBridgeError(
            "provider league identity must be explicitly verified"
        )
    try:
        kind = ObservationEvidenceKind(evidence_kind)
    except (TypeError, ValueError) as exc:
        raise TheRundownBridgeError("evidence_kind is invalid") from exc
    if not isinstance(synthetic_reconstruction, bool):
        raise TheRundownBridgeError("synthetic_reconstruction must be boolean")
    if kind is ObservationEvidenceKind.REAL_OBSERVED and synthetic_reconstruction:
        raise TheRundownBridgeError("REAL_OBSERVED evidence cannot be synthetic")
    if (
        kind is not ObservationEvidenceKind.REAL_OBSERVED
        and not synthetic_reconstruction
    ):
        raise TheRundownBridgeError("non-real evidence must be marked synthetic")
    if (
        isinstance(network_request_count, bool)
        or not isinstance(network_request_count, int)
        or network_request_count not in {0, 1}
    ):
        raise TheRundownBridgeError("network_request_count must be zero or one")
    if not isinstance(result.network_called, bool):
        raise TheRundownBridgeError("adapter network_called must be boolean")
    if network_request_count != int(result.network_called):
        raise TheRundownBridgeError("request count does not match adapter execution")
    if kind is ObservationEvidenceKind.REAL_OBSERVED and network_request_count != 1:
        raise TheRundownBridgeError("REAL_OBSERVED evidence requires one request")
    if (
        not isinstance(maximum_odds_age_seconds, int)
        or isinstance(maximum_odds_age_seconds, bool)
        or maximum_odds_age_seconds <= 0
    ):
        raise TheRundownBridgeError("maximum_odds_age_seconds must be positive")
    if (
        isinstance(quota_cost_units, bool)
        or not isinstance(quota_cost_units, (int, float))
        or not isfinite(float(quota_cost_units))
        or quota_cost_units < 0
    ):
        raise TheRundownBridgeError("quota_cost_units must be finite and non-negative")

    try:
        result.validate()
    except (ProductionContractError, TypeError, ValueError) as exc:
        raise TheRundownBridgeError("adapter result is invalid") from exc
    if result.state is not ProviderState.AVAILABLE or result.observation is None:
        raise TheRundownBridgeError("only an available observation can be bridged")
    if kind is ObservationEvidenceKind.REAL_OBSERVED and result.status_code != 200:
        raise TheRundownBridgeError("REAL_OBSERVED evidence requires HTTP 200")
    observation = result.observation
    try:
        observation.validate(require_fresh=False)
    except (ProductionContractError, TypeError, ValueError) as exc:
        raise TheRundownBridgeError("normalized observation is invalid") from exc
    if observation.candidate_only is not True:
        raise TheRundownBridgeError("only candidate-only observations are eligible")
    if observation.provider_identity != THERUNDOWN_PROVIDER_IDENTITY:
        raise TheRundownBridgeError("provider identity mismatch")
    if observation.league_code not in TOP5_LEAGUES:
        raise TheRundownBridgeError("observation league is outside Top-5 scope")
    if observation.league_code != expected_fixture.league_code:
        raise TheRundownBridgeError("fixture league mismatch")
    if observation.fixture_key != expected_fixture.fixture_key:
        raise TheRundownBridgeError("fixture key mismatch")
    if (
        observation.home_team.strip().casefold()
        != expected_fixture.home_team.strip().casefold()
        or observation.away_team.strip().casefold()
        != expected_fixture.away_team.strip().casefold()
        or observation.kickoff_utc != expected_fixture.kickoff
    ):
        raise TheRundownBridgeError("fixture participant or kickoff mismatch")
    if observation.market_type != MARKET_PREMATCH_1X2:
        raise TheRundownBridgeError("pre-match 1X2 market is required")
    if not observation.bookmaker_identity.strip():
        raise TheRundownBridgeError("bookmaker identity is required")
    source_timestamp = observation.source_timestamp
    captured_at = _aware(observation.captured_at, "captured_at")
    if source_timestamp is None:
        raise TheRundownBridgeError("source timestamp is required")
    source_timestamp = _aware(source_timestamp, "source_timestamp")
    age_seconds = (captured_at - source_timestamp).total_seconds()
    if age_seconds < 0 or age_seconds > maximum_odds_age_seconds:
        raise TheRundownBridgeError("odds are stale or timestamped in the future")
    if observation.source_timing_provenance is not TimingProvenance.SOURCE_TIMESTAMP:
        raise TheRundownBridgeError("source timestamp provenance is required")
    if captured_at >= _aware(observation.kickoff_utc, "kickoff"):
        raise TheRundownBridgeError("only pre-match capture is eligible")
    prices = (observation.home_odds, observation.draw_odds, observation.away_odds)
    if any(
        value is None or not isfinite(float(value)) or float(value) <= 1.0
        for value in prices
    ):
        raise TheRundownBridgeError("complete finite 1X2 prices are required")
    raw_response_digest = _digest(result.raw_response_digest, "raw_response_digest")
    normalized_digest = _digest(
        result.normalized_record_digest, "normalized_record_digest"
    )
    provider_record_digest = _digest(
        observation.raw_record_digest, "provider_record_digest"
    )
    if observation.quota_state_after != result.quota_after:
        raise TheRundownBridgeError(
            "quota-after evidence does not match adapter result"
        )
    if observation.rate_limit_state != result.rate_limit_state:
        raise TheRundownBridgeError("rate-limit evidence does not match adapter result")
    _validate_authorization(
        authorization_metadata,
        evidence_kind=kind,
        network_request_count=network_request_count,
        observation=observation,
        canonical_league=expected_fixture.league_code,
    )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "provider_identity": observation.provider_identity,
        "evidence_kind": kind.value,
        "canonical_league": observation.league_code,
        "provider_league_code": provider_league_code,
        "provider_league_identity_verified": True,
        "fixture_observed": True,
        "fixture_key": observation.fixture_key,
        "provider_event_id": observation.provider_fixture_id,
        "home_team": observation.home_team,
        "away_team": observation.away_team,
        "home_away_identity_verified": True,
        "kickoff": _aware(observation.kickoff_utc, "kickoff").isoformat(),
        "market_type": observation.market_type,
        "market_phase": "PRE_MATCH",
        "odds": {
            "home": observation.home_odds,
            "draw": observation.draw_odds,
            "away": observation.away_odds,
        },
        "bookmaker_observed": True,
        "bookmaker_identity": observation.bookmaker_identity,
        "source_timestamp": source_timestamp.isoformat(),
        "source_timing_provenance": observation.source_timing_provenance.value,
        "captured_at": captured_at.isoformat(),
        "provider_request_id": observation.request_identity,
        "observation_id": observation_id,
        "source_provenance": observation.source_provenance,
        "raw_record_digest": raw_response_digest,
        "provider_record_digest": provider_record_digest,
        "normalized_record_digest": normalized_digest,
        "adapter_version": observation.adapter_version,
        "adapter_source_sha": adapter_source_sha,
        "authorization_metadata": dict(authorization_metadata),
        "quota_state_before": observation.quota_state_before.as_payload(),
        "quota_state_after": result.quota_after.as_payload(),
        "rate_limit_state": result.rate_limit_state.as_payload(),
        "quota_cost_units": float(quota_cost_units),
        "network_request_count": network_request_count,
        "synthetic_reconstruction": synthetic_reconstruction,
        "provider_status": result.state.value,
        "failure_codes": [],
    }


def bridge_therundown_observations(
    observations: Sequence[NormalizedOddsObservation],
    *,
    expected_fixture: Fixture,
    evidence_ids: Mapping[str, str],
    observation_ids: Mapping[str, str],
    provider_league_code: str,
    provider_league_identity_verified: bool,
    evidence_kind: ObservationEvidenceKind | str,
    synthetic_reconstruction: bool,
    network_request_count: int,
    response_status_code: int,
    maximum_odds_age_seconds: int,
    quota_cost_units: float,
    adapter_source_sha: str,
    authorization_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Project the complete tuple returned by ``fetch_observations``.

    The B2 adapter deliberately returns normalized observations rather than an
    ``AdapterResult`` so every complete bookmaker survives the legacy cascade
    boundary. Response status and request count are therefore explicit caller
    attestations; they are never inferred from an observation-only tuple.
    """

    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise TheRundownBridgeError("observations must be a sequence")
    items = tuple(observations)
    if not items:
        raise TheRundownBridgeError("at least one observation is required")
    if any(not isinstance(item, NormalizedOddsObservation) for item in items):
        raise TheRundownBridgeError("observations must use the normalized contract")
    if (
        isinstance(response_status_code, bool)
        or not isinstance(response_status_code, int)
        or response_status_code != 200
    ):
        raise TheRundownBridgeError(
            "accepted fetch_observations output requires HTTP 200"
        )
    bookmakers = tuple(item.bookmaker_identity for item in items)
    if any(not value.strip() for value in bookmakers) or len(set(bookmakers)) != len(
        bookmakers
    ):
        raise TheRundownBridgeError(
            "bookmaker observations must be identified uniquely"
        )
    if not isinstance(evidence_ids, Mapping) or not isinstance(
        observation_ids, Mapping
    ):
        raise TheRundownBridgeError(
            "explicit observation and evidence IDs are required"
        )
    if set(evidence_ids) != set(bookmakers) or set(observation_ids) != set(bookmakers):
        raise TheRundownBridgeError(
            "every bookmaker must have explicit evidence and observation IDs"
        )
    provider_league_code = _text(provider_league_code, "provider_league_code")
    provider_league_code_key = provider_league_code.casefold()
    for item in items:
        provider_metadata = item.metadata
        observed_provider_league = provider_metadata.get("league_name")
        if (
            not isinstance(observed_provider_league, str)
            or observed_provider_league.strip().casefold() != provider_league_code_key
            or provider_metadata.get("competition_identity") != item.league_code
        ):
            raise TheRundownBridgeError(
                "provider league identity is not bound to the observation"
            )

    first = items[0]
    raw_response_digest = _digest(
        first.metadata.get("raw_response_digest"), "raw_response_digest"
    )
    common = (
        first.provider_identity,
        first.league_code,
        first.fixture_key,
        first.provider_fixture_id,
        first.request_identity,
        first.kickoff_utc,
        first.captured_at,
        first.request_started_at,
        first.request_completed_at,
        first.quota_state_before,
        first.quota_state_after,
        first.rate_limit_state,
        first.raw_record_digest,
        first.adapter_version,
        raw_response_digest,
    )
    for item in items[1:]:
        item_raw_response_digest = _digest(
            item.metadata.get("raw_response_digest"), "raw_response_digest"
        )
        if (
            item.provider_identity,
            item.league_code,
            item.fixture_key,
            item.provider_fixture_id,
            item.request_identity,
            item.kickoff_utc,
            item.captured_at,
            item.request_started_at,
            item.request_completed_at,
            item.quota_state_before,
            item.quota_state_after,
            item.rate_limit_state,
            item.raw_record_digest,
            item.adapter_version,
            item_raw_response_digest,
        ) != common:
            raise TheRundownBridgeError(
                "bookmaker observations do not share one request provenance"
            )

    _validate_quota_consistency(
        first,
        network_request_count=network_request_count,
        quota_cost_units=float(quota_cost_units),
    )

    bridged: list[dict[str, object]] = []
    for item in items:
        result = AdapterResult(
            state=ProviderState.AVAILABLE,
            reason="accepted_candidate_only",
            observation=item,
            status_code=response_status_code,
            network_called=network_request_count == 1,
            quota_after=item.quota_state_after,
            rate_limit_state=item.rate_limit_state,
            raw_response_digest=raw_response_digest,
            normalized_record_digest=digest_record(item.as_payload()),
        )
        bridged.append(
            bridge_therundown_observation(
                result,
                expected_fixture=expected_fixture,
                evidence_id=evidence_ids[item.bookmaker_identity],
                observation_id=observation_ids[item.bookmaker_identity],
                provider_league_code=provider_league_code,
                provider_league_identity_verified=provider_league_identity_verified,
                evidence_kind=evidence_kind,
                synthetic_reconstruction=synthetic_reconstruction,
                network_request_count=network_request_count,
                maximum_odds_age_seconds=maximum_odds_age_seconds,
                quota_cost_units=quota_cost_units,
                adapter_source_sha=adapter_source_sha,
                authorization_metadata=authorization_metadata,
            )
        )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "provider_identity": THERUNDOWN_PROVIDER_IDENTITY,
        "evidence": bridged,
    }


__all__ = [
    "TheRundownBridgeError",
    "bridge_therundown_observation",
    "bridge_therundown_observations",
]
