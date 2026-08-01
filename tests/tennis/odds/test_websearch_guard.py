"""J8-B6 + B12: WebSearch single-quote gate + Median-Aggregation."""
from __future__ import annotations

import pytest

from src.tennis.odds import websearch


def _hit(a: float, b: float):
    return {"a": a, "b": b}


def test_single_quote_marks_no_bet(monkeypatch):
    calls = []

    def _fake(pa, pb, tournament=""):
        calls.append(tournament)
        # Nur die erste Variante liefert etwas
        return _hit(1.90, 2.00) if len(calls) == 1 else None

    monkeypatch.setattr(
        "scripts.tennis_scan._websearch_tennis_fallback", _fake, raising=False,
    )
    q = websearch.fetch({"player_a": "A", "player_b": "B", "sport_key": "atp_wimbledon"})
    assert q is not None
    assert q.no_bet_flag is True, "Single-Quote soll no_bet_flag=True setzen"


def test_multiple_quotes_produce_signal(monkeypatch):
    hits = [_hit(1.90, 2.00), _hit(1.85, 2.05), _hit(1.88, 2.02)]

    def _fake(pa, pb, tournament=""):
        return hits.pop(0) if hits else None

    monkeypatch.setattr(
        "scripts.tennis_scan._websearch_tennis_fallback", _fake, raising=False,
    )
    q = websearch.fetch({"player_a": "A", "player_b": "B", "sport_key": "atp_wimbledon"})
    assert q is not None
    assert q.no_bet_flag is False
    assert q.bookies_count >= 2
