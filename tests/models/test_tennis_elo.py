"""Tests for surface-adjusted tennis Elo model."""
from datetime import datetime

import pandas as pd
import pytest

from src.models.tennis_elo import (
    TennisEloRatings,
    compute_tennis_elo,
    predict_winner,
    _apply_decay,
    _DEFAULT_RATING,
    _DECAY,
    _expected,
    _SURFACE_WEIGHTS,
)


def _make_matches(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[
        "tourney_date", "winner_name", "loser_name", "surface", "tourney_level",
    ])


def test_expected_equal_ratings():
    assert abs(_expected(1500, 1500) - 0.5) < 1e-9


def test_expected_better_player():
    assert _expected(1700, 1500) > 0.5


def test_ratings_update_after_match():
    df = _make_matches(
        (pd.Timestamp("2024-01-01"), "Alcaraz", "Djokovic", "grass", "G"),
    )
    ratings = compute_tennis_elo(df)
    assert ratings.get_overall("Alcaraz") > _DEFAULT_RATING
    assert ratings.get_overall("Djokovic") < _DEFAULT_RATING


def test_surface_ratings_independent():
    df = _make_matches(
        (pd.Timestamp("2024-01-01"), "Alcaraz", "Djokovic", "grass", "G"),
        (pd.Timestamp("2024-02-01"), "Djokovic", "Alcaraz", "clay", "G"),
    )
    ratings = compute_tennis_elo(df)
    # On grass: Alcaraz won, should be higher
    assert ratings.get_surface("Alcaraz", "grass") > ratings.get_surface("Djokovic", "grass")
    # On clay: Djokovic won, should be higher
    assert ratings.get_surface("Djokovic", "clay") > ratings.get_surface("Alcaraz", "clay")


def test_predict_winner_sums_to_one():
    ratings = TennisEloRatings()
    ratings.overall["A"] = 1600
    ratings.overall["B"] = 1500
    probs = predict_winner("A", "B", ratings, "grass")
    assert abs(probs["p_a"] + probs["p_b"] - 1.0) < 1e-9


def test_predict_winner_better_player_higher_prob():
    ratings = TennisEloRatings()
    ratings.overall["Strong"] = 1700
    ratings.overall["Weak"] = 1300
    probs = predict_winner("Strong", "Weak", ratings, "hard")
    assert probs["p_a"] > probs["p_b"]


def test_unknown_player_uses_default():
    ratings = TennisEloRatings()
    probs = predict_winner("Unknown_A", "Unknown_B", ratings, "grass")
    assert abs(probs["p_a"] - 0.5) < 0.01  # near 50/50 for equal unknowns


def test_blended_rating_uses_both_pools():
    ratings = TennisEloRatings()
    ratings.overall["Player"] = 1600
    ratings.by_surface["grass"] = {"Player": 1700}
    blended = ratings.get_blended("Player", "grass", w_surface=0.70)
    expected = 0.70 * 1700 + 0.30 * 1600
    assert abs(blended - expected) < 1e-6


def test_decay_pulls_toward_default():
    pool = {"A": 1700.0, "B": 1300.0}
    _apply_decay(pool)
    assert abs(pool["A"] - (1500.0 + _DECAY * 200.0)) < 1e-9
    assert abs(pool["B"] - (1500.0 + _DECAY * (-200.0))) < 1e-9


def test_annual_decay_applied_at_year_boundary():
    # Two matches in different years — decay should run between them
    df = _make_matches(
        (pd.Timestamp("2023-07-01"), "Alcaraz", "Djokovic", "grass", "G"),
        (pd.Timestamp("2024-07-01"), "Alcaraz", "Djokovic", "grass", "G"),
    )
    ratings = compute_tennis_elo(df)
    # After year boundary, Djokovic (loser in 2023) had their rating decay toward 1500
    # before taking the second loss. Net effect: Djokovic is less penalised than if no decay.
    # Just verify we ran without error and got non-default ratings.
    assert ratings.get_overall("Alcaraz") != _DEFAULT_RATING
    assert ratings.get_overall("Djokovic") != _DEFAULT_RATING


def test_decay_not_applied_within_same_year():
    # Two matches same year — no decay between them
    df = _make_matches(
        (pd.Timestamp("2024-01-01"), "A", "B", "hard", "M"),
        (pd.Timestamp("2024-06-01"), "A", "B", "hard", "G"),
    )
    ratings_same_year = compute_tennis_elo(df)

    # Compare to two matches in different years (decay will be applied between them)
    df2 = _make_matches(
        (pd.Timestamp("2023-06-01"), "A", "B", "hard", "M"),
        (pd.Timestamp("2024-06-01"), "A", "B", "hard", "G"),
    )
    ratings_diff_year = compute_tennis_elo(df2)

    # A wins both: with decay A's 2023 rating was pulled down before 2024 match
    # so A's final rating is lower when decay occurred
    assert ratings_same_year.get_overall("A") > ratings_diff_year.get_overall("A")


# --- Dynamic surface blend tests ---

