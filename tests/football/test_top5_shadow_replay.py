from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
)
from src.football.top5_research_binding import M5_CANDIDATE_ID
from src.football.top5_shadow_replay import (
    ALLOWED_DEV_PARTITIONS,
    ENGINEERING_VALIDATION_MARKER,
    OFFLINE_REPLAY_MARKER,
    OFFLINE_REPLAY_NAMESPACE,
    REAL_SHADOW_NAMESPACE,
    HistoricalReplayInput,
    OfflineReplayError,
    ReplayClosingAttachment,
    ReplayResultAttachment,
    ReplayResultStatus,
)
from src.football.top5_shadow_replay_evidence import archive_coverage
from src.football.top5_shadow_replay_runner import run_offline_replay

BASE = datetime(2023, 9, 1, 12, tzinfo=timezone.utc)
CONTRACT = SignalTimeContract(60, 180, 300)
SOURCE_SHA = "a" * 40
LEAGUES = tuple(sorted(("BL1", "EPL", "LL", "SA", "L1")))


def _fixture(league: str, suffix: str) -> Fixture:
    return Fixture(
        fixture_key=f"historical:{league}:{suffix}",
        league_code=league,
        home_team=f"{league} Home",
        away_team=f"{league} Away",
        kickoff=BASE + timedelta(minutes=120),
    )


def _snapshot(
    fixture: Fixture, *, captured_at: datetime = BASE, partition: str = "2324"
) -> MarketSnapshot:
    return MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=captured_at,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source=f"historical:football-data:{partition}",
        odds={"home": 2.0, "draw": 3.5, "away": 4.0},
        snapshot_id=f"snapshot:{fixture.fixture_key}",
    )


def _input(
    league: str, suffix: str, *, snapshot: bool = True, partition: str = "2324"
) -> HistoricalReplayInput:
    fixture = _fixture(league, suffix)
    return HistoricalReplayInput(
        partition=partition,
        fixture=fixture,
        source_identity=f"historical:football-data:{partition}",
        replayed_at=BASE,
        signal_snapshot=_snapshot(fixture, partition=partition) if snapshot else None,
    )


def _run_all_leagues():
    return run_offline_replay(
        [_input(league, "001") for league in LEAGUES],
        signal_time=CONTRACT,
        integration_sha=SOURCE_SHA,
    )


def test_replay_supports_all_five_leagues_with_honest_coverage():
    run = _run_all_leagues()

    assert tuple(item.league_code for item in run.coverage) == LEAGUES
    assert all(item.candidate_historical_fixtures == 1 for item in run.coverage)
    assert all(item.legitimate_replay_inputs == 1 for item in run.coverage)
    assert all(item.predictions_produced == 1 for item in run.coverage)
    assert all(item.predictions_rejected == 0 for item in run.coverage)
    assert len(run.archive.predictions) == 5
    assert all(prediction.model_identity == M5_CANDIDATE_ID for prediction in run.archive.predictions.values())
    assert all(prediction.no_bet and not prediction.publication for prediction in run.archive.predictions.values())


def test_missing_historical_snapshot_is_reported_as_rejected_not_fabricated():
    run = run_offline_replay(
        [_input("BL1", "missing", snapshot=False)],
        signal_time=CONTRACT,
        integration_sha=SOURCE_SHA,
    )

    coverage = run.coverage[0]
    assert coverage.candidate_historical_fixtures == 1
    assert coverage.legitimate_replay_inputs == 0
    assert coverage.predictions_produced == 0
    assert coverage.predictions_rejected == 1
    assert run.archive.predictions == {}


@pytest.mark.parametrize("partition", ("2425", "2526", "2627", "unknown"))
def test_sealed_and_unapproved_partitions_fail_closed(partition):
    candidate = _input("BL1", "sealed")
    candidate = replace(candidate, partition=partition)
    with pytest.raises(OfflineReplayError, match="partition"):
        candidate.validate()


@pytest.mark.parametrize("partition", tuple(sorted(ALLOWED_DEV_PARTITIONS)))
def test_matching_historical_provenance_partition_is_accepted(partition):
    _input("BL1", f"matching-{partition}", partition=partition).validate()


