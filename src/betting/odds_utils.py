import math
import re


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


def market_to_all_odds_key(market: str) -> str | None:
    """Map a ledger market string to the flat key used in docs/data/signals.json all_odds."""
    m = market.lower().strip()
    if m in ("home", "draw", "away", "btts_yes", "btts_no", "dc_1x", "dc_x2", "dc_12"):
        return m

    ou = re.fullmatch(r"o/u([0-9]+(?:\.[0-9]+)?)_(over|under)", m)
    if ou:
        line = str(float(ou.group(1))).replace(".", "")
        return f"{ou.group(2)}{line}"

    ah = re.fullmatch(r"ah([+-]?[0-9]+(?:\.[0-9]+)?)_(home|away)", m)
    if ah:
        line = float(ah.group(1))
        side = ah.group(2)
        home_line = line if side == "home" else -line
        return f"ah{home_line}_{side}"

    return None


def extract_market_odds(match: dict, market: str) -> float:
    """Return the current odds for a market from a fetch_upcoming_matches() match dict."""
    m = market.lower().strip()

    direct_keys = {
        "home": "home_odds",
        "draw": "draw_odds",
        "away": "away_odds",
        "btts_yes": "btts_yes_odds",
        "btts_no": "btts_no_odds",
        "dc_1x": "dc_1x_odds",
        "dc_x2": "dc_x2_odds",
        "dc_12": "dc_12_odds",
    }
    if m in direct_keys:
        try:
            return float(match.get(direct_keys[m], 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    ou = re.fullmatch(r"o/u([0-9]+(?:\.[0-9]+)?)_(over|under)", m)
    if ou:
        line = float(ou.group(1))
        side = ou.group(2)
        totals = match.get("totals_lines", {}) or {}
        line_block = totals.get(line, totals.get(str(line), {})) or {}
        try:
            closing = float(line_block.get(side, 0) or 0)
        except (TypeError, ValueError):
            closing = 0.0
        if closing > 1.0:
            return closing
        legacy_key = "over_odds" if line == 2.5 and side == "over" else None
        if line == 2.5 and side == "under":
            legacy_key = "under_odds"
        if legacy_key:
            try:
                return float(match.get(legacy_key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    ah = re.fullmatch(r"ah([+-]?[0-9]+(?:\.[0-9]+)?)_(home|away)", m)
    if ah:
        line = float(ah.group(1))
        side = ah.group(2)
        home_line = line if side == "home" else -line
        spreads = match.get("spreads", {}) or {}
        line_block = spreads.get(home_line, spreads.get(str(home_line), {})) or {}
        try:
            closing = float(line_block.get(side, 0) or 0)
        except (TypeError, ValueError):
            closing = 0.0
        if closing > 1.0:
            return closing

        legacy_key = {
            (-0.5, "home"): "ah_home_odds",
            (-0.5, "away"): "ah_away_odds",
            (-1.0, "home"): "ah1_home_odds",
            (-1.0, "away"): "ah1_away_odds",
            (-1.5, "home"): "ah15_home_odds",
            (-1.5, "away"): "ah15_away_odds",
            (-2.0, "home"): "ah2_home_odds",
            (-2.0, "away"): "ah2_away_odds",
            (-2.5, "home"): "ah25_home_odds",
            (-2.5, "away"): "ah25_away_odds",
        }.get((home_line, side))
        if legacy_key:
            try:
                return float(match.get(legacy_key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

    return 0.0
