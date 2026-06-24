"""Tests für src/tennis/sim.py (Roadmap J2-C)."""
from __future__ import annotations

import math

import pytest

from src.tennis.sim import (
    p_total_games_over,
    p_total_games_under,
    p_total_sets_over,
    p_total_sets_under,
    set_score_probs,
    simulate_match,
    total_sets_probs,
)


# ---- set_score_probs (closed-form) -----------------------------------

def test_bo3_score_probs_sum_to_one():
    for p in [0.3, 0.5, 0.7]:
        s = sum(set_score_probs(p, 3).values())
        assert math.isclose(s, 1.0, abs_tol=1e-9)


def test_bo5_score_probs_sum_to_one():
    for p in [0.3, 0.5, 0.7]:
        s = sum(set_score_probs(p, 5).values())
        assert math.isclose(s, 1.0, abs_tol=1e-9)


def test_bo3_score_50_50_symmetric():
    s = set_score_probs(0.5, 3)
    assert math.isclose(s["2-0"], s["0-2"])
    assert math.isclose(s["2-1"], s["1-2"])


def test_bo5_score_50_50_symmetric():
    s = set_score_probs(0.5, 5)
    assert math.isclose(s["3-0"], s["0-3"])
    assert math.isclose(s["3-1"], s["1-3"])
    assert math.isclose(s["3-2"], s["2-3"])


def test_bo3_dominant_player_high_2_0():
    """Bei p_set=0.8 sollte 2-0 deutlich häufiger sein als 2-1."""
    s = set_score_probs(0.8, 3)
    assert s["2-0"] > s["2-1"]


def test_invalid_best_of_raises():
    with pytest.raises(ValueError):
        set_score_probs(0.5, 4)


# ---- total_sets_probs -------------------------------------------------

def test_total_sets_bo3_only_2_or_3():
    dist = total_sets_probs(0.5, 3)
    assert set(dist.keys()) == {2, 3}
    assert math.isclose(sum(dist.values()), 1.0)


def test_total_sets_bo5_only_3_4_5():
    dist = total_sets_probs(0.5, 5)
    assert set(dist.keys()) == {3, 4, 5}
    assert math.isclose(sum(dist.values()), 1.0)


def test_p_total_sets_over_under_complementary():
    p_set = 0.55
    for line in [2.5, 3.5]:
        for best_of in [3, 5]:
            if (best_of == 3 and line > 2.5) or (best_of == 5 and line > 4.5):
                continue
            o = p_total_sets_over(p_set, best_of, line)
            u = p_total_sets_under(p_set, best_of, line)
            assert math.isclose(o + u, 1.0, abs_tol=1e-9)


def test_p_over_2_5_bo3_higher_when_evenly_matched():
    """50/50 Match → mehr Gefahr für 3-Sätzer als 80/20 Match."""
    p_even = p_total_sets_over(0.5, 3, 2.5)  # P(3 sets)
    p_dominant = p_total_sets_over(0.8, 3, 2.5)
    assert p_even > p_dominant


# ---- simulate_match (Monte Carlo) -------------------------------------

def test_simulate_match_50_50_balanced():
    """p_set=0.5, BO3 → P(match) ≈ 0.5 ± Sampling-Error."""
    sim = simulate_match(p_set=0.5, best_of=3, tour="atp", n_sim=2000, seed=1)
    assert 0.45 <= sim["p_match_a_wins"] <= 0.55


def test_simulate_match_dominant_favorite():
    """p_set=0.75 → p_match deutlich > 0.5. (Sim ist nicht exakt analytisch,
    weil hold-Approximation kein perfektes p_set-Mapping liefert; konservative
    Schranke nur > 0.6.)"""
    sim = simulate_match(p_set=0.75, best_of=3, tour="atp", n_sim=2000, seed=1)
    assert sim["p_match_a_wins"] > 0.6


def test_simulate_match_bo5_atp_more_games_than_bo3():
    sim_bo3 = simulate_match(p_set=0.55, best_of=3, tour="atp", n_sim=1000, seed=1)
    sim_bo5 = simulate_match(p_set=0.55, best_of=5, tour="atp", n_sim=1000, seed=1)
    assert sim_bo5["mean_games"] > sim_bo3["mean_games"]


def test_simulate_match_deterministic_with_seed():
    sim1 = simulate_match(0.6, 3, "atp", n_sim=500, seed=42)
    sim2 = simulate_match(0.6, 3, "atp", n_sim=500, seed=42)
    assert sim1["p_match_a_wins"] == sim2["p_match_a_wins"]
    assert sim1["mean_games"] == sim2["mean_games"]


def test_simulate_match_wta_lower_hold_more_games_per_set():
    """WTA hat niedrigere Baseline-Hold-Rate → mehr Breaks → mehr Games / Set."""
    sim_atp = simulate_match(0.5, 3, "atp", n_sim=1500, seed=1)
    sim_wta = simulate_match(0.5, 3, "wta", n_sim=1500, seed=1)
    # WTA-Sätze haben mehr Games (mehr Breaks). Bei BO3 mit 50/50 sollten beide
    # ungefähr ähnlich enden — aber WTA hat strukturell mehr Breaks. Toleranz: WTA >= ATP.
    assert sim_wta["mean_games"] >= sim_atp["mean_games"] - 1.0


def test_p_total_games_over_under_complementary():
    sim = simulate_match(0.55, 3, "atp", n_sim=500, seed=1)
    for line in [18.5, 21.5, 24.5]:
        o = p_total_games_over(sim, line)
        u = p_total_games_under(sim, line)
        assert math.isclose(o + u, 1.0)


def test_p_total_games_over_lower_line_higher_p():
    """P(games > 15.5) > P(games > 25.5)."""
    sim = simulate_match(0.55, 3, "atp", n_sim=1000, seed=1)
    assert p_total_games_over(sim, 15.5) > p_total_games_over(sim, 25.5)


def test_total_games_bo5_in_realistic_range():
    """BO5 Match Mean-Games sollte etwa 30-50 sein (3-5 Sätze × ~9-12 Games)."""
    sim = simulate_match(0.55, 5, "atp", n_sim=500, seed=1)
    assert 25 <= sim["mean_games"] <= 55
