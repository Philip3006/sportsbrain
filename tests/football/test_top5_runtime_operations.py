"""Offline regression coverage for the unified Top-5 runtime seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    SignalTimeContract,
)
from src.football.provider_cascade.contracts import CANDIDATE_ONLY_PROVIDER_IDENTITIES
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_runtime_operations import (
    TOP5_PRODUCTION_PROVIDER,
    TOP5_RUNTIME_LEAGUES,
    InMemoryTop5RuntimeCheckpointStore,
    Top5ActivationPrecheckInput,
    Top5LeagueRuntimeInput,
    Top5ResultBinding,
    Top5RuntimeConfig,
    Top5RuntimeError,
    Top5RuntimeRequest,
    Top5RuntimeStage,
    Top5RuntimeStatus,
    run_offline_top5_runtime,
    top5_activation_precheck,
)

BASE = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
SIGNAL_TIME = SignalTimeContract(30, 180, 900)
HEX = "a" * 64


def _league_input(league: str, *, with_result: bool = True) -> Top5LeagueRuntimeInput:
    fixture = Fixture(
        f"{league}|fixture-1",
        league,
        f"{league} Home",
        f"{league} Away",
        BASE + timedelta(minutes=120),
    )
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=BASE,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="offline-the-odds-api",
        odds={"home": 2.1, "draw": 3.2, "away": 3.4},
        snapshot_id=f"snapshot:{league}:signal",
    )
    results = (
        (
            Top5ResultBinding(
                fixture_key=fixture.fixture_key,
                league_code=league,
                result_id=f"result:{league}:1",
                result_source=f"football_data:{league}",
                outcome="home",
                source_timestamp=BASE + timedelta(hours=3),
                captured_at=BASE + timedelta(hours=3, minutes=1),
                result_digest=HEX,
                synthetic=True,
            ),
        )
        if with_result
        else ()
    )
    closing = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=BASE + timedelta(hours=3),
        kind=MarketSnapshotKind.CLOSING,
        source="offline-closing-benchmark",
        odds={"home": 1.9, "draw": 3.4, "away": 4.0},
        snapshot_id=f"snapshot:{league}:closing",
    )
    return Top5LeagueRuntimeInput(
        adapter=TOP5_LEAGUE_ADAPTERS[league],
        fixtures=(fixture,),
        signal_snapshots=(snapshot,),
        signal_time=SIGNAL_TIME,
        results=results,
        closing_benchmark_snapshots=(closing,),
    )


def _request(*, with_results: bool = True, **overrides: object) -> Top5RuntimeRequest:
    inputs = {
        league: _league_input(league, with_result=with_results)
        for league in TOP5_RUNTIME_LEAGUES
    }
    values: dict[str, object] = {
        "run_id": "runtime-run:offline-001",
        "session_id": "runtime-session:offline-001",
        "requested_at": BASE,
        "inputs": inputs,
    }
    values.update(overrides)
    return Top5RuntimeRequest(**values)


def test_success_runs_exactly_five_leagues_and_reaches_settled() -> None:
    store = InMemoryTop5RuntimeCheckpointStore()
    report = run_offline_top5_runtime(_request(), checkpoint_store=store)

    assert report.status is Top5RuntimeStatus.READY
    assert report.completed_stages == (
        Top5RuntimeStage.FIXTURES_DISCOVERED,
        Top5RuntimeStage.ODDS_REFRESHED,
        Top5RuntimeStage.PREDICTIONS_GENERATED,
        Top5RuntimeStage.SIGNALS_READY,
        Top5RuntimeStage.RESULTS_INGESTED,
        Top5RuntimeStage.SETTLED,
    )
    assert set(report.league_status) == set(TOP5_RUNTIME_LEAGUES)
    assert report.prediction_count == 5
    assert report.signal_count == 5
    assert report.settlement_count == 5
    assert report.provider_request_count == 5
    assert report.network_request_count == 0
    assert report.retry_count == 0
    assert report.no_bet is True
    assert report.publication_enabled is False
    assert report.activation_enabled is False
    assert report.committed is True
    assert report.as_payload() == report.as_payload()


def test_replay_is_idempotent_and_conflicting_replay_fails_closed() -> None:
    store = InMemoryTop5RuntimeCheckpointStore()
    request = _request()
    first = run_offline_top5_runtime(request, checkpoint_store=store)
    replay = run_offline_top5_runtime(request, checkpoint_store=store)
    assert replay == first

    changed = replace(request, session_id="runtime-session:changed")
    with pytest.raises(Top5RuntimeError, match="does not match its checkpoint"):
        run_offline_top5_runtime(changed, checkpoint_store=store)


def test_missing_results_stops_before_settlement_and_never_creates_qualification_output() -> (
    None
):
    report = run_offline_top5_runtime(_request(with_results=False))

    assert report.status is Top5RuntimeStatus.DEGRADED
    assert report.completed_stages[-1] is Top5RuntimeStage.SIGNALS_READY
    assert Top5RuntimeStage.SETTLED not in report.completed_stages
    assert report.signal_count == 5
    assert report.settlement_count == 0
    assert report.qualification_output_count == 0


def test_one_league_failure_returns_zero_committed_outputs() -> None:
    inputs = dict(_request().inputs)
    inputs["LL"] = replace(inputs["LL"], signal_snapshots=())
    store = InMemoryTop5RuntimeCheckpointStore()
    report = run_offline_top5_runtime(
        replace(_request(), inputs=inputs), checkpoint_store=store
    )

    assert report.status is Top5RuntimeStatus.FAILED
    assert report.completed_stages == (Top5RuntimeStage.FAILED_CLOSED,)
    assert report.prediction_count == 0
    assert report.signal_count == 0
    assert report.settlement_count == 0
    assert report.committed is False
    assert store.load(report.run_id) is None


def test_candidate_provider_cannot_enter_runtime_inputs() -> None:
    candidate = next(iter(CANDIDATE_ONLY_PROVIDER_IDENTITIES))
    inputs = dict(_request().inputs)
    inputs["BL1"] = replace(inputs["BL1"], provider_identity=candidate)
    with pytest.raises(Top5RuntimeError, match="candidate provider"):
        run_offline_top5_runtime(replace(_request(), inputs=inputs))


def test_closing_benchmark_is_not_prediction_input() -> None:
    report = run_offline_top5_runtime(_request())
    assert report.signal_count == 5
    assert report.no_bet is True
    assert report.health.latest_stage is Top5RuntimeStage.SETTLED


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_retries", 2),
        ("timeout_seconds", 0),
        ("maximum_odds_age_seconds", 0),
    ],
)
def test_runtime_policy_is_bounded(field: str, value: object) -> None:
    with pytest.raises(Top5RuntimeError):
        Top5RuntimeConfig(**{field: value}).validate()


def test_exact_five_league_scope_rejects_missing_duplicate_and_unknown() -> None:
    inputs = dict(_request().inputs)
    inputs.pop("L1")
    with pytest.raises(Top5RuntimeError, match="exactly the five"):
        _request(inputs=inputs).validate()

    inputs = dict(_request().inputs)
    inputs["EPL"] = inputs["BL1"]
    with pytest.raises(Top5RuntimeError, match="key differs"):
        _request(inputs=inputs).validate()

    inputs = dict(_request().inputs)
    inputs["UCL"] = inputs.pop("BL1")
    with pytest.raises(Top5RuntimeError, match="exactly the five"):
        _request(inputs=inputs).validate()


def test_precheck_is_blocked_by_default_and_never_mutates() -> None:
    report = top5_activation_precheck(Top5ActivationPrecheckInput())
    assert report.status == "TOP5_RUNTIME_ACTIVATION_BLOCKED"
    assert report.runtime_status is Top5RuntimeStatus.BLOCKED_BY_EVIDENCE
    assert report.ready is False
    assert report.mutation_performed is False
    assert "five-league evidence is not valid" in report.failures
    assert "explicit activation authorization is missing" in report.failures


def test_structural_evidence_cannot_substitute_for_signal_time_activation_approval() -> (
    None
):
    report = top5_activation_precheck(
        Top5ActivationPrecheckInput(
            evidence_reference="structural-provider-evidence:run-004",
            five_league_evidence_valid=True,
            builder1_acceptance_passed=True,
            provider_authority_granted=True,
            model_bound=True,
            research_bound=True,
            signal_time_approved=False,
            scheduler_ready=True,
            health_ready=True,
            rollback_ready=True,
            activation_authorized=True,
            no_synthetic_evidence=True,
        )
    )

    assert report.ready is False
    assert "Signal-Time approval is missing" in report.failures
    assert report.mutation_performed is False


def test_precheck_can_report_ready_only_with_all_explicit_inputs() -> None:
    report = top5_activation_precheck(
        Top5ActivationPrecheckInput(
            evidence_reference="b4-b1-evidence:five-league-001",
            five_league_evidence_valid=True,
            builder1_acceptance_passed=True,
            provider_authority_granted=True,
            model_bound=True,
            research_bound=True,
            signal_time_approved=True,
            scheduler_ready=True,
            health_ready=True,
            rollback_ready=True,
            activation_authorized=True,
            no_synthetic_evidence=True,
        )
    )
    assert report.status == "TOP5_RUNTIME_ACTIVATION_READY"
    assert report.ready is True
    assert report.activation_mode == "disabled"
    assert report.provider_authority == TOP5_PRODUCTION_PROVIDER
    assert report.mutation_performed is False
    assert report.warnings


def test_precheck_rejects_candidate_authority() -> None:
    candidate = next(iter(CANDIDATE_ONLY_PROVIDER_IDENTITIES))
    report = top5_activation_precheck(
        Top5ActivationPrecheckInput(provider_authority=candidate)
    )
    assert report.ready is False
    assert "candidate provider" in report.failures[0]


def test_result_binding_and_checkpoint_payloads_are_deterministic() -> None:
    report = run_offline_top5_runtime(_request())
    assert report.as_payload()["schema_version"] == "top5-runtime-operations-v1"
    result = _league_input("BL1").results[0]
    assert result.as_payload()["settlement_id"] == result.settlement_id
    assert report.health.as_payload()["league_status"] == {
        league: "SETTLED" for league in TOP5_RUNTIME_LEAGUES
    }
