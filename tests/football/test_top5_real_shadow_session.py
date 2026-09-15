"""Regression coverage for the durable Top-5 real-shadow lifecycle."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_real_shadow_attachments import (
    RealShadowClosingAttachment,
    RealShadowResultAttachment,
    RealShadowResultStatus,
)
from src.football.top5_real_shadow_contracts import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    OFFLINE_REPLAY_MARKER,
    REAL_OBSERVED_MARKER,
    TEST_FIXTURE_MARKER,
    TOP5_REAL_SHADOW_LEAGUES,
    NormalizedProviderObservation,
    RealShadowContractError,
    RealShadowExperiment,
)
from src.football.top5_real_shadow_session import (
    RealShadowSession,
    RealShadowSessionStatus,
)
from src.football.top5_real_shadow_session_evidence import build_shadow_evidence
from src.football.top5_shadow_validation import (
    ShadowEvidenceBundle,
    ShadowSafetyRejection,
)

BASE = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
INTEGRATION_SHA = "b" * 40


def experiment() -> RealShadowExperiment:
    return RealShadowExperiment("shadow-experiment:test-v1", 30, 180, 300, 0)


def observation(league: str = "BL1", *, fixture_key: str | None = None, eligible: bool = True, mode: str = TEST_FIXTURE_MARKER) -> NormalizedProviderObservation:
    fixture = fixture_key or f"{league}:fixture-001"
    return NormalizedProviderObservation(
        league_code=league,
        fixture_key=fixture,
        provider_fixture_id=f"provider-{fixture}",
        home_team="Home FC",
        away_team="Away FC",
        kickoff_utc=BASE + timedelta(hours=3),
        market_type="h2h_1x2",
        home_odds=2.2,
        draw_odds=3.4,
        away_odds=3.1,
        provider_identity="provider-a",
        bookmaker_identity="bookmaker-a",
        source_timestamp=BASE - timedelta(seconds=30),
        captured_at=BASE,
        request_identity=f"request-{fixture}",
        raw_record_digest="a" * 64,
        adapter_version="adapter-v1",
        provider_priority=1,
        fallback_depth=0,
        cascade_trace={"attempts": ["provider-a"], "selected": "provider-a"},
        quality_metadata={"market": "h2h_1x2"},
        eligibility_state="eligible" if eligible else "rejected",
        signal_snapshot_id=f"signal-{fixture}",
        observation_mode=mode,
        independent_validation={
            "accepted": True,
            "prediction_input_allowed": True,
            "contract_version": "top5-provider-cascade-validation-v1",
            "selected_provider": "provider-a",
            "errors": [],
        } if eligible else None,
    )


def make_session(*, scope: tuple[str, ...] = ("BL1",), fixture_mode: bool = True) -> RealShadowSession:
    return RealShadowSession.create(
        "shadow-session:test",
        experiment=experiment(),
        league_scope=scope,
        integration_sha=INTEGRATION_SHA,
        created_at=BASE,
        fixture_mode=fixture_mode,
    )


def test_fixture_observation_is_explicitly_non_real_m5_and_no_bet() -> None:
    session = make_session()
    assert session.record_observation(observation()) is True
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    assert prediction.marker == TEST_FIXTURE_MARKER
    assert prediction.model_identity == M5_CANDIDATE_ID
    assert prediction.research_sha == FROZEN_RESEARCH_SHA
    assert prediction.no_bet is True
    assert prediction.publication is False
    assert prediction.activation is False
    assert session.status is RealShadowSessionStatus.AWAITING_RESULTS


def test_real_observation_requires_explicit_real_mode() -> None:
    session = make_session(fixture_mode=False)
    session.record_observation(observation(mode=REAL_OBSERVED_MARKER))
    session.finalize_predictions()
    assert next(iter(session.predictions.values())).marker == REAL_OBSERVED_MARKER


def test_test_fixture_cannot_enter_default_real_session() -> None:
    session = make_session(fixture_mode=False)
    session.record_observation(observation())
    session.finalize_predictions()
    assert session.status is RealShadowSessionStatus.FAILED_CLOSED
    assert not session.predictions


def test_duplicate_observation_is_idempotent_but_conflict_fails_closed() -> None:
    session = make_session()
    first = observation()
    session.record_observation(first)
    assert session.record_observation(first) is False
    assert session.duplicate_suppressed == 1
    with pytest.raises(RealShadowContractError, match="conflicting observation"):
        session.record_observation(replace(first, home_odds=2.3))


def test_validation_receipt_is_required_for_eligible_prediction_input() -> None:
    invalid = replace(observation(), independent_validation=None)
    with pytest.raises(RealShadowContractError, match="independent accepted validation"):
        invalid.validate(experiment())


def test_rejected_provider_observation_fails_closed_without_prediction() -> None:
    session = make_session()
    session.record_observation(observation(eligible=False))
    session.finalize_predictions()
    assert session.status is RealShadowSessionStatus.FAILED_CLOSED
    assert not session.predictions
    assert session.rejections["BL1:fixture-001"].reason


def test_all_top5_leagues_are_isolated_and_evidence_validates() -> None:
    session = make_session(scope=TOP5_REAL_SHADOW_LEAGUES)
    for league in TOP5_REAL_SHADOW_LEAGUES:
        session.record_observation(observation(league, fixture_key=f"{league}:fixture-001"))
    session.finalize_predictions()
    assert {item.league_code for item in session.predictions.values()} == set(TOP5_REAL_SHADOW_LEAGUES)
    payload = build_shadow_evidence(session)
    bundle = ShadowEvidenceBundle.from_payload(payload)
    bundle.validate()
    assert len(payload["predictions"]) == 5
    assert all(item["trace"]["selected"] == "provider-a" for item in payload["cascade_traces"])
    assert all(item["validation_receipt"]["prediction_input_allowed"] is True for item in payload["cascade_traces"])


def test_result_and_closing_are_temporally_bound_append_only_attachments() -> None:
    session = make_session()
    session.record_observation(observation())
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    result = RealShadowResultAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        result_source="provider-results",
        provider_result_id="result-001",
        result_timestamp=prediction.kickoff,
        attached_at=prediction.kickoff + timedelta(minutes=1),
        status=RealShadowResultStatus.FINAL,
        home_score=2,
        away_score=1,
        actual_outcome="home",
    )
    closing = RealShadowClosingAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        closing_source="closing-provider",
        bookmaker="bookmaker-a",
        closing_timestamp=prediction.kickoff - timedelta(minutes=5),
        attached_at=prediction.kickoff + timedelta(minutes=1),
        odds={"home": 2.0, "draw": 3.5, "away": 3.4},
        closing_snapshot_id="closing-001",
    )
    assert session.attach_result(result) is True
    assert session.attach_result(result) is False
    assert session.attach_closing(closing) is True
    assert session.status is RealShadowSessionStatus.COMPLETE
    with pytest.raises(RealShadowContractError, match="conflicting result"):
        session.attach_result(replace(result, provider_result_id="result-002", attachment_sha=""))
    payload = build_shadow_evidence(session)
    ShadowEvidenceBundle.from_payload(payload).validate()
    assert payload["closing_benchmark_evidence"][0]["used_for_prediction"] is False


def test_result_cannot_be_attached_before_prediction_finalization() -> None:
    session = make_session()
    session.record_observation(observation())
    with pytest.raises(RealShadowContractError, match="finalized prediction"):
        session.attach_result(
            RealShadowResultAttachment(
                prediction_id="missing",
                prediction_artifact_sha="a" * 64,
                fixture_key="BL1:fixture-001",
                league_code="BL1",
                result_source="provider-results",
                provider_result_id=None,
                result_timestamp=BASE,
                attached_at=BASE,
                status=RealShadowResultStatus.POSTPONED,
                home_score=None,
                away_score=None,
                actual_outcome=None,
            )
        )


@pytest.mark.parametrize("status", [RealShadowResultStatus.POSTPONED, RealShadowResultStatus.CANCELLED, RealShadowResultStatus.ABANDONED])
def test_non_final_result_statuses_remain_unresolved(status: RealShadowResultStatus) -> None:
    session = make_session()
    session.record_observation(observation())
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    result = RealShadowResultAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        result_source="provider-results",
        provider_result_id=None,
        result_timestamp=prediction.captured_at,
        attached_at=prediction.captured_at + timedelta(minutes=1),
        status=status,
        home_score=None,
        away_score=None,
        actual_outcome=None,
    )
    assert session.attach_result(result) is True
    assert session.status is RealShadowSessionStatus.PARTIALLY_RESOLVED
    assert session.manifest()["pending_result_count"] == 1


@pytest.mark.parametrize("field", ["fixture_key", "league_code"])
def test_result_identity_mismatch_is_rejected(field: str) -> None:
    session = make_session()
    session.record_observation(observation())
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    result = RealShadowResultAttachment(
        prediction_id=prediction.prediction_id,
        prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key,
        league_code=prediction.league_code,
        result_source="provider-results",
        provider_result_id="result-identity",
        result_timestamp=prediction.kickoff,
        attached_at=prediction.kickoff + timedelta(minutes=1),
        status=RealShadowResultStatus.FINAL,
        home_score=1,
        away_score=0,
        actual_outcome="home",
    )
    bad_value = "BL1:other-fixture" if field == "fixture_key" else "EPL"
    with pytest.raises(RealShadowContractError, match="result does not match prediction identity"):
        session.attach_result(replace(result, **{field: bad_value, "attachment_sha": ""}))


def test_final_result_before_kickoff_is_rejected() -> None:
    session = make_session()
    session.record_observation(observation())
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    result = RealShadowResultAttachment(
        prediction_id=prediction.prediction_id, prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key, league_code=prediction.league_code,
        result_source="provider-results", provider_result_id="result-early",
        result_timestamp=prediction.kickoff - timedelta(seconds=1),
        attached_at=prediction.kickoff + timedelta(minutes=1), status=RealShadowResultStatus.FINAL,
        home_score=1, away_score=0, actual_outcome="home",
    )
    with pytest.raises(RealShadowContractError, match="final result precedes kickoff"):
        session.attach_result(result)


@pytest.mark.parametrize("closing_timestamp", [BASE - timedelta(seconds=31), BASE + timedelta(hours=3, seconds=1)])
def test_closing_outside_signal_to_kickoff_window_is_rejected(closing_timestamp: datetime) -> None:
    session = make_session()
    session.record_observation(observation())
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    closing = RealShadowClosingAttachment(
        prediction_id=prediction.prediction_id, prediction_artifact_sha=prediction.artifact_sha,
        fixture_key=prediction.fixture_key, league_code=prediction.league_code,
        closing_source="closing-provider", bookmaker="bookmaker-a",
        closing_timestamp=closing_timestamp, attached_at=prediction.kickoff + timedelta(minutes=1),
        odds={"home": 2.0, "draw": 3.5, "away": 3.4}, closing_snapshot_id="closing-window",
    )
    with pytest.raises(RealShadowContractError, match="closing is outside"):
        session.attach_closing(closing)


def test_closing_cannot_be_marked_as_prediction_input() -> None:
    session = make_session()
    session.record_observation(observation())
    session.finalize_predictions()
    payload = build_shadow_evidence(session)
    payload["closing_benchmark_evidence"] = [{**payload["closing_benchmark_evidence"][0], "used_for_prediction": True}] if payload["closing_benchmark_evidence"] else []
    if not payload["closing_benchmark_evidence"]:
        prediction = next(iter(session.predictions.values()))
        payload["closing_benchmark_evidence"] = [{
            "provenance": payload["predictions"][0]["provenance"],
            "prediction_id": prediction.prediction_id,
            "signal_snapshot_id": prediction.signal_snapshot_id,
            "closing_snapshot_id": "closing-input",
            "signal_probabilities": dict(prediction.market_probabilities),
            "closing_probabilities": dict(prediction.market_probabilities),
            "used_for_prediction": True,
        }]
    with pytest.raises(ShadowSafetyRejection, match="closing benchmark"):
        ShadowEvidenceBundle.from_payload(payload).validate()


def test_champions_league_input_is_rejected() -> None:
    with pytest.raises(RealShadowContractError, match="outside Top-5"):
        observation("UCL").validate(experiment())


def test_offline_replay_marker_cannot_enter_real_session() -> None:
    session = make_session()
    session.record_observation(observation())
    prediction_id = next(iter(session.predictions))
    session.predictions[prediction_id] = replace(session.predictions[prediction_id], marker=OFFLINE_REPLAY_MARKER)
    with pytest.raises(RealShadowContractError, match="safety markers"):
        session.validate()


@pytest.mark.parametrize("field", ["kickoff_utc", "source_timestamp", "captured_at"])
def test_observation_timestamps_must_be_timezone_aware(field: str) -> None:
    values = observation().__dict__
    values[field] = values[field].replace(tzinfo=None)
    with pytest.raises(RealShadowContractError, match="timezone-aware"):
        NormalizedProviderObservation(**values)
