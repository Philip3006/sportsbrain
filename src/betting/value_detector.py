from dataclasses import dataclass

import numpy as np

from src.betting.kelly import expected_value, kelly_fraction, stake
from src.betting.odds_utils import remove_margin_shin
from src.config import MIN_EDGE, MAX_STAKE_PCT


@dataclass
class BetSignal:
    match_id: str
    home: str
    away: str
    market: str          # "home" | "draw" | "away"
    model_prob: float
    fair_prob: float     # Shin-debiased market probability
    decimal_odds: float
    ev: float            # model_prob * decimal_odds - 1
    kelly_f: float
    stake_pct: float     # fraction of bankroll recommended
    confidence: str      # "HIGH" if both DC and LightGBM agree, else "MEDIUM"


_MARKETS = ["home", "draw", "away"]
# model_probs index order: [p_away, p_draw, p_home]
_MODEL_IDX = {"home": 2, "draw": 1, "away": 0}
# raw_odds tuple order: (home_odds, draw_odds, away_odds)
_ODDS_IDX = {"home": 0, "draw": 1, "away": 2}


def detect_value(
    home: str,
    away: str,
    model_probs: np.ndarray,
    raw_odds: tuple[float, float, float],
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    max_stake_pct: float = MAX_STAKE_PCT,
    match_id: str = "",
) -> list[BetSignal]:
    """
    Checks all three markets for positive EV.
    model_probs: array of [p_away, p_draw, p_home]
    raw_odds: (home_decimal, draw_decimal, away_decimal)
    Returns qualifying BetSignals (may be empty).
    """
    fair_home, fair_draw, fair_away = remove_margin_shin(raw_odds)
    fair_probs = {"home": fair_home, "draw": fair_draw, "away": fair_away}

    signals = []
    for market in _MARKETS:
        model_p = float(model_probs[_MODEL_IDX[market]])
        odds = raw_odds[_ODDS_IDX[market]]
        ev = expected_value(model_p, odds)

        if ev <= min_edge:
            continue

        kf = kelly_fraction(model_p, odds)
        stake_pct = stake(kf, 1.0, max_stake_pct)  # as fraction of bankroll

        signals.append(
            BetSignal(
                match_id=match_id or f"{home}_vs_{away}",
                home=home,
                away=away,
                market=market,
                model_prob=model_p,
                fair_prob=fair_probs[market],
                decimal_odds=odds,
                ev=ev,
                kelly_f=kf,
                stake_pct=stake_pct,
                confidence="MEDIUM",
            )
        )

    return signals


def detect_value_ah(
    home: str,
    away: str,
    ah_probs: dict[str, float],
    ah_home_odds: float,
    ah_away_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    max_stake_pct: float = MAX_STAKE_PCT,
    match_id: str = "",
) -> list[BetSignal]:
    """
    Checks Asian Handicap -0.5/+0.5 market for positive EV.
    ah_probs: {p_ah_home, p_ah_away} from dc.predict_asian_handicap()
    """
    signals = []
    for side, model_p, odds, market in [
        ("home", ah_probs["p_ah_home"], ah_home_odds, "ah-0.5_home"),
        ("away", ah_probs["p_ah_away"], ah_away_odds, "ah+0.5_away"),
    ]:
        if odds <= 1.0:
            continue
        ev = expected_value(model_p, odds)
        if ev <= min_edge:
            continue
        kf = kelly_fraction(model_p, odds)
        signals.append(BetSignal(
            match_id=match_id or f"{home}_vs_{away}",
            home=home,
            away=away,
            market=market,
            model_prob=model_p,
            fair_prob=model_p,
            decimal_odds=odds,
            ev=ev,
            kelly_f=kf,
            stake_pct=stake(kf, 1.0, max_stake_pct),
            confidence="MEDIUM",
        ))
    return signals


def detect_value_totals(
    home: str,
    away: str,
    totals_probs: dict[str, float],
    over_odds: float,
    under_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    max_stake_pct: float = MAX_STAKE_PCT,
    match_id: str = "",
) -> list[BetSignal]:
    """
    Checks O/U 2.5 market for positive EV.
    totals_probs: {p_over, p_under, line} from dc.predict_totals()
    """
    signals = []
    line = totals_probs.get("line", 2.5)
    for side, model_p, odds in [
        ("over", totals_probs["p_over"], over_odds),
        ("under", totals_probs["p_under"], under_odds),
    ]:
        if odds <= 1.0:
            continue
        ev = expected_value(model_p, odds)
        if ev <= min_edge:
            continue
        kf = kelly_fraction(model_p, odds)
        signals.append(BetSignal(
            match_id=match_id or f"{home}_vs_{away}",
            home=home,
            away=away,
            market=f"o/u{line}_{side}",
            model_prob=model_p,
            fair_prob=model_p,
            decimal_odds=odds,
            ev=ev,
            kelly_f=kf,
            stake_pct=stake(kf, 1.0, max_stake_pct),
            confidence="MEDIUM",
        ))
    return signals


def set_confidence(signal: BetSignal, dc_probs: dict, lgbm_probs: np.ndarray) -> BetSignal:
    """
    Upgrades confidence to HIGH if both DC and LightGBM independently support the bet.
    dc_probs: {p_home, p_draw, p_away}
    lgbm_probs: [p_away, p_draw, p_home]
    """
    dc_p = dc_probs.get(f"p_{signal.market}", 0.0)
    lgbm_p = float(lgbm_probs[_MODEL_IDX[signal.market]])
    # Both models must show positive EV independently
    if (dc_p * signal.decimal_odds > 1.0) and (lgbm_p * signal.decimal_odds > 1.0):
        signal.confidence = "HIGH"
    return signal
