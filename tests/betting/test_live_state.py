from src.betting.live_state import pnl_from_mode, resolve_market_state


def test_under_2_5_becomes_early_loss_on_third_goal():
    result = resolve_market_state("o/u2.5_under", 2, 1, completed=False)
    assert result == {"status": "lost", "pnl_mode": "full_loss"}


def test_btts_yes_becomes_early_win_once_both_scored():
    result = resolve_market_state("btts_yes", 1, 1, completed=False)
    assert result == {"status": "won", "pnl_mode": "full_win"}


def test_result_market_stays_unresolved_while_match_is_live():
    assert resolve_market_state("home", 1, 0, completed=False) is None


def test_goals_2_4_no_becomes_early_win_above_four_goals():
    result = resolve_market_state("goals_2_4_no", 3, 2, completed=False)
    assert result == {"status": "won", "pnl_mode": "full_win"}


def test_scorer_market_wins_from_normalized_live_scorer_name():
    result = resolve_market_state(
        "scorer_João Félix Sequeira",
        1,
        0,
        completed=False,
        scorer_names={"Joao Felix Sequeira"},
    )
    assert result == {"status": "won", "pnl_mode": "full_win"}


def test_quarter_total_full_time_uses_half_loss_pnl_mode():
    result = resolve_market_state("o/u2.25_over", 1, 1, completed=True)
    assert result == {"status": "lost", "pnl_mode": "half_loss"}


def test_pnl_from_mode_handles_half_win():
    assert pnl_from_mode("half_win", 2.50, 10.0) == 7.5
