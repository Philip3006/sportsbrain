from __future__ import annotations

from copy import deepcopy

import pytest

from src.analysis.nations_league_live_edge import (
    NationsLeagueLiveEdgeError,
    build_edge_analysis,
    build_market_snapshot,
    normalize_market_snapshot_input,
    select_causal_market_snapshot,
    validate_edge_analysis,
    validate_market_snapshot,
)
from src.betting.odds_utils import remove_margin_shin


FIXTURE = "uefa-nl:edge-test"
RELEASE = "a" * 64
RECORD = "b" * 64
PREDICTION = "2026-10-01T17:00:00Z"


def _raw_snapshot(**changes):
    value = {
        "provider": "isports_api",
        "bookmaker": "Research bookmaker median",
        "captured_at": "2026-10-01T16:59:00Z",
        "fixture_id": FIXTURE,
        "odds_decimal": {"home": 2.0, "draw": 3.5, "away": 4.0},
    }
    value.update(changes)
    return value


def _snapshot(**changes):
    return build_market_snapshot(_raw_snapshot(**changes))


def _edge(model=None, snapshots=()):
    return build_edge_analysis(
        model or {"home": 0.55, "draw": 0.25, "away": 0.20},
        fixture_id=FIXTURE,
        phase="initial",
        model_release_id=RELEASE,
        prediction_record_id=RECORD,
        prediction_timestamp=PREDICTION,
        market_snapshots=snapshots,
    )


def test_complete_snapshot_has_provenance_digest_and_existing_shin_devig():
    snapshot = _snapshot()
    expected = remove_margin_shin((2.0, 3.5, 4.0))
    assert snapshot["provider"] == "isports_api"
    assert snapshot["bookmaker"] == "Research bookmaker median"
    assert snapshot["overround"] == pytest.approx(0.035714, abs=1e-6)
    assert snapshot["margin_free_probabilities"]["home"] == pytest.approx(expected[0], abs=1e-6)
    assert validate_market_snapshot(snapshot) == snapshot


@pytest.mark.parametrize(
    "changes",
    [
        {"odds_decimal": {"home": 2.0, "draw": 3.5}},
        {"odds_decimal": {"home": 1.0, "draw": 3.5, "away": 4.0}},
        {"odds_decimal": {"home": "2.0", "draw": 3.5, "away": 4.0}},
    ],
)
def test_incomplete_or_malformed_market_fails_closed(changes):
    with pytest.raises(NationsLeagueLiveEdgeError):
        build_market_snapshot(_raw_snapshot(**changes))


def test_snapshot_digest_is_deterministic():
    assert _snapshot() == _snapshot()
    changed = _snapshot(bookmaker="Other bookmaker")
    assert changed["snapshot_digest"] != _snapshot()["snapshot_digest"]


def test_exact_cutoff_and_older_snapshot_are_causal_but_future_snapshot_is_not():
    exact = _snapshot(captured_at=PREDICTION)
    older = _snapshot(captured_at="2026-10-01T16:00:00Z")
    future = _snapshot(captured_at="2026-10-01T17:00:01Z")
    selected, status = select_causal_market_snapshot(
        [older, exact, future], fixture_id=FIXTURE, prediction_timestamp=PREDICTION
    )
    assert status == "VALID"
    assert selected == exact
    future_only, future_status = select_causal_market_snapshot(
        [future], fixture_id=FIXTURE, prediction_timestamp=PREDICTION
    )
    assert future_only is None
    assert future_status == "NO_MARKET_SNAPSHOT"


def test_stale_market_is_explicitly_classified():
    stale = _snapshot(captured_at="2026-10-01T16:00:00Z")
    selected, status = select_causal_market_snapshot(
        [stale], fixture_id=FIXTURE, prediction_timestamp="2026-10-01T17:00:01Z"
    )
    assert selected is None
    assert status == "MARKET_STALE"


def test_edge_uses_model_minus_market_and_model_times_decimal_odds():
    result = _edge(snapshots=[_snapshot()])
    home = result["outcomes"]["home"]
    assert home["probability_edge"] == pytest.approx(0.067241, abs=1e-6)
    assert home["ev"] == pytest.approx(0.1, abs=1e-6)
    assert result["edge_status"] == "EDGE_MEASURED"
    assert result["candidate_outcomes"] == ["home"]


