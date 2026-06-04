from src.config import KELLY_FRAC, MAX_STAKE_PCT


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
    max_pct: float = MAX_STAKE_PCT,
) -> float:
    """
    Returns recommended stake in currency units.
    Capped at max_pct of bankroll regardless of Kelly output.
    """
    if kelly_f <= 0 or bankroll <= 0:
        return 0.0
    return min(kelly_f * bankroll, max_pct * bankroll)


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """EV = model_prob * decimal_odds - 1. Positive = value bet."""
    return model_prob * decimal_odds - 1.0