def test_prediction_and_signal_snapshot_partitions_must_match_declared_partition():
    candidate = _input("BL1", "mismatch")
    with pytest.raises(OfflineReplayError, match="source partition"):
        replace(candidate, source_identity="historical:football-data:2122").validate()

    mismatched_snapshot = replace(
        candidate.signal_snapshot,
        source="historical:market-snapshot:2122",
    )
    with pytest.raises(OfflineReplayError, match="snapshot partition"):
        replace(candidate, signal_snapshot=mismatched_snapshot).validate()


def test_prediction_digest_is_immutable_and_attachments_are_separate():
    run = run_offline_replay([_input("BL1", "immutable")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    before = prediction.as_payload()
    with pytest.raises(TypeError):
        prediction.probabilities["home"] = 0.9

    result = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:football-data:2324",
        result_timestamp=BASE + timedelta(hours=2),
        attached_at=BASE + timedelta(hours=2, minutes=1),
        home_score=2,
        away_score=1,
    )
    closing = ReplayClosingAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        closing_source="historical:closing-snapshot:2324",
        bookmaker="historical-bookmaker",
        closing_timestamp=BASE + timedelta(minutes=100),
        attached_at=BASE + timedelta(hours=2, minutes=1),
        odds={"home": 1.9, "draw": 3.6, "away": 4.2},
    )
    assert run.archive.attach_result(result) is True
    assert run.archive.attach_closing(closing) is True
    assert prediction.as_payload() == before
    assert prediction.artifact_sha == before["artifact_sha"]
    assert "closing" not in prediction.as_payload()
    assert "result" not in prediction.as_payload()


def test_duplicate_result_is_idempotent_and_conflict_is_rejected():
    run = run_offline_replay([_input("BL1", "duplicate")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    result = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2324",
        result_timestamp=BASE + timedelta(hours=2),
        attached_at=BASE + timedelta(hours=2, minutes=1),
        home_score=0,
        away_score=0,
    )
    assert run.archive.attach_result(result) is True
    assert run.archive.attach_result(result) is False
    conflicting = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2324",
        result_timestamp=BASE + timedelta(hours=2),
        attached_at=BASE + timedelta(hours=2, minutes=1),
        home_score=1,
        away_score=0,
    )
    with pytest.raises(OfflineReplayError, match="conflicting"):
        run.archive.attach_result(conflicting)