@pytest.mark.parametrize(
    "model,outcome",
    [
        ({"home": 0.5, "draw": 0.25, "away": 0.25}, "home"),
        ({"home": 0.25, "draw": 0.5, "away": 0.25}, "draw"),
        ({"home": 0.25, "draw": 0.25, "away": 0.5}, "away"),
    ],
)
def test_each_outcome_can_be_exposed_as_a_research_candidate(model, outcome):
    snapshot = _snapshot(
        odds_decimal={"home": 2.5, "draw": 2.5, "away": 2.5}
    )
    result = _edge(model=model, snapshots=[snapshot])
    assert result["candidate_outcomes"] == [outcome]
    assert result["highest_edge_outcome"] == outcome


def test_no_market_is_valid_and_cannot_be_backfilled():
    result = _edge()
    assert result["edge_status"] == "NO_MARKET_SNAPSHOT"
    assert result["market_snapshot"] is None
    assert result["outcomes"] == {}
    assert result["no_bet"] is True
    assert result["betting_enabled"] is False
    assert result["ledger_mutation"] is False
    assert not {"stake", "bankroll", "kelly"}.intersection(result)
    assert validate_edge_analysis(result, fixture_id=FIXTURE, prediction_record_id=RECORD) == result


def test_initial_and_refinement_keep_distinct_causal_market_history():
    initial_snapshot = _snapshot(captured_at="2026-10-01T16:50:00Z")
    refinement_snapshot = _snapshot(
        captured_at="2026-10-01T17:30:00Z",
        odds_decimal={"home": 2.4, "draw": 3.2, "away": 3.9},
    )
    initial = _edge(snapshots=[initial_snapshot])
    refinement = build_edge_analysis(
        {"home": 0.55, "draw": 0.25, "away": 0.20},
        fixture_id=FIXTURE,
        phase="refinement",
        model_release_id=RELEASE,
        prediction_record_id="c" * 64,
        prediction_timestamp="2026-10-01T17:31:00Z",
        market_snapshots=[initial_snapshot, refinement_snapshot],
    )
    assert initial["market_snapshot"]["snapshot_digest"] == initial_snapshot["snapshot_digest"]
    assert refinement["market_snapshot"]["snapshot_digest"] == refinement_snapshot["snapshot_digest"]
    assert initial["edge_digest"] != refinement["edge_digest"]
    assert initial["market_snapshot"] != refinement["market_snapshot"]


def test_invalid_market_is_explicitly_classified_without_invalidating_model():
    result = _edge(snapshots=[_raw_snapshot(odds_decimal={"home": 1.0, "draw": 3.5, "away": 4.0})])
    assert result["edge_status"] == "MARKET_INVALID"
    assert result["market_snapshot"] is None


def test_existing_isports_artifact_shape_can_be_normalized_offline():
    fixtures = [{
        "fixture_id": FIXTURE,
        "home_team": "Republic of Ireland",
        "away_team": "Austria",
        "kickoff_utc": "2026-10-01T18:45:00Z",
    }]
    artifact = {
        "schema": "nations-league-isports-shadow-v2",
        "provider": "isports_api",
        "captured_at": "2026-10-01T16:00:00Z",
        "fixtures": [{
            "provider_match_id": "42",
            "home_team": "Ireland",
            "away_team": "Austria",
            "kickoff": "2026-10-01T18:45:00Z",
            "captured_at": "2026-10-01T16:00:00Z",
            "market": {
                "bookmaker": "iSports European odds component-wise median",
                "odds_decimal": {"home": 2.0, "draw": 3.5, "away": 4.0},
            },
        }],
    }
    normalized = normalize_market_snapshot_input(artifact, fixtures=fixtures)
    assert list(normalized) == [FIXTURE]
    assert normalized[FIXTURE][0]["provider_match_id"] == "42"


def test_edge_validator_rejects_private_financial_state():
    result = _edge()
    private = deepcopy(result)
    private["stake"] = 1
    with pytest.raises(NationsLeagueLiveEdgeError, match="private financial"):
        validate_edge_analysis(private, fixture_id=FIXTURE, prediction_record_id=RECORD)
