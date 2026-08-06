"""Tier 5 — DC-Modell-Implied Odds (Display-only Fallback).

Wenn keine echte Odds-Quelle liefert: Pseudo-Quoten aus DC+Elo-Modell
mit no_bet_flag=True. Ledger verweigert Eintrag, EV/Kelly übersprungen.
PWA zeigt Badge "Modell-Preis, keine Marktquote".
"""
from __future__ import annotations

from typing import Optional

from src.football.odds.base import FootballOddsQuote

name = "implied_dc"
tier = 5


def implied_from_probs(
    home_team: str,
    away_team: str,
    p_home: float,
    p_draw: float,
    p_away: float,
    margin: float = 0.06,
) -> FootballOddsQuote:
    """Konvertiert DC-Wahrscheinlichkeiten → Pseudo-Quoten mit Overround."""
    eps = 1e-6

    def _q(p: float) -> float:
        p = max(0.02, min(0.96, p))
        return max(1.01, round(1.0 / (p * (1.0 + margin)), 2))

    return FootballOddsQuote(
        home_team=home_team,
        away_team=away_team,
        source="implied_dc",
        source_tier=tier,
        h2h_home=_q(p_home),
        h2h_draw=_q(p_draw),
        h2h_away=_q(p_away),
        bookmaker="model",
        bookies_count_1x2=0,
        confidence=0.25,
        no_bet_flag=True,
    )


def fetch(match_hint: dict) -> Optional[FootballOddsQuote]:
    home_raw = match_hint.get("home_team", "")
    away_raw = match_hint.get("away_team", "")
    if not home_raw or not away_raw:
        return None

    probs = match_hint.get("model_probs") or {}
    p_home = float(probs.get("p_home", 0.0))
    p_draw = float(probs.get("p_draw", 0.0))
    p_away = float(probs.get("p_away", 0.0))

    if p_home + p_draw + p_away < 0.99:
        return None

    return implied_from_probs(home_raw, away_raw, p_home, p_draw, p_away)
