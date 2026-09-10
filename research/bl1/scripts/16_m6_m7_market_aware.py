"""FLAGSHIP-BL1 — M6 market+Elo blend, M7 market residual (v6 STRUCTURAL).

CEO BL1 V6 §1, §4, §8:

  §1 No direct raw-pickle load of BL1 dataset files. Access routes through
     `09_partitions.py::load_development_with_market()`.
  §4 Unified missing-market policy via `canonical_market.apply_policy()`.
     M5, M6 and M7 all evaluate on the same set of rows.
  §8 Terminology: no "opening" / "market_open" — canonical / pre-closing.

M6: p = alpha × p_canonical_market + (1 - alpha) × p_Elo
    alpha selected on nested chronological DEV OOF (2425/2526 outcomes
    never touched). Grid: 0.0..1.0 by 0.1.
    Under apply_policy(): if the canonical row is missing, the row is
    dropped for BOTH M5 and M6 — so at alpha=1.0, M6 == M5 exactly.

M7: LGBM residual with signal-time features:
    - canonical pre-closing no-vig market probabilities
    - Elo pre-match
    - DC probabilities (per-season snapshot)
    - DC strengths
    - rolling form (3, 6)
    - rolling goals for/against
    - rest days
    - promoted flags
    - domestic midweek density

Closing prices NEVER enter features.

Outputs:
  research/bl1/results/oof_m6_dev_v3.csv
  research/bl1/results/oof_m7_dev_v3.csv
  research/bl1/results/m6_alpha_sweep.csv
  research/bl1/results/m6_m7_summary.csv
"""
from __future__ import annotations

import importlib.util
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


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


partitions = _load("bl1_partitions", ROOT / "research/bl1/scripts/09_partitions.py")
market = _load("bl1_canonical_market", ROOT / "research/bl1/scripts/canonical_market.py")

RES = ROOT / "research" / "bl1" / "results"
SNAP_DIR = RES / "dc_snapshots"
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
RAW_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
CALIB_TRAIN_FOLDS = ["1819", "1920"]
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


def _load_dc_snapshots() -> dict:
    """DC snapshots are precomputed research artefacts (not raw dataset)."""
    return {p.stem.split("_")[1]: pickle.load(open(p, "rb")) for p in sorted(SNAP_DIR.glob("dc_*.pkl"))}


def _load_elo_series():
    """Precomputed Elo series (research artefact, not raw dataset)."""
    with open(RES / "elo_series_dev.pkl", "rb") as f:
        elo_series = pickle.load(f)
    elo_series["date"] = pd.to_datetime(elo_series["date"])
    return elo_series


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
    """Feature builder — evaluates ONLY on rows where the canonical market
    is available (apply_policy has already been applied by the caller)."""
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])[["elo_home_pre", "elo_away_pre"]]
    rows, y_arr = [], []
    slice_df = slice_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    # Canonical market probs + row filter — same policy as M5/M6.
    kept, p_mkt_all = market.apply_policy(slice_df)
    for i, r in kept.iterrows():
        home, away = r["home_team"], r["away_team"]
        date = pd.Timestamp(r["date"])
        season = r["season"]

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

        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh = float(elo_row["elo_home_pre"])
            ea = float(elo_row["elo_away_pre"])
        except (KeyError, ValueError):
            eh = ea = ELO_DEFAULT
        ph_e, pd_e, pa_e = elo_win_probability(eh, ea, neutral=False)

        p_mkt = p_mkt_all[i]  # [away, draw, home]
        promoted_set = promoted_map.get(season, set())
        feat = {
            "mkt_p_home": p_mkt[2], "mkt_p_draw": p_mkt[1], "mkt_p_away": p_mkt[0],
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
    return pd.DataFrame(rows), np.array(y_arr, dtype=np.int64), kept


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


def _apply_policy_and_elo(fold_df: pd.DataFrame, elo_series: pd.DataFrame):
    """Returns (kept_df, p_mkt (n,3), p_elo (n,3), y)."""
    kept, p_mkt = market.apply_policy(fold_df.sort_values(["date", "home_team"], kind="stable"))
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])
    ys, pelo = [], []
    for _, r in kept.iterrows():
        date = pd.Timestamp(r["date"]); home = r["home_team"]; away = r["away_team"]
        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh = float(elo_row["elo_home_pre"]); ea = float(elo_row["elo_away_pre"])
        except Exception:
            eh = ea = ELO_DEFAULT
        ph, pd_, pa = elo_win_probability(eh, ea, neutral=False)
        pelo.append([pa, pd_, ph])
        ys.append(int(r["y"]))
    pelo_arr = np.array(pelo).reshape(-1, 3) if pelo else np.empty((0, 3))
    return kept, p_mkt, pelo_arr, np.array(ys, dtype=int)


