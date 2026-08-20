#!/usr/bin/env python3
"""Read-only 500-match sensitivity audit for Tennis rank and Elo uncertainty parity.

Baseline: MODEL1-001 fixed RollingState/date, but current live rank priors=1500 and
surface_count defaults=0.
Counterfactuals:
  RANK: use historical pre-match WRank/LRank, keep surface_count defaults=0
  UNCERTAINTY: keep rank priors=1500, pass pre-match Elo surface counts
  BOTH: use ranks + surface counts
No model/ledger/runtime writes.
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import MIN_EDGE, TENNIS_MIN_EDGE_BY_CATEGORY
from src.data.tennis_odds import fetch_full_tour_odds
from src.models import tennis_lgbm as tlgbm
from src.models.tennis_elo import TennisEloRatings, predict_winner
from src.tennis.ensemble import _LGBM_WEIGHT
from src.tennis.features import RollingState, build_match_features

N_TARGET = 500
MODEL_DIR = ROOT / "models" / "tennis_lgbm"


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
    return (
        (p_a * odds_a - 1.0 > 0 and p_a - fa >= min_edge)
        or ((1.0 - p_a) * odds_b - 1.0 > 0 and (1.0 - p_a) - fb >= min_edge)
    )


def summary(vals):
    a = np.asarray(vals, dtype=float)
    return {
        "mean_abs_pp": float(a.mean() * 100),
        "median_abs_pp": float(np.median(a) * 100),
        "p90_abs_pp": float(np.quantile(a, .90) * 100),
        "p95_abs_pp": float(np.quantile(a, .95) * 100),
        "max_abs_pp": float(a.max() * 100),
        "count_ge_1pp": int((a >= .01).sum()),
        "count_ge_2pp": int((a >= .02).sum()),
        "count_ge_5pp": int((a >= .05).sum()),
        "count_ge_10pp": int((a >= .10).sum()),
    }


def main():
    model = tlgbm.load(MODEL_DIR)
    cols = list(model.feature_columns)
    df = fetch_full_tour_odds(tours=["atp", "wta"], years=range(2010, 2026), cache=False)
    if df.empty:
        raise RuntimeError("historical tennis corpus unavailable")
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Winner", "Loser"]).sort_values("Date").reset_index(drop=True)
    df = df[df["Date"] < pd.Timestamp("2026-01-01")].reset_index(drop=True)

    state = RollingState(window=10)
    elo = TennisEloRatings()
    deltas = {k: {"raw": [], "cal": [], "ens": [], "flips": 0, "edge_cross": 0} for k in ("rank", "uncertainty", "both")}
    n = 0

    def sym(feat_ab, feat_ba):
        xa, xb = pd.DataFrame([feat_ab]), pd.DataFrame([feat_ba])
        raw_a = float(model.model.predict_proba(xa[cols].to_numpy())[:, 1][0])
        raw_b = float(model.model.predict_proba(xb[cols].to_numpy())[:, 1][0])
        raw = (raw_a + 1.0 - raw_b) / 2.0
        cal_a = float(model.predict_p_a(xa)[0])
        cal_b = float(model.predict_p_a(xb)[0])
        cal = (cal_a + 1.0 - cal_b) / 2.0
        return raw, cal

    def features(pa, pb, surface, category, best_of, date, ra, rb, ea, eb, esa, esb, ca, cb):
        return build_match_features(
            player_a=pa, player_b=pb, surface=surface,
            best_of=best_of, category=category, round_str="",
            rank_a=ra, rank_b=rb,
            elo_a=ea, elo_b=eb, elo_surface_a=esa, elo_surface_b=esb,
            state=state, date=date,
            surface_count_a=ca, surface_count_b=cb,
        )

    for _, m in df.iterrows():
        winner, loser = str(m["Winner"]), str(m["Loser"])
        date = pd.Timestamp(m["Date"])
        surface = str(m.get("surface_std") or "hard").lower()
        category = str(m.get("category") or "atp250")
        bo = m.get("Best of")
        best_of = int(bo) if pd.notna(bo) else 3
        rw, rl = fnum(m.get("WRank"), 1500), fnum(m.get("LRank"), 1500)
        ew, el = elo.get_overall(winner), elo.get_overall(loser)
        ews, els = elo.get_blended(winner, surface), elo.get_blended(loser, surface)
        cw, cl = elo.get_surface_count(winner, surface), elo.get_surface_count(loser, surface)

        if date >= pd.Timestamp("2025-01-01") and n < N_TARGET and state.form.get(winner) and state.form.get(loser):
            scenarios = {
                "baseline": (1500.0, 1500.0, 0, 0),
                "rank": (rw, rl, 0, 0),
                "uncertainty": (1500.0, 1500.0, cw, cl),
                "both": (rw, rl, cw, cl),
            }
            probs = {}
            elo_p = float(predict_winner(winner, loser, elo, surface)["p_a"])
            weight = _LGBM_WEIGHT.get(surface, _LGBM_WEIGHT["default"])
            for name, (ra, rb, ca, cb) in scenarios.items():
                ab = features(winner, loser, surface, category, best_of, date, ra, rb, ew, el, ews, els, ca, cb)
                ba = features(loser, winner, surface, category, best_of, date, rb, ra, el, ew, els, ews, cb, ca)
                raw, cal = sym(ab, ba)
                ens = weight * cal + (1.0 - weight) * elo_p
                probs[name] = (raw, cal, ens)

            odds_w, odds_l = fnum(m.get("B365W"), 0.0), fnum(m.get("B365L"), 0.0)
            floor = float(TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE))
            base_raw, base_cal, base_ens = probs["baseline"]
            base_action = market_actionable(base_ens, odds_w, odds_l, floor)
            for name in ("rank", "uncertainty", "both"):
                raw, cal, ens = probs[name]
                d = deltas[name]
                d["raw"].append(abs(raw - base_raw))
                d["cal"].append(abs(cal - base_cal))
                d["ens"].append(abs(ens - base_ens))
                d["flips"] += int((ens >= .5) != (base_ens >= .5))
                d["edge_cross"] += int(market_actionable(ens, odds_w, odds_l, floor) != base_action)
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
        state.update(winner, loser, surface, date=date, winner_rank=rw, loser_rank=rl,
                     sets_w=sets_w, sets_l=sets_l, tiebreaks_won_by_winner=tb_w,
                     tiebreaks_won_by_loser=tb_l)
        elo.update(winner, loser, surface, category)
        if n >= N_TARGET:
            break

    if n != N_TARGET:
        raise RuntimeError(f"only {n} eligible matches")

    out = {"sample": n}
    for name, d in deltas.items():
        out[name] = {
            "raw_classifier": summary(d["raw"]),
            "calibrated_lgbm": summary(d["cal"]),
            "final_ensemble": summary(d["ens"]),
            "direction_flips": d["flips"],
            "edge_status_crossings": d["edge_cross"],
        }
    print("MODEL1_RANK_UNCERTAINTY=" + json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
