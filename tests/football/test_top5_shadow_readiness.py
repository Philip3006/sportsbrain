from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_dispatch import (
    OfflineDispatchLedger,
    make_dispatch_key,
    signal_time_contract_id,
)
from src.football.top5_health import Top5ShadowHealth
from src.football.top5_offline import run_offline_compatibility
from src.football.top5_provider_semantics import (
    BulkProviderRequest,
    BulkRequestOutcome,
    FallbackScenario,
    LogicalFixtureEvaluation,
    ProviderCostModel,
    ProviderRequestContext,
)
from src.football.top5_quota import (
    QuotaAssumptions,
    QuotaHorizon,
    compare_quota_architectures,
    estimate_quota,
)
from src.football.top5_signal_time import SignalTimeCandidate, simulate_signal_time
from src.football.top5_source_matrix import TOP5_SOURCE_MATRIX

BASE = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
CONTRACT = SignalTimeContract(60, 180, 120)
TOP5_CODES = tuple(TOP5_LEAGUE_ADAPTERS)


def _fixture(code: str = "BL1", key: str = "fixture-1", minutes: int = 120) -> Fixture:
    return Fixture(key, code, "Home FC", "Away FC", BASE + timedelta(minutes=minutes))


def _snapshot(
    fixture_key: str = "fixture-1",
    *,
    captured_at: datetime = BASE,
    kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME,
    snapshot_id: str = "snapshot-1",
) -> MarketSnapshot:
    return MarketSnapshot(
        fixture_key=fixture_key,
        captured_at=captured_at,
        kind=kind,
        source="offline-provider",
        odds={"home": 2.0, "away": 2.5},
        snapshot_id=snapshot_id,
    )


def test_five_adapters_are_explicit_and_remain_unbound_and_disabled():
    assert set(TOP5_LEAGUE_ADAPTERS) == {"BL1", "EPL", "LL", "SA", "L1"}
    for adapter in TOP5_LEAGUE_ADAPTERS.values():
        adapter.validate()
        assert adapter.config.activation_mode is ActivationMode.DISABLED
        assert adapter.config.model_adapter_id == "unbound"
        assert adapter.model_adapter_slot == "unbound"
        assert adapter.signal_time_slot == "unconfigured"
        assert adapter.health_identity == f"top5_shadow:{adapter.league_code}"
        assert set(adapter.artifact_namespaces) == {
            "shadow_archive",
            "staged_public",
            "health",
        }


def test_source_matrix_records_repo_routes_and_unresolved_live_authority():
    assert set(TOP5_SOURCE_MATRIX) == set(TOP5_LEAGUE_ADAPTERS)
    assert TOP5_SOURCE_MATRIX["BL1"].competition_id == "soccer_germany_bundesliga"
    assert TOP5_SOURCE_MATRIX["EPL"].fallback_sources
    assert "/sports/{sport}/scores" in TOP5_SOURCE_MATRIX["EPL"].fallback_sources[0]
    assert "sport-level" in TOP5_SOURCE_MATRIX["EPL"].fallback_sources[0]
    assert TOP5_SOURCE_MATRIX["LL"].fallback_sources == (
        "none in current results_router for this football-data code",
    )
    for semantics in TOP5_SOURCE_MATRIX.values():
        assert "closing excluded" in semantics.freshness_policy
        assert "undecided" in semantics.live_authority
        assert semantics.recommendation.startswith("Keep disabled")


def test_provider_semantics_keep_logical_http_and_cost_units_distinct():
    context = ProviderRequestContext("BL1", "the_odds_api", "soccer_germany_bundesliga", ("h2h",), ("eu",))
    evaluation = LogicalFixtureEvaluation(
        fixture_key="fixture-1",
        league_code="BL1",
        attempted_at=BASE,
        attempt_number=0,
        request_bucket=BASE.isoformat(),
        provider=context,
    )
    bulk = BulkProviderRequest(
        league_code="BL1",
        provider_name="the_odds_api",
        sport_key="soccer_germany_bundesliga",
        requested_at=BASE,
        request_bucket=BASE.isoformat(),
        markets=("h2h",),
        regions=("eu",),
        fixture_keys=("fixture-1", "fixture-2"),
    )

    evaluation.validate()
    bulk.validate()
    assert len(bulk.fixture_keys) == 2
    assert ProviderCostModel(bulk_request_cost_units=2.5).cost_units(bulk_requests=1) == 2.5


