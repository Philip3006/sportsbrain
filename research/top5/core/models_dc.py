"""Top-5 — M1 (Dixon-Coles walk-forward) and M2 (standalone Elo).

Matches BL1 v7 11_walk_forward_v2.py:
  - M1 outer folds: calib_seed_folds + outer_folds (6 folds for BL1)
  - M2 outer folds: same 6 folds
  - DC snapshot[S] used for season S predictions
  - Elo series precomputed; elo_home_pre/away_pre from the series
  - Elo-within-fold: uses elo_home_pre from precomputed series (already as-of)

Outputs (per league):
  results/oof_m1_dev.csv         DC walk-forward OOF
  results/oof_m2_dev.csv         Elo standalone OOF
  results/fold_summary_m1m2.csv  per-fold metrics
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import artefacts as art
from . import dixon_coles as dc
from . import elo as elo_mod
from . import metrics
from . import partitions
from ..config import LeagueConfig


def _all_eval_folds(config: LeagueConfig) -> list[str]:
    """All folds used for M1/M2 OOF: calib seeds + outer folds."""
    return list(config.calib_seed_folds) + list(config.outer_folds)


def run_m1(config: LeagueConfig, top5_root: Path) -> dict:
    """DC walk-forward OOF over calib_seed_folds + outer_folds.

    Requires artefacts (phi, snapshots, elo_series) already generated.
    """
    res = config.results_dir(top5_root)
    res.mkdir(parents=True, exist_ok=True)

    snap = art.load_snapshots(config, top5_root)
    elo_series = art.load_elo_series(config, top5_root)
    dev_raw = partitions.load_development(config.raw_pkl(top5_root), config.dev_seasons)
    dev_raw["y"] = dev_raw.apply(
        lambda r: metrics.label_from_scores(r["home_score"], r["away_score"]), axis=1)
    dev_raw["date"] = pd.to_datetime(dev_raw["date"])

    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])[
        ["elo_home_pre", "elo_away_pre"]]

    eval_folds = _all_eval_folds(config)
    rows, fold_summary = [], []
    for fold in eval_folds:
        fold_df = dev_raw[dev_raw["season"] == fold].sort_values(
            ["date", "home_team"], kind="stable").reset_index(drop=True)
        y = fold_df["y"].to_numpy()
        p_dc = np.zeros((len(fold_df), 3))
        p_elo = np.zeros((len(fold_df), 3))

        for i, r in fold_df.iterrows():
            home, away = r["home_team"], r["away_team"]
            dc_params = snap.get(fold)
            if dc_params is None or home not in dc_params.attack or away not in dc_params.attack:
                # Base-rate fallback matching BL1 v7 11_walk_forward_v2.py
                p_dc[i] = [0.297, 0.253, 0.450]
            else:
                preds = dc.predict_match(home, away, dc_params)
                p_dc[i] = [preds["p_away"], preds["p_draw"], preds["p_home"]]
            try:
                elo_row = elo_lookup.loc[(pd.Timestamp(r["date"]), home, away)]
                eh, ea = float(elo_row["elo_home_pre"]), float(elo_row["elo_away_pre"])
            except (KeyError, ValueError):
                eh = ea = elo_mod.ELO_DEFAULT
            ph_e, pd_e, pa_e = elo_mod.elo_win_probability(eh, ea, neutral=False)
            p_elo[i] = [pa_e, pd_e, ph_e]

        b_dc = metrics.brier(y, p_dc)
        b_elo = metrics.brier(y, p_elo)
        l_dc = metrics.logloss(y, p_dc)
        fold_summary.append({"fold": fold, "n": len(fold_df),
                              "brier_m1_dc": b_dc, "brier_m2_elo": b_elo,
                              "logloss_m1_dc": l_dc})
        print(f"  M1/M2 {config.key} fold={fold}: n={len(fold_df)} "
              f"DC-Brier={b_dc:.4f} Elo-Brier={b_elo:.4f}", flush=True)
        for j, r in fold_df.iterrows():
            rows.append({
                "season": fold, "date": r["date"],
                "home_team": r["home_team"], "away_team": r["away_team"],
                "y": int(r["y"]),
                "home_score": int(r["home_score"]), "away_score": int(r["away_score"]),
                "m1_p_away": p_dc[j, 0], "m1_p_draw": p_dc[j, 1], "m1_p_home": p_dc[j, 2],
                "m2_p_away": p_elo[j, 0], "m2_p_draw": p_elo[j, 1], "m2_p_home": p_elo[j, 2],
            })

    oof = pd.DataFrame(rows)
    oof.to_csv(res / "oof_m1_dev.csv", index=False)
    pd.DataFrame(fold_summary).to_csv(res / "fold_summary_m1m2.csv", index=False)

    # Aggregate only on outer_folds (primary eval scope)
    outer_oof = oof[oof["season"].isin(config.outer_folds)]
    y_all = outer_oof["y"].to_numpy()
    p_dc_all = outer_oof[["m1_p_away", "m1_p_draw", "m1_p_home"]].to_numpy()
    b_m1 = metrics.brier(y_all, p_dc_all)
    l_m1 = metrics.logloss(y_all, p_dc_all)
    print(f"[M1/{config.key}] outer folds Brier={b_m1:.4f} n={len(outer_oof)}", flush=True)
    return {"league": config.key, "model": "M1_dc", "n": int(len(outer_oof)),
            "brier": b_m1, "logloss": l_m1}


def run_m2(config: LeagueConfig, top5_root: Path) -> dict:
    """Standalone Elo OOF — reads from oof_m1_dev.csv (shares artefacts)."""
    res = config.results_dir(top5_root)
    oof_m1 = pd.read_csv(res / "oof_m1_dev.csv", dtype={"season": str})
    outer_oof = oof_m1[oof_m1["season"].isin(config.outer_folds)]
    y_all = outer_oof["y"].to_numpy()
    p_elo_all = outer_oof[["m2_p_away", "m2_p_draw", "m2_p_home"]].to_numpy()
    b_m2 = metrics.brier(y_all, p_elo_all)
    l_m2 = metrics.logloss(y_all, p_elo_all)
    print(f"[M2/{config.key}] outer folds Brier={b_m2:.4f} n={len(outer_oof)}", flush=True)

    # Save standalone M2 OOF for downstream
    m2_oof = outer_oof[["season", "date", "home_team", "away_team", "y",
                          "m2_p_away", "m2_p_draw", "m2_p_home"]].copy()
    m2_oof.to_csv(res / "oof_m2_dev.csv", index=False)
    return {"league": config.key, "model": "M2_elo", "n": int(len(outer_oof)),
            "brier": b_m2, "logloss": l_m2}
