"""Agent 8 — Modell-Implied Odds als Display-Only-Fallback.

Wenn keine echte Odds-Quelle liefert, generiert diese Quelle Pseudo-Quoten aus
dem Ensemble-Modell (Elo + LGBM + Surface + H2H) mit `no_bet_flag=True`. Der
Match erscheint dadurch in der PWA, aber:
  - Ledger verweigert Eintrag (siehe src/betting/ledger.append_bets)
  - EV/Kelly-Berechnung übersprungen
  - PWA zeigt Badge "Modell-Preis, keine Marktquote"
"""
from __future__ import annotations

from typing import Optional

from src.tennis.odds.base import OddsQuote


def implied_from_prob(p_a: float,
                      player_a: str,
                      player_b: str,
                      margin: float = 0.05) -> OddsQuote:
    """Wandelt Modell-Wahrscheinlichkeit p_a in Pseudo-Quote (mit Overround)."""
    p_a = max(0.02, min(0.98, p_a))
    p_b = 1.0 - p_a
    # Overround dazu, damit UI-Anzeige realistisch aussieht
    q_a = 1.0 / (p_a * (1.0 + margin))
    q_b = 1.0 / (p_b * (1.0 + margin))
    return OddsQuote(
        player_a=player_a,
        player_b=player_b,
        h2h_a=round(q_a, 2),
        h2h_b=round(q_b, 2),
        source="implied_elo",
        source_tier=5,
        bookmaker="model",
        confidence=0.3,
        no_bet_flag=True,
        bookies_count=0,
    )


def fetch_implied(match_hint: dict, ratings=None) -> Optional[OddsQuote]:
    """OddsSource.fetch-Interface. Benötigt ratings + surface im Hint."""
    pa = match_hint.get("player_a", "")
    pb = match_hint.get("player_b", "")
    if not pa or not pb:
        return None

    p_a = match_hint.get("model_p_a")
    if p_a is None and ratings is not None:
        try:
            from src.tennis.ensemble import predict_winner_ensemble
            probs = predict_winner_ensemble(
                pa, pb, ratings,
                surface=match_hint.get("surface", "hard"),
                best_of=match_hint.get("best_of", 3),
                category=match_hint.get("category", "atp250"),
                name_source=match_hint.get("name_source", "odds_api"),
            )
            p_a = probs.get("p_a", 0.5)
        except Exception:
            p_a = 0.5

    return implied_from_prob(float(p_a or 0.5), pa, pb)
