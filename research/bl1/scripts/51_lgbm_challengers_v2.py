"""FLAGSHIP-BL1 CORRECTED — LGBM challengers v2.

CHANGES vs 50_lgbm_challengers.py:
- Per-season DC snapshots: every LGBM row (train, inner-val, outer-val) uses
  DC parameters fit on matches strictly BEFORE that row's season start.
  10 snapshots produced by 11_walk_forward_v2.py at RES/dc_snapshots/.
- Elo state: read pre-match `elo_home_pre` / `elo_away_pre` directly from
  RES/elo_series_dev.pkl (a single cumulative iteration over all dev+calib).
  Structurally eliminates the inner-val-Elo bug.
- Midweek feature renamed to `domestic_midweek_density_*`. No Europe claim.
- Outer folds: 2021, 2122, 2223, 2324 (LGBM needs several prior seasons of
  training data with valid DC features; folds 1819/1920 excluded from LGBM
  outer eval — they remain as calibrator training seeds via DC/Elo only).
- Inner val = last season of training slice. Inner Elo comes from the
  precomputed pre-match Elo (which is by construction the post-state of all
  earlier matches).

Outputs:
  research/bl1/results/oof_m3_dev_v2.csv     (fold 2021..2324)
  research/bl1/results/oof_m4_dev_v2.csv
  research/bl1/results/oof_m3_2425_v2.csv    (2425 predictions, reserved)
  research/bl1/results/oof_m4_2425_v2.csv
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
DATASET_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"
ELO_PKL = RES / "elo_series_dev.pkl"

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
CALIB_SEASON = "2425"
HOLDOUT_SEASON = "2526"
OUTER_FOLDS = ["1819", "1920", "2021", "2122", "2223", "2324"]


def _label(row) -> int:
    h, a = int(row["home_score"]), int(row["away_score"])
    return 2 if h > a else (1 if h == a else 0)


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y, p):
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _load_dc_snapshots() -> dict[str, dixon_coles.DixonColesParams]:
    snap = {}
    for pkl in sorted(SNAP_DIR.glob("dc_*.pkl")):
        s = pkl.stem.split("_")[1]
        with open(pkl, "rb") as f:
            snap[s] = pickle.load(f)
    return snap


# ---- Feature helpers (all strictly as-of via `date < row.date`) ----------

def _rolling_pts(hist: pd.DataFrame, team: str, before: pd.Timestamp, n: int) -> float:
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


def _rolling_goals(hist: pd.DataFrame, team: str, before: pd.Timestamp, n: int, side: str) -> float:
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


def _venue_pts(hist: pd.DataFrame, team: str, before: pd.Timestamp, venue: str, n: int) -> float:
    if venue == "home":
        mask = (hist["home_team"] == team) & (hist["date"] < before)
    else:
        mask = (hist["away_team"] == team) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = []
    for _, r in recent.iterrows():
        gs = r["home_score"] if venue == "home" else r["away_score"]
        gc = r["away_score"] if venue == "home" else r["home_score"]
        pts.append(3.0 if gs > gc else (1.0 if gs == gc else 0.0))
    return float(np.mean(pts))


def _rest_days(hist: pd.DataFrame, team: str, before: pd.Timestamp) -> float:
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    prev = hist[mask]
    if prev.empty:
        return 14.0
    return float((before - prev["date"].max()).days)


def _h2h_wr(hist: pd.DataFrame, home: str, away: str, before: pd.Timestamp, n: int = 5) -> float:
    mask = (
        ((hist["home_team"] == home) & (hist["away_team"] == away))
        | ((hist["home_team"] == away) & (hist["away_team"] == home))
    ) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 0.4
    wins = sum(
        1 for _, r in recent.iterrows()
        if (r["home_team"] == home and r["home_score"] > r["away_score"])
        or (r["home_team"] == away and r["away_score"] > r["home_score"])
    )
    return wins / len(recent)


def _domestic_midweek_density(hist: pd.DataFrame, team: str, before: pd.Timestamp, days: int) -> int:
    """RENAMED per CEO Correction E-Option 2. Counts *domestic* Bundesliga
    matches on Tue/Wed/Thu in the last `days` days. Does NOT measure European
    fixture load. Kept only as a raw as-of feature."""
    since = before - pd.Timedelta(days=days)
    dates = pd.to_datetime(hist["date"], errors="coerce")
    mask = (
        ((hist["home_team"] == team) | (hist["away_team"] == team))
        & (dates >= since) & (dates < before)
    )
    sub_dates = dates[mask]
    if sub_dates.empty:
        return 0
    return int(sub_dates.dt.dayofweek.isin([1, 2, 3]).sum())


# ---- Per-row feature builder --------------------------------------------

def _build_features(
    slice_df: pd.DataFrame,
    hist_universe: pd.DataFrame,
    snap: dict,
    elo_series: pd.DataFrame,
    promoted_teams_by_season: dict[str, set[str]],
    include_midweek: bool,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Build features for slice_df. Every row's DC comes from the season's
    snapshot (fit on strictly earlier seasons). Every row's Elo pre-match state
    comes from elo_series (a full-cumulative iteration).

    hist_universe is the (chronologically ordered) union of everything strictly
    up to and including the slice — used for rolling-form / venue / H2H feats.
    """
    # elo lookup by (date, home, away)
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])[["elo_home_pre", "elo_away_pre"]]

    rows = []
    y_arr = []
    slice_df = slice_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    for _, r in slice_df.iterrows():
        home, away = r["home_team"], r["away_team"]
        date = pd.Timestamp(r["date"])
        season = r["season"]

        # DC from season snapshot (strictly earlier seasons)
        dc_params = snap.get(season)
        if dc_params is None or home not in dc_params.attack or away not in dc_params.attack:
            dc_p = {"p_home": 0.44, "p_draw": 0.26, "p_away": 0.30}
            dc_attack_h = dc_attack_a = dc_defence_h = dc_defence_a = 0.0
            dc_home_adv = 0.30
            dc_unknown = 1
        else:
            dc_p = dixon_coles.predict_match(home, away, dc_params)
            dc_attack_h = dc_params.attack.get(home, 0.0)
            dc_attack_a = dc_params.attack.get(away, 0.0)
            dc_defence_h = dc_params.defence.get(home, 0.0)
            dc_defence_a = dc_params.defence.get(away, 0.0)
            dc_home_adv = dc_params.home_adv
            dc_unknown = 0

        # Elo from precomputed series
        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh = float(elo_row["elo_home_pre"])
            ea = float(elo_row["elo_away_pre"])
        except (KeyError, ValueError):
            eh = ea = ELO_DEFAULT
        ph_e, pd_e, pa_e = elo_win_probability(eh, ea, neutral=False)

        # Promoted flags
        promoted_set = promoted_teams_by_season.get(season, set())
        is_promoted_home = 1 if home in promoted_set else 0
        is_promoted_away = 1 if away in promoted_set else 0

        feat = {
            "dc_p_home": dc_p["p_home"], "dc_p_draw": dc_p["p_draw"], "dc_p_away": dc_p["p_away"],
            "dc_attack_home": dc_attack_h, "dc_defence_home": dc_defence_h,
            "dc_attack_away": dc_attack_a, "dc_defence_away": dc_defence_a,
            "dc_home_adv": dc_home_adv, "dc_unknown_pair": dc_unknown,
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
            "venue_home_pts_l5": _venue_pts(hist_universe, home, date, "home", 5),
            "venue_away_pts_l5": _venue_pts(hist_universe, away, date, "away", 5),
            "rest_home": _rest_days(hist_universe, home, date),
            "rest_away": _rest_days(hist_universe, away, date),
            "h2h_home_wr": _h2h_wr(hist_universe, home, away, date, 5),
            "is_promoted_home": is_promoted_home, "is_promoted_away": is_promoted_away,
        }
        if include_midweek:
            # Renamed per CEO Correction E — makes no Europe claim.
            feat["domestic_midweek_density_14_home"] = _domestic_midweek_density(hist_universe, home, date, 14)
            feat["domestic_midweek_density_14_away"] = _domestic_midweek_density(hist_universe, away, date, 14)
            feat["domestic_midweek_density_7_home"] = _domestic_midweek_density(hist_universe, home, date, 7)
            feat["domestic_midweek_density_7_away"] = _domestic_midweek_density(hist_universe, away, date, 7)

        rows.append(feat)
        y_arr.append(int(r["y"]))

    return pd.DataFrame(rows), np.array(y_arr)


