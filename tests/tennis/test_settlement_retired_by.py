"""J8-M4: retired_by-Attribution beim Match-Winner-Settle.

Wenn Score-Fetcher keinen expliziten winner setzt, aber retired_by kennt,
sollte der nicht-aufgebende Spieler als Gewinner gelten.
"""
from __future__ import annotations

from src.betting.tennis_settlement import settle_tennis_market


def _result(**kw):
    base = {"status": "retired", "sets": [(6, 4), (3, 1)], "best_of": 3,
            "winner": None, "retired_by": None}
    base.update(kw)
    return base


def test_retired_by_a_makes_b_winner():
    r = _result(retired_by="a")
    assert settle_tennis_market("home", r) == "lost"
    assert settle_tennis_market("away", r) == "won"


def test_retired_by_b_makes_a_winner():
    r = _result(retired_by="b")
    assert settle_tennis_market("home", r) == "won"
    assert settle_tennis_market("away", r) == "lost"


def test_no_retired_by_still_pushes():
    r = _result(retired_by=None)
    assert settle_tennis_market("home", r) == "push"


def test_completed_match_ignores_retired_by():
    r = {"status": "completed", "sets": [(6, 4), (6, 2)], "best_of": 3,
         "winner": "a", "retired_by": "b"}
    # winner-Feld ist nicht-None → wird nicht überschrieben
    assert settle_tennis_market("home", r) == "won"
