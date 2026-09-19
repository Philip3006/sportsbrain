"""APP-B4 compatibility proof for a future TheRundown Top-5 observation.

These tests use only deterministic synthetic ``NormalizedOddsObservation``
values.  TheRundown is intentionally not registered in the active provider
repertoire; the test-only observation can cross the existing shadow contracts
only as explicitly synthetic, candidate-only evidence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshotKind,
    ProductionContractError,
)
from src.football.provider_cascade import (
    DEFAULT_PROVIDER_ORDER,
    FOOTBALL_PROVIDER_REPERTOIRE,
    MARKET_PREMATCH_1X2,
    CascadeDecisionTrace,
    CascadeResult,
    NormalizedOddsObservation,
    ProviderAttemptTrace,
    ProviderCascadeConfig,
    ProviderState,
    TimingProvenance,
    TransportCapability,
    accepted_for_builder1,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_real_shadow_contracts import (
    REAL_OBSERVED_MARKER,
    TEST_FIXTURE_MARKER,
    NormalizedProviderObservation,
    RealShadowContractError,
    RealShadowExperiment,
)
from src.football.top5_real_shadow_session import (
    RealShadowSession,
    RealShadowSessionStatus,
)

BASE = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
SOURCE = BASE - timedelta(seconds=30)
KICKOFF = BASE + timedelta(hours=3)
INTEGRATION_SHA = "b" * 40
LEAGUE_NAMES = {
    "EPL": "Premier League",
    "BL1": "Bundesliga",
    "LL": "La Liga",
    "SA": "Serie A",
    "L1": "Ligue 1",
}


def _experiment() -> RealShadowExperiment:
    return RealShadowExperiment(
        "shadow-experiment:app-b4-therundown-v1",
        minimum_lead_minutes=30,
        maximum_lead_minutes=180,
        maximum_odds_age_seconds=300,
        kickoff_tolerance_seconds=0,
    )


def _therundown_observation(league_code: str) -> NormalizedOddsObservation:
    label = LEAGUE_NAMES[league_code]
    fixture_key = f"therundown:{league_code}:fixture-001"
    return NormalizedOddsObservation(
        league_code=league_code,
        fixture_key=fixture_key,
        provider_fixture_id=f"tr-{league_code}-fixture-001",
        home_team=f"{label} Home",
        away_team=f"{label} Away",
        kickoff_utc=KICKOFF,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=2.15,
        draw_odds=3.35,
        away_odds=3.70,
        provider_identity="therundown",
        bookmaker_identity="therundown:bookmaker:alpha",
        source_timestamp=SOURCE,
        captured_at=BASE,
        request_identity=f"test-injected:therundown:{league_code}:request-001",
        request_started_at=BASE - timedelta(seconds=1),
        request_completed_at=BASE,
        latency_ms=0,
        provider_priority=0,
        fallback_depth=0,
        source_provenance=f"synthetic://therundown/top5/{league_code}/odds",
        raw_record_digest="a" * 64,
        adapter_version="therundown-top5-test-v1",
        candidate_only=True,
        metadata={
            "evidence_kind": TEST_FIXTURE_MARKER,
            "synthetic": True,
            "transport_capability": TransportCapability.TEST_INJECTED.value,
            "provider": "therundown",
        },
        source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
    )


def _shadow_observation(
    observation: NormalizedOddsObservation,
) -> NormalizedProviderObservation:
    """Project the canonical candidate into the existing fixture-only seam."""

    return NormalizedProviderObservation(
        league_code=observation.league_code,
        fixture_key=observation.fixture_key,
        provider_fixture_id=observation.provider_fixture_id,
        home_team=observation.home_team,
        away_team=observation.away_team,
        kickoff_utc=observation.kickoff_utc,
        market_type="h2h_1x2",
        home_odds=observation.home_odds,
        draw_odds=observation.draw_odds,
        away_odds=observation.away_odds,
        provider_identity=observation.provider_identity,
        bookmaker_identity=observation.bookmaker_identity,
        source_timestamp=observation.source_timestamp,
        captured_at=observation.captured_at,
        request_identity=observation.request_identity,
        raw_record_digest=observation.raw_record_digest,
        adapter_version=observation.adapter_version,
        provider_priority=observation.provider_priority + 1,
        fallback_depth=observation.fallback_depth,
        cascade_trace={
            "configured_provider_order": [observation.provider_identity],
            "selected": observation.provider_identity,
            "network_called": False,
            "transport_capability": TransportCapability.TEST_INJECTED.value,
        },
        quality_metadata={
            "market": observation.market_type,
            "source_provenance": observation.source_provenance,
            "evidence_kind": TEST_FIXTURE_MARKER,
        },
        eligibility_state="eligible",
        signal_snapshot_id=f"test-fixture-snapshot:{observation.league_code}",
        independent_validation=None,
        observation_mode=TEST_FIXTURE_MARKER,
        latency_ms=observation.latency_ms,
        network_request_count=0,
        request_cost_units=0.0,
    )


def _session(league_code: str, *, fixture_mode: bool) -> RealShadowSession:
    return RealShadowSession.create(
        f"shadow-session:app-b4-therundown:{league_code}",
        experiment=_experiment(),
        league_scope=(league_code,),
        integration_sha=INTEGRATION_SHA,
        created_at=BASE,
        fixture_mode=fixture_mode,
    )


@pytest.mark.parametrize("league_code", tuple(LEAGUE_NAMES))
def test_therundown_candidate_preserves_identity_and_prices_across_shadow_contracts(
    league_code: str,
) -> None:
    canonical = _therundown_observation(league_code)
    canonical.validate(require_fresh=False)
    shadow = _shadow_observation(canonical)
    shadow.validate(_experiment())

    fixture = shadow.fixture()
    snapshot = shadow.snapshot()
    assert fixture == Fixture(
        canonical.fixture_key,
        canonical.league_code,
        canonical.home_team,
        canonical.away_team,
        canonical.kickoff_utc,
    )
    assert shadow.league_code == canonical.league_code == league_code
    assert shadow.fixture_key == canonical.fixture_key
    assert shadow.provider_identity == canonical.provider_identity == "therundown"
    assert shadow.bookmaker_identity == canonical.bookmaker_identity
    assert (shadow.home_odds, shadow.draw_odds, shadow.away_odds) == (
        canonical.home_odds,
        canonical.draw_odds,
        canonical.away_odds,
    )
    assert shadow.source_timestamp == canonical.source_timestamp == SOURCE
    assert shadow.captured_at == canonical.captured_at == BASE
    assert shadow.quality_metadata["source_provenance"] == canonical.source_provenance
    assert snapshot.fixture_key == canonical.fixture_key
    assert snapshot.captured_at == SOURCE
    assert snapshot.kind is MarketSnapshotKind.SIGNAL_TIME
    assert snapshot.source == "test_fixture:therundown:therundown:bookmaker:alpha"
    assert dict(snapshot.odds) == {
        "home": canonical.home_odds,
        "draw": canonical.draw_odds,
        "away": canonical.away_odds,
    }

    session = _session(league_code, fixture_mode=True)
    assert session.record_observation(shadow) is True
    session.finalize_predictions()
    session.validate()
    prediction = next(iter(session.predictions.values()))

    assert prediction.marker == TEST_FIXTURE_MARKER
    assert prediction.league_code == league_code
    assert prediction.fixture_key == canonical.fixture_key
    assert prediction.provider_identity == "therundown"
    assert prediction.bookmaker_identity == canonical.bookmaker_identity
    assert prediction.provider_fixture_id == canonical.provider_fixture_id
    assert prediction.source_timestamp == SOURCE
    assert prediction.captured_at == BASE
    assert prediction.no_bet is True
    assert prediction.publication is False
    assert prediction.activation is False
    assert session.publication is False
    assert session.activation is False
    assert session.status is RealShadowSessionStatus.AWAITING_RESULTS


def test_test_fixture_snapshot_cannot_be_labeled_real_observed() -> None:
    shadow = _shadow_observation(_therundown_observation("EPL"))
    with pytest.raises(
        RealShadowContractError, match="canonical|receipt|REAL_OBSERVED"
    ):
        replace(shadow, observation_mode=REAL_OBSERVED_MARKER).validate(_experiment())


def test_locally_constructed_real_evidence_cannot_promote_the_candidate() -> None:
    shadow = _shadow_observation(_therundown_observation("BL1"))
    with pytest.raises(RealShadowContractError, match="canonical|receipt"):
        replace(
            shadow,
            observation_mode=REAL_OBSERVED_MARKER,
            independent_validation={
                "accepted": True,
                "prediction_input_allowed": True,
                "evidence_kind": REAL_OBSERVED_MARKER,
            },
            canonical_observation={
                "evidence_kind": REAL_OBSERVED_MARKER,
                "fixture_key": shadow.fixture_key,
            },
        ).validate(_experiment())


def test_synthetic_candidate_is_rejected_by_non_fixture_real_shadow_session() -> None:
    session = _session("LL", fixture_mode=False)
    assert session.record_observation(
        _shadow_observation(_therundown_observation("LL"))
    )
    session.finalize_predictions()
    assert session.status is RealShadowSessionStatus.FAILED_CLOSED
    assert session.predictions == {}
    assert session.publication is False
    assert session.activation is False


def test_candidate_cascade_is_not_builder1_authority() -> None:
    observation = _therundown_observation("SA")
    attempt = ProviderAttemptTrace(
        attempt_index=0,
        provider="therundown",
        state=ProviderState.AVAILABLE,
        result="success",
        reason="synthetic_candidate",
        network_called=False,
        request_identity=observation.request_identity,
        transport_capability=TransportCapability.TEST_INJECTED,
        configured_provider_order=("therundown",),
        preflight_allowed=False,
        budget_decision="REJECTED",
        network_request_count=0,
        provider_record_id=observation.provider_fixture_id,
        raw_record_digest=observation.raw_record_digest,
        fixture_key=observation.fixture_key,
        league_code=observation.league_code,
        home_team=observation.home_team,
        away_team=observation.away_team,
        kickoff=observation.kickoff_utc,
        home_odds=observation.home_odds,
        draw_odds=observation.draw_odds,
        away_odds=observation.away_odds,
        bookmaker_identity=observation.bookmaker_identity,
        source_identity=observation.source_provenance,
        source_timestamp=observation.source_timestamp,
        adapter_version=observation.adapter_version,
    )
    trace = CascadeDecisionTrace(
        fixture_key=observation.fixture_key,
        attempts=(attempt,),
        selected_provider="therundown",
        fallback_depth=0,
        total_latency_ms=0,
        fail_closed=False,
        configured_provider_order=("therundown",),
    )
    result = CascadeResult(observation, trace)
    result.validate()
    assert result.accepted is True
    assert result.observation is not None and result.observation.candidate_only is True
    with pytest.raises(ValueError, match="receipt|self-authorize"):
        accepted_for_builder1(result)


def test_therundown_is_not_in_active_provider_repertoire_or_configuration() -> None:
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert DEFAULT_PROVIDER_ORDER == ("the_odds_api",)
    assert all(
        adapter.config.provider_mapping is not None
        and adapter.config.provider_mapping.provider_name == "the_odds_api"
        for adapter in TOP5_LEAGUE_ADAPTERS.values()
    )
    with pytest.raises(ProductionContractError, match="unknown providers"):
        ProviderCascadeConfig.from_mapping(
            {
                "provider_order": ["therundown"],
                "providers": {"therundown": {}},
            }
        )


def test_all_five_league_codes_are_the_existing_top5_scope() -> None:
    assert set(LEAGUE_NAMES) == {"EPL", "BL1", "LL", "SA", "L1"}
    assert set(LEAGUE_NAMES) == set(TOP5_LEAGUE_ADAPTERS)
