from dataclasses import dataclass, field

import numpy as np

from src.betting.kelly import dynamic_stake_eur, expected_value, kelly_fraction
from src.betting.odds_utils import remove_margin_shin
from src.config import MIN_EDGE, MAX_STAKE_EUR, GOALS_RANGE_MAX_STAKE


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


# EVs ≥ this are virtually always Qualifier-bias artefacts in the current
# data (see docs/audit_2026-06-12.md, sections A & H — Algeria 37 %,
# Côte d'Ivoire 38 %, USA-Mexico o/u3.5_under 37 %). Apply only to football;
# tennis has no analogous bias.
_BIAS_EV_CAP = 0.30


def _bias_safety_confidence(base_confidence: str, ev: float) -> str:
    """Downgrade implausibly-high EV signals to LOW.

    Final safety net after _consistency_confidence — even if both models
    agree and the market gate passes, EVs around 30 % or more are in the
    current dataset overwhelmingly Qualifier-training artefacts rather
    than real edges.
    """
    if ev >= _BIAS_EV_CAP and base_confidence != "LOW":
        return "LOW"
    return base_confidence


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
        # Even without DC consistency check, market-disagreement gate applies.
        if market_disagreement_low(ensemble_p, fair_p):
            return "LOW"
        return base_confidence
    ensemble_above = ensemble_p > fair_p
    dc_above = dc_p > fair_p
    if ensemble_above != dc_above:
        return "LOW"
    # Lever 6 gate: model significantly diverges from market → high overconfidence risk.
    if market_disagreement_low(ensemble_p, fair_p) or market_disagreement_low(dc_p, fair_p):
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
        confidence = _bias_safety_confidence(confidence, ev)
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
    # Note: quarter-ball lines (e.g. -1.25) go through detect_value_ah_quarter, not this function

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
        confidence = _bias_safety_confidence(confidence, ev)
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
    totals_probs: {p_over, p_under, p_push, line} from dc.predict_totals()

    Handles push (whole-ball lines like 2.0, 3.0): EV = p_over*(o-1) + p_push*0 - p_under.
    Quarter-ball lines: use detect_value_totals_quarter instead.

    min_edge_under: overrides min_edge for UNDER side only.
    dc_probs: optional dict with keys p_over/p_under (DC totals output).
    """
    signals = []
    line = totals_probs.get("line", 2.5)
    p_push = totals_probs.get("p_push", 0.0)
    p_over = totals_probs["p_over"]
    p_under = totals_probs["p_under"]

    for side, model_p, odds, dc_key in [
        ("over", p_over, over_odds, "p_over"),
        ("under", p_under, under_odds, "p_under"),
    ]:
        if odds <= 1.0:
            continue
        effective_min = min_edge_under if (side == "under" and min_edge_under is not None) else min_edge

        if p_push > 0:
            # Whole-ball push-aware EV: p_over*(o-1) + p_push*0 - p_under
            p_lose = max(0.0, 1.0 - model_p - p_push)
            ev = model_p * (odds - 1) - p_lose
            p_eff = model_p / max(model_p + p_lose, 1e-10)
            kf = kelly_fraction(p_eff, odds)
        else:
            ev = expected_value(model_p, odds)
            kf = kelly_fraction(model_p, odds)

        if ev < effective_min - 1e-9:
            continue
        dc_p = dc_probs.get(dc_key) if dc_probs else None
        confidence = _consistency_confidence(model_p, 0.5, dc_p, "MEDIUM")
        confidence = _bias_safety_confidence(confidence, ev)
        signals.append(_make_signal(
            match_id, home, away, f"o/u{line}_{side}",
            model_p, model_p, odds, ev, kf, confidence, bankroll,
        ))
    return signals


def detect_value_totals_quarter(
    home: str,
    away: str,
    quarter_probs: dict,
    over_odds: float,
    under_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    match_id: str = "",
    min_edge_under: float | None = None,
    dc_probs: dict | None = None,
) -> list[BetSignal]:
    """Quarter-ball O/U (e.g., 2.25, 2.75): computes EV from two adjacent legs.
    quarter_probs: from dc.predict_totals_all() with quarter_ball=True.

    For Over 2.25: split O/U 2.0 (push at 2) + O/U 2.5 (no push).
    - total ≥ 3: Full WIN for Over
    - total = 2: O/U 2.0 pushes (Half LOSS for Over)
    - total ≤ 1: Full LOSS for Over
    (Symmetric half WIN for Under at total=2)
    """
    line = quarter_probs.get("line", 0)
    p_lower = quarter_probs["lower_probs"]
    p_push_lower = p_lower.get("p_push", 0.0)  # push from whole-ball leg

    # P(A) = full win for Over = P(total > upper line) = p_lower["p_over"] since both lines agree
    P_A = p_lower["p_over"]        # full win Over = both legs over
    P_B = p_push_lower             # push on whole-ball leg = half loss Over / half win Under
    P_C = p_lower["p_under"]       # full loss Over = both legs under... but wait:

    # Actually for O/U 2.25 (lower=2.0, upper=2.5):
    # P_A = P(total >= 3) = p_lower["p_over"] [≡ p_upper["p_over"]]
    # P_B = P(total = 2) = p_lower["p_push"] [only lower has push]
    # P_C = P(total <= 1) = p_lower["p_under"] [= 1 - P_A - P_B]
    # Verify: P_A + P_B + P_C = p_over + p_push + p_under = 1 ✓

    signals = []

    if over_odds > 1.0:
        ev_over = P_A * (over_odds - 1) - P_B * 0.5 - P_C
        if ev_over >= min_edge - 1e-9:
            # effective prob for Kelly: p_eff such that EV = p_eff*(o-1) - (1-p_eff)
            # p_eff = (EV + 1) / over_odds
            p_eff = (ev_over + 1) / over_odds if over_odds > 1 else P_A
            kf = kelly_fraction(min(p_eff, 0.99), over_odds)
            model_p_over = P_A + 0.5 * P_B
            dc_p_over = dc_probs.get("p_over") if dc_probs else None
            confidence = _consistency_confidence(model_p_over, 0.5, dc_p_over, "MEDIUM")
            confidence = _bias_safety_confidence(confidence, ev_over)
            signals.append(_make_signal(
                match_id, home, away, f"o/u{line}_over",
                model_p_over, model_p_over, over_odds, ev_over, kf, confidence, bankroll,
            ))

    effective_min_under = min_edge_under if min_edge_under is not None else min_edge
    if under_odds > 1.0:
        ev_under = P_C * (under_odds - 1) + P_B * 0.5 * (under_odds - 1) - P_A
        if ev_under >= effective_min_under - 1e-9:
            p_eff = (ev_under + 1) / under_odds if under_odds > 1 else P_C
            kf = kelly_fraction(min(p_eff, 0.99), under_odds)
            model_p_under = P_C + 0.5 * P_B
            dc_p_under = dc_probs.get("p_under") if dc_probs else None
            confidence = _consistency_confidence(model_p_under, 0.5, dc_p_under, "MEDIUM")
            confidence = _bias_safety_confidence(confidence, ev_under)
            signals.append(_make_signal(
                match_id, home, away, f"o/u{line}_under",
                model_p_under, model_p_under, under_odds, ev_under, kf, confidence, bankroll,
            ))

    return signals


def detect_value_ah_quarter(
    home: str,
    away: str,
    quarter_probs: dict,
    ah_home_odds: float,
    ah_away_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    match_id: str = "",
    line: float = -1.25,
    dc_probs: dict | None = None,
) -> list[BetSignal]:
    """Quarter-ball AH (e.g., -1.25, -0.75): computes EV from two adjacent legs.
    quarter_probs: from dc.predict_asian_handicap_all() with quarter_ball=True.

    For -1.25 HOME (lower=-1.0, upper=-1.5):
    - diff ≥ 2: Full WIN for Home
    - diff = 1: Half LOSS for Home (lower pushes, upper loses) / Half WIN for Away
    - diff ≤ 0: Full LOSS for Home / Full WIN for Away
    """
    p_lower = quarter_probs["lower_probs"]
    # The whole-ball leg is the one with a push; get the push probability
    p_push_lower = p_lower.get("p_push", 0.0)
    p_push_upper = quarter_probs["upper_probs"].get("p_push", 0.0)

    # For standard quarter-ball: exactly one leg has a push
    # P_A = P(full win home) = same from both legs (lower.p_ah_home == upper.p_ah_home for -1.25)
    P_A = p_lower["p_ah_home"]   # P(diff >= required for full win home)
    P_B = p_push_lower + p_push_upper  # push from whole-ball leg
    P_C = p_lower["p_ah_away"]   # P(full loss home / full win away for the less-aggressive leg)

    # For -1.25: P_C = P(diff <= 0) [full loss for home]
    # For -0.75: lower=-0.5 (no push), upper=-1.0 (push at diff=1)
    #   P_A = P(diff >= 1) from -0.5 leg, P(diff >= 2) from -1.0 leg
    #   This doesn't hold that P_A is the same for both legs!
    # Need to handle -0.75 carefully: the "full win" is when BOTH legs win.
    # For -0.75 HOME:
    #   lower=-0.5: win if diff >= 1
    #   upper=-1.0: win if diff >= 2, push if diff=1
    # Combined HOME:
    #   diff >= 2: both win → Full WIN
    #   diff = 1: lower wins, upper pushes → Half WIN for home! (not half loss)
    #   diff <= 0: both lose → Full LOSS
    #
    # So the "pivot" direction depends on which leg is more aggressive.
    # For -1.25: whole line is LESS aggressive (lower) → pivot = half LOSS for home
    # For -0.75: whole line is MORE aggressive (upper) → pivot = half WIN for home

    # Determine pivot direction: if lower leg has push, pivot is half-loss for home
    whole_is_lower = p_push_lower > 0

    _SUPPORTED = {-0.5, -1.0, -1.5, -2.0, -2.5, 0.5, 1.0, 1.5, 2.0, 2.5}

    signals = []
    home_label = f"ah{line:+.2f}_home"
    away_label = f"ah{line:+.2f}_away"

    if ah_home_odds > 1.0:
        if whole_is_lower:
            # -1.25 type: pivot = half LOSS for home
            # P_A = P(full win home), P_B = P(half loss home), P_C = 1-P_A-P_B
            ev_home = P_A * (ah_home_odds - 1) - P_B * 0.5 - P_C
        else:
            # -0.75 type: pivot = half WIN for home
            # For -0.75: P_A=P(diff>=2) from -1.0 leg, P_B=push from -1.0 (diff=1)
            # BUT: P_lower["p_ah_home"] for lower=-0.5 is P(diff>=1), not P(diff>=2)
            # So P_A here needs to be P(full win both legs) = P(diff>=2) = p_upper["p_ah_home"]
            P_A_actual = quarter_probs["upper_probs"]["p_ah_home"]  # more aggressive leg's win
            P_B_actual = p_push_upper
            P_C_actual = 1 - P_A_actual - P_B_actual
            ev_home = P_A_actual * (ah_home_odds - 1) + P_B_actual * 0.5 * (ah_home_odds - 1) - P_C_actual
            P_A, P_B, P_C = P_A_actual, P_B_actual, P_C_actual

        if ev_home >= min_edge - 1e-9:
            p_eff = (ev_home + 1) / ah_home_odds if ah_home_odds > 1 else 0
            kf = kelly_fraction(min(p_eff, 0.99), ah_home_odds)
            dc_p_home = dc_probs.get("p_ah_home") if dc_probs else None
            confidence = _consistency_confidence(P_A, 0.5, dc_p_home, "MEDIUM")
            confidence = _bias_safety_confidence(confidence, ev_home)
            signals.append(_make_signal(
                match_id, home, away, home_label,
                P_A, P_A, ah_home_odds, ev_home, kf, confidence, bankroll,
            ))

    if ah_away_odds > 1.0:
        if whole_is_lower:
            # -1.25 type: pivot = half WIN for away
            ev_away = P_C * (ah_away_odds - 1) + P_B * 0.5 * (ah_away_odds - 1) - P_A
        else:
            # -0.75 type: pivot = half LOSS for away
            P_A_away = quarter_probs["upper_probs"]["p_ah_away"]
            P_B_away = p_push_upper
            P_C_away = 1 - P_A_away - P_B_away
            ev_away = P_A_away * (ah_away_odds - 1) - P_B_away * 0.5 - P_C_away

        if ev_away >= min_edge - 1e-9:
            p_eff = (ev_away + 1) / ah_away_odds if ah_away_odds > 1 else 0
            kf = kelly_fraction(min(p_eff, 0.99), ah_away_odds)
            dc_p_away = dc_probs.get("p_ah_away") if dc_probs else None
            confidence = _consistency_confidence(P_C, 0.5, dc_p_away, "MEDIUM")
            confidence = _bias_safety_confidence(confidence, ev_away)
            signals.append(_make_signal(
                match_id, home, away, away_label,
                P_C, P_C, ah_away_odds, ev_away, kf, confidence, bankroll,
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
        confidence = _bias_safety_confidence(confidence, ev)
        signals.append(_make_signal(
            match_id, home, away, side,
            model_p, fair_p, odds, ev, kf, confidence, bankroll,
        ))
    return signals


def detect_value_double_chance(
    home: str,
    away: str,
    p_home: float,
    p_draw: float,
    p_away: float,
    dc_1x_odds: float,
    dc_x2_odds: float,
    dc_both_odds: float,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    match_id: str = "",
) -> list[BetSignal]:
    """
    Checks Double Chance market (1X, X2, 12) for positive EV.
    p_home/p_draw/p_away: final ensemble probabilities.
    dc_1x_odds: Home or Draw; dc_x2_odds: Draw or Away; dc_both_odds: Home or Away.
    """
    # DC outcomes overlap (1X and X2 share draw), so standard proportional margin
    # removal doesn't apply. Use model probabilities directly as fair baseline.
    signals = []
    for market, model_p, odds, fair_p in [
        ("dc_1x", p_home + p_draw, dc_1x_odds, p_home + p_draw),
        ("dc_x2", p_draw + p_away, dc_x2_odds, p_draw + p_away),
        ("dc_12", p_home + p_away, dc_both_odds, p_home + p_away),
    ]:
        if odds <= 1.0:
            continue
        ev = expected_value(model_p, odds)
        if ev < min_edge - 1e-9:
            continue
        kf = kelly_fraction(model_p, odds)
        signals.append(_make_signal(
            match_id, home, away, market,
            model_p, fair_p, odds, ev, kf, "MEDIUM", bankroll,
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


def detect_value_goals_range(
    home: str,
    away: str,
    p_model: float,
    implied_p: float,
    market: str,
    bankroll: float = 1000.0,
    min_edge: float = MIN_EDGE,
    match_id: str = "",
) -> list[BetSignal]:
    """EV check for Tore-Bereich markets (goals_2_4, h1_goals_2_4, h2_goals_2_4).

    Checks both JA (2-4 goals) and NEIN (not 2-4 goals) sides.
    implied_p = market-derived P(2-4); fair odds derived synthetically from O/U lines.
    H1-JA blocked (WM 2018+2022 backtest: structurally negative).
    Stake capped at GOALS_RANGE_MAX_STAKE during initial data-collection phase.
    """
    if implied_p <= 0.0 or implied_p >= 1.0:
        return []
    fair_ja_odds   = 1.0 / implied_p
    fair_nein_odds = 1.0 / (1.0 - implied_p)
    signals = []
    for side_market, p_side, fair_odds in [
        (market,          p_model,       fair_ja_odds),
        (market + "_no",  1.0 - p_model, fair_nein_odds),
    ]:
        # H1-JA blocked: model systematically overestimates H1 scoring
        if market == "h1_goals_2_4" and p_side == p_model:
            continue
        ev = expected_value(p_side, fair_odds)
        if ev < min_edge - 1e-9:
            continue
        kf = kelly_fraction(p_side, fair_odds)
        confidence = _bias_safety_confidence("MEDIUM", ev)
        # Use the base market key for both sides (JA/NEIN encoded in model_prob direction)
        sig = _make_signal(
            match_id, home, away, side_market,
            p_side, 1.0 - implied_p if "no" in side_market else implied_p,
            fair_odds, ev, kf, confidence, bankroll,
        )
        sig.stake_eur = min(sig.stake_eur, GOALS_RANGE_MAX_STAKE)
        sig.stake_pct = sig.stake_eur / bankroll if bankroll > 0 else 0.0
        signals.append(sig)
    return signals


_MARKET_DISAGREEMENT_THRESHOLD = 0.10  # 10pp Modell-vs-Markt → LOW
# Lever 6: empirisch (scripts/fit_closing_anchor.py) liefert Closing-Line Brier 0.574
# vs Modell 0.626 → unser Modell ist deutlich schlechter als der Markt. Bei großer
# Abweichung ist Overconfidence das Default-Risiko, nicht Edge.


def market_disagreement_low(model_prob: float, market_implied_prob: float,
                            threshold: float = _MARKET_DISAGREEMENT_THRESHOLD) -> bool:
    """Returns True if model deviates from market by more than threshold (pp)."""
    return abs(float(model_prob) - float(market_implied_prob)) > threshold


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
