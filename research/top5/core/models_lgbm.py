"""Top-5 — M3, M4 (LGBM challengers) and M7 (LGBM market residual).

Matches BL1 v7:
  - 51_lgbm_challengers_v2.py  → M3 (with domestic_midweek_density), M4 (without)
  - 16_m6_m7_market_aware.py   → M7 (market probs + DC + Elo + rolling features)

Feature set and LGBM hyperparameters are frozen to BL1 v7 reference. The only
adaptation for multi-league generality: promoted teams are always computed from
the dataset (no hardcoded team lists), and DC+Elo features come from the generic
artefacts pipeline rather than BL1-specific precomputed pkl files.

Outer folds:
  M3/M4: calib_seed_folds + outer_folds  (6 for BL1)
  M7:    outer_folds only                 (4 for BL1)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    _HAS_LGBM = True
except ImportError:
    _HAS_LGBM = False

from . import artefacts as art
from . import canonical_market as market
from . import dixon_coles as dc
from . import elo as elo_mod
from . import metrics
from . import partitions
from ..config import LeagueConfig

_LGBM_PARAMS = dict(
    objective="multiclass", num_class=3,
    n_estimators=500, learning_rate=0.05, num_leaves=15,
    min_child_samples=30, reg_lambda=1.0,
    subsample=0.8, colsample_bytree=0.9,
    random_state=42, n_jobs=1, verbose=-1,
)
_LGBM_NO_EARLY = dict(_LGBM_PARAMS, n_estimators=200)


# --------------------------------------------------------------------------
# Shared feature helpers — identical to BL1 v7 (51_lgbm_challengers_v2.py)
# --------------------------------------------------------------------------

def _rolling_pts(hist: pd.DataFrame, team: str, before: pd.Timestamp, n: int) -> float:
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = [3.0 if r["home_score"] > r["away_score"] and r["home_team"] == team
           else (3.0 if r["away_score"] > r["home_score"] and r["away_team"] == team
                 else (1.0 if r["home_score"] == r["away_score"] else 0.0))
           for _, r in recent.iterrows()]
    return float(np.mean(pts))


def _rolling_goals(hist: pd.DataFrame, team: str, before: pd.Timestamp,
                   n: int, side: str) -> float:
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 1.3
    vals = []
    for _, r in recent.iterrows():
        is_home = r["home_team"] == team
        if side == "for":
            vals.append(float(r["home_score"] if is_home else r["away_score"]))
        else:
            vals.append(float(r["away_score"] if is_home else r["home_score"]))
    return float(np.mean(vals))


def _venue_pts(hist: pd.DataFrame, team: str, before: pd.Timestamp,
               venue: str, n: int) -> float:
    if venue == "home":
        mask = (hist["home_team"] == team) & (hist["date"] < before)
    else:
        mask = (hist["away_team"] == team) & (hist["date"] < before)
    recent = hist[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = [3.0 if r["home_score"] > r["away_score"] and venue == "home"
           else (3.0 if r["away_score"] > r["home_score"] and venue == "away"
                 else (1.0 if r["home_score"] == r["away_score"] else 0.0))
           for _, r in recent.iterrows()]
    return float(np.mean(pts))


def _rest_days(hist: pd.DataFrame, team: str, before: pd.Timestamp) -> float:
    mask = ((hist["home_team"] == team) | (hist["away_team"] == team)) & (hist["date"] < before)
    prev = hist[mask]
    if prev.empty:
        return 14.0
    return float((before - prev["date"].max()).days)


def _h2h_wr(hist: pd.DataFrame, home: str, away: str,
             before: pd.Timestamp, n: int = 5) -> float:
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


def _domestic_midweek_density(hist: pd.DataFrame, team: str,
                               before: pd.Timestamp, days: int) -> int:
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


def _promoted_by_season(raw: pd.DataFrame) -> dict[str, set[str]]:
    seasons_sorted = sorted(raw["season"].unique())
    out: dict[str, set[str]] = {}
    prev: set[str] | None = None
    for s in seasons_sorted:
        cur = set(raw[raw["season"] == s]["home_team"]).union(
            raw[raw["season"] == s]["away_team"])
        out[s] = (cur - prev) if prev is not None else set()
        prev = cur
    return out


# --------------------------------------------------------------------------
# M3 / M4 feature builder (from 51_lgbm_challengers_v2.py)
# --------------------------------------------------------------------------

def _build_features_m3m4(
    slice_df: pd.DataFrame,
    hist_universe: pd.DataFrame,
    snap: dict,
    elo_lookup,
    promoted_map: dict[str, set[str]],
    include_midweek: bool,
) -> tuple[pd.DataFrame, np.ndarray]:
    rows, y_arr = [], []
    slice_df = slice_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    for _, r in slice_df.iterrows():
        home, away = r["home_team"], r["away_team"]
        date = pd.Timestamp(r["date"])
        season = r["season"]

        dc_params = snap.get(season)
        if dc_params is None or home not in dc_params.attack or away not in dc_params.attack:
            dc_p = {"p_home": 0.44, "p_draw": 0.26, "p_away": 0.30}
            dc_atk_h = dc_atk_a = dc_def_h = dc_def_a = 0.0
            dc_ha = 0.30; dc_unk = 1
        else:
            dc_p = dc.predict_match(home, away, dc_params)
            dc_atk_h = dc_params.attack.get(home, 0.0)
            dc_atk_a = dc_params.attack.get(away, 0.0)
            dc_def_h = dc_params.defence.get(home, 0.0)
            dc_def_a = dc_params.defence.get(away, 0.0)
            dc_ha = dc_params.home_adv; dc_unk = 0

        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh, ea = float(elo_row["elo_home_pre"]), float(elo_row["elo_away_pre"])
        except (KeyError, ValueError):
            eh = ea = elo_mod.ELO_DEFAULT
        ph_e, pd_e, pa_e = elo_mod.elo_win_probability(eh, ea, neutral=False)

        promoted_set = promoted_map.get(season, set())
        feat = {
            "dc_p_home": dc_p["p_home"], "dc_p_draw": dc_p["p_draw"], "dc_p_away": dc_p["p_away"],
            "dc_attack_home": dc_atk_h, "dc_defence_home": dc_def_h,
            "dc_attack_away": dc_atk_a, "dc_defence_away": dc_def_a,
            "dc_home_adv": dc_ha, "dc_unknown_pair": dc_unk,
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
            "is_promoted_home": 1 if home in promoted_set else 0,
            "is_promoted_away": 1 if away in promoted_set else 0,
        }
        if include_midweek:
            feat["domestic_midweek_density_14_home"] = _domestic_midweek_density(hist_universe, home, date, 14)
            feat["domestic_midweek_density_14_away"] = _domestic_midweek_density(hist_universe, away, date, 14)
            feat["domestic_midweek_density_7_home"] = _domestic_midweek_density(hist_universe, home, date, 7)
            feat["domestic_midweek_density_7_away"] = _domestic_midweek_density(hist_universe, away, date, 7)
        rows.append(feat)
        y_arr.append(int(r["y"]))
    return pd.DataFrame(rows), np.array(y_arr, dtype=np.int64)


def _run_lgbm_fold(model_name: str, include_midweek: bool,
                   outer: str, raw_dev: pd.DataFrame,
                   snap: dict, elo_lookup, promoted_map: dict,
                   eval_folds: list[str]) -> pd.DataFrame | None:
    """Run one outer fold for M3/M4."""
    if not _HAS_LGBM:
        return None
    val_df = raw_dev[raw_dev["season"] == outer].copy()
    train_df = raw_dev[raw_dev["date"] < val_df["date"].min()].copy()
    inner_seasons = sorted(train_df["season"].unique())
    if len(inner_seasons) < 2:
        X_tr, y_tr = _build_features_m3m4(train_df, train_df, snap, elo_lookup, promoted_map, include_midweek)
        X_val, y_val = _build_features_m3m4(val_df, train_df, snap, elo_lookup, promoted_map, include_midweek)
        X_val = X_val.reindex(columns=X_tr.columns, fill_value=0.0)
        m = lgb.LGBMClassifier(**_LGBM_NO_EARLY)
        m.fit(X_tr, y_tr)
    else:
        inner_val_season = inner_seasons[-1]
        train_inner = train_df[train_df["season"] != inner_val_season]
        train_iv = train_df[train_df["season"] == inner_val_season]
        X_tr, y_tr = _build_features_m3m4(train_inner, train_inner, snap, elo_lookup, promoted_map, include_midweek)
        X_iv, y_iv = _build_features_m3m4(train_iv, train_df, snap, elo_lookup, promoted_map, include_midweek)
        X_val, y_val = _build_features_m3m4(val_df, train_df, snap, elo_lookup, promoted_map, include_midweek)
        cols = X_tr.columns.tolist()
        X_iv = X_iv.reindex(columns=cols, fill_value=0.0)
        X_val = X_val.reindex(columns=cols, fill_value=0.0)
        m = lgb.LGBMClassifier(**_LGBM_PARAMS)
        m.fit(X_tr, y_tr, eval_set=[(X_iv, y_iv)],
              callbacks=[lgb.early_stopping(30, verbose=False)])

    probs = m.predict_proba(X_val)
    kv = val_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    b = metrics.brier(y_val, probs)
    print(f"  [{model_name}/{outer}] n_val={len(kv)} Brier={b:.4f}", flush=True)
    return pd.DataFrame({
        "season": outer, "date": kv["date"].values,
        "home_team": kv["home_team"].values, "away_team": kv["away_team"].values,
        "y": y_val,
        f"{model_name}_p_away": probs[:, 0],
        f"{model_name}_p_draw": probs[:, 1],
        f"{model_name}_p_home": probs[:, 2],
    })


def run_m3_m4(config: LeagueConfig, top5_root: Path) -> dict:
    """M3 (with domestic_midweek_density) and M4 (without) LGBM challengers."""
    if not _HAS_LGBM:
        print(f"[M3/M4/{config.key}] lightgbm not installed — skip", flush=True)
        return {}
    res = config.results_dir(top5_root)
    res.mkdir(parents=True, exist_ok=True)

    snap = art.load_snapshots(config, top5_root)
    elo_series = art.load_elo_series(config, top5_root)
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])[
        ["elo_home_pre", "elo_away_pre"]]

    dev_raw = partitions.load_development(config.raw_pkl(top5_root), config.dev_seasons)
    dev_raw["y"] = dev_raw.apply(
        lambda r: metrics.label_from_scores(r["home_score"], r["away_score"]), axis=1)
    dev_raw["date"] = pd.to_datetime(dev_raw["date"])
    dev_raw = dev_raw.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    promoted_map = _promoted_by_season(dev_raw)

    eval_folds = list(config.calib_seed_folds) + list(config.outer_folds)
    out: dict = {}
    for model_name, inc_mw in (("m4", False), ("m3", True)):
        print(f"\n=== {model_name.upper()} ({config.key}) ===", flush=True)
        fold_dfs = []
        for outer in eval_folds:
            df = _run_lgbm_fold(model_name, inc_mw, outer, dev_raw,
                                 snap, elo_lookup, promoted_map, eval_folds)
            if df is not None:
                fold_dfs.append(df)
        if fold_dfs:
            oof = pd.concat(fold_dfs, ignore_index=True)
            oof.to_csv(res / f"oof_{model_name}_dev_v2.csv", index=False)
            outer_oof = oof[oof["season"].isin(config.outer_folds)]
            y_all = outer_oof["y"].to_numpy()
            p_all = outer_oof[[f"{model_name}_p_away", f"{model_name}_p_draw",
                                f"{model_name}_p_home"]].to_numpy()
            b = metrics.brier(y_all, p_all)
            l = metrics.logloss(y_all, p_all)
            out[model_name] = {"league": config.key, "model": model_name.upper(),
                               "n": int(len(outer_oof)), "brier": b, "logloss": l}
            print(f"[{model_name.upper()}/{config.key}] outer Brier={b:.4f} n={len(outer_oof)}", flush=True)
    return out


# --------------------------------------------------------------------------
# M7 feature builder (from 16_m6_m7_market_aware.py)
# --------------------------------------------------------------------------

def _build_features_m7(slice_df: pd.DataFrame, hist_universe: pd.DataFrame,
                        snap: dict, elo_lookup,
                        promoted_map: dict[str, set[str]],
                        ) -> tuple[pd.DataFrame, np.ndarray, pd.DataFrame]:
    """Market + DC + Elo + rolling features. Returns (X, y, kept_df)."""
    kept, p_mkt_all = market.apply_policy(
        slice_df.sort_values(["date", "home_team"], kind="stable"))
    rows, y_arr = [], []
    for i, r in kept.iterrows():
        home, away = r["home_team"], r["away_team"]
        date = pd.Timestamp(r["date"])
        season = r["season"]

        dc_params = snap.get(season)
        if dc_params is None or home not in dc_params.attack or away not in dc_params.attack:
            dc_p = {"p_home": 0.44, "p_draw": 0.26, "p_away": 0.30}
            dc_atk_h = dc_atk_a = dc_def_h = dc_def_a = 0.0
        else:
            dc_p = dc.predict_match(home, away, dc_params)
            dc_atk_h = dc_params.attack.get(home, 0.0)
            dc_atk_a = dc_params.attack.get(away, 0.0)
            dc_def_h = dc_params.defence.get(home, 0.0)
            dc_def_a = dc_params.defence.get(away, 0.0)

        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh, ea = float(elo_row["elo_home_pre"]), float(elo_row["elo_away_pre"])
        except (KeyError, ValueError):
            eh = ea = elo_mod.ELO_DEFAULT
        ph_e, pd_e, pa_e = elo_mod.elo_win_probability(eh, ea, neutral=False)

        p_mkt = p_mkt_all[i]
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


def run_m7(config: LeagueConfig, top5_root: Path) -> dict:
    """M7 LGBM market residual model (outer_folds only)."""
    if not _HAS_LGBM:
        print(f"[M7/{config.key}] lightgbm not installed — skip", flush=True)
        return {}
    res = config.results_dir(top5_root)
    res.mkdir(parents=True, exist_ok=True)

    snap = art.load_snapshots(config, top5_root)
    elo_series = art.load_elo_series(config, top5_root)
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])[
        ["elo_home_pre", "elo_away_pre"]]

    dev = partitions.load_development_with_market(
        config.raw_pkl(top5_root), config.full_pkl(top5_root),
        config.dev_seasons, include_closing=False)
    dev["y"] = dev.apply(
        lambda r: metrics.label_from_scores(r["home_score"], r["away_score"]), axis=1)
    dev["date"] = pd.to_datetime(dev["date"])
    dev = dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    dev, _ = market.apply_policy(dev)
    promoted_map = _promoted_by_season(dev)

    oof_rows = []
    for outer in config.outer_folds:
        val_df = dev[dev["season"] == outer].copy()
        train_df = dev[dev["date"] < val_df["date"].min()].copy()
        inner_seasons = sorted(train_df["season"].unique())
        if len(inner_seasons) < 2:
            X_tr, y_tr, _ = _build_features_m7(train_df, train_df, snap, elo_lookup, promoted_map)
            X_val, y_val, kv = _build_features_m7(val_df, train_df, snap, elo_lookup, promoted_map)
            X_val = X_val.reindex(columns=X_tr.columns, fill_value=0.0)
            m = lgb.LGBMClassifier(**_LGBM_NO_EARLY)
            m.fit(X_tr, y_tr)
        else:
            inner_val_season = inner_seasons[-1]
            train_inner = train_df[train_df["season"] != inner_val_season]
            train_iv = train_df[train_df["season"] == inner_val_season]
            X_tr, y_tr, _ = _build_features_m7(train_inner, train_inner, snap, elo_lookup, promoted_map)
            X_iv, y_iv, _ = _build_features_m7(train_iv, train_df, snap, elo_lookup, promoted_map)
            X_val, y_val, kv = _build_features_m7(val_df, train_df, snap, elo_lookup, promoted_map)
            cols = X_tr.columns.tolist()
            X_iv = X_iv.reindex(columns=cols, fill_value=0.0)
            X_val = X_val.reindex(columns=cols, fill_value=0.0)
            m = lgb.LGBMClassifier(**_LGBM_PARAMS)
            m.fit(X_tr, y_tr, eval_set=[(X_iv, y_iv)],
                  callbacks=[lgb.early_stopping(30, verbose=False)])

        probs = m.predict_proba(X_val)
        kv = kv.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        b = metrics.brier(y_val, probs)
        print(f"  [M7/{config.key}/{outer}] n={len(kv)} Brier={b:.4f}", flush=True)
        oof_rows.append(pd.DataFrame({
            "season": outer, "date": kv["date"].values,
            "home_team": kv["home_team"].values, "away_team": kv["away_team"].values,
            "y": y_val,
            "m7_p_away": probs[:, 0], "m7_p_draw": probs[:, 1], "m7_p_home": probs[:, 2],
        }))

    oof = pd.concat(oof_rows, ignore_index=True)
    oof.to_csv(res / "oof_m7_dev_v3.csv", index=False)
    y_all = oof["y"].to_numpy()
    p_all = oof[["m7_p_away", "m7_p_draw", "m7_p_home"]].to_numpy()
    b7 = metrics.brier(y_all, p_all)
    l7 = metrics.logloss(y_all, p_all)
    print(f"[M7/{config.key}] Brier={b7:.4f} n={len(oof)}", flush=True)
    return {"league": config.key, "model": "M7_lgbm_residual",
            "n": int(len(oof)), "brier": b7, "logloss": l7}