def test_offline_harness_proves_prediction_shadow_artifact_and_provenance_boundaries():
    adapter = TOP5_LEAGUE_ADAPTERS["BL1"]
    result = run_offline_compatibility(
        adapter,
        [_fixture()],
        [_snapshot()],
        signal_time=CONTRACT,
        now=BASE,
    )

    assert len(result.pipeline.predictions) == 1
    assert len(result.pipeline.signals) == 1
    assert result.pipeline.predictions[0].model_adapter_id == "offline-dummy-model"
    assert result.pipeline.predictions[0].snapshot_kind is MarketSnapshotKind.SIGNAL_TIME
    assert result.pipeline.signals[0].no_bet_flag is True
    assert result.pipeline.signals[0].prediction_id == result.pipeline.predictions[0].prediction_id
    assert result.pipeline.signals[0].activation_mode is ActivationMode.SHADOW
    assert result.no_bet is True
    assert result.closing_odds_used is False
    assert result.artifact_flow.publication_enabled is False
    assert result.artifact_references[0].publication_enabled is False
    health_payload = result.health.as_payload()
    assert health_payload["registered"] is False
    assert health_payload["logical_fixture_evaluations"] == 1
    assert health_payload["bulk_provider_requests"] == 1
    assert health_payload["fallback_requests"] == 0


def test_offline_harness_rejects_closing_odds_and_foreign_league():
    adapter = TOP5_LEAGUE_ADAPTERS["BL1"]
    with pytest.raises(ProductionContractError, match="closing snapshot"):
        run_offline_compatibility(
            adapter,
            [_fixture()],
            [_snapshot(kind=MarketSnapshotKind.CLOSING)],
            signal_time=CONTRACT,
            now=BASE,
        )


@pytest.mark.parametrize("league_code", TOP5_CODES)
def test_each_league_has_isolated_offline_compatibility_and_immutable_metadata(league_code):
    adapter = TOP5_LEAGUE_ADAPTERS[league_code]
    canonical_config = adapter.config
    mapping = canonical_config.provider_mapping
    assert mapping is not None
    shadow_config = adapter.shadow_test_config(CONTRACT)
    result = run_offline_compatibility(
        adapter,
        [_fixture(code=league_code, key=f"{league_code}-fixture")],
        [_snapshot(f"{league_code}-fixture", snapshot_id=f"{league_code}-snapshot")],
        signal_time=CONTRACT,
        now=BASE,
    )

    assert adapter.config is canonical_config
    assert adapter.config.activation_mode is ActivationMode.DISABLED
    assert adapter.config.model_adapter_id == adapter.model_adapter_slot == "unbound"
    assert adapter.signal_time_slot == "unconfigured"
    assert shadow_config.activation_mode is ActivationMode.SHADOW
    assert shadow_config.model_adapter_id == "offline-dummy-model"
    assert shadow_config.signal_time == CONTRACT
    assert mapping.sport_key == adapter.config.provider_sport_key
    assert adapter.provider_competition_id == mapping.competition_id
    assert result.pipeline.signals[0].league_code == league_code
    assert result.pipeline.signals[0].no_bet_flag is True
    assert result.pipeline.signals[0].snapshot_kind is MarketSnapshotKind.SIGNAL_TIME
    assert result.health.health_identity == adapter.health_identity
    assert result.health.as_payload()["health_identity"] == f"top5_shadow:{league_code}"
    assert result.artifact_references[0].shadow_archive_path.startswith(
        f"results/shadow/top5/{league_code}/"
    )
    assert result.artifact_references[0].staged_public_path.startswith(
        f"docs/data/top5/shadow/{league_code}/"
    )


@pytest.mark.parametrize("league_code", TOP5_CODES)
def test_each_league_rejects_closing_odds_and_cross_league_fixtures(league_code):
    adapter = TOP5_LEAGUE_ADAPTERS[league_code]
    other_code = next(code for code in TOP5_CODES if code != league_code)
    fixture = _fixture(code=league_code, key=f"{league_code}-fixture")
    with pytest.raises(ProductionContractError, match="closing snapshot"):
        run_offline_compatibility(
            adapter,
            [fixture],
            [_snapshot(fixture.fixture_key, kind=MarketSnapshotKind.CLOSING)],
            signal_time=CONTRACT,
            now=BASE,
        )
    with pytest.raises(ProductionContractError, match="foreign league"):
        run_offline_compatibility(
            adapter,
            [_fixture(code=other_code, key=f"{other_code}-fixture")],
            [_snapshot(f"{other_code}-fixture")],
            signal_time=CONTRACT,
            now=BASE,
        )