def test_surface_count_increments_after_update():
    """update() increments surface_counts for both winner and loser."""
    ratings = TennisEloRatings()
    assert ratings.get_surface_count("Alcaraz", "grass") == 0
    ratings.update("Alcaraz", "Djokovic", "grass", "G")
    assert ratings.get_surface_count("Alcaraz", "grass") == 1
    assert ratings.get_surface_count("Djokovic", "grass") == 1
    ratings.update("Alcaraz", "Federer", "grass", "G")
    assert ratings.get_surface_count("Alcaraz", "grass") == 2
    assert ratings.get_surface_count("Federer", "grass") == 1
    # clay counts stay zero
    assert ratings.get_surface_count("Alcaraz", "clay") == 0


def test_dynamic_blend_new_player_trusts_overall():
    """Player with 0 grass matches gets low surface weight (~0.15)."""
    ratings = TennisEloRatings()
    ratings.overall["NewPlayer"] = 1600
    ratings.by_surface["grass"] = {"NewPlayer": 1700}
    # 0 grass matches → w_surface = 0.15
    blended_dynamic = ratings.get_blended("NewPlayer", "grass")
    blended_fixed = ratings.get_blended("NewPlayer", "grass", w_surface=_SURFACE_WEIGHTS["grass"])
    # Dynamic blend is closer to overall (1600) than fixed blend
    assert blended_dynamic < blended_fixed, "0-match player should trust overall more than fixed weight"
    assert abs(blended_dynamic - (0.15 * 1700 + 0.85 * 1600)) < 1.0


def test_dynamic_blend_experienced_player_trusts_surface():
    """Player with 20 grass matches reaches the surface cap weight."""
    ratings = TennisEloRatings()
    ratings.overall["GrassKing"] = 1500
    ratings.by_surface["grass"] = {"GrassKing": 1700}
    ratings.surface_counts["grass"] = {"GrassKing": 20}
    blended = ratings.get_blended("GrassKing", "grass")
    cap = _SURFACE_WEIGHTS["grass"]  # 0.60 for grass
    expected = cap * 1700 + (1.0 - cap) * 1500
    assert abs(blended - expected) < 1.0, "20-match player should use full surface cap weight"


def test_predict_winner_uses_per_player_dynamic_weight():
    """Grass specialist (10 grass matches) vs clay specialist (0 grass matches)."""
    df = _make_matches(*[
        (pd.Timestamp(f"2024-0{(m % 9) + 1}-01"), "GrassSpec", f"opp_g{m}", "grass", "G")
        for m in range(9)
    ])
    ratings = compute_tennis_elo(df)
    assert ratings.get_surface_count("GrassSpec", "grass") == 9
    assert ratings.get_surface_count("ClaySpec", "grass") == 0
    probs = predict_winner("GrassSpec", "ClaySpec", ratings, "grass")
    assert abs(probs["p_a"] + probs["p_b"] - 1.0) < 1e-9
    # GrassSpec's surface Elo built from 9 wins → higher weight → favoured
    assert probs["p_a"] > probs["p_b"]


# --- Recency K-factor tests ---

def test_recency_k_reduces_old_match_impact():
    """Recent match (this year) should move ratings more than the same match 5 years ago."""
    ref = datetime(2026, 6, 9)

    df_recent = _make_matches(
        (pd.Timestamp("2026-05-01"), "A", "B", "grass", "G"),
    )
    df_old = _make_matches(
        (pd.Timestamp("2021-05-01"), "A", "B", "grass", "G"),
    )
    r_recent = compute_tennis_elo(df_recent, reference_date=ref)
    r_old    = compute_tennis_elo(df_old,    reference_date=ref)

    # Recent win → higher rating for A
    assert r_recent.get_overall("A") > r_old.get_overall("A")
    # Recent loss → lower rating for B (more penalised)
    assert r_recent.get_overall("B") < r_old.get_overall("B")


def test_recency_k_same_as_default_when_no_reference():
    """Without reference_date, behaviour is unchanged (backward compatible)."""
    df = _make_matches(
        (pd.Timestamp("2024-01-01"), "Alcaraz", "Djokovic", "grass", "G"),
    )
    ratings_default = compute_tennis_elo(df)
    ratings_no_ref  = compute_tennis_elo(df, reference_date=None)
    assert ratings_default.get_overall("Alcaraz") == ratings_no_ref.get_overall("Alcaraz")


def test_recency_k_gradual_not_cliff():
    """Match 2 years ago should have more weight than 4 years ago (gradual decay)."""
    ref = datetime(2026, 6, 9)

    df_2yr = _make_matches((pd.Timestamp("2024-06-09"), "A", "B", "grass", "G"))
    df_4yr = _make_matches((pd.Timestamp("2022-06-09"), "A", "B", "grass", "G"))

    r_2yr = compute_tennis_elo(df_2yr, reference_date=ref)
    r_4yr = compute_tennis_elo(df_4yr, reference_date=ref)

    # 2-year-old win carries more weight: A's rating deviation is larger
    assert (r_2yr.get_overall("A") - _DEFAULT_RATING) > (r_4yr.get_overall("A") - _DEFAULT_RATING)
