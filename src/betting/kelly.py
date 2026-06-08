from src.config import KELLY_FRAC, MIN_STAKE_EUR, MAX_STAKE_EUR


def dynamic_stake_eur(ev: float, confidence: str) -> float:
    """
    Returns absolute stake in EUR, scaling linearly from MIN_STAKE_EUR to MAX_STAKE_EUR.
    EV is clipped at 20% to prevent model artifacts from causing oversized bets.
    HIGH confidence signals receive a +10% bonus (still capped at MAX_STAKE_EUR).
    """
    ev_clipped = min(ev, 0.20)
    ev_range = 0.20 - 0.03  # min_edge = 3%
    amount = MIN_STAKE_EUR + (ev_clipped - 0.03) / ev_range * (MAX_STAKE_EUR - MIN_STAKE_EUR)
    amount = max(MIN_STAKE_EUR, min(MAX_STAKE_EUR, amount))
    if confidence == "HIGH":
        amount = min(amount * 1.10, MAX_STAKE_EUR)
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
