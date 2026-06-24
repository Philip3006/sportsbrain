"""
Stake-v2 — Korrelations-aware Stake-Adjustment.

Wird einmal pro Scan (vor `append_bets()`) aufgerufen. Reduziert Stakes
wenn mehrere Signale auf dasselbe Match laufen und sich entweder direkt
widersprechen (Heim-Sieg + Gast-Spieler trifft) oder positiv korreliert
sind (Sieg + Over-Quote auf der gleichen Seite).

Regeln:
1. Negativ-Korrelation: Underdog-Leg × NEG_CORR_DISCOUNT, mit `stake_reason`
   markiert (Drop nur falls neuer Stake < MIN_STAKE_EUR).
2. Positiv-Korrelation: Sieg + Over/BTTS-Yes mit Modell p_over > 55% →
   beide Legs × POS_CORR_DISCOUNT.
3. Match-Exposure-Cap: Σ stake_eur ≤ tier_hi × MAX_MATCH_EXPOSURE_MULT;
   anderenfalls proportional skaliert.
"""
from __future__ import annotations

from collections import defaultdict

from src.betting.kelly import get_stake_bounds
from src.betting.value_detector import BetSignal
from src.config import (
    MAX_MATCH_EXPOSURE_MULT,
    MIN_STAKE_EUR,
    NEG_CORR_DISCOUNT,
    POS_CORR_DISCOUNT,
)


def _signal_side(s: BetSignal) -> str:
    """Klassifiziert ein Signal als 'home', 'away' oder 'neutral'."""
    m = s.market
    if m.startswith("scorer_"):
        # Scorer-Side stammt aus dem Goalscorer-Detector (BetSignal.player_team).
        return s.player_team or "neutral"
    if m == "home" or m == "dc_1x":
        return "home"
    if m == "away" or m == "dc_x2":
        return "away"
    # AH: ah-0.5_home favorisiert Heim, ah+0.5_home gibt Heim eine halbe Tor Vorsprung etc.
    if m.startswith("ah"):
        if m.endswith("_home"):
            try:
                line = float(m[2:].split("_")[0])
                return "home" if line <= 0 else "away"  # ah-x_home = Heim deckt, ah+x_home = Heim braucht Hilfe
            except ValueError:
                return "neutral"
        if m.endswith("_away"):
            try:
                line = float(m[2:].split("_")[0])
                return "away" if line <= 0 else "home"
            except ValueError:
                return "neutral"
    return "neutral"


def _is_over(market: str) -> bool:
    return market.startswith("o/u") and market.endswith("_over")


def apply_correlation_adjustments(signals: list[BetSignal], bankroll: float) -> list[BetSignal]:
    """Adjustiert Stakes über Korrelations-Regeln. Reihenfolge: Neg → Pos → Match-Cap.

    Mutiert die übergebenen Signale (Stake-Felder + stake_reason) und gibt die
    gleiche Liste zurück. Leere/Single-Signal-Matches passieren ungeprüft.
    """
    if not signals:
        return signals

    _, tier_hi = get_stake_bounds(bankroll)
    exposure_cap = tier_hi * MAX_MATCH_EXPOSURE_MULT

    by_match: dict[str, list[BetSignal]] = defaultdict(list)
    for s in signals:
        by_match[s.match_id].append(s)

    for match_id, group in by_match.items():
        if len(group) < 2:
            continue
        _apply_neg_correlation(group)
        _apply_pos_correlation(group)
        _apply_match_cap(group, exposure_cap, bankroll)

    # stake_pct nachziehen (falls bankroll > 0)
    if bankroll > 0:
        for s in signals:
            s.stake_pct = s.stake_eur / bankroll

    return signals


def _apply_neg_correlation(group: list[BetSignal]) -> None:
    """Wenn ein Match >=1 Home-Side- und >=1 Away-Side-Signal hat, wird das
    kleinste betroffene Leg jeder Seite × NEG_CORR_DISCOUNT — mit Marker."""
    home_legs = [s for s in group if _signal_side(s) == "home"]
    away_legs = [s for s in group if _signal_side(s) == "away"]
    if not home_legs or not away_legs:
        return

    home_total = sum(s.stake_eur for s in home_legs)
    away_total = sum(s.stake_eur for s in away_legs)

    # Underdog-Seite = die mit kleinerem Gesamt-Stake. Jedes Leg dort wird reduziert.
    if home_total < away_total:
        underdog = home_legs
        other_label = "away_signal"
    else:
        underdog = away_legs
        other_label = "home_signal"

    for s in underdog:
        new_stake = s.stake_eur * NEG_CORR_DISCOUNT
        # Nicht unter MIN — clamp statt droppen (User-Vorgabe: Leg behalten, markieren).
        new_stake = max(new_stake, MIN_STAKE_EUR if s.stake_eur >= MIN_STAKE_EUR else new_stake)
        s.stake_eur = round(new_stake, 2)
        if s.stake_reason:
            s.stake_reason += f"|neg_corr_vs_{other_label}"
        else:
            s.stake_reason = f"neg_corr_vs_{other_label}"


def _apply_pos_correlation(group: list[BetSignal]) -> None:
    """Sieg-Side-Leg + Over-Leg im selben Match mit Modell p_over > 0.55
    → beide × POS_CORR_DISCOUNT. Auch BTTS-Yes + Over.

    Modell-p_over wird vom Over-Signal selbst gelesen (model_prob)."""
    overs = [s for s in group if _is_over(s.market) and s.model_prob > 0.55]
    if not overs:
        return

    side_legs = [s for s in group if _signal_side(s) in ("home", "away")]
    btts_yes = [s for s in group if s.market == "btts_yes"]
    partners = side_legs + btts_yes
    if not partners:
        return

    affected = set()
    for over_sig in overs:
        for partner in partners:
            affected.add(id(over_sig))
            affected.add(id(partner))

    for s in group:
        if id(s) in affected:
            s.stake_eur = round(s.stake_eur * POS_CORR_DISCOUNT, 2)
            tag = "pos_corr_over"
            if s.stake_reason:
                if tag not in s.stake_reason:
                    s.stake_reason += f"|{tag}"
            else:
                s.stake_reason = tag


def _apply_match_cap(group: list[BetSignal], exposure_cap: float, bankroll: float) -> None:
    """Σ aller Stakes des Matches ≤ exposure_cap; sonst proportional skalieren."""
    total = sum(s.stake_eur for s in group)
    if total <= exposure_cap or total <= 0:
        return
    factor = exposure_cap / total
    for s in group:
        s.stake_eur = round(s.stake_eur * factor, 2)
        tag = "match_cap"
        if s.stake_reason:
            if tag not in s.stake_reason:
                s.stake_reason += f"|{tag}"
        else:
            s.stake_reason = tag
