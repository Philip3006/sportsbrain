"""Tests fuer src/betting/tennis_settlement.py."""
from __future__ import annotations

import pytest

from src.betting.tennis_settlement import (
    is_tennis_market,
    settle_tennis_market,
)


# --------------------------------------------------------------------------- #
# is_tennis_market                                                            #
# --------------------------------------------------------------------------- #

def test_is_tennis_market_known_prefixes():
    assert is_tennis_market("ah-1.5_a")
    assert is_tennis_market("ah+1.5_b")
    assert is_tennis_market("first_set_a")
    assert is_tennis_market("first_set_b")
    assert is_tennis_market("o/u_sets_2.5_over")
    assert is_tennis_market("o/u_games_22.5_under")
    assert is_tennis_market("score_2-1")


def test_is_tennis_market_ambiguous_home_away():
    """home/away sind mehrdeutig - Funktion darf NICHT true zurueckgeben."""
    assert not is_tennis_market("home")
    assert not is_tennis_market("away")


def test_is_tennis_market_football_specific():
    assert not is_tennis_market("btts_yes")
    assert not is_tennis_market("o/u2.5_over")   # Football uses no underscore before line
    assert not is_tennis_market("ah-0.5_home")   # Football uses home/away


# --------------------------------------------------------------------------- #
# Match Winner                                                                #
# --------------------------------------------------------------------------- #

def _completed(sets, winner, best_of=3):
    return {"status": "completed", "sets": sets, "winner": winner, "best_of": best_of}


def test_match_winner_home_wins():
    r = _completed([(6, 3), (6, 4)], "a")
    assert settle_tennis_market("home", r) == "won"
    assert settle_tennis_market("away", r) == "lost"


def test_match_winner_away_wins():
    r = _completed([(3, 6), (4, 6)], "b")
    assert settle_tennis_market("home", r) == "lost"
    assert settle_tennis_market("away", r) == "won"


def test_match_pending_state():
    assert settle_tennis_market("home", {"status": "scheduled", "sets": []}) == "pending"
    assert settle_tennis_market("home", {"status": "in_progress", "sets": [(3, 2)]}) == "pending"
    assert settle_tennis_market("home", {"status": "", "sets": []}) == "pending"


# --------------------------------------------------------------------------- #
# First Set                                                                   #
# --------------------------------------------------------------------------- #

def test_first_set_a_wins_first():
    r = _completed([(6, 4), (4, 6), (6, 3)], "a")
    assert settle_tennis_market("first_set_a", r) == "won"
    assert settle_tennis_market("first_set_b", r) == "lost"


def test_first_set_b_wins_first():
    r = _completed([(4, 6), (6, 3), (6, 4)], "a")
    assert settle_tennis_market("first_set_a", r) == "lost"
    assert settle_tennis_market("first_set_b", r) == "won"


# --------------------------------------------------------------------------- #
# Asian Handicap +/-1.5 Sets                                                  #
# --------------------------------------------------------------------------- #

def test_ah_minus_1_5_a_wins_dominant():
    """ah-1.5_a gewinnt nur wenn Spieler A mit >=2 Saetzen Diff siegt."""
    r = _completed([(6, 3), (6, 4)], "a", best_of=3)  # 2-0 fuer A
    assert settle_tennis_market("ah-1.5_a", r) == "won"


def test_ah_minus_1_5_a_loses_3_setter():
    r = _completed([(6, 3), (4, 6), (6, 4)], "a", best_of=3)  # 2-1 fuer A
    assert settle_tennis_market("ah-1.5_a", r) == "lost"


def test_ah_plus_1_5_b_wins_when_b_wins():
    r = _completed([(3, 6), (4, 6)], "b")
    assert settle_tennis_market("ah+1.5_b", r) == "won"


def test_ah_plus_1_5_b_wins_when_b_close_loss():
    """B verliert 1-2 -> ah+1.5_b gewinnt (Cover)."""
    r = _completed([(6, 3), (4, 6), (6, 4)], "a", best_of=3)  # A 2-1
    assert settle_tennis_market("ah+1.5_b", r) == "won"


def test_ah_plus_1_5_b_loses_when_b_swept():
    r = _completed([(6, 3), (6, 4)], "a", best_of=3)  # A 2-0
    assert settle_tennis_market("ah+1.5_b", r) == "lost"


# --------------------------------------------------------------------------- #
# Total Sets O/U                                                              #
# --------------------------------------------------------------------------- #

def test_ou_sets_2_5_over_wins_in_3_setter():
    r = _completed([(6, 3), (4, 6), (6, 4)], "a", best_of=3)  # 3 sets
    assert settle_tennis_market("o/u_sets_2.5_over", r) == "won"
    assert settle_tennis_market("o/u_sets_2.5_under", r) == "lost"


def test_ou_sets_2_5_under_wins_in_straight_sets():
    r = _completed([(6, 3), (6, 4)], "a", best_of=3)  # 2 sets
    assert settle_tennis_market("o/u_sets_2.5_over", r) == "lost"
    assert settle_tennis_market("o/u_sets_2.5_under", r) == "won"


def test_ou_sets_3_5_bo5():
    r = _completed([(6, 4), (4, 6), (6, 3), (7, 5)], "a", best_of=5)  # 4 sets
    assert settle_tennis_market("o/u_sets_3.5_over", r) == "won"
    assert settle_tennis_market("o/u_sets_3.5_under", r) == "lost"


# --------------------------------------------------------------------------- #
# Total Games O/U                                                             #
# --------------------------------------------------------------------------- #

