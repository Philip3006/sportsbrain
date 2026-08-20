#!/usr/bin/env python3
"""Read-only audit of feasible live Tennis rank source.

Candidate production source: latest prior WRank/LRank observed for each player in
same tennis-data historical corpus already loaded by the scanner.

Measures on 2025 holdout matches:
- coverage
- age of latest prior rank observation
- inferred-rank error vs actual target match rank
- residual LGBM/ensemble error: inferred as-of ranks vs actual target ranks
No writes to model/ledger/runtime state.
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.tennis_odds import fetch_full_tour_odds
from src.models import tennis_lgbm as tlgbm
from src.models.tennis_elo import TennisEloRatings, predict_winner
from src.tennis.ensemble import _LGBM_WEIGHT
from src.tennis.features import RollingState, build_match_features

N_TARGET = 500
MODEL = tlgbm.load(ROOT / "models" / "tennis_lgbm")
COLS = list(MODEL.feature_columns)


def valid_rank(x):
    try:
        v = float(x)
        return v if np.isfinite(v) and v > 0 else None
    except Exception:
        return None


def stats(a):
    a = np.asarray(a, dtype=float)
    return {
        "n": int(len(a)),
        "mean": float(a.mean()) if len(a) else None,
        "median": float(np.median(a)) if len(a) else None,
        "p90": float(np.quantile(a, .90)) if len(a) else None,
        "p95": float(np.quantile(a, .95)) if len(a) else None,
        "max": float(a.max()) if len(a) else None,
    }


def prob_stats(a):
    s = stats(np.asarray(a) * 100.0)
    s["unit"] = "percentage_points"
    return s


def sym(state, date, surface, category, best_of, winner, loser,
        rw, rl, ew, el, ews, els, cw, cl):
    common = dict(surface=surface, best_of=best_of, category=category,
                  round_str="", state=state, date=date)
    ab = build_match_features(
        player_a=winner, player_b=loser,
        rank_a=rw, rank_b=rl, elo_a=ew, elo_b=el,
        elo_surface_a=ews, elo_surface_b=els,
        surface_count_a=cw, surface_count_b=cl, **common,
    )
    ba = build_match_features(
        player_a=loser, player_b=winner,
        rank_a=rl, rank_b=rw, elo_a=el, elo_b=ew,
        elo_surface_a=els, elo_surface_b=ews,
        surface_count_a=cl, surface_count_b=cw, **common,
    )
    xa, xb = pd.DataFrame([ab]), pd.DataFrame([ba])
    pa = float(MODEL.predict_p_a(xa)[0])
    pb = float(MODEL.predict_p_a(xb)[0])
    return (pa + 1.0 - pb) / 2.0


def main():
    df = fetch_full_tour_odds(tours=["atp", "wta"], years=range(2010, 2026), cache=False)
    if df.empty:
        raise RuntimeError("historical tennis corpus unavailable")
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Winner", "Loser"]).sort_values("Date").reset_index(drop=True)
    df = df[df["Date"] < pd.Timestamp("2026-01-01")].reset_index(drop=True)

    state = RollingState(window=10)
    elo = TennisEloRatings()
    # player -> (rank, observed_match_date)
    latest_rank = {}

    eligible = 0
    covered = 0
    target_n = 0
    rank_abs_errors = []
    rank_signed_errors = []
    ages = []
    within = {5: 0, 10: 0, 25: 0, 50: 0, 100: 0}
    cal_residual = []
    ensemble_residual = []

    for _, m in df.iterrows():
        w, l = str(m["Winner"]), str(m["Loser"])
        date = pd.Timestamp(m["Date"])
        surface = str(m.get("surface_std") or "hard").lower()
        category = str(m.get("category") or "atp250")
        bo = m.get("Best of")
        best_of = int(bo) if pd.notna(bo) else 3
        rw_actual, rl_actual = valid_rank(m.get("WRank")), valid_rank(m.get("LRank"))

        if date >= pd.Timestamp("2025-01-01") and target_n < N_TARGET \
                and state.form.get(w) and state.form.get(l):
            eligible += 1
            iw = latest_rank.get(w)
            il = latest_rank.get(l)
            if rw_actual is not None and rl_actual is not None and iw and il:
                covered += 1
                target_n += 1
                rw_inf, dw = iw
                rl_inf, dl = il
                for inferred, actual, obs_date in ((rw_inf, rw_actual, dw), (rl_inf, rl_actual, dl)):
                    err = inferred - actual
                    rank_signed_errors.append(err)
                    ae = abs(err)
                    rank_abs_errors.append(ae)
                    ages.append(max(0, (date - obs_date).days))
                    for k in within:
                        within[k] += int(ae <= k)

                ew, el = elo.get_overall(w), elo.get_overall(l)
                ews, els = elo.get_blended(w, surface), elo.get_blended(l, surface)
                cw, cl = elo.get_surface_count(w, surface), elo.get_surface_count(l, surface)
                p_inf = sym(state, date, surface, category, best_of, w, l,
                            rw_inf, rl_inf, ew, el, ews, els, cw, cl)
                p_actual = sym(state, date, surface, category, best_of, w, l,
                               rw_actual, rl_actual, ew, el, ews, els, cw, cl)
                cal_residual.append(abs(p_inf - p_actual))
                elo_p = float(predict_winner(w, l, elo, surface)["p_a"])
                wt = _LGBM_WEIGHT.get(surface, _LGBM_WEIGHT["default"])
                ens_inf = wt * p_inf + (1.0 - wt) * elo_p
                ens_actual = wt * p_actual + (1.0 - wt) * elo_p
                ensemble_residual.append(abs(ens_inf - ens_actual))

        # Update rank source only AFTER target evaluation.
        if rw_actual is not None:
            latest_rank[w] = (rw_actual, date)
        if rl_actual is not None:
            latest_rank[l] = (rl_actual, date)

        # Update rolling/Elo after feature extraction.
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
                if gw == 7 and gl == 6: tb_w += 1
                elif gl == 7 and gw == 6: tb_l += 1
            except Exception:
                continue
        state.update(w, l, surface, date=date,
                     winner_rank=rw_actual, loser_rank=rl_actual,
                     sets_w=sets_w, sets_l=sets_l,
                     tiebreaks_won_by_winner=tb_w, tiebreaks_won_by_loser=tb_l)
        elo.update(w, l, surface, category)

        if target_n >= N_TARGET:
            break

    if target_n != N_TARGET:
        raise RuntimeError(f"only {target_n} fully covered target matches")

    denom = len(rank_abs_errors)
    result = {
        "target_matches": target_n,
        "eligible_seen_until_target_complete": eligible,
        "coverage_until_target_complete": covered / eligible if eligible else None,
        "player_rank_observations": denom,
        "rank_abs_error": stats(rank_abs_errors),
        "rank_signed_error": stats(rank_signed_errors),
        "rank_observation_age_days": stats(ages),
        "within_rank_points": {str(k): within[k] / denom for k in within},
        "residual_calibrated_lgbm_error_inferred_vs_actual": prob_stats(cal_residual),
        "residual_final_ensemble_error_inferred_vs_actual": prob_stats(ensemble_residual),
        "source_contract": "latest prior WRank/LRank from same tennis-data corpus; never current/future target row",
    }
    print("MODEL1_RANK_SOURCE=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
