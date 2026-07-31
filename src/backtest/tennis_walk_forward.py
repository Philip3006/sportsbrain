"""Tennis Walk-forward Backtest + CLV (Roadmap J2-L).

Rollierendes Zeitfenster:
  - Train:  N Jahre vor `val_start`
  - Val:    [val_start, val_end)
  - Repeat: verschiebe Fenster um `step_months`, bis Ende der Daten

Für jedes Val-Fenster: LGBM neu trainieren, auf Val-Matches evaluieren.
CLV: für jede simulierte "value bet" (edge ≥ min_edge) vergleiche
Trainingszeit-Odd (AvgW/L) mit Closing-Odd (B365W/L, historisch).

Output-Report: results/audits/tennis_walk_forward_<date>.md
  - Roll-out-Chunks mit n/ROI/Brier
  - Per-Surface + Per-Category-Split
  - CLV-Histogramm (mean/median/std)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from src.models import tennis_lgbm as tlgbm
from src.models.tennis_elo import TennisEloRatings
from src.tennis.features import FEATURE_COLUMNS, RollingState, build_match_features


_MIN_RANK = 1500.0
_DEFAULT_MIN_EDGE = 0.05  # 5% edge required (tennis is high-volume)


@dataclass
class WalkForwardResult:
    """Ergebnis pro Chunk."""
    val_start: pd.Timestamp
    val_end: pd.Timestamp
    n_train: int
    n_val: int
    lgbm_brier: float
    elo_brier: float
    lgbm_bets: int
    lgbm_roi_pct: float
    elo_bets: int
    elo_roi_pct: float
    mean_clv_lgbm: float
    per_surface: dict = field(default_factory=dict)


def _elo_p(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


def _implied(odds: float) -> float:
    return 1.0 / odds if odds and odds > 1.0 else 0.5


def _kelly(p: float, o: float, fractional: float = 0.25) -> float:
    """Fractional Kelly stake fraction."""
    q = 1 - p
    b = o - 1
    if b <= 0:
        return 0.0
    f = (p * b - q) / b
    return max(0.0, min(0.05, f * fractional))  # 5% cap


def _build_full_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """Full walk-forward feature-build with Elo update. Row per match (side=Winner
    for label=1, but stores both Winner/Loser odds for CLV computation)."""
    df = df.sort_values("Date").reset_index(drop=True).copy()
    state = RollingState(window=10)
    elo = TennisEloRatings()
    rng = random.Random(42)

    rows: list[dict] = []
    for _, m in df.iterrows():
        winner = m["Winner"]; loser = m["Loser"]
        surface = str(m.get("surface_std") or "hard")
        _bo = m.get("Best of")
        best_of = int(_bo) if pd.notna(_bo) else 3
        category = str(m.get("category") or "atp250")
        round_str = str(m.get("Round") or "")
        rw = m.get("WRank"); rl = m.get("LRank")
        rank_w = float(rw) if pd.notna(rw) and rw else _MIN_RANK
        rank_l = float(rl) if pd.notna(rl) and rl else _MIN_RANK
        date = m["Date"]

        elo_w_over = elo.get_overall(winner); elo_l_over = elo.get_overall(loser)
        elo_w_surf = elo.get_blended(winner, surface); elo_l_surf = elo.get_blended(loser, surface)

        # Random swap
        if rng.random() < 0.5:
            pa, pb, ra, rb, ea, eb, eas, ebs, y = winner, loser, rank_w, rank_l, elo_w_over, elo_l_over, elo_w_surf, elo_l_surf, 1
            # MaxW/L = best-price across bookmakers (analog Scanner-Live). Realistischer
            # als AvgW/L (Marktdurchschnitt). B365W/L als CLV-Referenz (closing/pre-tournament).
            odds_a = m.get("MaxW") or m.get("AvgW"); odds_b = m.get("MaxL") or m.get("AvgL")
            close_a = m.get("B365W"); close_b = m.get("B365L")
        else:
            pa, pb, ra, rb, ea, eb, eas, ebs, y = loser, winner, rank_l, rank_w, elo_l_over, elo_w_over, elo_l_surf, elo_w_surf, 0
            odds_a = m.get("MaxL") or m.get("AvgL"); odds_b = m.get("MaxW") or m.get("AvgW")
            close_a = m.get("B365L"); close_b = m.get("B365W")

        feats = build_match_features(
            player_a=pa, player_b=pb, surface=surface, best_of=best_of,
            category=category, round_str=round_str,
            rank_a=ra, rank_b=rb, elo_a=ea, elo_b=eb,
            elo_surface_a=eas, elo_surface_b=ebs, state=state, date=date,
        )
        def _safe_odds(v):
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return np.nan
            try:
                f = float(v)
                return f if 1.01 < f < 100 else np.nan
            except (ValueError, TypeError):
                return np.nan
        row = {
            "date": date, "y": y, "elo_p_a": _elo_p(eas, ebs),
            "surface": surface, "category": category,
            "odds_a": _safe_odds(odds_a), "odds_b": _safe_odds(odds_b),
            "close_a": _safe_odds(close_a), "close_b": _safe_odds(close_b),
        }
        row.update(feats)
        rows.append(row)

        state.update(winner, loser, surface, date=date)
        elo.update(winner, loser, surface, category)

    return pd.DataFrame(rows)


def run_walk_forward(
    df_full: pd.DataFrame,
    val_start: pd.Timestamp,
    val_end: pd.Timestamp,
    train_years: int = 3,
    min_edge: float = _DEFAULT_MIN_EDGE,
    bankroll: float = 100.0,
) -> WalkForwardResult:
    """Trainiert Modell auf [val_start-train_years, val_start), evaluiert auf
    [val_start, val_end)."""
    train_start = val_start - relativedelta(years=train_years)
    train = df_full[(df_full["date"] >= train_start) & (df_full["date"] < val_start)]
    val = df_full[(df_full["date"] >= val_start) & (df_full["date"] < val_end)].copy()

    if len(train) < 500 or len(val) < 50:
        return WalkForwardResult(val_start, val_end, len(train), len(val),
                                 np.nan, np.nan, 0, 0.0, 0, 0.0, 0.0)

    model = tlgbm.train_tennis_lgbm(
        X_train=train, y_train=train["y"].to_numpy(),
        X_cal=None, y_cal=None,
    )
    p_lgbm = model.predict_p_a(val)
    p_elo = val["elo_p_a"].to_numpy()
    y_val = val["y"].to_numpy()

    # Blended prediction
    p_ens = 0.45 * p_lgbm + 0.55 * p_elo

    # Simulate bets: edge = p - implied_odds
    lgbm_bets, lgbm_pnl_pct, clvs = _simulate_bets(val, p_ens, min_edge)
    elo_bets, elo_pnl_pct, _ = _simulate_bets(val, p_elo, min_edge)

    per_surface: dict[str, dict] = {}
    for surf in ("hard", "clay", "grass"):
        mask = val["surface"] == surf
        if mask.sum() > 30:
            n, roi, _ = _simulate_bets(val[mask].reset_index(drop=True), p_ens[mask], min_edge)
            per_surface[surf] = {"n_bets": n, "roi_pct": roi}

    return WalkForwardResult(
        val_start=val_start, val_end=val_end,
        n_train=len(train), n_val=len(val),
        lgbm_brier=float(np.mean((p_ens - y_val) ** 2)),
        elo_brier=float(np.mean((p_elo - y_val) ** 2)),
        lgbm_bets=lgbm_bets, lgbm_roi_pct=lgbm_pnl_pct,
        elo_bets=elo_bets, elo_roi_pct=elo_pnl_pct,
        mean_clv_lgbm=float(np.mean(clvs)) if clvs else 0.0,
        per_surface=per_surface,
    )


def _simulate_bets(val: pd.DataFrame, probs: np.ndarray, min_edge: float) -> tuple[int, float, list[float]]:
    """Für jedes Val-Match: bet side_a wenn p_a - implied(odds_a) > min_edge, etc.
    Returns (n_bets, roi_pct, clv_values)."""
    n_bets = 0
    total_pnl = 0.0
    total_stake = 0.0
    clvs = []

    val_r = val.reset_index(drop=True)
    for i, row in val_r.iterrows():
        p_a = probs[i]
        y = row["y"]

        for side, p, odds, close in (
            ("a", p_a, row["odds_a"], row["close_a"]),
            ("b", 1 - p_a, row["odds_b"], row["close_b"]),
        ):
            if pd.isna(odds) or odds < 1.05:
                continue
            edge = p - _implied(odds)
            if edge < min_edge:
                continue
            stake = _kelly(p, odds) * 100
            if stake < 0.5:
                continue
            n_bets += 1
            total_stake += stake
            won = (side == "a" and y == 1) or (side == "b" and y == 0)
            pnl = (odds - 1) * stake if won else -stake
            total_pnl += pnl

            if pd.notna(close) and close > 1.05:
                clv = odds / close - 1.0
                clvs.append(max(-0.99, min(2.00, clv)))

    roi = 100.0 * total_pnl / total_stake if total_stake > 0 else 0.0
    return n_bets, roi, clvs
