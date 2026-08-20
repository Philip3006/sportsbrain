#!/usr/bin/env python3
"""Read-only FND-MODEL1-001 RollingState counterfactual audit.

OLD: empty RollingState + date=None
NEW: populated chronological RollingState + actual match date

Keeps other known live mismatches fixed between OLD/NEW so this isolates
RollingState impact. No ledger/model/runtime writes.
"""
from __future__ import annotations

from pathlib import Path
import json
import numpy as np
import pandas as pd

from src.config import MIN_EDGE, TENNIS_MIN_EDGE_BY_CATEGORY
from src.data.tennis_odds import fetch_full_tour_odds
from src.models import tennis_lgbm as tlgbm
from src.models.tennis_elo import TennisEloRatings, predict_winner
from src.tennis.ensemble import _LGBM_WEIGHT
from src.tennis.features import RollingState, build_match_features

N_TARGET = 500
MODEL_DIR = Path("models/tennis_lgbm")


def fnum(x, default):
    try:
        if pd.isna(x):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def market_actionable(p_a, odds_a, odds_b, min_edge):
    if odds_a <= 1.0 or odds_b <= 1.0:
        return False
    qa, qb = 1.0 / odds_a, 1.0 / odds_b
    s = qa + qb
    if s <= 0:
        return False
    fa, fb = qa / s, qb / s
    ev_a = p_a * odds_a - 1.0
    ev_b = (1.0 - p_a) * odds_b - 1.0
    edge_a = p_a - fa
    edge_b = (1.0 - p_a) - fb
    return (ev_a > 0 and edge_a >= min_edge) or (ev_b > 0 and edge_b >= min_edge)


def summarize(vals):
    a = np.asarray(vals, dtype=float)
    return {
        "n": int(len(a)),
        "mean_abs_pp": float(a.mean() * 100),
        "median_abs_pp": float(np.median(a) * 100),
        "p75_abs_pp": float(np.quantile(a, 0.75) * 100),
        "p90_abs_pp": float(np.quantile(a, 0.90) * 100),
        "p95_abs_pp": float(np.quantile(a, 0.95) * 100),
        "max_abs_pp": float(a.max() * 100),
        "count_ge_1pp": int((a >= 0.01).sum()),
        "count_ge_2pp": int((a >= 0.02).sum()),
        "count_ge_5pp": int((a >= 0.05).sum()),
        "count_ge_10pp": int((a >= 0.10).sum()),
    }