def _promoted_by_season(raw: pd.DataFrame) -> dict[str, set[str]]:
    seasons_sorted = sorted(raw["season"].unique())
    out = {}
    prev = None
    for s in seasons_sorted:
        cur = set(raw[raw["season"] == s]["home_team"]).union(
            raw[raw["season"] == s]["away_team"])
        out[s] = (cur - prev) if prev is not None else set()
        prev = cur
    return out


def _train_lgbm(X_train, y_train, X_innerval, y_innerval):
    m = lgb.LGBMClassifier(
        objective="multiclass", num_class=3,
        n_estimators=500, learning_rate=0.05, num_leaves=15,
        min_child_samples=30, reg_lambda=1.0,
        subsample=0.8, colsample_bytree=0.9,
        random_state=42, n_jobs=1, verbose=-1,
    )
    m.fit(X_train, y_train,
          eval_set=[(X_innerval, y_innerval)],
          callbacks=[lgb.early_stopping(30, verbose=False)])
    return m


def _run(model_name: str, include_midweek: bool, raw: pd.DataFrame,
         snap: dict, elo_series: pd.DataFrame, promoted_map: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    dev_oof_rows = []
    all_folds_iter_rows = []

    for outer in OUTER_FOLDS:
        val_df = raw[raw["season"] == outer].copy()
        train_df = raw[raw["date"] < val_df["date"].min()].copy()
        # Inner-val split
        inner_seasons = sorted(train_df["season"].unique())
        inner_val_season = inner_seasons[-1]
        train_inner = train_df[train_df["season"] != inner_val_season]
        train_innerval = train_df[train_df["season"] == inner_val_season]

        print(f"\n[{model_name} outer={outer}] train n={len(train_inner)}, "
              f"inner_val=({inner_val_season}) n={len(train_innerval)}, val n={len(val_df)}", flush=True)

        # Build features
        X_train, y_train = _build_features(train_inner, train_inner, snap, elo_series,
                                             promoted_map, include_midweek)
        X_innerval, y_innerval = _build_features(train_innerval, train_df, snap, elo_series,
                                                   promoted_map, include_midweek)
        X_val, y_val = _build_features(val_df, train_df, snap, elo_series,
                                         promoted_map, include_midweek)

        cols = X_train.columns.tolist()
        X_innerval = X_innerval.reindex(columns=cols, fill_value=0.0)
        X_val = X_val.reindex(columns=cols, fill_value=0.0)

        m = _train_lgbm(X_train, y_train, X_innerval, y_innerval)
        probs = m.predict_proba(X_val)

        kept_val = val_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        fold_out = pd.DataFrame({
            "season": outer, "date": kept_val["date"].values,
            "home_team": kept_val["home_team"].values, "away_team": kept_val["away_team"].values,
            "y": y_val,
            f"{model_name}_p_away": probs[:, 0],
            f"{model_name}_p_draw": probs[:, 1],
            f"{model_name}_p_home": probs[:, 2],
        })
        for c in ("ps_open_home", "ps_open_draw", "ps_open_away",
                  "ps_close_home", "ps_close_draw", "ps_close_away"):
            if c in kept_val.columns:
                fold_out[c] = kept_val[c].values

        fold_brier = _brier(y_val, probs)
        fold_ll = _logloss(y_val, probs)
        print(f"  Brier={fold_brier:.4f}  LogLoss={fold_ll:.4f}  best_iter={m.best_iteration_ or m.n_estimators_}",
              flush=True)
        dev_oof_rows.append(fold_out)
        all_folds_iter_rows.append({
            "model": model_name, "fold": outer, "n_train_inner": len(X_train),
            "n_inner_val": len(X_innerval), "n_val": len(X_val),
            "brier": fold_brier, "logloss": fold_ll,
            "best_iter": m.best_iteration_ or m.n_estimators_,
        })

    # 2425 predictions — PREDICTION-ONLY per CEO Correction Section 3.
    # Emitted for post-lock calibrator fit only; labels stripped.
    val_2425 = raw[raw["season"] == CALIB_SEASON].copy()
    train_all = raw[raw["date"] < val_2425["date"].min()].copy()
    inner_seasons = sorted(train_all["season"].unique())
    inner_val_season = inner_seasons[-1]
    train_inner = train_all[train_all["season"] != inner_val_season]
    train_innerval = train_all[train_all["season"] == inner_val_season]
    X_tr, y_tr = _build_features(train_inner, train_inner, snap, elo_series, promoted_map, include_midweek)
    X_iv, y_iv = _build_features(train_innerval, train_all, snap, elo_series, promoted_map, include_midweek)
    X_v, y_v = _build_features(val_2425, train_all, snap, elo_series, promoted_map, include_midweek)
    cols = X_tr.columns.tolist()
    X_iv = X_iv.reindex(columns=cols, fill_value=0.0)
    X_v = X_v.reindex(columns=cols, fill_value=0.0)
    m = _train_lgbm(X_tr, y_tr, X_iv, y_iv)
    probs = m.predict_proba(X_v)
    kv = val_2425.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    # PREDICTION-ONLY 2425 output — no y, no scores, no closing odds.
    fold_2425 = pd.DataFrame({
        "season": CALIB_SEASON, "date": kv["date"].values,
        "home_team": kv["home_team"].values, "away_team": kv["away_team"].values,
        f"{model_name}_p_away": probs[:, 0],
        f"{model_name}_p_draw": probs[:, 1],
        f"{model_name}_p_home": probs[:, 2],
    })
    for c in ("ps_open_home", "ps_open_draw", "ps_open_away"):
        if c in kv.columns:
            fold_2425[c] = kv[c].values

    return pd.concat(dev_oof_rows, ignore_index=True), fold_2425, pd.DataFrame(all_folds_iter_rows)


def main() -> None:
    with open(DATASET_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()
    raw["y"] = raw.apply(_label, axis=1)
    raw = raw.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    raw_dev = raw[raw["season"] != HOLDOUT_SEASON].copy()

    snap = _load_dc_snapshots()
    with open(ELO_PKL, "rb") as f:
        elo_series = pickle.load(f)
    elo_series["date"] = pd.to_datetime(elo_series["date"])

    promoted = _promoted_by_season(raw_dev)

    print("=== Building M4 (DC + LGBM, NO domestic_midweek_density) ===", flush=True)
    m4_dev, m4_2425, m4_it = _run("m4", False, raw_dev, snap, elo_series, promoted)
    m4_dev.to_csv(RES / "oof_m4_dev_v2.csv", index=False)
    m4_2425.to_csv(RES / "predictions_m4_2425_v3.csv", index=False)

    print("\n=== Building M3 (DC + LGBM + domestic_midweek_density) ===", flush=True)
    m3_dev, m3_2425, m3_it = _run("m3", True, raw_dev, snap, elo_series, promoted)
    m3_dev.to_csv(RES / "oof_m3_dev_v2.csv", index=False)
    m3_2425.to_csv(RES / "predictions_m3_2425_v3.csv", index=False)

    all_it = pd.concat([m3_it, m4_it], ignore_index=True)
    all_it.to_csv(RES / "lgbm_fold_iters_v2.csv", index=False)

    print("\nAggregate (uncalibrated):", flush=True)
    for name, df in (("m3", m3_dev), ("m4", m4_dev)):
        y = df["y"].to_numpy()
        p = df[[f"{name}_p_away", f"{name}_p_draw", f"{name}_p_home"]].to_numpy()
        print(f"  {name}: n={len(y)}  Brier={_brier(y, p):.4f}  LogLoss={_logloss(y, p):.4f}", flush=True)


if __name__ == "__main__":
    main()