def test_dispatch_keys_are_deterministic_isolated_and_retryable_only_explicitly():
    first = _snapshot()
    key = make_dispatch_key("BL1", "fixture-1", CONTRACT, first)
    assert key == make_dispatch_key("BL1", "fixture-1", CONTRACT, first)
    assert key.value.startswith("top5-shadow:")
    assert key != make_dispatch_key("BL1", "fixture-1", CONTRACT, _snapshot(snapshot_id="snapshot-2"))
    assert key != make_dispatch_key("EPL", "fixture-1", CONTRACT, first)
    assert signal_time_contract_id(CONTRACT) != signal_time_contract_id(SignalTimeContract(30, 180, 120))

    ledger = OfflineDispatchLedger()
    assert ledger.claim(key).accepted is True
    assert ledger.claim(key).accepted is False
    retry = ledger.claim(key, retry_reason="provider_timeout")
    assert retry.accepted is True
    assert retry.retry_count == 2
    assert ledger.claim(key, retry_reason="").accepted is False


def test_signal_time_simulator_reports_window_retry_stale_and_inference_diagnostics():
    candidate = SignalTimeCandidate(
        CONTRACT,
        retry_interval_seconds=60,
        max_retries=2,
        inference_duration_seconds=30,
    )
    eligible = _fixture(key="eligible")
    stale = _fixture(key="stale")
    missed = _fixture(key="missed", minutes=30)
    result = simulate_signal_time(
        [eligible, stale, missed],
        {
            "eligible": [_snapshot("eligible")],
            "stale": [_snapshot("stale", captured_at=BASE - timedelta(minutes=5))],
        },
        candidate,
        start_at=BASE,
    )

    eligible_diag, stale_diag, missed_diag = result.fixture_diagnostics
    assert eligible_diag.eligible is True
    assert eligible_diag.first_eligible_execution == BASE
    assert eligible_diag.expected_inference_time == BASE + timedelta(seconds=30)
    assert eligible_diag.retry_count == 0
    assert stale_diag.eligible is False
    assert stale_diag.stale_odds_rejections == 3
    assert stale_diag.retry_count == 2
    assert missed_diag.eligible is False
    assert "missed" in missed_diag.failure_reasons[0]
    assert result.coverage == pytest.approx(1 / 3)
    assert result.scheduled_attempts == 6
    assert result.fixture_evaluation_count == 4
    assert result.bulk_request_count == 3
    assert result.fallback_event_request_count == 0
    assert len(result.request_batches[0].fixture_keys) == 2


def test_signal_time_simulator_prevents_duplicate_dispatches():
    fixture = _fixture()
    snapshot = _snapshot()
    ledger = OfflineDispatchLedger()
    key = make_dispatch_key("BL1", fixture.fixture_key, CONTRACT, snapshot)
    assert ledger.claim(key).accepted
    result = simulate_signal_time(
        [fixture],
        {fixture.fixture_key: [snapshot]},
        SignalTimeCandidate(CONTRACT, max_retries=2),
        start_at=BASE,
        dispatch_ledger=ledger,
    )
    assert result.eligible_count == 0
    assert result.duplicate_dispatches_prevented == 1
    assert result.fixture_diagnostics[0].attempts_evaluated == 1


def test_signal_time_simulator_rejects_naive_schedule_time():
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        simulate_signal_time(
            [_fixture()],
            {"fixture-1": [_snapshot()]},
            SignalTimeCandidate(CONTRACT),
            start_at=BASE.replace(tzinfo=None),
        )


def test_signal_time_bulk_failure_uses_event_fallback_without_network():
    fixture = _fixture()
    result = simulate_signal_time(
        [fixture],
        {fixture.fixture_key: [_snapshot()]},
        SignalTimeCandidate(CONTRACT, max_retries=0),
        start_at=BASE,
        fallback_scenarios={
            "BL1": FallbackScenario(
                "unsupported-market",
                BulkRequestOutcome.UNSUPPORTED_MARKET,
            ),
        },
    )

    assert result.eligible_count == 1
    assert result.bulk_request_count == 1
    assert result.fallback_event_request_count == 1
    assert result.provider_failure_count == 1
    assert result.bulk_provider_failure_count == 1
    assert result.fallback_event_requests[0].fixture_key == fixture.fixture_key
    assert result.bulk_requests[0].outcome is BulkRequestOutcome.UNSUPPORTED_MARKET


