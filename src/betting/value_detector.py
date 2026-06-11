from dataclasses import dataclass, field

import numpy as np

from src.betting.kelly import dynamic_stake_eur, expected_value, kelly_fraction
from src.betting.odds_utils import remove_margin_shin
from src.config import MIN_EDGE, MAX_STAKE_EUR


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
    stake_pct: float     # stake_eur / bankroll (for ledger / % display)
    confidence: str      # "HIGH" if both DC and LightGBM agree, else "MEDIUM"
    stake_eur: float = 0.0    # absolute stake in EUR
    b365_odds: float = 0.0    # Pinnacle reference quote (0 = not available)
    elo_prob: float = 0.0     # Elo win probability for the bet's outcome (0 = not computed)
    n_models_agree: int = 0   # how many of [DC, Elo, LGBM] see value (0–3); 0 = not computed / non-1X2 market


_MARKETS = ["home", "draw", "away"]
# model_probs index order: [p_away, p_draw, p_home]
_MODEL_IDX = {"home": 2, "draw": 1, "away": 0}
# raw_odds tuple order: (home_odds, draw_odds, away_odds)
_ODDS_IDX = {"home": 0, "draw": 1, "away": 2}


def _make_signal(
    match_id: str, home: str, away: str, market: str,
    model_p: float, fair_p: float, odds: float, ev: float,
    kf: float, confidence: str, bankroll: float,
) -> BetSignal:
    stake_eur = dynamic_stake_eur(ev, confidence)
    return BetSignal(
        match_id=match_id or f"{home}_vs_{away}",
        home=home, away=away, market=market,
        model_prob=model_p, fair_prob=fair_p,
        decimal_odds=odds, ev=ev, kelly_f=kf,
        stake_pct=stake_eur / bankroll if bankroll > 0 else 0.0,
        confidence=confidence,
        stake_eur=stake_eur,
    )


def _consistency_confidence(
    ensemble_p: float,
    fair_p: float,
    dc_p: float | None,
    base_confidence: str,
) -> str:
    """
    Returns a downgraded confidence of "LOW" when DC and ensemble disagree about
    which side of the Shin-adjusted fair probability they're on.

    E.g. ensemble=26.3% > fair=17.8% (sees value) but dc=6.3% < fair=17.8%
    (does NOT see value) → conflicting signals → LOW.

    If dc_p is None the check is skipped and base_confidence is returned unchanged.
    """
    if dc_p is None:
        return base_confidence
    ensemble_above = ensemble_p > fair_p
    dc_above = dc_p > fair_p
    if ensemble_above != dc_above:
        return "LOW"
    return base_confidence


def detect_value(
    home: str,
    away: str,
    model_probs: np.ndarray,
    raw_odds: tuple[float, float, float],
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    max_stake_pct: float = MAX_STAKE_EUR,  # kept for API compatibility
    match_id: str = "",
    dc_probs: dict | None = None,
    min_edge_override: dict[str, float] | None = None,
) -> list[BetSignal]:
    """
    Checks all three markets for positive EV.
    model_probs: array of [p_away, p_draw, p_home]
    raw_odds: (home_decimal, draw_decimal, away_decimal)

    dc_probs: optional dict with keys p_home/p_draw/p_away from Dixon-Coles.
    When supplied, a consistency gate checks whether DC and ensemble both sit on
    the same side of the Shin-adjusted fair probability.  If they disagree the
    signal is kept but downgraded to confidence="LOW".

    min_edge_override: optional per-market edge thresholds (e.g. {"away": 0.045}).
    When provided, overrides min_edge for the specified markets.  Unspecified
    markets fall back to min_edge.  Used for confederation-aware bias correction.
    """
    fair_home, fair_draw, fair_away = remove_margin_shin(raw_odds)
    fair_probs = {"home": fair_home, "draw": fair_draw, "away": fair_away}

    signals = []
    for market in _MARKETS:
        effective_min_edge = (
            min_edge_override.get(market, min_edge)
            if min_edge_override is not None
            else min_edge
        )
        model_p = float(model_probs[_MODEL_IDX[market]])
        odds = raw_odds[_ODDS_IDX[market]]
        ev = expected_value(model_p, odds)
        if ev < effective_min_edge - 1e-9:
            continue
        kf = kelly_fraction(model_p, odds)
        fair_p = fair_probs[market]
        dc_p = dc_probs.get(f"p_{market}") if dc_probs else None
        confidence = _consistency_confidence(model_p, fair_p, dc_p, "MEDIUM")
        signals.append(_make_signal(
            match_id, home, away, market,
            model_p, fair_p, odds, ev, kf, confidence, bankroll,
        ))
    return signals