@pytest.mark.parametrize("partition", ("2526", "unknown"))
def test_result_and_closing_sources_reject_unapproved_partitions(partition):
    run = run_offline_replay([_input("BL1", "attachment-partition")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    result = ReplayResultAttachment.from_scores(
        prediction,
        result_source=f"historical:results:{partition}",
        result_timestamp=prediction.kickoff,
        attached_at=prediction.kickoff + timedelta(minutes=1),
        home_score=1,
        away_score=0,
    )
    with pytest.raises(OfflineReplayError, match="partition"):
        run.archive.attach_result(result)
    closing = ReplayClosingAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        closing_source=f"historical:closing:{partition}",
        bookmaker="bookmaker-a",
        closing_timestamp=prediction.kickoff - timedelta(minutes=10),
        attached_at=prediction.kickoff + timedelta(minutes=1),
        odds={"home": 2.1, "draw": 3.2, "away": 3.9},
    )
    with pytest.raises(OfflineReplayError, match="partition"):
        run.archive.attach_closing(closing)


def test_result_and_closing_partitions_must_match_prediction_partition():
    run = run_offline_replay([_input("BL1", "attachment-mismatch")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    result = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2122",
        result_timestamp=prediction.kickoff,
        attached_at=prediction.kickoff + timedelta(minutes=1),
        home_score=1,
        away_score=0,
    )
    with pytest.raises(OfflineReplayError, match="result partition"):
        run.archive.attach_result(result)
    closing = ReplayClosingAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        closing_source="historical:closing:2122",
        bookmaker="bookmaker-a",
        closing_timestamp=prediction.kickoff - timedelta(minutes=10),
        attached_at=prediction.kickoff + timedelta(minutes=1),
        odds={"home": 2.1, "draw": 3.2, "away": 3.9},
    )
    with pytest.raises(OfflineReplayError, match="closing partition"):
        run.archive.attach_closing(closing)


def test_final_result_requires_kickoff_but_non_final_result_may_precede_it():
    run = run_offline_replay([_input("BL1", "result-causality")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    early_final = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2324",
        result_timestamp=prediction.kickoff - timedelta(minutes=1),
        attached_at=prediction.kickoff,
        home_score=1,
        away_score=0,
    )
    with pytest.raises(OfflineReplayError, match="kickoff"):
        run.archive.attach_result(early_final)
    postponed = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2324",
        result_timestamp=prediction.kickoff - timedelta(minutes=1),
        attached_at=prediction.kickoff,
        home_score=None,
        away_score=None,
        status=ReplayResultStatus.POSTPONED,
    )
    assert run.archive.attach_result(postponed) is True


def test_closing_must_fall_between_signal_time_and_kickoff():
    run = run_offline_replay([_input("BL1", "closing-causality")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))

    def closing_at(timestamp):
        return ReplayClosingAttachment(
            prediction_id=prediction.prediction_id,
            prediction_artifact_sha=prediction.artifact_sha,
            fixture_key=prediction.fixture_key,
            league_code=prediction.league_code,
            closing_source="historical:closing:2324",
            bookmaker="bookmaker-a",
            closing_timestamp=timestamp,
            attached_at=prediction.kickoff + timedelta(minutes=1),
            odds={"home": 2.1, "draw": 3.2, "away": 3.9},
        )

    with pytest.raises(OfflineReplayError, match="signal-to-kickoff"):
        run.archive.attach_closing(closing_at(prediction.source_timestamp - timedelta(seconds=1)))
    with pytest.raises(OfflineReplayError, match="signal-to-kickoff"):
        run.archive.attach_closing(closing_at(prediction.kickoff + timedelta(seconds=1)))
    assert run.archive.attach_closing(closing_at(prediction.kickoff)) is True


def test_malformed_attachment_timestamp_fails_closed():
    run = run_offline_replay([_input("BL1", "timestamp")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        ReplayResultAttachment.from_scores(
            prediction,
            result_source="historical:results:2324",
            result_timestamp=BASE.replace(tzinfo=None, hour=14),
            attached_at=prediction.kickoff,
            home_score=1,
            away_score=0,
        )


@pytest.mark.parametrize("status", tuple(ReplayResultStatus)[1:])
def test_postponed_cancelled_and_abandoned_results_remain_unresolved(status):
    run = run_offline_replay([_input("BL1", status.value)], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    result = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2324",
        result_timestamp=BASE + timedelta(hours=2),
        attached_at=BASE + timedelta(hours=2, minutes=1),
        home_score=None,
        away_score=None,
        status=status,
    )
    assert result.actual_outcome is None
    assert run.archive.attach_result(result) is True
    assert run.to_builder2_bundle().result_attachments[0].resolved is False


def test_result_wrong_fixture_or_league_fails_closed():
    run = run_offline_replay([_input("BL1", "identity")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    result = ReplayResultAttachment.from_scores(
        prediction,
        result_source="historical:results:2324",
        result_timestamp=BASE + timedelta(hours=2),
        attached_at=BASE + timedelta(hours=2, minutes=1),
        home_score=1,
        away_score=0,
    )
    with pytest.raises(OfflineReplayError, match="identity"):
        run.archive.attach_result(replace(result, fixture_key="other-fixture", attachment_sha=""))
    with pytest.raises(OfflineReplayError, match="identity"):
        run.archive.attach_result(replace(result, league_code="EPL", attachment_sha=""))


def test_closing_attachment_is_benchmark_only_and_cannot_leak():
    run = run_offline_replay([_input("BL1", "closing")], signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    prediction = next(iter(run.archive.predictions.values()))
    closing = ReplayClosingAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        closing_source="historical:closing:2324",
        bookmaker="bookmaker-a",
        closing_timestamp=BASE + timedelta(minutes=110),
        attached_at=BASE + timedelta(hours=2),
        odds={"home": 2.1, "draw": 3.2, "away": 3.9},
        used_for_prediction=True,
    )
    with pytest.raises(OfflineReplayError, match="prediction inputs"):
        closing.validate()


def test_replay_is_deterministic_independent_of_input_order():
    inputs = [_input("BL1", "a"), _input("EPL", "b"), _input("SA", "c")]
    first = run_offline_replay(inputs, signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    second = run_offline_replay(tuple(reversed(inputs)), signal_time=CONTRACT, integration_sha=SOURCE_SHA)
    assert first.archive.as_payload() == second.archive.as_payload()
    assert first.to_builder2_bundle().as_payload() == second.to_builder2_bundle().as_payload()


def test_builder2_contract_metrics_and_claims_are_compatible():
    run = _run_all_leagues()
    for prediction in tuple(run.archive.predictions.values()):
        run.archive.attach_result(
            ReplayResultAttachment.from_scores(
                prediction,
                result_source="historical:results:2324",
                result_timestamp=BASE + timedelta(hours=2),
                attached_at=BASE + timedelta(hours=2, minutes=1),
                home_score=1,
                away_score=0,
            )
        )
        run.archive.attach_closing(
            ReplayClosingAttachment(
                prediction_id=prediction.prediction_id,
                prediction_artifact_sha=prediction.artifact_sha,
                fixture_key=prediction.fixture_key,
                league_code=prediction.league_code,
                closing_source="historical:closing:2324",
                bookmaker="bookmaker-a",
                closing_timestamp=BASE + timedelta(minutes=110),
                attached_at=BASE + timedelta(hours=2, minutes=1),
                odds={"home": 2.1, "draw": 3.2, "away": 3.9},
            )
        )
    bundle = run.to_builder2_bundle()
    bundle.validate()
    assert bundle.as_payload()["contract_version"] == "top5-shadow-evidence-v1"
    metrics = run.performance_payload()
    assert metrics["sample_size"] == 5
    assert metrics["resolved_result_count"] == 5
    assert metrics["closing_comparison_count"] == 5
    assert metrics["fully_attached_observation_count"] == 5
    assert metrics["offline_replay"] is True
    assert metrics["profitability_claim"] is False
    assert metrics["significance_claim"] is False
    assert metrics["deployable_edge_claim"] is False
    assert all(item.provider_covered is False for item in bundle.observations)


def test_archive_namespace_and_safety_markers_prevent_real_promotion():
    run = _run_all_leagues()
    payload = run.archive.as_payload()
    assert payload["marker"] == OFFLINE_REPLAY_MARKER
    assert payload["status"] == ENGINEERING_VALIDATION_MARKER
    assert payload["namespace"] == OFFLINE_REPLAY_NAMESPACE
    assert payload["real_shadow_namespace"] == REAL_SHADOW_NAMESPACE
    assert payload["promotable_to_real_observed"] is False
    assert set(ALLOWED_DEV_PARTITIONS) == {"2021", "2122", "2223", "2324"}


def test_archive_coverage_reports_all_five_leagues_without_claiming_provider_validation():
    run = _run_all_leagues()
    report = archive_coverage(run)
    assert [item["league"] for item in report["by_league"]] == list(LEAGUES)
    assert report["builder2_coverage_compatibility"]["leagues"]
    assert run.performance_payload()["closing_comparison_count"] == 0


def test_invalid_source_and_non_top5_fixture_fail_closed():
    candidate = _input("BL1", "source")
    with pytest.raises(OfflineReplayError, match="historical"):
        replace(candidate, source_identity="live:provider").validate()
    with pytest.raises(OfflineReplayError, match="five approved"):
        _input("UCL", "not-allowed").validate()


def test_replay_does_not_write_production_artifacts():
    run = _run_all_leagues()
    payload = run.as_payload()
    assert payload["archive"]["namespace"] == OFFLINE_REPLAY_NAMESPACE
    assert payload["archive"]["promotable_to_real_observed"] is False
    assert payload["performance"]["engineering_validation_only"] is True
