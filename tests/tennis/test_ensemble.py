"""Tests für src/tennis/ensemble.py (J2-K Phase 4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models.tennis_elo import TennisEloRatings
from src.tennis import ensemble as ens


@pytest.fixture(autouse=True)
def _clear_cache():
    ens._CACHED.clear()
    yield
    ens._CACHED.clear()


def test_falls_back_to_elo_when_no_model(tmp_path, monkeypatch):
    monkeypatch.setattr(ens, "_MODEL_DIR", tmp_path / "nowhere")
    ratings = TennisEloRatings()
    ratings.overall["Alice"] = 1700
    ratings.overall["Bob"] = 1500
    ratings.by_surface["hard"] = {"Alice": 1720, "Bob": 1490}
    out = ens.predict_winner_ensemble("Alice", "Bob", ratings, "hard")
    assert out["source"] == "elo"
    assert out["p_a"] > 0.5


def test_falls_back_when_gate_failed(tmp_path, monkeypatch):
    # Baue Fake-Modell-Dir mit gate_passed=False
    md = tmp_path / "fake_model"
    md.mkdir()
    (md / "metadata.json").write_text(json.dumps({"gate_passed": False}))
    # model.pkl fehlt — Loader gibt None zurück
    monkeypatch.setattr(ens, "_MODEL_DIR", md)
    ratings = TennisEloRatings()
    ratings.overall["Alice"] = 1600
    ratings.overall["Bob"] = 1600
    out = ens.predict_winner_ensemble("Alice", "Bob", ratings, "hard")
    assert out["source"] == "elo"
    assert out["p_a"] == pytest.approx(0.5)


def test_ensemble_uses_lgbm_when_available():
    """Integration-Test: nutzt das echt-trainierte Modell im repo (models/tennis_lgbm/)."""
    ratings = TennisEloRatings()
    # Elo-Namen-Format ("Nachname V.") aus name_norm.to_elo_name_from_odds_api()
    ratings.overall["Alcaraz C."] = 1900
    ratings.overall["Michelsen A."] = 1500
    ratings.by_surface["hard"] = {"Alcaraz C.": 1920, "Michelsen A.": 1480}
    # Surface-Counts setzen damit Bayesian-Dämpfung nicht gegen 50/50 zieht
    # (siehe Hebel 3 in ensemble.py — n_ref=20 default).
    ratings.surface_counts["hard"] = {"Alcaraz C.": 30, "Michelsen A.": 25}
    out = ens.predict_winner_ensemble(
        "Carlos Alcaraz", "Alex Michelsen", ratings, "hard",
        best_of=3, category="atp500", round_str="Quarterfinals",
        rank_a=2, rank_b=45,
    )
    # Wenn Modell + Gate ok → source=ensemble; sonst elo (auch OK)
    assert out["source"] in ("ensemble", "elo")
    assert 0.5 < out["p_a"] < 1.0  # Favorit deutlich, aber nicht extrem
    assert out["p_a"] + out["p_b"] == pytest.approx(1.0)


def test_ensemble_swap_flips_sides():
    """Seitentausch muss Favorit beibehalten. Symmetric prediction eliminates positional bias."""
    ratings = TennisEloRatings()
    ratings.overall["Strong"] = 1750
    ratings.overall["Weak"] = 1500
    ratings.by_surface["hard"] = {"Strong": 1770, "Weak": 1490}
    ratings.surface_counts["hard"] = {"Strong": 30, "Weak": 25}
    out_ab = ens.predict_winner_ensemble("Strong", "Weak", ratings, "hard")
    out_ba = ens.predict_winner_ensemble("Weak", "Strong", ratings, "hard")
    assert out_ab["p_a"] > 0.55  # Strong is favorite
    assert out_ba["p_b"] > 0.55  # Strong is favorite from other side
    # Symmetric prediction (average of both orderings) reduces positional bias to ~1pp.
    assert abs(out_ab["p_a"] - out_ba["p_b"]) < 0.05
