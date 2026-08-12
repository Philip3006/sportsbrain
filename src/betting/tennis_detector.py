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

from src.betting.gates import gate_for
from src.betting.kelly import (
    apply_risk_cap,
    dynamic_stake_eur,
    expected_value,
    kelly_fraction,
)
from src.betting.value_detector import BetSignal
from src.config import MAX_EV, MIN_EDGE

_TENNIS_GATE = gate_for("tennis")


def _devig_2way(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Proportional devigging for a 2-outcome market (no draw)."""
    p_a = 1.0 / odds_a
    p_b = 1.0 / odds_b
    total = p_a + p_b
    return p_a / total, p_b / total


_MIN_PROB = _TENNIS_GATE.min_prob   # 0.35 — skip extreme underdogs
_MAX_ODDS = _TENNIS_GATE.max_odds   # 4.50 — skip extreme prices


def _signal(
    match_id: str,
    player_a: str,
    player_b: str,
    market: str,
    model_p: float,
    fair_p: float,
    odds: float,
    bankroll: float,
    min_edge: float = MIN_EDGE,
    min_prob: float = _MIN_PROB,
    max_odds: float = _MAX_ODDS,
) -> BetSignal | None:
    if model_p < min_prob:
        return None
    if odds > max_odds:
        return None
    ev = expected_value(model_p, odds)
    if ev < min_edge or ev > MAX_EV:
        return None
    kf = kelly_fraction(model_p, odds)
    theoretical_eur = dynamic_stake_eur(ev, "MEDIUM", bankroll, decimal_odds=odds)
    final_eur, cap_applied, placeable = apply_risk_cap(theoretical_eur, bankroll)
    sig = BetSignal(
        match_id=match_id or f"{player_a}_vs_{player_b}",
        home=player_a,
        away=player_b,
        market=market,
        model_prob=model_p,
        fair_prob=fair_p,
        decimal_odds=odds,
        ev=ev,
        kelly_f=kf,
        stake_pct=final_eur / bankroll if bankroll > 0 else 0.0,
        confidence="MEDIUM",
        stake_eur=final_eur,
        theoretical_stake_eur=theoretical_eur,
        cap_applied=cap_applied,
    )
    if not placeable:
        sig.no_bet_flag = True
        sig.stake_reason = "risk_cap_not_placeable"
    return sig


def _p_match_from_p_set_bo5(p_s: float) -> float:
    """P(player_a wins best-of-5 match) given per-set win prob p_s."""
    q = 1.0 - p_s
    return p_s**3 * (1.0 + 3.0 * q + 6.0 * q**2)


def _p_match_from_p_set_bo3(p_s: float) -> float:
    """P(player_a wins best-of-3 match) given per-set win prob p_s."""
    return p_s**2 * (3.0 - 2.0 * p_s)


# Keep backward-compatible alias pointing to BO5 (ATP default)
_p_match_from_p_set = _p_match_from_p_set_bo5


def _p_set_from_p_match(p_match: float, bo5: bool = True) -> float:
    """
    Numerical inversion via binary search (50 iterations).
    bo5=True for ATP (best-of-5), bo5=False for WTA (best-of-3).
    """
    forward = _p_match_from_p_set_bo5 if bo5 else _p_match_from_p_set_bo3
    lo, hi = 1e-6, 1.0 - 1e-6
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if forward(mid) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _set_handicap_probs(p_a_wins: float, bo5: bool = True) -> dict[str, float]:
    """
    Approximates set handicap probabilities from match-win probability.

    bo5=True  → ATP (best-of-5):  ah-1.5_a = A wins 3:0 or 3:1
    bo5=False → WTA (best-of-3):  ah-1.5_a = A wins 2:0 (only way to win by ≥2 sets)

    ah+1.5_b = complement (B wins OR close match).
    """
    if p_a_wins <= 0 or p_a_wins >= 1:
        return {"ah-1.5_a": 0.0, "ah+1.5_b": 0.0}

    p_set = _p_set_from_p_match(p_a_wins, bo5=bo5)

    if bo5:
        q = 1.0 - p_set
        p_3_0 = p_set ** 3
        p_3_1 = 3.0 * p_set**3 * q
        p_a_dominant = p_3_0 + p_3_1  # 3:0 or 3:1
    else:
        # BO3: only 2:0 counts as ah-1.5_a (win by 2 sets net)
        p_a_dominant = p_set ** 2  # P(A wins first 2 sets straight)

    return {
        "ah-1.5_a": max(0.0, min(1.0, p_a_dominant)),
        "ah+1.5_b": max(0.0, min(1.0, 1.0 - p_a_dominant)),
    }


def _first_set_probs(p_a_wins: float, bo5: bool = True) -> dict[str, float]:
    """P(player_a wins first set) — uses per-set probability as proxy for first-set win.
    bo5/bo3 affects per-set inversion; first-set probability itself is p_set regardless of format."""
    if p_a_wins <= 0 or p_a_wins >= 1:
        return {"first_set_a": 0.5, "first_set_b": 0.5}
    p_set = _p_set_from_p_match(p_a_wins, bo5=bo5)
    return {"first_set_a": p_set, "first_set_b": 1.0 - p_set}


def _p_hold_from_serve_stats(stats) -> float:
    """P(win service game) from ServeAggregate serve stats.

    Uses: first_serve_pct × first_serve_win_pct + (1 - first_serve_pct) × second_serve_win_pct
    Falls back to dominance_rate if individual stats not meaningful.
    """
    if stats is None:
        return 0.0
    try:
        fs_in = float(stats.first_serve_pct or 0)
        fs_win = float(stats.first_serve_win_pct or 0)
        ss_win = float(stats.second_serve_win_pct or 0)
        if fs_in > 0.3 and fs_win > 0.3:
            return max(0.4, min(0.95, fs_in * fs_win + (1 - fs_in) * ss_win))
    except Exception:
        pass
    # fallback: dominance_rate ≈ point win%, convert to game hold via p^4/(p^4+(1-p)^4)
    try:
        p = max(0.4, min(0.8, float(stats.dominance_rate or 0.5)))
        p4 = p ** 4
        return p4 / (p4 + (1 - p) ** 4)
    except Exception:
        return 0.0


def _first_set_probs_serve_based(p_hold_a: float, p_hold_b: float) -> dict[str, float]:
    """First-set probability from per-game hold probs.

    Uses composite per-game advantage:
      p_comp = (p_hold_a + (1 - p_hold_b)) / 2  ← A's avg win prob per game
    Then: P(A wins set) ≈ p_comp^6 / (p_comp^6 + (1 - p_comp)^6)
    Gives realistic range ~[0.50, 0.85] for plausible serve advantages.
    """
    p_comp = max(0.15, min(0.85, (p_hold_a + (1.0 - p_hold_b)) / 2.0))
    p6 = p_comp ** 6
    q6 = (1.0 - p_comp) ** 6
    p_a = p6 / (p6 + q6)
    return {"first_set_a": max(0.10, min(0.90, p_a)),
            "first_set_b": max(0.10, min(0.90, 1.0 - p_a))}


_HIGH_EV_ATP  = 0.15  # ATP: upgrade to HIGH only at very strong edge (backtest -5.9%)
_HIGH_EV_WTA  = 0.08  # WTA grass has +10.3% ROI: smaller edge required for HIGH


def _confidence_for(ev: float, tour: str) -> str:
    threshold = _HIGH_EV_WTA if tour.lower() == "wta" else _HIGH_EV_ATP
    return "HIGH" if ev >= threshold else "MEDIUM"


# ---------------------------------------------------------------------------
# Phase J2-C: Total Sets + Total Games + Set Betting
# ---------------------------------------------------------------------------

def detect_total_sets(
    player_a: str,
    player_b: str,
    p_match_a: float,
    odds_over: float,
    odds_under: float,
    line: float,
    best_of: int,
    bankroll: float,
    match_id: str = "",
    min_edge: float = MIN_EDGE,
    tour: str = "atp",
) -> list[BetSignal]:
    """O/U Total Sets (z.B. line=2.5 für BO3, 3.5 für BO5)."""
    from src.tennis.sim import p_total_sets_over

    bo5 = best_of == 5
    if p_match_a <= 0 or p_match_a >= 1 or odds_over <= 1 or odds_under <= 1:
        return []
    p_set = _p_set_from_p_match(p_match_a, bo5=bo5)
    p_over = p_total_sets_over(p_set, best_of, line)
    p_under = 1.0 - p_over
    fair_over, fair_under = _devig_2way(odds_over, odds_under)

    out: list[BetSignal] = []
    sig = _signal(match_id, player_a, player_b, f"o/u_sets_{line}_over",
                  p_over, fair_over, odds_over, bankroll, min_edge,
                  min_prob=0.10, max_odds=10.0)
    if sig:
        out.append(sig)
    sig = _signal(match_id, player_a, player_b, f"o/u_sets_{line}_under",
                  p_under, fair_under, odds_under, bankroll, min_edge,
                  min_prob=0.10, max_odds=10.0)
    if sig:
        out.append(sig)
    for s in out:
        s.league = tour
    return out


def detect_total_games(
    player_a: str,
    player_b: str,
    p_match_a: float,
    odds_over: float,
    odds_under: float,
    line: float,
    best_of: int,
    bankroll: float,
    match_id: str = "",
    min_edge: float = MIN_EDGE,
    tour: str = "atp",
    n_sim: int = 2000,
    serve_stats_a=None,
    serve_stats_b=None,
) -> list[BetSignal]:
    """O/U Total Games. Monte-Carlo via src.tennis.sim.simulate_match.

    serve_stats_a / serve_stats_b: ServeAggregate (optional). When both players
    have ≥10 matches, player-specific hold rates replace the tour-average baseline.
    """
    from src.tennis.sim import simulate_match, p_total_games_over

    bo5 = best_of == 5
    if p_match_a <= 0 or p_match_a >= 1 or odds_over <= 1 or odds_under <= 1:
        return []
    p_set = _p_set_from_p_match(p_match_a, bo5=bo5)

    hold_a: float | None = None
    hold_b: float | None = None
    if (serve_stats_a is not None and serve_stats_b is not None
            and getattr(serve_stats_a, "n_matches", 0) >= 10
            and getattr(serve_stats_b, "n_matches", 0) >= 10):
        ha = _p_hold_from_serve_stats(serve_stats_a)
        hb = _p_hold_from_serve_stats(serve_stats_b)
        if ha > 0.3 and hb > 0.3:
            hold_a, hold_b = ha, hb

    sim = simulate_match(p_set, best_of, tour, n_sim=n_sim, seed=42, hold_a=hold_a, hold_b=hold_b)
    p_over = p_total_games_over(sim, line)
    p_under = 1.0 - p_over
    fair_over, fair_under = _devig_2way(odds_over, odds_under)

    out: list[BetSignal] = []
    sig = _signal(match_id, player_a, player_b, f"o/u_games_{line}_over",
                  p_over, fair_over, odds_over, bankroll, min_edge,
                  min_prob=0.10, max_odds=10.0)
    if sig:
        out.append(sig)
    sig = _signal(match_id, player_a, player_b, f"o/u_games_{line}_under",
                  p_under, fair_under, odds_under, bankroll, min_edge,
                  min_prob=0.10, max_odds=10.0)
    if sig:
        out.append(sig)
    for s in out:
        s.league = tour
    return out


def detect_set_betting(
    player_a: str,
    player_b: str,
    p_match_a: float,
    scoreline_odds: dict[str, float],
    best_of: int,
    bankroll: float,
    match_id: str = "",
    min_edge: float = MIN_EDGE,
    tour: str = "atp",
) -> list[BetSignal]:
    """Exakte Set-Endergebnisse (Correct-Score-Stil).

    scoreline_odds: {"2-0": 1.85, "2-1": 3.40, ...} — nur Lines mit Quote werden geprüft.
    """
    from src.tennis.sim import set_score_probs

    bo5 = best_of == 5
    if p_match_a <= 0 or p_match_a >= 1:
        return []
    p_set = _p_set_from_p_match(p_match_a, bo5=bo5)
    probs = set_score_probs(p_set, best_of)

    # fair_prob aus de-vig der gegebenen Quoten (sum aller invers)
    if not scoreline_odds:
        return []
    inv_total = sum(1.0 / o for o in scoreline_odds.values() if o > 1.0)
    out: list[BetSignal] = []
    for score, odds in scoreline_odds.items():
        if odds <= 1.0 or score not in probs:
            continue
        fair_p = (1.0 / odds) / inv_total if inv_total > 0 else 1.0 / odds
        sig = _signal(match_id, player_a, player_b, f"score_{score}",
                      probs[score], fair_p, odds, bankroll, min_edge,
                      min_prob=0.02, max_odds=30.0)
        if sig:
            out.append(sig)
    for s in out:
        s.league = tour
    return out


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
    first_set_odds_a: float = 0.0,
    first_set_odds_b: float = 0.0,
    min_edge: float = MIN_EDGE,
    tour: str = "atp",
    first_set_source_tier: int = 5,
    serve_stats_a=None,
    serve_stats_b=None,
) -> list[BetSignal]:
    """
    Detects value in tennis match markets.

    probs: {'p_a': float, 'p_b': float} from predict_winner()
    odds_a / odds_b: decimal odds for player_a / player_b match winner
    ah_odds_a / ah_odds_b: decimal odds for set handicap -1.5/+1.5 (0 = not available)
    min_edge: edge floor — raise for ATP (backtest -5.9%) vs WTA (+10.3%).
    tour: "atp"|"wta" — affects HIGH confidence threshold and stake sizing.

    Returns list of BetSignal (empty if no value found).
    Two-bucket output: at most 1 directional (match/first-set) + 1 structural (set AH).
    """
    p_a = probs.get("p_a", 0.0)
    p_b = probs.get("p_b", 0.0)
    bo5 = tour.lower() != "wta"  # WTA plays best-of-3; ATP plays best-of-5

    directional: list[BetSignal] = []
    structural: list[BetSignal] = []

    # Bucket A — directional (match winner + first set: correlated with outcome)
    if odds_a > 1.0 and odds_b > 1.0:
        fair_a, fair_b = _devig_2way(odds_a, odds_b)
        sig = _signal(match_id, player_a, player_b, "home", p_a, fair_a, odds_a, bankroll, min_edge)
        if sig:
            directional.append(sig)
        sig = _signal(match_id, player_a, player_b, "away", p_b, fair_b, odds_b, bankroll, min_edge)
        if sig:
            directional.append(sig)

    # Phase 4c: First-Set-Markt — nur bei Tier-1/2-Quoten (kein Implied/WebSearch).
    if first_set_odds_a > 1.0 and first_set_odds_b > 1.0 and first_set_source_tier <= 2:
        # Use serve-stats model when both players have sufficient data (n ≥ 10)
        p_hold_a = _p_hold_from_serve_stats(serve_stats_a)
        p_hold_b = _p_hold_from_serve_stats(serve_stats_b)
        if p_hold_a > 0.3 and p_hold_b > 0.3 \
                and getattr(serve_stats_a, "n_matches", 0) >= 10 \
                and getattr(serve_stats_b, "n_matches", 0) >= 10:
            fs_probs = _first_set_probs_serve_based(p_hold_a, p_hold_b)
        else:
            fs_probs = _first_set_probs(p_a, bo5=bo5)
        fair_fs_a, fair_fs_b = _devig_2way(first_set_odds_a, first_set_odds_b)
        sig = _signal(match_id, player_a, player_b, "first_set_a",
                      fs_probs["first_set_a"], fair_fs_a, first_set_odds_a, bankroll, min_edge)
        if sig:
            directional.append(sig)
        sig = _signal(match_id, player_a, player_b, "first_set_b",
                      fs_probs["first_set_b"], fair_fs_b, first_set_odds_b, bankroll, min_edge)
        if sig:
            directional.append(sig)

    # Bucket B — structural (set AH: margin-of-victory, less correlated with result)
    if ah_odds_a > 1.0 and ah_odds_b > 1.0:
        ah_probs = _set_handicap_probs(p_a, bo5=bo5)
        fair_ah_a, fair_ah_b = _devig_2way(ah_odds_a, ah_odds_b)
        sig = _signal(match_id, player_a, player_b, "ah-1.5_a",
                      ah_probs["ah-1.5_a"], fair_ah_a, ah_odds_a, bankroll, min_edge)
        if sig:
            structural.append(sig)
        sig = _signal(match_id, player_a, player_b, "ah+1.5_b",
                      ah_probs["ah+1.5_b"], fair_ah_b, ah_odds_b, bankroll, min_edge)
        if sig:
            structural.append(sig)

    # Per-match: best directional + best structural (prevent correlated overexposure)
    selected: list[BetSignal] = []
    if directional:
        selected.append(max(directional, key=lambda s: s.ev))
    if structural:
        selected.append(max(structural, key=lambda s: s.ev))

    # Tour-aware confidence upgrade: WTA has stronger backtest edge → lower HIGH bar
    for s in selected:
        s.confidence = _confidence_for(s.ev, tour)
        if s.confidence == "HIGH":
            theoretical_eur = dynamic_stake_eur(s.ev, "HIGH", bankroll, decimal_odds=s.decimal_odds)
            final_eur, cap_applied, placeable = apply_risk_cap(theoretical_eur, bankroll)
            s.stake_eur = final_eur
            s.stake_pct = final_eur / bankroll if bankroll > 0 else 0.0
            s.theoretical_stake_eur = theoretical_eur
            s.cap_applied = cap_applied
            if not placeable:
                s.no_bet_flag = True
                s.stake_reason = "risk_cap_not_placeable"
        s.league = tour  # O1-4: tennis signals always carry tour ("atp"/"wta") as league

    return selected
