"""Tests für src/tennis/ensemble.py (J2-K Phase 4)."""
from __future__ import annotations

import json

import pytest

from src.models.tennis_elo import TennisEloRatings
from src.tennis import ensemble as ens
from src.tennis.features import RollingState


@pytest.fixture(autouse=True)
def _clear_cache():
    ens._CACHED.clear()
    yield
    ens._CACHED.clear()


def _ratings(a: str = "Alice", b: str = "Bob") -> TennisEloRatings:
    ratings = TennisEloRatings()
    ratings.overall[a] = 1700
    ratings.overall[b] = 1500
    ratings.by_surface["hard"] = {a: 1720, b: 1490}
    ratings.surface_counts["hard"] = {a: 30, b: 25}
    return ratings


def _populated_state(a: str, b: str) -> RollingState:
    state = RollingState()
    # Both target players must have real historical state for production LGBM.
    state.update(a, b, "hard")
    state.update(b, a, "hard")
    return state


def test_falls_back_to_elo_when_no_model(tmp_path, monkeypatch):
    monkeypatch.setattr(ens, "_MODEL_DIR", tmp_path / "nowhere")
    ratings = _ratings()
    out = ens.predict_winner_ensemble("Alice", "Bob", ratings, "hard")
    assert out["source"] == "elo"
    assert out["p_a"] > 0.5


def test_falls_back_when_gate_failed(tmp_path, monkeypatch):
    md = tmp_path / "fake_model"
    md.mkdir()
    (md / "metadata.json").write_text(json.dumps({"gate_passed": False}))
    monkeypatch.setattr(ens, "_MODEL_DIR", md)
    ratings = TennisEloRatings()
    ratings.overall["Alice"] = 1600
    ratings.overall["Bob"] = 1600
    out = ens.predict_winner_ensemble("Alice", "Bob", ratings, "hard")
    assert out["source"] == "elo"
    assert out["p_a"] == pytest.approx(0.5)


def test_ensemble_uses_lgbm_when_available():
    ratings = TennisEloRatings()
    ratings.overall["Alcaraz C."] = 1900
    ratings.overall["Michelsen A."] = 1500
    ratings.by_surface["hard"] = {"Alcaraz C.": 1920, "Michelsen A.": 1480}
    ratings.surface_counts["hard"] = {"Alcaraz C.": 30, "Michelsen A.": 25}
    state = _populated_state("Alcaraz C.", "Michelsen A.")
    out = ens.predict_winner_ensemble(
        "Carlos Alcaraz", "Alex Michelsen", ratings, "hard",
        best_of=3, category="atp500", round_str="Quarterfinals",
        rank_a=2, rank_b=45,
        state=state,
        match_date="2026-08-20T15:00:00Z",
        use_live_stats=False,
    )
    assert out["source"] in ("ensemble", "elo")
    assert 0.5 < out["p_a"] < 1.0
    assert out["p_a"] + out["p_b"] == pytest.approx(1.0)
    assert "rolling_state_invalid" not in out
    assert "prediction_time_unavailable" not in out


def test_ensemble_swap_flips_sides():
    ratings = TennisEloRatings()
    ratings.overall["Strong"] = 1750
    ratings.overall["Weak"] = 1500
    ratings.by_surface["hard"] = {"Strong": 1770, "Weak": 1490}
    ratings.surface_counts["hard"] = {"Strong": 30, "Weak": 25}
    state = _populated_state("Strong", "Weak")
    kwargs = {
        "state": state,
        "match_date": "2026-08-20T15:00:00Z",
        "use_live_stats": False,
    }
    out_ab = ens.predict_winner_ensemble("Strong", "Weak", ratings, "hard", **kwargs)
    out_ba = ens.predict_winner_ensemble("Weak", "Strong", ratings, "hard", **kwargs)
    assert out_ab["p_a"] > 0.55
    assert out_ba["p_b"] > 0.55
    assert abs(out_ab["p_a"] - out_ba["p_b"]) < 0.05


def test_ensemble_none_state_returns_elo_with_flag():
    ratings = _ratings("Alice C.", "Bob D.")
    out = ens.predict_winner_ensemble(
        "Alice C.", "Bob D.", ratings, "hard",
        state=None,
        match_date="2026-08-20T15:00:00Z",
    )
    assert out["source"] == "elo"
    assert out.get("rolling_state_unavailable") is True
    assert out["p_a"] > 0.5


def test_ensemble_empty_state_fails_closed():
    ratings = _ratings("Alice C.", "Bob D.")
    out = ens.predict_winner_ensemble(
        "Alice C.", "Bob D.", ratings, "hard",
        state=RollingState(),
        match_date="2026-08-20T15:00:00Z",
    )
    assert out["source"] == "elo"
    assert out.get("rolling_state_invalid") is True


def test_ensemble_partial_state_fails_closed():
    ratings = _ratings("Alice C.", "Bob D.")
    state = RollingState()
    state.update("Alice C.", "Other X.", "hard")
    out = ens.predict_winner_ensemble(
        "Alice C.", "Bob D.", ratings, "hard",
        state=state,
        match_date="2026-08-20T15:00:00Z",
    )
    assert out["source"] == "elo"
    assert out.get("rolling_state_invalid") is True


@pytest.mark.parametrize("bad_date", [None, "", "not-a-date"])
def test_ensemble_invalid_prediction_time_fails_closed(bad_date):
    ratings = _ratings("Alice C.", "Bob D.")
    state = _populated_state("Alice C.", "Bob D.")
    out = ens.predict_winner_ensemble(
        "Alice C.", "Bob D.", ratings, "hard",
        state=state,
        match_date=bad_date,
        use_live_stats=False,
    )
    assert out["source"] == "elo"
    assert out.get("prediction_time_unavailable") is True


def test_ensemble_valid_timezone_timestamp_not_rejected():
    ratings = _ratings("Alice C.", "Bob D.")
    state = _populated_state("Alice C.", "Bob D.")
    out = ens.predict_winner_ensemble(
        "Alice C.", "Bob D.", ratings, "hard",
        state=state,
        match_date="2026-08-20T17:00:00+02:00",
        use_live_stats=False,
    )
    assert "prediction_time_unavailable" not in out
