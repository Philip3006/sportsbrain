"""
Value detection for ATP tennis markets.

Markets supported:
  "home"  — player_a (conventionally listed first by TheOddsAPI) wins match
  "away"  — player_b wins match
  "ah-1.5_a" — player_a wins at least 2 sets more than player_b (3:1 or 3:0 in best-of-5)
  "ah+1.5_b" — player_b wins or loses by no more than 1 set (3:2 or better)

BetSignal.home = player_a, BetSignal.away = player_b (tennis has no actual home/away).
"""
from __future__ import annotations

from src.betting.kelly import dynamic_stake_eur, expected_value, kelly_fraction
from src.betting.value_detector import BetSignal
from src.config import MAX_EV, MIN_EDGE


def _devig_2way(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Proportional devigging for a 2-outcome market (no draw)."""
    p_a = 1.0 / odds_a
    p_b = 1.0 / odds_b
    total = p_a + p_b
    return p_a / total, p_b / total


def _signal(
    match_id: str,
    player_a: str,
    player_b: str,
    market: str,
    model_p: float,
    fair_p: float,
    odds: float,
    bankroll: float,
) -> BetSignal | None:
    ev = expected_value(model_p, odds)
    if ev < MIN_EDGE or ev > MAX_EV:
        return None
    kf = kelly_fraction(model_p, odds)
    stake_eur = dynamic_stake_eur(ev, "MEDIUM")
    return BetSignal(
        match_id=match_id or f"{player_a}_vs_{player_b}",
        home=player_a,
        away=player_b,
        market=market,
        model_prob=model_p,
        fair_prob=fair_p,
        decimal_odds=odds,
        ev=ev,
        kelly_f=kf,
        stake_pct=stake_eur / bankroll if bankroll > 0 else 0.0,
        confidence="MEDIUM",
        stake_eur=stake_eur,
    )


def _p_match_from_p_set(p_s: float) -> float:
    """P(player_a wins best-of-5 match) given per-set win prob p_s."""
    q = 1.0 - p_s
    return p_s**3 * (1.0 + 3.0 * q + 6.0 * q**2)


def _p_set_from_p_match(p_match: float) -> float:
    """Numerical inversion of _p_match_from_p_set via binary search (50 iterations)."""
    lo, hi = 1e-6, 1.0 - 1e-6
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if _p_match_from_p_set(mid) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _set_handicap_probs(p_a_wins: float) -> dict[str, float]:
    """
    Approximates set handicap probabilities for best-of-5 from match-win probability.

    Correctly inverts the best-of-5 binomial: P(win match) depends on per-set prob p_s via
      P = p_s^3 * (1 + 3q + 6q^2)  where q = 1-p_s.

    ah-1.5_a = player_a wins 3:0 or 3:1 (wins by >=2 sets net)
    ah+1.5_b = player_b wins OR 3:2 for player_a (NOT 3:0 or 3:1)
    """
    if p_a_wins <= 0 or p_a_wins >= 1:
        return {"ah-1.5_a": 0.0, "ah+1.5_b": 0.0}

    p_set = _p_set_from_p_match(p_a_wins)
    q = 1.0 - p_set

    p_3_0 = p_set ** 3
    p_3_1 = 3.0 * p_set**3 * q
    p_a_dominant = p_3_0 + p_3_1  # 3:0 or 3:1

    return {
        "ah-1.5_a": max(0.0, min(1.0, p_a_dominant)),
        "ah+1.5_b": max(0.0, min(1.0, 1.0 - p_a_dominant)),
    }


def detect_value_tennis(
    player_a: str,
    player_b: str,
    probs: dict[str, float],
    odds_a: float,
    odds_b: float,
    bankroll: float = 1000.0,
    match_id: str = "",
    ah_odds_a: float = 0.0,
    ah_odds_b: float = 0.0,
) -> list[BetSignal]:
    """
    Detects value in tennis match markets.

    probs: {'p_a': float, 'p_b': float} from predict_winner()
    odds_a / odds_b: decimal odds for player_a / player_b match winner
    ah_odds_a / ah_odds_b: decimal odds for set handicap -1.5/+1.5 (0 = not available)

    Returns list of BetSignal (empty if no value found).
    """
    signals: list[BetSignal] = []
    p_a = probs.get("p_a", 0.0)
    p_b = probs.get("p_b", 0.0)

    # Proportional devigged fair probabilities from market odds
    if odds_a > 1.0 and odds_b > 1.0:
        fair_a, fair_b = _devig_2way(odds_a, odds_b)

        sig = _signal(match_id, player_a, player_b, "home", p_a, fair_a, odds_a, bankroll)
        if sig:
            signals.append(sig)

        sig = _signal(match_id, player_a, player_b, "away", p_b, fair_b, odds_b, bankroll)
        if sig:
            signals.append(sig)

    # Set handicap markets
    ah_probs = _set_handicap_probs(p_a)

    if ah_odds_a > 1.0 and ah_odds_b > 1.0:
        fair_ah_a, fair_ah_b = _devig_2way(ah_odds_a, ah_odds_b)
        sig = _signal(
            match_id, player_a, player_b, "ah-1.5_a",
            ah_probs["ah-1.5_a"], fair_ah_a, ah_odds_a, bankroll,
        )
        if sig:
            signals.append(sig)

        sig = _signal(
            match_id, player_a, player_b, "ah+1.5_b",
            ah_probs["ah+1.5_b"], fair_ah_b, ah_odds_b, bankroll,
        )
        if sig:
            signals.append(sig)

    return signals