def detect_value_ah(
    home: str,
    away: str,
    ah_probs: dict[str, float],
    ah_home_odds: float,
    ah_away_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    max_stake_pct: float = MAX_STAKE_EUR,  # kept for API compatibility
    match_id: str = "",
    dc_probs: dict | None = None,
    line: float = -0.5,
) -> list[BetSignal]:
    """
    Checks Asian Handicap market for positive EV. Supports lines -0.5, +0.5,
    -1.0, +1.0, -1.5, +1.5.

    ah_probs: {p_ah_home, p_ah_away, p_push} from dc.predict_asian_handicap().
    p_push > 0 for whole-number lines (±1.0).

    For whole-line handicaps with push:
      EV = p_win * (odds - 1) + p_push * 0 + p_lose * (-1)
    Kelly uses effective probability excluding push:
      p_eff = p_win / (p_win + p_lose)  [standard industry approximation]

    dc_probs: optional dict with keys p_home/p_away.  When supplied, the
    consistency gate downgrades to "LOW" if DC and ensemble disagree.
    For AH markets the "fair_p" baseline is the model's own probability (no
    Shin-margin source available), so the gate compares against 0.5 instead.
    """
    p_push = ah_probs.get("p_push", 0.0)
    has_push = p_push > 0.0

    # Determine market label suffixes based on line
    _HOME_LABEL = {-0.5: "ah-0.5_home", -1.0: "ah-1.0_home", -1.5: "ah-1.5_home",
                   -2.0: "ah-2.0_home", -2.5: "ah-2.5_home",
                   0.5: "ah+0.5_home", 1.0: "ah+1.0_home", 1.5: "ah+1.5_home",
                   2.0: "ah+2.0_home", 2.5: "ah+2.5_home"}
    _AWAY_LABEL = {-0.5: "ah+0.5_away", -1.0: "ah+1.0_away", -1.5: "ah+1.5_away",
                   -2.0: "ah+2.0_away", -2.5: "ah+2.5_away",
                   0.5: "ah-0.5_away", 1.0: "ah-1.0_away", 1.5: "ah-1.5_away",
                   2.0: "ah-2.0_away", 2.5: "ah-2.5_away"}
    home_market = _HOME_LABEL.get(line, f"ah{line:+.1f}_home")
    away_market = _AWAY_LABEL.get(line, f"ah{line:+.1f}_away")

    signals = []
    for p_win, odds, market, dc_key in [
        (ah_probs["p_ah_home"], ah_home_odds, home_market, "p_home"),
        (ah_probs["p_ah_away"], ah_away_odds, away_market, "p_away"),
    ]:
        if odds <= 1.0:
            continue

        if has_push:
            # Push-aware EV: p_win*(odds-1) + p_push*0 + p_lose*(-1)
            p_lose = max(0.0, 1.0 - p_win - p_push)
            ev = p_win * (odds - 1) + p_lose * (-1)
            # Kelly: treat push as non-event — use effective probs excluding push
            p_eff = p_win / max(p_win + p_lose, 1e-10)
            kf = kelly_fraction(p_eff, odds)
        else:
            ev = expected_value(p_win, odds)
            kf = kelly_fraction(p_win, odds)

        if ev < min_edge - 1e-9:
            continue

        # For AH the implied fair is ~0.5 (balanced book); use it as baseline.
        dc_p = dc_probs.get(dc_key) if dc_probs else None
        confidence = _consistency_confidence(p_win, 0.5, dc_p, "MEDIUM")
        signals.append(_make_signal(
            match_id, home, away, market,
            p_win, p_win, odds, ev, kf, confidence, bankroll,
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
    max_stake_pct: float = MAX_STAKE_EUR,  # kept for API compatibility
    match_id: str = "",
    dc_probs: dict | None = None,
    min_edge_under: float | None = None,
) -> list[BetSignal]:
    """
    Checks O/U market for positive EV.
    totals_probs: {p_over, p_under, line} from dc.predict_totals()

    min_edge_under: if set, overrides min_edge for UNDER side only.
    Use to compensate for DC's systematic OVER underestimation (calibration finding:
    model under-calls OVER by ~3-6pp → UNDER signals need higher threshold).

    dc_probs: optional dict with keys p_over/p_under (DC totals output).
    """
    signals = []
    line = totals_probs.get("line", 2.5)
    for side, model_p, odds, dc_key in [
        ("over", totals_probs["p_over"], over_odds, "p_over"),
        ("under", totals_probs["p_under"], under_odds, "p_under"),
    ]:
        if odds <= 1.0:
            continue
        effective_min = min_edge_under if (side == "under" and min_edge_under is not None) else min_edge
        ev = expected_value(model_p, odds)
        if ev < effective_min - 1e-9:
            continue
        kf = kelly_fraction(model_p, odds)
        dc_p = dc_probs.get(dc_key) if dc_probs else None
        confidence = _consistency_confidence(model_p, 0.5, dc_p, "MEDIUM")
        signals.append(_make_signal(
            match_id, home, away, f"o/u{line}_{side}",
            model_p, model_p, odds, ev, kf, confidence, bankroll,
        ))
    return signals


def detect_value_btts(
    home: str,
    away: str,
    btts_probs: dict[str, float],
    btts_yes_odds: float,
    btts_no_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    max_stake_pct: float = MAX_STAKE_EUR,  # kept for API compatibility
    match_id: str = "",
    dc_probs: dict | None = None,
) -> list[BetSignal]:
    """
    Checks BTTS (Both Teams to Score) market for positive EV.
    btts_probs: {p_btts_yes, p_btts_no} from dc.predict_btts()

    dc_probs: optional dict with keys p_btts_yes/p_btts_no (DC btts output).
    Consistency gate downgrades to "LOW" when models conflict relative to the
    fair baseline (~0.5 for balanced BTTS books).
    """
    # Compute market-implied fair probabilities for the consistency gate.
    # 2-outcome market: simple proportional margin removal (no Shin needed).
    # If either odds is missing, fall back to 0.5.
    if btts_yes_odds > 1.0 and btts_no_odds > 1.0:
        total_inv = 1.0 / btts_yes_odds + 1.0 / btts_no_odds
        fair_yes = (1.0 / btts_yes_odds) / total_inv
        fair_no = (1.0 / btts_no_odds) / total_inv
    else:
        fair_yes = fair_no = 0.5

    signals = []
    for side, model_p, odds, dc_key, fair_p in [
        ("btts_yes", btts_probs["p_btts_yes"], btts_yes_odds, "p_btts_yes", fair_yes),
        ("btts_no", btts_probs["p_btts_no"], btts_no_odds, "p_btts_no", fair_no),
    ]:
        if odds <= 1.0:
            continue
        ev = expected_value(model_p, odds)
        if ev < min_edge - 1e-9:
            continue
        kf = kelly_fraction(model_p, odds)
        dc_p = dc_probs.get(dc_key) if dc_probs else None
        confidence = _consistency_confidence(model_p, fair_p, dc_p, "MEDIUM")
        signals.append(_make_signal(
            match_id, home, away, side,
            model_p, fair_p, odds, ev, kf, confidence, bankroll,
        ))
    return signals


def detect_value_ftts(
    home: str,
    away: str,
    ftts_probs: dict[str, float],
    ftts_home_odds: float,
    ftts_away_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    match_id: str = "",
) -> list[BetSignal]:
    """
    Checks First Team to Score (FTTS) market for positive EV.
    ftts_probs: {p_home_first, p_away_first} from dc.predict_first_scorer()
    """
    signals = []
    for market, model_p, odds in [
        ("ftts_home", ftts_probs["p_home_first"], ftts_home_odds),
        ("ftts_away", ftts_probs["p_away_first"], ftts_away_odds),
    ]:
        if odds <= 1.0:
            continue
        ev = expected_value(model_p, odds)
        if ev < min_edge - 1e-9:
            continue
        kf = kelly_fraction(model_p, odds)
        signals.append(_make_signal(
            match_id, home, away, market,
            model_p, model_p, odds, ev, kf, "MEDIUM", bankroll,
        ))
    return signals


def set_confidence(signal: BetSignal, dc_probs: dict, lgbm_probs: np.ndarray) -> BetSignal:
    """
    Upgrades confidence to HIGH when model(s) strongly support the bet.

    1X2 markets: requires BOTH DC and LightGBM to see value (p * odds > 1.0).
    Non-1X2 markets (BTTS, O/U): LightGBM has no direct class output for these.
       Uses model_prob (the DC-computed probability) with a stricter threshold (≥10% EV)
       so only high-conviction DC signals get the HIGH bonus.
    Recomputes stake_eur and stake_pct when upgraded.
    """
    lgbm_idx = _MODEL_IDX.get(signal.market)

    if lgbm_idx is None:
        # Non-1X2 market — BTTS, O/U, AH variants.
        # LightGBM predicts 1X2 classes, not these outcomes directly.
        # Use model_prob (DC-computed) with stricter threshold (≥10% DC-implied EV).
        if signal.confidence != "LOW" and signal.model_prob * signal.decimal_odds > 1.10:
            signal.confidence = "HIGH"
            new_eur = dynamic_stake_eur(signal.ev, "HIGH")
            bankroll = signal.stake_eur / signal.stake_pct if signal.stake_pct > 0 else 1000.0
            signal.stake_eur = new_eur
            signal.stake_pct = new_eur / bankroll
        return signal

    # 1X2 market — require both DC and LightGBM to agree.
    dc_p = dc_probs.get(f"p_{signal.market}", 0.0)
    lgbm_p = float(lgbm_probs[lgbm_idx])
    if signal.confidence != "LOW" and (dc_p * signal.decimal_odds > 1.0) and (lgbm_p * signal.decimal_odds > 1.0):
        signal.confidence = "HIGH"
        new_eur = dynamic_stake_eur(signal.ev, "HIGH")
        bankroll = signal.stake_eur / signal.stake_pct if signal.stake_pct > 0 else 1000.0
        signal.stake_eur = new_eur
        signal.stake_pct = new_eur / bankroll
    return signal