def test_signal_time_partial_fallback_only_recovers_selected_events():
    fixtures = [_fixture(key="one"), _fixture(key="two")]
    result = simulate_signal_time(
        fixtures,
        {"one": [_snapshot("one")], "two": [_snapshot("two")]},
        SignalTimeCandidate(CONTRACT, max_retries=0),
        start_at=BASE,
        fallback_scenarios={
            "BL1": FallbackScenario(
                "partial-event-fallback",
                BulkRequestOutcome.PARTIAL_EVENT_FALLBACK,
                ("one",),
            ),
        },
    )

    assert result.eligible_count == 1
    assert result.missed_count == 1
    assert result.fallback_event_request_count == 1
    assert result.fixture_diagnostics[1].max_retries_exhausted is True


def test_signal_time_no_snapshot_exhausts_max_retries_and_never_exceeds_bound():
    result = simulate_signal_time(
        [_fixture()],
        {},
        SignalTimeCandidate(CONTRACT, retry_interval_seconds=60, max_retries=1),
        start_at=BASE,
    )

    diagnostic = result.fixture_diagnostics[0]
    assert diagnostic.eligible is False
    assert diagnostic.attempts_evaluated == 2
    assert diagnostic.logical_fixture_evaluations == 2
    assert diagnostic.retry_count == 1
    assert diagnostic.max_retries_exhausted is True


def test_signal_time_retry_accepts_changed_snapshot_generation_once():
    fixture = _fixture()
    result = simulate_signal_time(
        [fixture],
        {
            fixture.fixture_key: [
                _snapshot(
                    fixture.fixture_key,
                    captured_at=BASE - timedelta(minutes=5),
                    snapshot_id="stale-generation",
                ),
                _snapshot(
                    fixture.fixture_key,
                    captured_at=BASE + timedelta(seconds=30),
                    snapshot_id="fresh-generation",
                ),
            ],
        },
        SignalTimeCandidate(CONTRACT, retry_interval_seconds=60, max_retries=1),
        start_at=BASE,
    )

    diagnostic = result.fixture_diagnostics[0]
    assert diagnostic.eligible is True
    assert diagnostic.retry_count == 1
    assert diagnostic.attempts_evaluated == 2
    assert result.bulk_request_count == 2


def test_signal_time_coalesces_compatible_fixtures_but_isolates_leagues():
    same_league = [_fixture(key="one"), _fixture(key="two")]
    same_result = simulate_signal_time(
        same_league,
        {"one": [_snapshot("one")], "two": [_snapshot("two")]},
        SignalTimeCandidate(CONTRACT, max_retries=0),
        start_at=BASE,
    )
    assert same_result.fixture_evaluation_count == 2
    assert same_result.bulk_request_count == 1
    assert same_result.bulk_requests[0].fixture_keys == ("one", "two")

    cross_league = [_fixture(code="BL1", key="shared"), _fixture(code="EPL", key="shared")]
    cross_result = simulate_signal_time(
        cross_league,
        {
            ("BL1", "shared"): [_snapshot("shared", snapshot_id="bl1-snapshot")],
            ("EPL", "shared"): [_snapshot("shared", snapshot_id="epl-snapshot")],
        },
        SignalTimeCandidate(CONTRACT, max_retries=0),
        start_at=BASE,
    )
    assert cross_result.eligible_count == 2
    assert cross_result.bulk_request_count == 2
    assert {request.league_code for request in cross_result.bulk_requests} == {"BL1", "EPL"}


@pytest.mark.parametrize(
    "outcome",
    [BulkRequestOutcome.PROVIDER_TIMEOUT, BulkRequestOutcome.EMPTY_PAYLOAD],
)
def test_signal_time_failure_scenarios_are_explicit_and_bounded(outcome):
    fixture = _fixture()
    result = simulate_signal_time(
        [fixture],
        {fixture.fixture_key: [_snapshot()]},
        SignalTimeCandidate(CONTRACT, retry_interval_seconds=60, max_retries=1),
        start_at=BASE,
        fallback_scenarios={"BL1": FallbackScenario(outcome.value, outcome)},
    )

    assert result.eligible_count == 1
    assert result.provider_failure_count == 1
    assert result.fallback_event_request_count == 1
    assert result.fixture_diagnostics[0].attempts_evaluated == 1