def main():
    model = tlgbm.load(MODEL_DIR)
    print("Loading historical corpus 2010-2025...")
    df = fetch_full_tour_odds(tours=["atp", "wta"], years=range(2010, 2026), cache=False)
    if df.empty:
        raise RuntimeError("historical tennis corpus unavailable")
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Winner", "Loser"]).sort_values("Date").reset_index(drop=True)
    df = df[df["Date"] < pd.Timestamp("2026-01-01")].reset_index(drop=True)
    print(f"Corpus rows: {len(df)}; range={df.Date.min()}..{df.Date.max()}")

    state = RollingState(window=10)
    empty_state = RollingState(window=10)
    elo = TennisEloRatings()
    raw_d, cal_d, ens_d = [], [], []
    flips = 0
    edge_floor_cross = 0
    n = 0

    def sym_probs(feat_ab, feat_ba):
        Xab = pd.DataFrame([feat_ab])
        Xba = pd.DataFrame([feat_ba])
        cols = list(model.feature_columns)
        raw_ab = float(model.model.predict_proba(Xab[cols].to_numpy())[:, 1][0])
        raw_ba = float(model.model.predict_proba(Xba[cols].to_numpy())[:, 1][0])
        raw = (raw_ab + (1.0 - raw_ba)) / 2.0
        cal_ab = float(model.predict_p_a(Xab)[0])
        cal_ba = float(model.predict_p_a(Xba)[0])
        cal = (cal_ab + (1.0 - cal_ba)) / 2.0
        return raw, cal

    for _, m in df.iterrows():
        winner, loser = str(m["Winner"]), str(m["Loser"])
        date = pd.Timestamp(m["Date"])
        surface = str(m.get("surface_std") or "hard").lower()
        category = str(m.get("category") or "atp250")
        bo = m.get("Best of")
        best_of = int(bo) if pd.notna(bo) else 3
        rw, rl = fnum(m.get("WRank"), 1500), fnum(m.get("LRank"), 1500)
        elo_w, elo_l = elo.get_overall(winner), elo.get_overall(loser)
        elo_ws, elo_ls = elo.get_blended(winner, surface), elo.get_blended(loser, surface)

        if date >= pd.Timestamp("2025-01-01") and n < N_TARGET:
            if state.form.get(winner) and state.form.get(loser):
                # Isolate MODEL1-001: both old/new retain current rank-prior and missing round.
                common_ab = dict(
                    player_a=winner, player_b=loser, surface=surface,
                    best_of=best_of, category=category, round_str="",
                    rank_a=1500.0, rank_b=1500.0,
                    elo_a=elo_w, elo_b=elo_l,
                    elo_surface_a=elo_ws, elo_surface_b=elo_ls,
                )
                common_ba = dict(
                    player_a=loser, player_b=winner, surface=surface,
                    best_of=best_of, category=category, round_str="",
                    rank_a=1500.0, rank_b=1500.0,
                    elo_a=elo_l, elo_b=elo_w,
                    elo_surface_a=elo_ls, elo_surface_b=elo_ws,
                )
                old_ab = build_match_features(**common_ab, state=empty_state, date=None)
                old_ba = build_match_features(**common_ba, state=empty_state, date=None)
                new_ab = build_match_features(**common_ab, state=state, date=date)
                new_ba = build_match_features(**common_ba, state=state, date=date)
                raw_old, cal_old = sym_probs(old_ab, old_ba)
                raw_new, cal_new = sym_probs(new_ab, new_ba)

                elo_p = float(predict_winner(winner, loser, elo, surface)["p_a"])
                w = _LGBM_WEIGHT.get(surface, _LGBM_WEIGHT["default"])
                ens_old = w * cal_old + (1.0 - w) * elo_p
                ens_new = w * cal_new + (1.0 - w) * elo_p

                raw_d.append(abs(raw_new - raw_old))
                cal_d.append(abs(cal_new - cal_old))
                ens_d.append(abs(ens_new - ens_old))
                flips += int((ens_old >= 0.5) != (ens_new >= 0.5))

                odds_w, odds_l = fnum(m.get("B365W"), 0.0), fnum(m.get("B365L"), 0.0)
                floor = float(TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE))
                edge_floor_cross += int(
                    market_actionable(ens_old, odds_w, odds_l, floor)
                    != market_actionable(ens_new, odds_w, odds_l, floor)
                )
                n += 1

        sets_w = sets_l = None
        try:
            if pd.notna(m.get("Wsets")) and pd.notna(m.get("Lsets")):
                sets_w, sets_l = int(m.get("Wsets")), int(m.get("Lsets"))
        except Exception:
            pass
        tb_w = tb_l = 0
        for i in range(1, 6):
            try:
                gw, gl = m.get(f"W{i}"), m.get(f"L{i}")
                if pd.isna(gw) or pd.isna(gl):
                    continue
                gw, gl = int(gw), int(gl)
                if gw == 7 and gl == 6:
                    tb_w += 1
                elif gl == 7 and gw == 6:
                    tb_l += 1
            except Exception:
                continue
        state.update(
            winner, loser, surface, date=date,
            winner_rank=rw, loser_rank=rl,
            sets_w=sets_w, sets_l=sets_l,
            tiebreaks_won_by_winner=tb_w,
            tiebreaks_won_by_loser=tb_l,
        )
        elo.update(winner, loser, surface, category)
        if n >= N_TARGET:
            break

    if n != N_TARGET:
        raise RuntimeError(f"only {n} eligible 2025 holdout matches; expected {N_TARGET}")

    result = {
        "audit": "FND-MODEL1-001 RollingState-only counterfactual",
        "sample": n,
        "raw_classifier": summarize(raw_d),
        "calibrated_lgbm": summarize(cal_d),
        "final_ensemble": summarize(ens_d),
        "final_ensemble_50pct_direction_flips": flips,
        "model_vs_market_edge_floor_status_crossings": edge_floor_cross,
        "notes": [
            "OLD=empty RollingState+date=None",
            "NEW=populated chronological RollingState+actual date",
            "rank prior=1500 and round missing kept identical to isolate MODEL1-001",
            "serve/bio external context omitted identically",
            "read-only; no ledger/model/promotion writes",
        ],
    }
    print("MODEL1_REPLAY_RESULT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
