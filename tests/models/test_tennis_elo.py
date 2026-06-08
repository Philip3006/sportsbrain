"""Tests for surface-adjusted tennis Elo model."""
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
