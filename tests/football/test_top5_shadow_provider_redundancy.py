"""Injected, no-network tests for the Top-5 shadow provider boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_provider_redundancy import (
    CURRENT_EXHAUSTED_ODDS_API,
    AdapterStatus,
    BetfairAdapter,
    CapabilityKind,
    CapabilityStatus,
    CompletenessState,
    EvidenceIdentity,
    ExpectedFixture,
    FootballDataClosingAdapter,
    MarketRole,
    OddsApiQuotaSnapshot,
    OddsApiRequestKind,
    OddsPortalAdapter,
    ProviderPlanningRequest,
    ProviderReadinessState,
    ShadowProviderContractError,
    ShadowSourceObservation,
    SourceError,
    SourceNormalizationError,
    SourceProvenance,
    SourceQualityPolicy,
    TheOddsAPIAdapter,
    assess_provider_readiness,
    authorize_odds_api_request,
    build_shadow_evidence_bundle,
    canonical_observation_digest,
    guarded_odds_api_call,
    make_fixture_key,
    normalize_team_name,
    plan_provider_paths,
    provider_inventory,
    provider_readiness_state,
    supported_candidate_adapters,
    to_shadow_observation_evidence,
    to_shadow_provider_evidence,
    validate_observation_batch,
    validate_source_observation,
)

UTC = timezone.utc
CAPTURED = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
KICKOFF = CAPTURED + timedelta(hours=2)
TEST_MAX_ODDS_AGE_SECONDS = 900
TEST_KICKOFF_TOLERANCE_SECONDS = 300
TEST_POLICY = SourceQualityPolicy(
    maximum_odds_age_seconds=TEST_MAX_ODDS_AGE_SECONDS,
    kickoff_tolerance_seconds=TEST_KICKOFF_TOLERANCE_SECONDS,
)
TEST_LIVE_PATH_PROOF = {
    "top5_competition_identity": True,
    "fixture_identity": True,
    "exact_kickoff": True,
    "source_timestamp": True,
    "complete_1x2": True,
    "provenance": True,
}


def _expected(
    *,
    league: str = "EPL",
    home: str = "Manchester United",
    away: str = "Arsenal",
    kickoff: datetime = KICKOFF,
) -> ExpectedFixture:
    return ExpectedFixture(
        league=league,
        fixture_key=make_fixture_key(league, home, away, kickoff),
        home_team=home,
        away_team=away,
        kickoff=kickoff,
    )


def _provenance(
    provider: str = "injected",
    *,
    raw_id: str = "record-1",
    retrieved_at: datetime = CAPTURED,
) -> SourceProvenance:
    uris = {
        "the_odds_api": "https://api.the-odds-api.com/v4/sports/soccer_epl/odds",
        "betfair": "https://api.betfair.com/exchange/betting/rest/v1.0/listMarketBook/",
        "oddsportal": "https://www.oddsportal.com/matches/football/2026-09-15/",
        "football_data": "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
    }
    return SourceProvenance(
        source_uri=uris.get(provider, "payload://injected"),
        raw_record_id=raw_id,
        retrieved_at=retrieved_at,
        adapter_version="test-adapter-v1",
    )


def _observation(
    expected: ExpectedFixture | None = None,
    *,
    provider: str = "the_odds_api",
    bookmaker: str | None = "test-bookmaker",
    home: str | None = None,
    away: str | None = None,
    kickoff: datetime | None = None,
    source_timestamp: datetime | None = CAPTURED - timedelta(minutes=5),
    home_odds: float | None = 2.2,
    draw_odds: float | None = 3.4,
    away_odds: float | None = 3.0,
    error: SourceError = SourceError.NONE,
    completeness: CompletenessState = CompletenessState.COMPLETE,
    role: MarketRole = MarketRole.SIGNAL_TIME,
    fixture_key: str | None = None,
) -> ShadowSourceObservation:
    expected = expected or _expected()
    home = home or expected.home_team
    away = away or expected.away_team
    kickoff = kickoff or expected.kickoff
    return ShadowSourceObservation(
        league=expected.league,
        fixture_key=fixture_key
        or make_fixture_key(expected.league, home, away, kickoff),
        home_team=home,
        away_team=away,
        kickoff=kickoff,
        provider_identity=provider,
        bookmaker_identity=bookmaker,
        market_type="h2h_1x2",
        home_odds=home_odds,
        draw_odds=draw_odds,
        away_odds=away_odds,
        capture_timestamp=CAPTURED,
        source_timestamp=source_timestamp,
        request_identity="request-1",
        source_provenance=_provenance(provider),
        completeness=completeness,
        confidence=0.9,
        error_classification=error,
        market_role=role,
    )


def _identity(prefix: str = "1") -> EvidenceIdentity:
    return EvidenceIdentity(
        evidence_id=f"evidence-{prefix}",
        artifact_id=f"artifact-{prefix}",
        artifact_sha="a" * 64,
        source_sha="b" * 64,
        candidate_id="candidate-shadow-v1",
        model_identity="unbound-model-slot",
        research_sha=FROZEN_RESEARCH_SHA,
    )


def _toa_payload(expected: ExpectedFixture | None = None) -> dict[str, object]:
    expected = expected or _expected()
    return {
        "id": "toa-event-1",
        "sport_key": "soccer_epl",
        "commence_time": expected.kickoff.isoformat(),
        "home_team": "Man Utd",
        "away_team": expected.away_team,
        "bookmakers": [
            {
                "key": "pinnacle",
                "last_update": (CAPTURED - timedelta(minutes=5)).isoformat(),
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Manchester United", "price": 2.2},
                            {"name": "Draw", "price": 3.4},
                            {"name": "Arsenal", "price": 3.0},
                        ],
                    }
                ],
            }
        ],
    }


def test_inventory_classifies_each_capability_without_authority() -> None:
    inventory = provider_inventory()
    assert len(inventory) == len({(row.source, row.capability) for row in inventory})
    assert {row.capability for row in inventory} == set(CapabilityKind)
    assert all(row.authoritative_approved is False for row in inventory)
    football_data_close = next(
        row
        for row in inventory
        if row.source == "football_data"
        and row.capability is CapabilityKind.CLOSING_BENCHMARK
    )
    assert football_data_close.status is CapabilityStatus.HISTORICAL_ONLY
    assert football_data_close.closing_benchmark_only is True
    assert football_data_close.usable_at_signal_time is False


def test_candidate_adapters_are_explicit_and_do_not_include_bl2_pinnacle() -> None:
    assert supported_candidate_adapters() == ("the_odds_api", "betfair", "oddsportal")
    assert AdapterStatus.CANDIDATE_ONLY is not None


def test_contract_support_is_not_live_path_readiness() -> None:
    for provider in ("betfair", "oddsportal"):
        assessment = assess_provider_readiness(provider)
        assert assessment.contract_supported is True
        assert assessment.state is ProviderReadinessState.CONTRACT_SUPPORTED
        assert assessment.ready_for_observation is False
        assert (
            provider_readiness_state(provider)
            is ProviderReadinessState.CONTRACT_SUPPORTED
        )

        request = ProviderPlanningRequest(
            league="EPL",
            window_start=CAPTURED,
            window_end=CAPTURED + timedelta(hours=1),
            required_market="h2h_1x2",
            freshness_requirement_seconds=TEST_MAX_ODDS_AGE_SECONDS,
            allowed_sources=(provider,),
            fixture_count=1,
            credentials_available={provider: True},
        )
        path = plan_provider_paths(request).paths[0]
        assert path.operationally_possible is False
        assert path.readiness_state is ProviderReadinessState.CONTRACT_SUPPORTED
        assert path.signal_time_usable is False
        assert plan_provider_paths(request).candidate_quota_independent_paths == ()


def test_explicit_prerequisites_advance_only_to_ready_for_observation() -> None:
    assessment = assess_provider_readiness("betfair", TEST_LIVE_PATH_PROOF)
    assert assessment.state is ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
    assert assessment.ready_for_observation is True
    assert (
        assess_provider_readiness(
            "betfair",
            TEST_LIVE_PATH_PROOF,
            real_observation_validated=True,
        ).state
        is ProviderReadinessState.REAL_OBSERVATION_VALIDATED
    )


def test_partial_live_path_proof_remains_fail_closed() -> None:
    assessment = assess_provider_readiness(
        "oddsportal", {"fixture_identity": True, "complete_1x2": True}
    )
    assert assessment.state is ProviderReadinessState.LIVE_PATH_PREREQUISITES_MISSING
    assert "source_timestamp" in assessment.missing_prerequisites


def test_real_observation_state_cannot_be_asserted_without_complete_proof() -> None:
    with pytest.raises(ShadowProviderContractError, match="cannot be asserted"):
        assess_provider_readiness("betfair", real_observation_validated=True)


def test_team_aliases_are_exact_not_fuzzy() -> None:
    assert normalize_team_name("Man Utd") == "manchester united"
    assert normalize_team_name("Bayern München") == "bayern munich"
    assert normalize_team_name("United") == "united"
    assert normalize_team_name("Manchester United") != normalize_team_name("United")


def test_the_odds_api_adapter_normalizes_bookmaker_and_source_metadata() -> None:
    expected = _expected()
    observations = TheOddsAPIAdapter.normalize(
        _toa_payload(expected),
        expected,
        capture_timestamp=CAPTURED,
        request_identity="toa-request-1",
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation.provider_identity == "the_odds_api"
    assert observation.bookmaker_identity == "pinnacle"
    assert observation.odds_age_seconds == 300.0
    assert observation.signal_time_input_allowed is True
    assert (
        validate_source_observation(observation, expected, TEST_POLICY).accepted is True
    )


def test_the_odds_api_adapter_supports_multiple_bookmakers_without_selecting_one() -> (
    None
):
    payload = _toa_payload()
    payload["bookmakers"] = [
        payload["bookmakers"][0],
        {
            "key": "bet365",
            "last_update": (CAPTURED - timedelta(minutes=4)).isoformat(),
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Manchester United", "price": 2.1},
                        {"name": "Draw", "price": 3.5},
                        {"name": "Arsenal", "price": 3.2},
                    ],
                }
            ],
        },
    ]
    observations = TheOddsAPIAdapter.normalize(
        payload,
        _expected(),
        capture_timestamp=CAPTURED,
        request_identity="toa-request-2",
    )
    assert {observation.bookmaker_identity for observation in observations} == {
        "pinnacle",
        "bet365",
    }
    assert all(
        validate_source_observation(
            observation, _expected(), TEST_POLICY
        ).authority_approved
        is False
        for observation in observations
    )


def test_betfair_adapter_requires_enriched_kickoff_and_timestamp() -> None:
    expected = _expected(league="BL1", home="Bayern München", away="Borussia Dortmund")
    payload = {
        "market_id": "1.234",
        "event": {
            "league": "BL1",
            "home_team": "Bayern Munich",
            "away_team": "Borussia Dortmund",
            "kickoff": expected.kickoff.isoformat(),
        },
        "source_timestamp": (CAPTURED - timedelta(minutes=2)).isoformat(),
        "runners": [
            {"selection": "home", "price": 1.9},
            {"selection": "draw", "price": 3.8},
            {"selection": "away", "price": 4.0},
        ],
    }
    observation = BetfairAdapter.normalize(
        payload, expected, capture_timestamp=CAPTURED, request_identity="bf-request-1"
    )[0]
    assert observation.bookmaker_identity == "betfair_exchange"
    assert (
        validate_source_observation(observation, expected, TEST_POLICY).accepted is True
    )

    legacy_payload = {"market_id": "1.234", "runners": payload["runners"]}
    with pytest.raises(SourceNormalizationError) as exc_info:
        BetfairAdapter.normalize(
            legacy_payload,
            expected,
            capture_timestamp=CAPTURED,
            request_identity="bf-request-2",
        )
    assert exc_info.value.error is SourceError.MISSING_TIMESTAMP


def test_oddsportal_legacy_row_is_rejected_instead_of_inventing_metadata() -> None:
    expected = _expected()
    with pytest.raises(SourceNormalizationError) as exc_info:
        OddsPortalAdapter.normalize(
            {
                "home": expected.home_team,
                "away": expected.away_team,
                "h": 2.2,
                "d": 3.4,
                "a": 3.0,
            },
            expected,
            capture_timestamp=CAPTURED,
            request_identity="op-legacy",
        )
    assert exc_info.value.error is SourceError.MISSING_PROVENANCE


def test_oddsportal_enriched_candidate_row_is_normalized_but_not_authorized() -> None:
    expected = _expected()
    observation = OddsPortalAdapter.normalize(
        {
            "match_id": "op-1",
            "league": "EPL",
            "home": "Man Utd",
            "away": "Arsenal",
            "kickoff": expected.kickoff.isoformat(),
            "source_timestamp": (CAPTURED - timedelta(minutes=3)).isoformat(),
            "h": 2.2,
            "d": 3.4,
            "a": 3.0,
        },
        expected,
        capture_timestamp=CAPTURED,
        request_identity="op-1",
    )[0]
    report = validate_source_observation(observation, expected, TEST_POLICY)
    assert report.accepted is True
    assert report.candidate_status is AdapterStatus.CANDIDATE_ONLY
    assert report.authority_approved is False


def test_football_data_requires_precise_injected_timestamps_and_is_closing_only() -> (
    None
):
    expected = _expected()
    row = {
        "match_id": "fd-1",
        "league": "EPL",
        "home_team": expected.home_team,
        "away_team": expected.away_team,
        "kickoff": expected.kickoff.isoformat(),
        "source_timestamp": (CAPTURED - timedelta(days=1)).isoformat(),
        "PSCH": 2.1,
        "PSCD": 3.5,
        "PSCA": 3.2,
    }
    observation = FootballDataClosingAdapter.normalize(
        row, expected, capture_timestamp=CAPTURED, request_identity="fd-1"
    )[0]
    assert observation.market_role is MarketRole.CLOSING_BENCHMARK
    assert (
        validate_source_observation(observation, expected, TEST_POLICY).accepted
        is False
    )
    assert (
        SourceError.CLOSING_LEAKAGE
        in validate_source_observation(observation, expected, TEST_POLICY).errors
    )

    with pytest.raises(SourceNormalizationError) as exc_info:
        FootballDataClosingAdapter.normalize(
            {"PSCH": 2.1, "PSCD": 3.5, "PSCA": 3.2},
            expected,
            capture_timestamp=CAPTURED,
            request_identity="fd-date-only",
        )
    assert exc_info.value.error is SourceError.MISSING_TIMESTAMP


def test_real_shadow_validation_requires_explicit_timing_policy() -> None:
    with pytest.raises(ShadowProviderContractError, match="SourceQualityPolicy"):
        validate_source_observation(_observation(), _expected(), None)
    with pytest.raises(ShadowProviderContractError, match="timing policy"):
        validate_source_observation(
            _observation(),
            _expected(),
            SourceQualityPolicy(
                maximum_odds_age_seconds=0,
                kickoff_tolerance_seconds=TEST_KICKOFF_TOLERANCE_SECONDS,
            ),
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda obs: replace(obs, confidence=-0.1),
        lambda obs: replace(obs, confidence=1.1),
        lambda obs: replace(obs, request_latency_ms=-1),
        lambda obs: replace(
            obs,
            completeness=CompletenessState.PARTIAL,
            error_classification=SourceError.NONE,
        ),
        lambda obs: replace(obs, request_identity=""),
        lambda obs: replace(obs, source_timestamp=CAPTURED + timedelta(seconds=1)),
        lambda obs: replace(
            obs,
            source_provenance=SourceProvenance(
                source_uri="not-a-uri",
                raw_record_id="record-1",
                retrieved_at=CAPTURED,
                adapter_version="test-adapter-v1",
            ),
        ),
        lambda obs: replace(obs, market_type=None),
    ],
)
def test_structural_observation_failure_is_always_rejected(mutator) -> None:
    report = validate_source_observation(
        mutator(_observation()), _expected(), TEST_POLICY
    )
    assert report.accepted is False
    assert report.prediction_input_allowed is False
    assert SourceError.INVALID_SOURCE_CONTRACT in report.errors


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (
            lambda obs, exp: replace(
                obs,
                fixture_key=make_fixture_key(
                    "EPL", "Liverpool", "Arsenal", exp.kickoff
                ),
            ),
            SourceError.WRONG_FIXTURE,
        ),
        (
            lambda obs, exp: replace(
                obs,
                home_team=exp.away_team,
                away_team=exp.home_team,
                fixture_key=make_fixture_key(
                    exp.league, exp.away_team, exp.home_team, exp.kickoff
                ),
            ),
            SourceError.INVERTED_HOME_AWAY,
        ),
        (
            lambda obs, exp: replace(
                obs,
                home_team="Liverpool",
                fixture_key=make_fixture_key(
                    exp.league, "Liverpool", exp.away_team, exp.kickoff
                ),
            ),
            SourceError.TEAM_ALIAS_MISMATCH,
        ),
        (
            lambda obs, exp: replace(
                obs,
                kickoff=exp.kickoff + timedelta(minutes=6),
                fixture_key=make_fixture_key(
                    exp.league,
                    exp.home_team,
                    exp.away_team,
                    exp.kickoff + timedelta(minutes=6),
                ),
            ),
            SourceError.KICKOFF_MISMATCH,
        ),
        (
            lambda obs, exp: replace(
                obs, source_timestamp=CAPTURED - timedelta(hours=2)
            ),
            SourceError.STALE_ODDS,
        ),
        (
            lambda obs, exp: replace(
                obs,
                draw_odds=None,
                completeness=CompletenessState.PARTIAL,
                error_classification=SourceError.MISSING_DRAW,
            ),
            SourceError.MISSING_DRAW,
        ),
        (
            lambda obs, exp: replace(obs, home_odds=1.1, draw_odds=1.1, away_odds=20.0),
            SourceError.ODDS_SANITY,
        ),
        (
            lambda obs, exp: replace(
                obs,
                source_provenance=None,
                completeness=CompletenessState.PARTIAL,
                error_classification=SourceError.MISSING_PROVENANCE,
            ),
            SourceError.MISSING_PROVENANCE,
        ),
    ],
)
def test_source_quality_gates_fail_closed(mutator, error) -> None:
    expected = _expected()
    observation = mutator(_observation(expected), expected)
    report = validate_source_observation(observation, expected, TEST_POLICY)
    assert report.accepted is False
    assert error in report.errors
    assert report.prediction_input_allowed is False
    assert report.authority_approved is False


@pytest.mark.parametrize(
    "error",
    [
        SourceError.TIMEOUT,
        SourceError.HTTP_403,
        SourceError.HTTP_429,
        SourceError.HTTP_5XX,
        SourceError.PROVIDER_UNAVAILABLE,
    ],
)
def test_provider_failures_are_evidence_and_never_accepted(error: SourceError) -> None:
    observation = _observation(
        error=error,
        completeness=CompletenessState.COMPLETE,
    )
    report = validate_source_observation(observation, _expected(), TEST_POLICY)
    assert report.accepted is False
    assert error in report.errors


def test_duplicate_snapshots_are_rejected() -> None:
    expected = _expected()
    first = _observation(expected)
    second = replace(first, request_identity="request-2")
    reports = validate_observation_batch(
        (first, second), {expected.fixture_key: expected}, TEST_POLICY
    )
    assert reports[0].accepted is True
    assert reports[1].accepted is False
    assert SourceError.DUPLICATE_SNAPSHOT in reports[1].errors


def test_missing_bookmaker_identity_is_rejected_when_policy_requires_it() -> None:
    observation = _observation(bookmaker=None)
    report = validate_source_observation(observation, _expected(), TEST_POLICY)
    assert report.accepted is False
    assert SourceError.MISSING_PROVENANCE in report.errors


def test_closing_benchmark_cannot_enter_v1_observation_evidence() -> None:
    observation = _observation(role=MarketRole.CLOSING_BENCHMARK)
    with pytest.raises(SourceNormalizationError) as exc_info:
        to_shadow_observation_evidence(
            observation, _expected(), _identity(), policy=TEST_POLICY
        )
    assert exc_info.value.error is SourceError.CLOSING_LEAKAGE


def test_valid_provider_output_bridges_to_top5_shadow_evidence_v1() -> None:
    expected = _expected()
    observation = _observation(expected)
    evidence = to_shadow_observation_evidence(
        observation, expected, _identity(), policy=TEST_POLICY
    )
    evidence.validate()
    assert evidence.provenance.research_sha == FROZEN_RESEARCH_SHA
    assert evidence.no_bet is True
    assert evidence.publication_enabled is False
    assert evidence.provider_covered is True

    provider_evidence = to_shadow_provider_evidence(
        observation, expected, _identity(), policy=TEST_POLICY
    )
    provider_evidence.validate()
    assert provider_evidence.provider_name == "the_odds_api"
    assert provider_evidence.availability is True


def test_invalid_provider_output_bridges_as_rejected_v1_evidence() -> None:
    expected = _expected()
    observation = _observation(
        expected,
        source_timestamp=CAPTURED - timedelta(hours=2),
    )
    evidence = to_shadow_observation_evidence(
        observation, expected, _identity(), policy=TEST_POLICY
    )
    evidence.validate()
    assert evidence.rejected is True
    assert evidence.eligible is False
    assert evidence.stale is True


def test_bundle_has_explicit_no_bet_safety_and_no_authority_selection() -> None:
    expected = _expected()
    bundle = build_shadow_evidence_bundle(
        (_observation(expected),),
        {expected.fixture_key: expected},
        {expected.fixture_key: _identity()},
        window_start=CAPTURED - timedelta(minutes=10),
        window_end=CAPTURED + timedelta(minutes=10),
        policy=TEST_POLICY,
    )
    bundle.validate()
    assert bundle.safety is not None
    assert bundle.safety.no_bet is True
    assert bundle.safety.publication_enabled is False
    assert bundle.predictions == ()


def test_observation_digest_is_deterministic() -> None:
    observation = _observation()
    assert canonical_observation_digest(observation) == canonical_observation_digest(
        observation
    )
    assert len(canonical_observation_digest(observation)) == 64


def test_exhausted_odds_api_rejects_paid_call_before_network() -> None:
    called = False

    def network_call() -> str:
        nonlocal called
        called = True
        return "must-not-run"

    preflight = authorize_odds_api_request(
        CURRENT_EXHAUSTED_ODDS_API, OddsApiRequestKind.ODDS
    )
    assert preflight.allowed is False
    assert preflight.reason == "quota_exhausted"
    assert preflight.retry_count == 0
    assert preflight.fallback_fanout_allowed is False
    result = guarded_odds_api_call(
        CURRENT_EXHAUSTED_ODDS_API, OddsApiRequestKind.ODDS, network_call
    )
    assert result.network_called is False
    assert result.error == "quota_exhausted"
    assert called is False


def test_zero_cost_authentication_is_distinguishable_from_paid_odds() -> None:
    auth = authorize_odds_api_request(
        CURRENT_EXHAUSTED_ODDS_API, OddsApiRequestKind.AUTHENTICATION
    )
    assert auth.allowed is True
    assert auth.estimated_cost_units == 0.0
    assert auth.reason == "zero_cost_authentication_endpoint"

    unauthenticated = authorize_odds_api_request(
        OddsApiQuotaSnapshot(False, 0, 0), OddsApiRequestKind.AUTHENTICATION
    )
    assert unauthenticated.allowed is False
    assert unauthenticated.reason == "authentication_missing"


def test_provider_planner_lists_quota_independent_paths_without_fanout_or_selection() -> (
    None
):
    request = ProviderPlanningRequest(
        league="EPL",
        window_start=CAPTURED,
        window_end=CAPTURED + timedelta(hours=4),
        required_market="h2h_1x2",
        freshness_requirement_seconds=900,
        allowed_sources=("the_odds_api", "betfair", "oddsportal"),
        fixture_count=10,
        remaining_quota={"the_odds_api": CURRENT_EXHAUSTED_ODDS_API},
        credentials_available={"the_odds_api": True, "betfair": True},
        cost_units_per_request={"betfair": 2.0},
        live_path_prerequisites={
            "betfair": TEST_LIVE_PATH_PROOF,
            "oddsportal": TEST_LIVE_PATH_PROOF,
        },
    )
    plan = plan_provider_paths(request)
    plan.validate()
    by_source = {path.source: path for path in plan.paths}
    assert by_source["the_odds_api"].operationally_possible is False
    assert "quota_exhausted" in by_source["the_odds_api"].reason
    assert by_source["betfair"].operationally_possible is True
    assert by_source["oddsportal"].operationally_possible is True
    assert plan.selected_source is None
    assert plan.automatic_fallback_enabled is False
    assert plan.automatic_fanout_requests == 0
    assert {path.source for path in plan.candidate_quota_independent_paths} == {
        "betfair",
        "oddsportal",
    }


def test_planner_fails_closed_without_quota_or_credentials() -> None:
    request = ProviderPlanningRequest(
        league="BL1",
        window_start=CAPTURED,
        window_end=CAPTURED + timedelta(hours=4),
        required_market="h2h_1x2",
        freshness_requirement_seconds=900,
        allowed_sources=("the_odds_api", "betfair", "football_data"),
        fixture_count=1,
    )
    plan = plan_provider_paths(request)
    by_source = {path.source: path for path in plan.paths}
    assert all(path.operationally_possible is False for path in plan.paths)
    assert "explicitly supplied" in by_source["the_odds_api"].reason
    assert "credentials" in by_source["betfair"].reason
    assert "not signal-time" in by_source["football_data"].reason


def test_fallback_fanout_is_never_auto_enabled_even_when_primary_is_exhausted() -> None:
    request = ProviderPlanningRequest(
        league="SA",
        window_start=CAPTURED,
        window_end=CAPTURED + timedelta(hours=2),
        required_market="h2h_1x2",
        freshness_requirement_seconds=600,
        allowed_sources=("the_odds_api", "oddsportal"),
        fixture_count=3,
        remaining_quota={"the_odds_api": 0},
    )
    plan = plan_provider_paths(request)
    assert plan.selected_source is None
    assert plan.automatic_fanout_requests == 0
    assert plan.automatic_fallback_enabled is False


def test_no_network_or_provider_client_is_used_by_injected_adapters() -> None:
    calls: list[str] = []

    def network_call() -> None:
        calls.append("network")

    TheOddsAPIAdapter.normalize(
        _toa_payload(),
        _expected(),
        capture_timestamp=CAPTURED,
        request_identity="injected-only",
    )
    guarded_odds_api_call(
        CURRENT_EXHAUSTED_ODDS_API, OddsApiRequestKind.ODDS, network_call
    )
    assert calls == []
