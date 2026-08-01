"""J8-M5: Impliziter AH-Rechner als Fallback wenn TheOddsAPI-AH nicht liefert.

Idee:
  - Wir haben H2H-Quoten (odds_a, odds_b) aus Multi-Source-Chain.
  - Wir kennen die p_set-Wahrscheinlichkeit über `_p_set_from_p_match`.
  - Daraus lässt sich `p_dominant` (Sieg mit ≥2 Sätzen Diff) ableiten.
  - Zusammen mit einem konservativen Overround (5%) → implizierte AH ±1.5 Quoten.

Nur als Fallback: `no_bet_flag=False` bleibt, aber Confidence < 0.5, damit
der Scanner sie behandeln kann wie eine „soft" Retail-Quote (Tier 4).
"""
from __future__ import annotations

from src.betting.tennis_detector import _p_set_from_p_match, _set_handicap_probs
from src.tennis.odds.base import OddsQuote


def implied_ah_from_h2h(
    p_a_match: float,
    player_a: str,
    player_b: str,
    bo5: bool = True,
    margin: float = 0.05,
) -> tuple[float, float, float, float]:
    """Return (p_ah_a, p_ah_b, odds_ah_a, odds_ah_b).

    p_ah_a: implicit P(A gewinnt mit ≥2 Sätzen Diff)
    p_ah_b: 1 - p_ah_a
    odds_ah_a: implizierte Quote inkl. Overround
    odds_ah_b: analog
    """
    p_a_match = max(0.05, min(0.95, p_a_match))
    ah = _set_handicap_probs(p_a_match, bo5=bo5)
    p_ah_a = ah["ah-1.5_a"]
    p_ah_b = ah["ah+1.5_b"]
    q_a = 1.0 / max(0.02, p_ah_a * (1.0 + margin))
    q_b = 1.0 / max(0.02, p_ah_b * (1.0 + margin))
    return p_ah_a, p_ah_b, round(max(1.05, q_a), 2), round(max(1.05, q_b), 2)


def fetch_ah_fallback(match_hint: dict, ratings=None) -> OddsQuote | None:
    """OddsSource-Interface für impliziten AH-Fallback.

    Braucht `model_p_a` oder ratings+surface im Hint (analog implied.py).
    Return None wenn kein Modell-Prior beschaffbar.
    """
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
            return None
    if p_a is None:
        return None

    bo5 = int(match_hint.get("best_of", 3)) == 5
    p_ah_a, p_ah_b, q_a, q_b = implied_ah_from_h2h(float(p_a), pa, pb, bo5=bo5)

    return OddsQuote(
        player_a=pa,
        player_b=pb,
        h2h_a=q_a,
        h2h_b=q_b,
        source="implied_ah",
        source_tier=4,
        bookmaker="model_derived",
        confidence=0.45,
        no_bet_flag=False,   # als Retail-artiger Fallback nutzbar
        bookies_count=0,
    )