def test_quota_estimator_is_configurable_and_compares_architectures_without_calls():
    horizons = (
        QuotaHorizon("matchday", {"BL1": 3, "EPL": 2, "LL": 1, "SA": 0, "L1": 0}),
        QuotaHorizon("week", {"BL1": 8, "EPL": 6, "LL": 5, "SA": 4, "L1": 4}),
        QuotaHorizon("72h", {"BL1": 3, "EPL": 2, "LL": 1, "SA": 0, "L1": 0}),
    )
    horizon = horizons[-1]
    conservative = QuotaAssumptions(signal_attempts_per_fixture=3, revalidation_requests_per_fixture=1)
    estimate = estimate_quota(horizon, conservative)
    assert estimate.total_fixtures == 6
    assert estimate.logical_signal_evaluations == 18
    assert estimate.bulk_odds_requests == 9
    assert estimate.fallback_event_requests == 0
    assert estimate.result_requests == 3
    assert estimate.revalidation_requests == 6
    assert estimate.raw_http_request_count == 18
    assert estimate.estimated_provider_cost_units == 18
    comparison = compare_quota_architectures(
        horizons,
        {"conservative": conservative, "single-pass": QuotaAssumptions()},
    )
    assert len(comparison["conservative"]) == 3
    assert comparison["single-pass"][-1].logical_signal_evaluations == 6
    assert comparison["single-pass"][-1].bulk_odds_requests == 3
    assert comparison["single-pass"][-1].raw_http_request_count == 6


def test_quota_estimator_separates_fallback_cost_and_http_units():
    horizon = QuotaHorizon("matchday", {"BL1": 4, "EPL": 0, "LL": 0, "SA": 0, "L1": 0})
    assumptions = QuotaAssumptions(
        signal_attempts_per_fixture=2,
        revalidation_requests_per_fixture=1,
        closing_capture_requests_per_fixture=1,
        fallback_event_requests_by_league={"BL1": 3},
        markets=("h2h", "totals"),
        regions=("eu", "uk"),
        signal_time_contract=CONTRACT,
        cost_model=ProviderCostModel(
            bulk_request_cost_units=2.0,
            fallback_event_request_cost_units=4.0,
            result_request_cost_units=0.5,
            revalidation_request_cost_units=1.5,
            closing_capture_request_cost_units=3.0,
        ),
    )
    estimate = estimate_quota(horizon, assumptions)
    assert estimate.logical_signal_evaluations == 8
    assert estimate.bulk_odds_requests == 2
    assert estimate.fallback_event_requests == 3
    assert estimate.result_requests == 1
    assert estimate.revalidation_requests == 4
    assert estimate.closing_capture_requests == 4
    assert estimate.raw_http_request_count == 14
    assert estimate.estimated_provider_cost_units == pytest.approx(34.5)
    assert estimate.signal_time_contract == CONTRACT
    assert estimate.as_payload()["signal_time_contract"]["maximum_odds_age_seconds"] == 120


def test_quota_estimator_accepts_expected_fallback_probability():
    estimate = estimate_quota(
        QuotaHorizon("72h", {"BL1": 4, "EPL": 0, "LL": 0, "SA": 0, "L1": 0}),
        QuotaAssumptions(
            result_requests_per_league=0,
            fallback_probability_by_league={"BL1": 0.5},
        ),
    )
    assert estimate.logical_signal_evaluations == 4
    assert estimate.bulk_odds_requests == 1
    assert estimate.fallback_event_requests == 2
    assert estimate.raw_http_request_count == 3


def test_disabled_health_requires_all_requested_fields_and_no_bet():
    health = Top5ShadowHealth(
        league_code="BL1",
        fixture_count=2,
        eligible_count=1,
        prediction_count=1,
        skipped_count=1,
        stale_count=0,
        provider_failure_count=0,
        retry_count=1,
        duplicate_suppression_count=0,
        model_adapter_identity="offline-dummy-model",
        contract_id=signal_time_contract_id(CONTRACT),
    )
    payload = health.as_payload()
    assert payload["league"] == "BL1"
    assert payload["no_bet"] is True
    assert payload["registered"] is False
