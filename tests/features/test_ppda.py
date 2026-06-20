"""Tests für src/features/ppda.py + src/data/statsbomb_ppda._ppda_from_events."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from src.data.statsbomb_ppda import _ppda_from_events, PRESSING_X, OPP_PASS_X_MAX
from src.features.ppda import (
    GLOBAL_FALLBACK_PPDA,
    confederation_mean_ppda,
    ppda_features,
    ppda_lambda_multipliers,
    team_match_ppda_series,
    team_rolling_ppda,
)


# ---------- _ppda_from_events ----------

def _ev(typ: str, team: str, x: float, *, duel_subtype: str | None = None) -> dict:
    ev = {"type": {"name": typ}, "team": {"name": team}, "location": [x, 40.0]}
    if duel_subtype is not None:
        ev["duel"] = {"type": {"name": duel_subtype}}
    return ev


def test_ppda_from_events_basic_division():
    """6 Opp-Pässe in Opp-60% / 6 Def-Aktionen im Press-Bereich = 1.0."""
    home, away = "TeamA", "TeamB"
    events = []
    # Away macht 6 Pässe in away's eigener 60% (x < 72)
    for _ in range(6):
        events.append(_ev("Pass", away, x=30.0))
    # Home macht 6 Defensiv-Aktionen im Press-Bereich (x >= 48)
    for _ in range(3):
        events.append(_ev("Interception", home, x=60.0))
    for _ in range(3):
        events.append(_ev("Duel", home, x=60.0, duel_subtype="Tackle"))

    res = _ppda_from_events(events, home, away)
    assert math.isclose(res["home_ppda"], 1.0, rel_tol=1e-6)
    # Away pressing: 0 def actions → NaN (Denominator < 5)
    assert math.isnan(res["away_ppda"])


def test_ppda_excludes_passes_in_opp_attacking_third():
    """Pässe mit x >= OPP_PASS_X_MAX zählen nicht für Press-Zähler."""
    home, away = "TeamA", "TeamB"
    events = [
        _ev("Pass", away, x=OPP_PASS_X_MAX + 5.0),  # ignored
        _ev("Pass", away, x=20.0),                  # counts
    ]
    # 5+ def actions damit Denominator nicht NaN-floored wird
    for _ in range(5):
        events.append(_ev("Foul Committed", home, x=PRESSING_X + 1.0))
    res = _ppda_from_events(events, home, away)
    # 1 Pass / 5 Def-Aktionen = 0.2
    assert math.isclose(res["home_ppda"], 0.2, rel_tol=1e-6)


def test_ppda_nan_on_low_denominator():
    home, away = "TeamA", "TeamB"
    events = [_ev("Pass", away, x=10.0) for _ in range(20)]
    # Nur 4 Def-Aktionen → NaN (Threshold = 5)
    for _ in range(4):
        events.append(_ev("Interception", home, x=80.0))
    res = _ppda_from_events(events, home, away)
    assert math.isnan(res["home_ppda"])


# ---------- features/ppda helpers ----------

def _ppda_df():
    return pd.DataFrame([
        {"date": pd.Timestamp("2024-01-01"), "home_team": "Germany", "away_team": "France",
         "home_ppda": 8.0, "away_ppda": 9.5},
        {"date": pd.Timestamp("2024-02-01"), "home_team": "Germany", "away_team": "Spain",
         "home_ppda": 10.0, "away_ppda": 11.0},
        {"date": pd.Timestamp("2024-03-01"), "home_team": "France", "away_team": "Germany",
         "home_ppda": float("nan"), "away_ppda": 9.0},
    ])


def test_team_match_ppda_series_filters_nan_and_respects_date():
    df = _ppda_df()
    s = team_match_ppda_series("Germany", pd.Timestamp("2024-04-01"), df, n_games=10)
    # Germany hatte 8.0 (home @ 2024-01-01), 10.0 (home @ 2024-02-01), 9.0 (away @ 2024-03-01)
    assert sorted(s.tolist()) == [8.0, 9.0, 10.0]

    # Vor 2024-02-15 → nur 2 Matches sichtbar
    s2 = team_match_ppda_series("Germany", pd.Timestamp("2024-02-15"), df, n_games=10)
    assert sorted(s2.tolist()) == [8.0, 10.0]


def test_team_rolling_ppda_uses_sample_mean_when_enough_data():
    df = _ppda_df()
    val = team_rolling_ppda("Germany", pd.Timestamp("2024-04-01"), df, n_games=10, min_matches=3)
    assert math.isclose(val, (8.0 + 10.0 + 9.0) / 3.0, rel_tol=1e-6)


def test_team_rolling_ppda_shrinks_to_prior_when_few_matches():
    df = _ppda_df()
    # Nur 1 Match vor 2024-01-15
    val = team_rolling_ppda(
        "Germany", pd.Timestamp("2024-01-15"), df,
        n_games=10, min_matches=3, prior_weight=3.0,
    )
    # Konföderations-Prior = UEFA-Mittel aus df vor diesem Datum = 8.0 + 9.5 (2 UEFA-Werte)
    prior = (8.0 + 9.5) / 2.0
    expected = (8.0 + 3.0 * prior) / (1 + 3.0)
    assert math.isclose(val, expected, rel_tol=1e-6)


def test_team_rolling_ppda_returns_global_fallback_when_team_unknown():
    df = pd.DataFrame(columns=["date", "home_team", "away_team", "home_ppda", "away_ppda"])
    val = team_rolling_ppda("Atlantis", pd.Timestamp("2024-04-01"), df)
    assert math.isclose(val, GLOBAL_FALLBACK_PPDA, rel_tol=1e-6)


def test_confederation_mean_ppda_filters_by_confederation():
    df = _ppda_df()
    uefa = confederation_mean_ppda("UEFA", pd.Timestamp("2024-04-01"), df)
    assert uefa is not None and uefa > 0
    # Konföderation ohne Daten → None
    assert confederation_mean_ppda("OFC", pd.Timestamp("2024-04-01"), df) is None


def test_ppda_features_returns_zero_dict_on_empty_input():
    out = ppda_features("A", "B", pd.Timestamp("2024-01-01"), None)
    assert out == {"ppda_home": 0.0, "ppda_away": 0.0, "ppda_diff": 0.0}


def test_lambda_multipliers_boost_when_pressing():
    """Niedriges PPDA (aggressives Pressing) → Multiplier > 1."""
    mh, ma = ppda_lambda_multipliers(ppda_home=6.0, ppda_away=14.0)
    assert mh > 1.0
    assert ma < 1.0


def test_lambda_multipliers_neutral_on_nan():
    mh, ma = ppda_lambda_multipliers(ppda_home=float("nan"), ppda_away=float("nan"))
    assert math.isclose(mh, 1.0, rel_tol=1e-9)
    assert math.isclose(ma, 1.0, rel_tol=1e-9)


def test_lambda_multipliers_respect_clip():
    mh, _ = ppda_lambda_multipliers(ppda_home=0.5, ppda_away=11.5,
                                    z_scale=1.0, boost=1.0, clip=0.10)
    # Extreme z würde Mult > 1.10 produzieren → muss auf 1.10 deckeln
    assert math.isclose(mh, 1.10, rel_tol=1e-9)


def test_ppda_features_computes_diff_with_correct_sign():
    df = _ppda_df()
    out = ppda_features("Germany", "France", pd.Timestamp("2024-04-01"), df)
    # diff = away - home → Wenn Germany aggressiver (niedriger PPDA), ist diff positiv
    assert out["ppda_diff"] == pytest.approx(out["ppda_away"] - out["ppda_home"], rel=1e-9)
