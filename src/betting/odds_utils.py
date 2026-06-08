import math


def remove_margin_shin(
    raw_odds: tuple[float, float, float],
    max_iter: int = 1000,
    tol: float = 1e-8,
) -> tuple[float, float, float]:
    """
    Shin (1993) iterative margin removal for a 3-outcome market.
    More accurate than power method: accounts for asymmetric bookmaker shading.
    Returns (p_home, p_draw, p_away) as fair probabilities summing to 1.
    """
    n = len(raw_odds)
    q = tuple(1.0 / o for o in raw_odds)
    overround = sum(q)

    if abs(overround - 1.0) < tol:
        return q  # no margin to remove

    # Iterative Shin algorithm
    # M stays constant (original overround); only z is updated each iteration.
    M = overround
    z = 0.0
    probs = list(q)
    for _ in range(max_iter):
        new_probs = []
        for qi in q:
            inner = z**2 + 4.0 * (1.0 - z) * (qi / M) ** 2
            p = (math.sqrt(max(inner, 0.0)) - z) / (2.0 * (1.0 - z))
            new_probs.append(p)
        probs = new_probs
        overround_new = sum(probs)
        if abs(overround_new - 1.0) < tol:
            break
        z = (overround_new - 1.0) / (overround_new - 1.0 + n)

    # Normalise
    total = sum(probs)
    p_home, p_draw, p_away = (p / total for p in probs)
    return p_home, p_draw, p_away


def overround(odds: tuple[float, ...]) -> float:
    return sum(1.0 / o for o in odds) - 1.0


def decimal_to_prob(odds: float) -> float:
    return 1.0 / odds


def prob_to_decimal(prob: float) -> float:
    if prob <= 0:
        raise ValueError(f"Probability must be positive, got {prob}")
    return 1.0 / prob


def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100.0 + 1.0
    return 100.0 / abs(american) + 1.0


def margin_pct(odds: tuple[float, ...]) -> float:
    """Bookmaker margin as a percentage of fair price."""
    vig = overround(odds)
    return vig / sum(1.0 / o for o in odds) * 100.0
