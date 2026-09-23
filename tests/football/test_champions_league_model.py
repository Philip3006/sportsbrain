"""Offline safety and determinism tests for the disabled CL model contract."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.config import LEAGUE_REGISTRY
from src.football.champions_league_model import (
    CHAMPIONS_LEAGUE_CODE,
    CHAMPIONS_LEAGUE_FEATURE_SCHEMA,
    CHAMPIONS_LEAGUE_MODEL_ID,
    CHAMPIONS_LEAGUE_PROVIDER,
    CHAMPIONS_LEAGUE_SPORT_KEY,
    ChampionsLeagueContractError,
    ChampionsLeagueEloModel,
    ChampionsLeagueFeatureSnapshot,
    ChampionsLeagueFixture,
    ChampionsLeagueModelArtifact,
    build_closing_benchmark,
    build_shadow_prediction,
    canonical_fixture_key,
    canonical_team_identity,
    champions_league_config,
)
from src.football.champions_league_runtime import (
    CHAMPIONS_LEAGUE_CODE as RUNTIME_CHAMPIONS_LEAGUE_CODE,
)
from src.football.champions_league_runtime import (
    CHAMPIONS_LEAGUE_PROVIDER as RUNTIME_CHAMPIONS_LEAGUE_PROVIDER,
)
from src.football.champions_league_runtime import (
    CHAMPIONS_LEAGUE_SPORT_KEY as RUNTIME_CHAMPIONS_LEAGUE_SPORT_KEY,
)
from src.football.production_contracts import (
    ActivationMode,
    MarketSnapshot,
    MarketSnapshotKind,
    SignalTimeContract,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(minutes=120)
SIGNAL_TIME = SignalTimeContract(60, 180, 900)


def _fixture(**overrides: object) -> ChampionsLeagueFixture:
    values: dict[str, object] = {
        "fixture_key": "UCL:event-001",
        "provider_event_id": "event-001",
        "home_team_id": "home-001",
        "away_team_id": "away-001",
        "home_team": "Home City",
        "away_team": "Away United",
        "kickoff": KICKOFF,
        "neutral_ground": False,
    }
    values.update(overrides)
    return ChampionsLeagueFixture(**values)


def _features(**overrides: object) -> ChampionsLeagueFeatureSnapshot:
    values: dict[str, object] = {
        "values": {"away_elo": 1500.0, "home_elo": 1550.0, "neutral_ground": 0.0},
        "observed_at": NOW - timedelta(minutes=5),
        "source_id": "elo-pre-match-001",
        "source_sha": "a" * 64,
        "research_sha": "b" * 64,
    }
    values.update(overrides)
    return ChampionsLeagueFeatureSnapshot(**values)


def _model(**overrides: object) -> ChampionsLeagueEloModel:
    values: dict[str, object] = {
        "model_id": CHAMPIONS_LEAGUE_MODEL_ID,
        "model_version": "1.0.0",
        "artifact_sha": "c" * 64,
        "calibration_artifact_sha": "d" * 64,
        "research_sha": "b" * 64,
    }
    values.update(overrides)
    return ChampionsLeagueEloModel(ChampionsLeagueModelArtifact(**values))


def _snapshot(
    *, kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME, **overrides: object
) -> MarketSnapshot:
    values: dict[str, object] = {
        "fixture_key": "UCL:event-001",
        "captured_at": NOW - timedelta(minutes=2),
        "kind": kind,
        "source": CHAMPIONS_LEAGUE_PROVIDER,
        "odds": {"home": 1.8, "draw": 3.7, "away": 4.2},
        "snapshot_id": "snapshot-001",
    }
    values.update(overrides)
    return MarketSnapshot(**values)


def _result():
    return build_shadow_prediction(
        _fixture(), _snapshot(), _features(), _model(), SIGNAL_TIME, NOW
    )


def test_canonical_identity_and_disabled_config_are_explicit() -> None:
    assert canonical_fixture_key("event-001") == "UCL:event-001"
    assert canonical_team_identity(" Home-City. ") == "home city"
    config = champions_league_config()
    config.assert_disabled()
    assert config.provider_mapping is not None
    assert config.provider_mapping.provider_name == CHAMPIONS_LEAGUE_PROVIDER
    assert CHAMPIONS_LEAGUE_SPORT_KEY not in LEAGUE_REGISTRY


def test_model_identity_matches_merged_runtime_identity() -> None:
    assert CHAMPIONS_LEAGUE_CODE == RUNTIME_CHAMPIONS_LEAGUE_CODE
    assert CHAMPIONS_LEAGUE_SPORT_KEY == RUNTIME_CHAMPIONS_LEAGUE_SPORT_KEY
    assert CHAMPIONS_LEAGUE_PROVIDER == RUNTIME_CHAMPIONS_LEAGUE_PROVIDER


def test_fixture_requires_provider_bound_identity_and_distinct_teams() -> None:
    with pytest.raises(ChampionsLeagueContractError, match="fixture key"):
        replace(_fixture(), fixture_key="UCL:other-event").validate()
    with pytest.raises(ChampionsLeagueContractError, match="team IDs"):
        replace(_fixture(), away_team_id="home-001").validate()
    with pytest.raises(ChampionsLeagueContractError, match="not a canonical"):
        replace(_fixture(), competition_code="UEFA.CHAMP").validate()


def test_valid_input_emits_generic_shadow_artifacts_only() -> None:
    result = _result()
    result.validate()
    assert result.model_input.fixture.league_code == CHAMPIONS_LEAGUE_CODE
    assert result.prediction.snapshot_kind is MarketSnapshotKind.SIGNAL_TIME
    assert result.signal.activation_mode is ActivationMode.SHADOW
    assert result.signal.no_bet_flag is True
    assert set(result.prediction.probabilities) == {"away", "draw", "home"}
    assert sum(result.prediction.probabilities.values()) == pytest.approx(1.0)


def test_same_contract_inputs_are_deterministic() -> None:
    first = _result()
    second = _result()
    assert first.prediction == second.prediction
    assert first.signal == second.signal


def test_result_revalidation_rejects_split_research_provenance() -> None:
    result = _result()
    with pytest.raises(ChampionsLeagueContractError, match="research identity"):
        replace(
            result,
            feature_snapshot=replace(result.feature_snapshot, research_sha="e" * 64),
        ).validate()


def test_closing_odds_are_rejected_as_prediction_input_and_separate_benchmark() -> None:
    with pytest.raises(ValueError, match="closing odds"):
        _result_from_snapshot(_snapshot(kind=MarketSnapshotKind.CLOSING))
    benchmark = build_closing_benchmark(
        _result(), _snapshot(kind=MarketSnapshotKind.CLOSING, snapshot_id="closing-001")
    )
    benchmark.validate()
    assert benchmark.prediction_id == _result().prediction.prediction_id


def _result_from_snapshot(snapshot: MarketSnapshot):
    return build_shadow_prediction(
        _fixture(), snapshot, _features(), _model(), SIGNAL_TIME, NOW
    )


@pytest.mark.parametrize(
    "feature_overrides, message",
    [
        ({"observed_at": KICKOFF}, "strictly pre-kickoff"),
        ({"values": {"away_elo": 1500.0, "home_elo": 1550.0}}, "feature schema"),
        (
            {"values": {"away_elo": 1500.0, "home_elo": 1550.0, "neutral_ground": 1.0}},
            "neutral_ground",
        ),
        (
            {
                "values": {
                    "away_elo": float("nan"),
                    "home_elo": 1550.0,
                    "neutral_ground": 0.0,
                }
            },
            "not finite",
        ),
    ],
)
def test_missing_future_or_invalid_features_fail_closed(
    feature_overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ChampionsLeagueContractError, match=message):
        build_shadow_prediction(
            _fixture(),
            _snapshot(),
            _features(**feature_overrides),
            _model(),
            SIGNAL_TIME,
            NOW,
        )


def test_naive_times_and_after_kickoff_signal_fail_closed() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _fixture(kickoff=KICKOFF.replace(tzinfo=None))
    with pytest.raises(ChampionsLeagueContractError, match="before kickoff"):
        late_kickoff = KICKOFF
        build_shadow_prediction(
            _fixture(kickoff=late_kickoff),
            _snapshot(captured_at=late_kickoff - timedelta(minutes=2)),
            _features(),
            _model(),
            SignalTimeContract(0, 180, 900),
            late_kickoff,
        )


def test_wrong_provider_snapshot_cannot_enter_cl_model() -> None:
    with pytest.raises(
        ChampionsLeagueContractError, match="canonical production provider"
    ):
        _result_from_snapshot(_snapshot(source="therundown_experimental"))


def test_missing_calibration_or_non_candidate_model_fails_closed() -> None:
    missing_calibration = _model(calibration_artifact_sha="")
    with pytest.raises(ChampionsLeagueContractError, match="calibration"):
        build_shadow_prediction(
            _fixture(), _snapshot(), _features(), missing_calibration, SIGNAL_TIME, NOW
        )
    with pytest.raises(ChampionsLeagueContractError, match="offline candidate"):
        bad = _model()
        build_shadow_prediction(
            _fixture(),
            _snapshot(),
            _features(),
            ChampionsLeagueEloModel(replace(bad.artifact, release_state="live")),
            SIGNAL_TIME,
            NOW,
        )


def test_shadow_signal_cannot_become_live_or_bet() -> None:
    signal = _result().signal
    with pytest.raises(ValueError, match="shadow"):
        replace(signal, activation_mode=ActivationMode.LIVE).validate()
    with pytest.raises(ValueError, match="no-bet"):
        replace(signal, no_bet_flag=False).validate()


def test_canonical_feature_schema_is_exact() -> None:
    assert CHAMPIONS_LEAGUE_FEATURE_SCHEMA == ("away_elo", "home_elo", "neutral_ground")
    assert _result().model_artifact.feature_schema == CHAMPIONS_LEAGUE_FEATURE_SCHEMA
