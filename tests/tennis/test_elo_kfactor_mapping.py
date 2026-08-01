"""J8-B1: category-slug → K-factor mapping.

Regression-Guard: bis 2026-08-01 lief `elo.update(..., category="grand_slam")`
über `_k("grand_slam")` → K=16 statt K=40, was Slam+Masters-Elo strukturell unterspielte.
"""
from __future__ import annotations

from src.models.tennis_elo import (
    TennisEloRatings,
    _k,
    category_to_level,
)


def test_single_letter_level_still_works():
    assert _k("G") == 40.0
    assert _k("M") == 32.0
    assert _k("A") == 24.0
    assert _k("F") == 20.0


def test_category_slugs_map_to_correct_k():
    assert _k("grand_slam") == 40.0
    assert _k("m1000") == 32.0
    assert _k("wta1000") == 32.0
    assert _k("atp500") == 24.0
    assert _k("wta500") == 24.0
    assert _k("tour_final") == 20.0


def test_atp250_wta250_fall_back_to_default_16():
    # Doc-Spec: ATP 250 / other → K=16
    assert _k("atp250") == 16.0
    assert _k("wta250") == 16.0


def test_unknown_and_empty_stay_default():
    assert _k("") == 16.0
    assert _k("something_new") == 16.0


def test_category_to_level_helper():
    assert category_to_level("grand_slam") == "g"
    assert category_to_level("m1000") == "m"
    assert category_to_level("wta1000") == "m"
    assert category_to_level("atp250") == ""  # kein Mapping → default


def test_elo_update_grand_slam_uses_k40():
    """Direkter Regression-Test: Slam-Update produziert die K=40-Delta."""
    baseline = TennisEloRatings()
    baseline.update("A", "B", "hard", "grand_slam")
    delta = baseline.get_overall("A") - 1500.0
    # p_expected=0.5, K=40 → delta = 40 * 0.5 = 20
    assert abs(delta - 20.0) < 0.01, f"Grand-Slam-Update lieferte Delta {delta:.2f}, erwartet 20.0"


def test_elo_update_atp250_uses_k16():
    baseline = TennisEloRatings()
    baseline.update("A", "B", "hard", "atp250")
    delta = baseline.get_overall("A") - 1500.0
    # p_expected=0.5, K=16 → delta = 8
    assert abs(delta - 8.0) < 0.01
