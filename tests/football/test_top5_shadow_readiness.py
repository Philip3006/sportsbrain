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
    assert TOP5_SOURCE_MATRIX["LL"].fallback_sources == (
        "none in current results_router for this football-data code",
    )
    for semantics in TOP5_SOURCE_MATRIX.values():
        assert "closing excluded" in semantics.freshness_policy
        assert "undecided" in semantics.live_authority
        assert semantics.recommendation.startswith("Keep disabled")


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
    assert result.health.as_payload()["registered"] is False


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
    with pytest.raises(ProductionContractError, match="foreign league"):
        run_offline_compatibility(
            adapter,
            [_fixture(code="EPL")],
            [_snapshot()],
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
    assert len(result.request_batches) == 4


def test_signal_time_simulator_prevents_duplicate_dispatches():
    fixture = _fixture()
    snapshot = _snapshot()
    ledger = OfflineDispatchLedger()
    key = make_dispatch_key("BL1", fixture.fixture_key, CONTRACT, snapshot)
    assert ledger.claim(key).accepted
    result = simulate_signal_time(
        [fixture],
        {fixture.fixture_key: [snapshot]},
        SignalTimeCandidate(CONTRACT, max_retries=0),
        start_at=BASE,
        dispatch_ledger=ledger,
    )
    assert result.eligible_count == 0
    assert result.duplicate_dispatches_prevented == 1


def test_signal_time_simulator_rejects_naive_schedule_time():
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        simulate_signal_time(
            [_fixture()],
            {"fixture-1": [_snapshot()]},
            SignalTimeCandidate(CONTRACT),
            start_at=BASE.replace(tzinfo=None),
        )


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
    assert estimate.total_requests == (6 * 3) + 6 + 3
    comparison = compare_quota_architectures(
        horizons,
        {"conservative": conservative, "single-pass": QuotaAssumptions()},
    )
    assert len(comparison["conservative"]) == 3
    assert comparison["conservative"][-1].total_requests > comparison["single-pass"][-1].total_requests


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
