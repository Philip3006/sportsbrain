"""Tests für src/tennis/features.py (J2-K)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.tennis.features import (
    FEATURE_COLUMNS, RollingState, build_match_features, features_to_row,
    _round_ordinal,
)


def test_feature_columns_stable():
    """Feature-Order ist Contract für Modell-Persistierung."""
    # 30 base + 11 J2-M serve-stat features = 41
    assert len(FEATURE_COLUMNS) == 41
    assert "elo_diff" in FEATURE_COLUMNS
    assert "form_diff" in FEATURE_COLUMNS
    assert "h2h_a_wr" in FEATURE_COLUMNS
    assert "rest_diff" in FEATURE_COLUMNS
    assert "serve_dom_diff" in FEATURE_COLUMNS
    assert "serve_ace_diff" in FEATURE_COLUMNS


def test_rolling_state_form_updates():
    state = RollingState(window=5)
    # Player A gewinnt 3, verliert 1
    state.update("Alice", "Bob", "hard")
    state.update("Alice", "Carol", "hard")
    state.update("Dave", "Alice", "hard")
    state.update("Alice", "Bob", "hard")
    assert state.wr("Alice") == pytest.approx(0.75)
    assert state.wr("Bob") == pytest.approx(0.0)


def test_rolling_state_h2h_symmetric():
    state = RollingState()
    state.update("Alice", "Bob", "clay")
    state.update("Bob", "Alice", "clay")
    state.update("Alice", "Bob", "clay")
    wr_ab, n_ab = state.h2h_wr("Alice", "Bob")
    wr_ba, n_ba = state.h2h_wr("Bob", "Alice")
    assert n_ab == n_ba == 3
    assert wr_ab == pytest.approx(2 / 3)
    assert wr_ba == pytest.approx(1 / 3)


def test_rolling_state_h2h_surface_split():
    state = RollingState()
    state.update("Alice", "Bob", "clay")
    state.update("Alice", "Bob", "hard")
    state.update("Bob", "Alice", "clay")
    wr_clay, n_clay = state.h2h_wr_surface("Alice", "Bob", "clay")
    wr_hard, n_hard = state.h2h_wr_surface("Alice", "Bob", "hard")
    assert n_clay == 2
    assert wr_clay == pytest.approx(0.5)
    assert n_hard == 1
    assert wr_hard == pytest.approx(1.0)


def test_rolling_state_rest_days():
    state = RollingState()
    d1 = pd.Timestamp("2025-01-01")
    d2 = pd.Timestamp("2025-01-10")
    state.update("Alice", "Bob", "hard", date=d1)
    assert state.rest_days("Alice", d2) == 9.0


def test_build_match_features_shape():
    state = RollingState()
    feats = build_match_features(
        player_a="Alice", player_b="Bob", surface="hard", best_of=3,
        category="atp250", round_str="Quarterfinals",
        rank_a=10, rank_b=25, elo_a=1600, elo_b=1550,
        elo_surface_a=1610, elo_surface_b=1540, state=state,
    )
    assert set(FEATURE_COLUMNS).issubset(feats.keys())
    assert feats["rank_diff"] == -15
    assert feats["elo_diff"] == 50
    assert feats["is_hard"] == 1
    assert feats["is_grass"] == 0
    assert feats["best_of"] == 3


def test_build_match_features_surface_flags():
    state = RollingState()
    for surf, expected in (("grass", "is_grass"), ("clay", "is_clay"), ("hard", "is_hard")):
        f = build_match_features(
            player_a="A", player_b="B", surface=surf, best_of=3,
            category="atp250", round_str="1st Round",
            rank_a=1, rank_b=1, elo_a=1500, elo_b=1500,
            elo_surface_a=1500, elo_surface_b=1500, state=state,
        )
        assert f[expected] == 1


def test_features_to_row_stable_order():
    state = RollingState()
    f = build_match_features(
        player_a="A", player_b="B", surface="hard", best_of=3,
        category="grand_slam", round_str="The Final",
        rank_a=1, rank_b=2, elo_a=1800, elo_b=1750,
        elo_surface_a=1810, elo_surface_b=1740, state=state,
    )
    row = features_to_row(f)
    assert len(row) == len(FEATURE_COLUMNS)


def test_round_ordinal_mapping():
    assert _round_ordinal("1st Round") == 1
    assert _round_ordinal("The Final") == 7
    assert _round_ordinal("Unbekannt") == 3  # default fallback


def test_h2h_empty_returns_prior():
    state = RollingState()
    wr, n = state.h2h_wr("New1", "New2")
    assert wr == 0.5
    assert n == 0
