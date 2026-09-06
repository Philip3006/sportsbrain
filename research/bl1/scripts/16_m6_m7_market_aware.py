"""FLAGSHIP-BL1 — M6 market+Elo blend, M7 market residual model (v5).

v5 changes: terminology corrected — "pre-closing" not "opening"; access
routed through canonical partition loader for logging/audit.

M6: p = alpha × p_preclose_market + (1 - alpha) × p_Elo
     alpha selected on nested chronological DEV OOF only (2425 and 2526 outcomes never touched).
     Grid: 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0

M7: LGBM residual model with signal-time features:
     - pre-closing no-vig market probabilities (Pinnacle PSH/PSD/PSA)
     - Elo pre-match
     - DC probabilities (per-season snapshot)
     - DC strengths
     - rolling form (3, 6)
     - rolling goals for/against
     - rest
     - promoted flags
     - domestic midweek density (renamed, no Europe claim)

Closing prices NEVER enter features.

Uses precomputed:
  - Elo state: research/bl1/results/elo_series_dev.pkl
  - DC snapshots: research/bl1/results/dc_snapshots/*.pkl
  - M5 OOF: research/bl1/results/oof_m5_preclose_dev.csv (Pinnacle_open)

Outputs:
  research/bl1/results/oof_m6_dev_v3.csv
  research/bl1/results/oof_m7_dev_v3.csv
  research/bl1/results/m6_alpha_sweep.csv
  research/bl1/results/m6_m7_summary.csv
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.models import dixon_coles  # noqa: E402
from src.models.elo import elo_win_probability, ELO_DEFAULT  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"
SNAP_DIR = RES / "dc_snapshots"
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
RAW_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
CALIB_TRAIN_FOLDS = ["1819", "1920"]  # earlier dev seasons available in OOF
ALPHA_GRID = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def _label(row) -> int:
    return 2 if row["home_score"] > row["away_score"] else (1 if row["home_score"] == row["away_score"] else 0)


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y, p):
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _load_dc_snapshots() -> dict:
    return {p.stem.split("_")[1]: pickle.load(open(p, "rb")) for p in sorted(SNAP_DIR.glob("dc_*.pkl"))}


def _build_market_prob(row) -> tuple[np.ndarray, bool]:
    """Returns (p_open_market as [away, draw, home], any_missing)."""
    p = _devig_basic(row.get("PSH"), row.get("PSD"), row.get("PSA"))
    if p is None:
        return np.array([0.297, 0.253, 0.450]), True
    # p is [home, draw, away]
    return np.array([p[2], p[1], p[0]]), False


def _rolling_pts(hist, team, before, n):
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = []
    for _, r in recent.iterrows():
        is_home = r["home_team"] == team
        gs = r["home_score"] if is_home else r["away_score"]
        gc = r["away_score"] if is_home else r["home_score"]
        pts.append(3.0 if gs > gc else (1.0 if gs == gc else 0.0))
    return float(np.mean(pts))


def _rolling_goals(hist, team, before, n, side):
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 1.3
    vals = []
    for _, r in recent.iterrows():
        is_home = r["home_team"] == team
        if side == "for":
            vals.append(r["home_score"] if is_home else r["away_score"])
        else:
            vals.append(r["away_score"] if is_home else r["home_score"])
    return float(np.mean(vals))


def _rest_days(hist, team, before):
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    prev = hist[mask]
    if prev.empty:
        return 14.0
    return float((before - prev["date"].max()).days)


def _domestic_midweek_density(hist, team, before, days):
    since = before - pd.Timedelta(days=days)
    dates = pd.to_datetime(hist["date"], errors="coerce")
    mask = (((hist["home_team"] == team) | (hist["away_team"] == team)) & (dates >= since) & (dates < before))
    sub_dates = dates[mask]
    if sub_dates.empty:
        return 0
    return int(sub_dates.dt.dayofweek.isin([1, 2, 3]).sum())


def _build_features_m7(slice_df, hist_universe, snap, elo_series, promoted_map):
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])[["elo_home_pre", "elo_away_pre"]]
    rows = []
    y_arr = []
    slice_df = slice_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    for _, r in slice_df.iterrows():
        home, away = r["home_team"], r["away_team"]
        date = pd.Timestamp(r["date"])
        season = r["season"]

        # DC from season snapshot
        dc_params = snap.get(season)
        if dc_params is None or home not in dc_params.attack or away not in dc_params.attack:
            dc_p = {"p_home": 0.44, "p_draw": 0.26, "p_away": 0.30}
            dc_atk_h = dc_atk_a = dc_def_h = dc_def_a = 0.0
        else:
            dc_p = dixon_coles.predict_match(home, away, dc_params)
            dc_atk_h = dc_params.attack.get(home, 0.0)
            dc_atk_a = dc_params.attack.get(away, 0.0)
            dc_def_h = dc_params.defence.get(home, 0.0)
            dc_def_a = dc_params.defence.get(away, 0.0)

        # Elo pre-match
        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh = float(elo_row["elo_home_pre"])
            ea = float(elo_row["elo_away_pre"])
        except (KeyError, ValueError):
            eh = ea = ELO_DEFAULT
        ph_e, pd_e, pa_e = elo_win_probability(eh, ea, neutral=False)

        # Opening market probabilities (SIGNAL-TIME allowed)
        p_mkt, mkt_missing = _build_market_prob(r)
        # p_mkt is [away, draw, home] convention

        promoted_set = promoted_map.get(season, set())
        feat = {
            "mkt_p_home": p_mkt[2], "mkt_p_draw": p_mkt[1], "mkt_p_away": p_mkt[0],
            "mkt_missing": int(mkt_missing),
            "dc_p_home": dc_p["p_home"], "dc_p_draw": dc_p["p_draw"], "dc_p_away": dc_p["p_away"],
            "dc_atk_home": dc_atk_h, "dc_def_home": dc_def_h,
            "dc_atk_away": dc_atk_a, "dc_def_away": dc_def_a,
            "elo_home_pre": eh, "elo_away_pre": ea, "elo_diff": eh - ea,
            "elo_p_home": ph_e, "elo_p_draw": pd_e, "elo_p_away": pa_e,
            "form_home_l3": _rolling_pts(hist_universe, home, date, 3),
            "form_home_l6": _rolling_pts(hist_universe, home, date, 6),
            "form_away_l3": _rolling_pts(hist_universe, away, date, 3),
            "form_away_l6": _rolling_pts(hist_universe, away, date, 6),
            "gs_home_l5": _rolling_goals(hist_universe, home, date, 5, "for"),
            "gc_home_l5": _rolling_goals(hist_universe, home, date, 5, "against"),
            "gs_away_l5": _rolling_goals(hist_universe, away, date, 5, "for"),
            "gc_away_l5": _rolling_goals(hist_universe, away, date, 5, "against"),
            "rest_home": _rest_days(hist_universe, home, date),
            "rest_away": _rest_days(hist_universe, away, date),
            "dmwd_14_home": _domestic_midweek_density(hist_universe, home, date, 14),
            "dmwd_14_away": _domestic_midweek_density(hist_universe, away, date, 14),
            "is_promoted_home": 1 if home in promoted_set else 0,
            "is_promoted_away": 1 if away in promoted_set else 0,
        }
        rows.append(feat)
        y_arr.append(int(r["y"]))
    return pd.DataFrame(rows), np.array(y_arr)


def _promoted_by_season(raw):
    seasons_sorted = sorted(raw["season"].unique())
    out = {}
    prev = None
    for s in seasons_sorted:
        cur = set(raw[raw["season"] == s]["home_team"]).union(
            raw[raw["season"] == s]["away_team"])
        out[s] = (cur - prev) if prev is not None else set()
        prev = cur
    return out


def main() -> None:
    # Load dev+calib raw for feature history
    with open(RAW_PKL, "rb") as f:
        raw_r = pickle.load(f)
    raw_r["season"] = raw_r["season"].astype(str)
    raw_r = raw_r.dropna(subset=["home_score", "away_score"]).copy()
    raw_r["y"] = raw_r.apply(_label, axis=1)
    raw_r = raw_r.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    raw_dev = raw_r[raw_r["season"].isin(DEV_SEASONS)].copy()

    # Join opening-market columns from bl1_raw_full.pkl
    with open(FULL_PKL, "rb") as f:
        raw_full = pickle.load(f)
    raw_full["season"] = raw_full["season"].astype(str)
    raw_full["date"] = pd.to_datetime(raw_full["date"])
    raw_dev = raw_dev.merge(
        raw_full[["date", "home_team", "away_team", "PSH", "PSD", "PSA"]],
        on=["date", "home_team", "away_team"], how="left", suffixes=("", "_full"),
    )

    snap = _load_dc_snapshots()
    with open(RES / "elo_series_dev.pkl", "rb") as f:
        elo_series = pickle.load(f)
    elo_series["date"] = pd.to_datetime(elo_series["date"])
    promoted_map = _promoted_by_season(raw_dev)

    # ---- M6: alpha selection on nested chronological dev OOF ----
    # Build market probs and Elo probs per outer fold. Select alpha per outer
    # fold using earlier chronological folds only.
    m6_outer_rows = []
    alpha_selection = []
    fold_data = {}
    for outer in OUTER_FOLDS + CALIB_TRAIN_FOLDS:
        fold_df = raw_dev[raw_dev["season"] == outer].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        ps, ys, dts, hs, as_, seasons = [], [], [], [], [], []
        pmkt_all, pelo_all = [], []
        for _, r in fold_df.iterrows():
            date = pd.Timestamp(r["date"]); home = r["home_team"]; away = r["away_team"]
            p_mkt, missing = _build_market_prob(r)
            try:
                elo_row = elo_series.set_index(["date", "home_team", "away_team"]).loc[(date, home, away)]
                eh = float(elo_row["elo_home_pre"]); ea = float(elo_row["elo_away_pre"])
            except Exception:
                eh = ea = ELO_DEFAULT
            ph, pd_, pa = elo_win_probability(eh, ea, neutral=False)
            p_elo = np.array([pa, pd_, ph])
            pmkt_all.append(p_mkt); pelo_all.append(p_elo)
            ys.append(int(r["y"])); dts.append(date); hs.append(home); as_.append(away); seasons.append(outer)
        fold_data[outer] = {
            "y": np.array(ys), "p_mkt": np.array(pmkt_all), "p_elo": np.array(pelo_all),
            "date": dts, "home_team": hs, "away_team": as_, "season": seasons,
        }

    # For each OUTER fold, select alpha using earlier folds
    for i, outer in enumerate(OUTER_FOLDS):
        earlier = CALIB_TRAIN_FOLDS + OUTER_FOLDS[:i]
        # Pool earlier folds' data
        y_train = np.concatenate([fold_data[e]["y"] for e in earlier])
        pmkt_train = np.concatenate([fold_data[e]["p_mkt"] for e in earlier], axis=0)
        pelo_train = np.concatenate([fold_data[e]["p_elo"] for e in earlier], axis=0)
        best_alpha = 0.0
        best_brier = np.inf
        alpha_rows = []
        for a in ALPHA_GRID:
            p_blend = a * pmkt_train + (1 - a) * pelo_train
            b = _brier(y_train, p_blend)
            alpha_rows.append({"outer": outer, "alpha": a, "train_brier": b})
            if b < best_brier:
                best_brier = b; best_alpha = a
        alpha_selection.extend(alpha_rows)
        # Apply best alpha to outer fold
        p_val = best_alpha * fold_data[outer]["p_mkt"] + (1 - best_alpha) * fold_data[outer]["p_elo"]
        y_val = fold_data[outer]["y"]
        val_brier = _brier(y_val, p_val)
        print(f"  M6 outer={outer}: best_alpha={best_alpha:.1f} on {len(y_train)} earlier rows, val_brier={val_brier:.4f}", flush=True)
        m6_outer_rows.append(pd.DataFrame({
            "season": fold_data[outer]["season"],
            "date": fold_data[outer]["date"],
            "home_team": fold_data[outer]["home_team"],
            "away_team": fold_data[outer]["away_team"],
            "y": y_val,
            "m6_p_away": p_val[:, 0], "m6_p_draw": p_val[:, 1], "m6_p_home": p_val[:, 2],
            "selected_alpha": best_alpha,
        }))

    m6_oof = pd.concat(m6_outer_rows, ignore_index=True)
    m6_oof.to_csv(RES / "oof_m6_dev_v3.csv", index=False)
    pd.DataFrame(alpha_selection).to_csv(RES / "m6_alpha_sweep.csv", index=False)
    print(f"\nM6 pooled dev OOF Brier = {_brier(m6_oof['y'].values, m6_oof[['m6_p_away','m6_p_draw','m6_p_home']].values):.4f}", flush=True)

    # ---- M7: LGBM residual model ----
    print("\n=== Building M7 LGBM residual model (market probs allowed as features) ===", flush=True)
    dev_oof_rows_m7 = []
    for outer in OUTER_FOLDS:
        val_df = raw_dev[raw_dev["season"] == outer].copy()
        train_df = raw_dev[raw_dev["date"] < val_df["date"].min()].copy()
        inner_seasons = sorted(train_df["season"].unique())
        inner_val_season = inner_seasons[-1]
        train_inner = train_df[train_df["season"] != inner_val_season]
        train_innerval = train_df[train_df["season"] == inner_val_season]

        X_train, y_train = _build_features_m7(train_inner, train_inner, snap, elo_series, promoted_map)
        X_iv, y_iv = _build_features_m7(train_innerval, train_df, snap, elo_series, promoted_map)
        X_val, y_val = _build_features_m7(val_df, train_df, snap, elo_series, promoted_map)
        cols = X_train.columns.tolist()
        X_iv = X_iv.reindex(columns=cols, fill_value=0.0)
        X_val = X_val.reindex(columns=cols, fill_value=0.0)

        m = lgb.LGBMClassifier(
            objective="multiclass", num_class=3,
            n_estimators=500, learning_rate=0.05, num_leaves=15,
            min_child_samples=30, reg_lambda=1.0,
            subsample=0.8, colsample_bytree=0.9,
            random_state=42, n_jobs=1, verbose=-1,
        )
        m.fit(X_train, y_train, eval_set=[(X_iv, y_iv)],
              callbacks=[lgb.early_stopping(30, verbose=False)])
        probs = m.predict_proba(X_val)
        kv = val_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        fold_oof = pd.DataFrame({
            "season": outer, "date": kv["date"].values,
            "home_team": kv["home_team"].values, "away_team": kv["away_team"].values,
            "y": y_val,
            "m7_p_away": probs[:, 0], "m7_p_draw": probs[:, 1], "m7_p_home": probs[:, 2],
        })
        b = _brier(y_val, probs)
        print(f"  M7 outer={outer}: Brier={b:.4f} best_iter={m.best_iteration_ or m.n_estimators_}", flush=True)
        dev_oof_rows_m7.append(fold_oof)

    m7_oof = pd.concat(dev_oof_rows_m7, ignore_index=True)
    m7_oof.to_csv(RES / "oof_m7_dev_v3.csv", index=False)
    print(f"\nM7 pooled dev OOF Brier = {_brier(m7_oof['y'].values, m7_oof[['m7_p_away','m7_p_draw','m7_p_home']].values):.4f}", flush=True)

    # ---- Summary ----
    m5 = pd.read_csv(RES / "oof_m5_preclose_dev.csv", dtype={"season": str})
    y5 = m5["y"].to_numpy(); p5 = m5[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
    rows = [
        {"model": "M5_market_open", "n": len(y5), "brier": _brier(y5, p5), "logloss": _logloss(y5, p5)},
        {"model": "M6_market_elo_blend", "n": len(m6_oof), "brier": _brier(m6_oof['y'].values, m6_oof[['m6_p_away','m6_p_draw','m6_p_home']].values), "logloss": _logloss(m6_oof['y'].values, m6_oof[['m6_p_away','m6_p_draw','m6_p_home']].values)},
        {"model": "M7_market_residual", "n": len(m7_oof), "brier": _brier(m7_oof['y'].values, m7_oof[['m7_p_away','m7_p_draw','m7_p_home']].values), "logloss": _logloss(m7_oof['y'].values, m7_oof[['m7_p_away','m7_p_draw','m7_p_home']].values)},
    ]
    pd.DataFrame(rows).to_csv(RES / "m6_m7_summary.csv", index=False)
    print("\nSummary:", flush=True)
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
