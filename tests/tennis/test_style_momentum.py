"""Tests für style_cluster + momentum (Hebel 2, 5)."""
from src.data.tennis_stats import ServeAggregate
from src.tennis.style_cluster import (
    classify_style, style_matchup_edge,
    SERVE_BOT, AGGRESSOR, BASELINER, COUNTER_PUNCHER, UNKNOWN,
)
from src.tennis.momentum import InPlayState, momentum_score, momentum_prob_adjustment


def _agg(**overrides) -> ServeAggregate:
    defaults = dict(n_matches=20, dominance_rate=0.50, ace_rate=0.05,
                    df_rate=0.03, win_rate=0.55, ace_df_ratio=1.5,
                    first_serve_pct=0.62, first_serve_win_pct=0.70,
                    second_serve_win_pct=0.55, bp_save_pct=0.65,
                    bp_conv_pct=0.40)
    defaults.update(overrides)
    return ServeAggregate(**defaults)


def test_serve_bot_classification():
    assert classify_style(_agg(ace_rate=0.10, df_rate=0.03)) == SERVE_BOT


def test_aggressor_classification():
    assert classify_style(_agg(dominance_rate=0.55, ace_rate=0.06)) == AGGRESSOR


def test_baseliner_classification():
    assert classify_style(_agg(dominance_rate=0.50, ace_rate=0.04)) == BASELINER


def test_unknown_when_small_sample():
    assert classify_style(_agg(n_matches=5)) == UNKNOWN
    assert classify_style(None) == UNKNOWN


def test_matchup_edge_symmetric_sign():
    assert style_matchup_edge(SERVE_BOT, COUNTER_PUNCHER) == 0.03
    assert style_matchup_edge(COUNTER_PUNCHER, SERVE_BOT) == -0.03
    assert style_matchup_edge(UNKNOWN, SERVE_BOT) == 0.0


def test_momentum_positive_for_leader():
    s = InPlayState(
        sets_won_a=1, sets_won_b=0,
        games_won_a_current_set=3, games_won_b_current_set=1,
        breaks_last5_games_a=1, breaks_last5_games_b=0,
        service_hold_streak_a=3, service_hold_streak_b=1,
        on_serve_a=True,
    )
    assert momentum_score(s) > 0
    adj = momentum_prob_adjustment(s, base_p_a=0.50)
    assert 0.50 < adj <= 0.55


def test_momentum_negative_for_trailer():
    s = InPlayState(
        sets_won_a=0, sets_won_b=1,
        games_won_a_current_set=1, games_won_b_current_set=3,
        breaks_last5_games_a=0, breaks_last5_games_b=1,
        service_hold_streak_a=0, service_hold_streak_b=2,
        on_serve_a=False,
    )
    assert momentum_score(s) < 0
    adj = momentum_prob_adjustment(s, base_p_a=0.50)
    assert 0.45 <= adj < 0.50


def test_momentum_capped():
    """Selbst bei extremem Score bleibt Adjustment innerhalb ±5pp."""
    s = InPlayState(
        sets_won_a=2, sets_won_b=0,
        games_won_a_current_set=5, games_won_b_current_set=0,
        breaks_last5_games_a=3, breaks_last5_games_b=0,
        service_hold_streak_a=5, service_hold_streak_b=0,
        on_serve_a=True,
    )
    adj = momentum_prob_adjustment(s, base_p_a=0.50, cap=0.05)
    assert 0.50 < adj <= 0.55