def test_ou_games_22_5_over():
    r = _completed([(6, 4), (7, 5), (6, 3)], "a", best_of=3)  # 10+12+9 = 31 games
    assert settle_tennis_market("o/u_games_22.5_over", r) == "won"


def test_ou_games_22_5_under():
    r = _completed([(6, 0), (6, 2)], "a", best_of=3)  # 6+8 = 14 games
    assert settle_tennis_market("o/u_games_22.5_under", r) == "won"


def test_ou_games_precise_boundary():
    r = _completed([(6, 4), (6, 4)], "a", best_of=3)  # 20 games
    assert settle_tennis_market("o/u_games_19.5_over", r) == "won"
    assert settle_tennis_market("o/u_games_20.5_over", r) == "lost"


# --------------------------------------------------------------------------- #
# Set Betting (Correct Score)                                                 #
# --------------------------------------------------------------------------- #

def test_score_bo3_2_0_hit():
    r = _completed([(6, 3), (6, 4)], "a", best_of=3)
    assert settle_tennis_market("score_2-0", r) == "won"
    assert settle_tennis_market("score_2-1", r) == "lost"
    assert settle_tennis_market("score_1-2", r) == "lost"


def test_score_bo3_2_1_hit():
    r = _completed([(6, 3), (4, 6), (6, 4)], "a", best_of=3)
    assert settle_tennis_market("score_2-1", r) == "won"
    assert settle_tennis_market("score_2-0", r) == "lost"


def test_score_bo5_3_2_hit():
    r = _completed([(6, 3), (4, 6), (6, 4), (3, 6), (7, 5)], "a", best_of=5)
    assert settle_tennis_market("score_3-2", r) == "won"
    assert settle_tennis_market("score_3-0", r) == "lost"


def test_score_pending_when_match_unfinished():
    r = {"status": "in_progress", "sets": [(6, 3), (2, 4)], "winner": None, "best_of": 3}
    # "in_progress" -> pending directly
    assert settle_tennis_market("score_2-0", r) == "pending"


# --------------------------------------------------------------------------- #
# Retirement / Walkover                                                       #
# --------------------------------------------------------------------------- #

def test_retirement_set1_all_void():
    """Retirement vor Abschluss Satz 1 -> alle Maerkte VOID."""
    r = {"status": "retired", "sets": [(3, 2)], "winner": None, "retired_by": "b", "best_of": 3}
    assert settle_tennis_market("home", r) == "push"
    assert settle_tennis_market("away", r) == "push"
    assert settle_tennis_market("first_set_a", r) == "push"
    assert settle_tennis_market("ah-1.5_a", r) == "push"
    assert settle_tennis_market("o/u_sets_2.5_over", r) == "push"
    assert settle_tennis_market("score_2-0", r) == "push"


def test_retirement_after_set1_match_winner_honored():
    """Retirement nach Satz 1 -> Match Winner honoriert, strukturell void."""
    r = {
        "status": "retired",
        "sets": [(6, 3), (2, 1)],
        "winner": "a",           # b retired, a fuehrend
        "retired_by": "b",
        "best_of": 3,
    }
    assert settle_tennis_market("home", r) == "won"
    assert settle_tennis_market("away", r) == "lost"
    assert settle_tennis_market("first_set_a", r) == "won"    # Satz 1 komplett gespielt
    assert settle_tennis_market("first_set_b", r) == "lost"
    assert settle_tennis_market("ah-1.5_a", r) == "push"      # strukturell void
    assert settle_tennis_market("o/u_sets_2.5_over", r) == "push"
    assert settle_tennis_market("o/u_games_22.5_over", r) == "push"
    assert settle_tennis_market("score_2-0", r) == "push"


def test_walkover_all_void():
    r = {"status": "walkover", "sets": [], "winner": None, "best_of": 3}
    assert settle_tennis_market("home", r) == "push"
    assert settle_tennis_market("away", r) == "push"
    assert settle_tennis_market("first_set_a", r) == "push"
    assert settle_tennis_market("ah-1.5_a", r) == "push"


def test_cancelled_all_void():
    r = {"status": "cancelled", "sets": [], "winner": None, "best_of": 3}
    assert settle_tennis_market("home", r) == "push"


# --------------------------------------------------------------------------- #
# Unknown / malformed markets                                                 #
# --------------------------------------------------------------------------- #

def test_unknown_market_returns_none():
    r = _completed([(6, 3), (6, 4)], "a")
    assert settle_tennis_market("some_random_market", r) is None
    assert settle_tennis_market("draw", r) is None  # Tennis kennt kein Unentschieden


def test_malformed_ou_market_returns_none():
    r = _completed([(6, 3), (6, 4)], "a")
    assert settle_tennis_market("o/u_sets_notafloat_over", r) is None
    assert settle_tennis_market("o/u_games_2.5_sideways", r) is None


# --------------------------------------------------------------------------- #
# F4: tennis_settle.py --no-push flag argparse                                #
# --------------------------------------------------------------------------- #

def test_tennis_settle_has_no_push_flag():
    """Regressions-Guard: --no-push muss in tennis_settle.py als argparse-Flag existieren."""
    from pathlib import Path
    src = Path(__file__).parent.parent.parent / "scripts" / "tennis_settle.py"
    text = src.read_text()
    assert '"--no-push"' in text, "--no-push flag fehlt"
    assert "no_push" in text, "no_push dest fehlt"
    # Push-Call gated
    assert "if no_push" in text or "elif no_push" in text or "not no_push" in text, \
        "no_push wird nicht als Gate um send_settlement_alert genutzt"
