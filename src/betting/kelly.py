from src.config import (
    KELLY_FRAC,
    MIN_STAKE_EUR,
    MAX_STAKE_EUR,
    STAKE_TIERS,
    GOALS_RANGE_TIER_PCT,
)


def get_stake_bounds(bankroll: float) -> tuple[float, float]:
    """Returns (min_stake_eur, max_stake_eur) for the bankroll tier.

    STAKE_TIERS is ordered high-threshold → low; the first row whose
    threshold ≤ bankroll wins. Falls back to the lowest tier for negative
    bankrolls (shouldn't happen, but keeps callers safe).
    """
    for threshold, lo, hi in STAKE_TIERS:
        if bankroll >= threshold:
            return lo, hi
    threshold, lo, hi = STAKE_TIERS[-1]
    return lo, hi


def goals_range_max_for(bankroll: float | None) -> float:
    """Tier-aware cap for Goals-Range stakes (more conservative than the main MAX)."""
    if bankroll is None:
        lo, hi = MIN_STAKE_EUR, MAX_STAKE_EUR
    else:
        lo, hi = get_stake_bounds(bankroll)
    return lo + GOALS_RANGE_TIER_PCT * (hi - lo)


def dynamic_stake_eur(
    ev: float,
    confidence: str,
    bankroll: float | None = None,
) -> float:
    """
    Returns absolute stake in EUR. Scales linearly from min→max across EV 3%→20%.
    EV is clipped at 20% to prevent model artifacts from causing oversized bets.
    HIGH confidence signals receive a +10% bonus (still capped at tier MAX).

    bankroll=None preserves legacy Tier-0 behaviour (€5–€15) for tests and
    callers that haven't been migrated yet.
    """
    if ev <= 0:
        return 0.0
    if bankroll is None:
        lo, hi = MIN_STAKE_EUR, MAX_STAKE_EUR
    else:
        lo, hi = get_stake_bounds(bankroll)
    ev_clipped = min(ev, 0.20)
    ev_range = 0.20 - 0.03  # min_edge = 3%
    amount = lo + (ev_clipped - 0.03) / ev_range * (hi - lo)
    amount = max(lo, min(hi, amount))
    if confidence == "HIGH":
        amount = min(amount * 1.10, hi)
    return amount


def kelly_fraction(
    model_prob: float,
    decimal_odds: float,
    fraction: float = KELLY_FRAC,
) -> float:
    """
    Fractional Kelly stake as proportion of bankroll.
    f* = fraction * (b*p - q) / b
    where b = decimal_odds - 1, p = model_prob, q = 1 - p.
    Returns 0.0 if edge is zero or negative.
    """
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    p = model_prob
    q = 1.0 - p
    f_full = (b * p - q) / b
    if f_full <= 0:
        return 0.0
    return fraction * f_full


def stake(
    kelly_f: float,
    bankroll: float,
    max_eur: float = MAX_STAKE_EUR,
) -> float:
    """
    Returns recommended stake in EUR.
    Capped at max_eur regardless of Kelly output.
    """
    if kelly_f <= 0 or bankroll <= 0:
        return 0.0
    return min(kelly_f * bankroll, max_eur)


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """EV = model_prob * decimal_odds - 1. Positive = value bet."""
    return model_prob * decimal_odds - 1.0