def main() -> None:
    # ---- Load DEV+market via canonical partition helper ----
    raw_dev = partitions.load_development_with_market(RAW_PKL, FULL_PKL, include_closing=False)
    raw_dev["y"] = raw_dev.apply(_label, axis=1)
    raw_dev["date"] = pd.to_datetime(raw_dev["date"])
    raw_dev = raw_dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    # Apply the unified missing-market policy at the DEV level so downstream
    # inner-val selection and rolling-history filters see the same evaluated
    # row set. Earlier dev seasons without AvgH coverage are excluded from
    # both training and evaluation — this is the deterministic shared policy.
    raw_dev, _ = market.apply_policy(raw_dev)
    print(f"[16_m6_m7] DEV+market after unified policy: n={len(raw_dev)}", flush=True)

    snap = _load_dc_snapshots()
    elo_series = _load_elo_series()
    promoted_map = _promoted_by_season(raw_dev)

    # ---- M6: unified apply_policy per fold, then chronological alpha selection ----
    fold_data = {}
    for outer in OUTER_FOLDS + CALIB_TRAIN_FOLDS:
        fold_df = raw_dev[raw_dev["season"] == outer].copy()
        kept, p_mkt, p_elo, ys = _apply_policy_and_elo(fold_df, elo_series)
        fold_data[outer] = {
            "y": ys, "p_mkt": p_mkt, "p_elo": p_elo,
            "date": kept["date"].tolist(),
            "home_team": kept["home_team"].tolist(),
            "away_team": kept["away_team"].tolist(),
            "season": [outer] * len(kept),
        }

    m6_outer_rows = []
    alpha_selection = []
    for i, outer in enumerate(OUTER_FOLDS):
        earlier = CALIB_TRAIN_FOLDS + OUTER_FOLDS[:i]
        y_train = np.concatenate([fold_data[e]["y"] for e in earlier])
        pmkt_train = np.concatenate([fold_data[e]["p_mkt"] for e in earlier], axis=0)
        pelo_train = np.concatenate([fold_data[e]["p_elo"] for e in earlier], axis=0)
        best_alpha, best_brier = 0.0, np.inf
        alpha_rows = []
        for a in ALPHA_GRID:
            p_blend = a * pmkt_train + (1 - a) * pelo_train
            b = _brier(y_train, p_blend)
            alpha_rows.append({"outer": outer, "alpha": a, "train_brier": b})
            if b < best_brier:
                best_brier = b; best_alpha = a
        alpha_selection.extend(alpha_rows)
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
    for col in ("m6_p_away", "m6_p_draw", "m6_p_home"):
        m6_oof[col] = m6_oof[col].round(12)
    m6_oof.to_csv(RES / "oof_m6_dev_v3.csv", index=False)
    _alpha = pd.DataFrame(alpha_selection)
    _alpha["train_brier"] = _alpha["train_brier"].round(12)
    _alpha.to_csv(RES / "m6_alpha_sweep.csv", index=False)
    print(f"\nM6 pooled dev OOF Brier = {_brier(m6_oof['y'].values, m6_oof[['m6_p_away','m6_p_draw','m6_p_home']].values):.4f}", flush=True)

    # ---- M7: LGBM residual, features built on the apply_policy-kept rows ----
    print("\n=== Building M7 LGBM residual model ===", flush=True)
    dev_oof_rows_m7 = []
    for outer in OUTER_FOLDS:
        val_df = raw_dev[raw_dev["season"] == outer].copy()
        train_df = raw_dev[raw_dev["date"] < val_df["date"].min()].copy()
        inner_seasons = sorted(train_df["season"].unique())
        # If fewer than 2 earlier seasons exist under the unified policy,
        # train on all earlier rows without an inner-val holdout (no early
        # stopping). This happens for the earliest outer fold when only one
        # earlier season has canonical market coverage.
        if len(inner_seasons) < 2:
            X_train, y_train, _ = _build_features_m7(train_df, train_df, snap, elo_series, promoted_map)
            X_val, y_val, kept_val = _build_features_m7(val_df, train_df, snap, elo_series, promoted_map)
            cols = X_train.columns.tolist()
            X_val = X_val.reindex(columns=cols, fill_value=0.0)
            m = lgb.LGBMClassifier(
                objective="multiclass", num_class=3,
                n_estimators=200, learning_rate=0.05, num_leaves=15,
                min_child_samples=30, reg_lambda=1.0,
                subsample=0.8, colsample_bytree=0.9,
                random_state=42, n_jobs=1, verbose=-1,
            )
            m.fit(X_train, y_train)
        else:
            inner_val_season = inner_seasons[-1]
            train_inner = train_df[train_df["season"] != inner_val_season]
            train_innerval = train_df[train_df["season"] == inner_val_season]
            X_train, y_train, _ = _build_features_m7(train_inner, train_inner, snap, elo_series, promoted_map)
            X_iv, y_iv, _ = _build_features_m7(train_innerval, train_df, snap, elo_series, promoted_map)
            X_val, y_val, kept_val = _build_features_m7(val_df, train_df, snap, elo_series, promoted_map)
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
        fold_oof = pd.DataFrame({
            "season": outer,
            "date": kept_val["date"].values,
            "home_team": kept_val["home_team"].values,
            "away_team": kept_val["away_team"].values,
            "y": y_val,
            "m7_p_away": probs[:, 0], "m7_p_draw": probs[:, 1], "m7_p_home": probs[:, 2],
        })
        b = _brier(y_val, probs)
        print(f"  M7 outer={outer}: Brier={b:.4f} best_iter={m.best_iteration_ or m.n_estimators_}", flush=True)
        dev_oof_rows_m7.append(fold_oof)

    m7_oof = pd.concat(dev_oof_rows_m7, ignore_index=True)
    for col in ("m7_p_away", "m7_p_draw", "m7_p_home"):
        m7_oof[col] = m7_oof[col].round(12)
    m7_oof.to_csv(RES / "oof_m7_dev_v3.csv", index=False)
    print(f"\nM7 pooled dev OOF Brier = {_brier(m7_oof['y'].values, m7_oof[['m7_p_away','m7_p_draw','m7_p_home']].values):.4f}", flush=True)

    # ---- Summary ----
    m5 = pd.read_csv(RES / "oof_m5_preclose_dev.csv", dtype={"season": str})
    y5 = m5["y"].to_numpy(); p5 = m5[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
    rows = [
        {"model": "M5_market_preclose", "n": len(y5), "brier": _brier(y5, p5), "logloss": _logloss(y5, p5)},
        {"model": "M6_market_elo_blend", "n": len(m6_oof), "brier": _brier(m6_oof['y'].values, m6_oof[['m6_p_away','m6_p_draw','m6_p_home']].values), "logloss": _logloss(m6_oof['y'].values, m6_oof[['m6_p_away','m6_p_draw','m6_p_home']].values)},
        {"model": "M7_market_residual", "n": len(m7_oof), "brier": _brier(m7_oof['y'].values, m7_oof[['m7_p_away','m7_p_draw','m7_p_home']].values), "logloss": _logloss(m7_oof['y'].values, m7_oof[['m7_p_away','m7_p_draw','m7_p_home']].values)},
    ]
    _summary = pd.DataFrame(rows)
    for col in ("brier", "logloss"):
        _summary[col] = _summary[col].round(12)
    _summary.to_csv(RES / "m6_m7_summary.csv", index=False)
    print("\nSummary:", flush=True)
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
