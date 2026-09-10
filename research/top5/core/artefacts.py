"""Top-5 — Research artefact generation: DC snapshots + Elo series.

Produces and caches precomputed artefacts consumed by M1, M2, M3, M4, M7.
All fits are strictly causal: DC snapshot[S] uses matches with season < S,
cutoff = min(date in S) derived from the actual data.

Matches BL1 v7 11_walk_forward_v2.py parameters:
  BEST_PHI = 0.0012, DC_REG = 0.005, DC_MAX_ITER = 1500, ELO_K = 20
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from . import dixon_coles as dc
from . import elo as elo_mod
from . import metrics
from . import partitions
from ..config import LeagueConfig

PHI_CANDIDATES = [0.0012, 0.0018, 0.0030]
DC_REG = 0.005
DC_MAX_ITER = 1500
ELO_K = 20.0


def _snap_dir(config: LeagueConfig, top5_root: Path) -> Path:
    d = config.results_dir(top5_root) / "dc_snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _elo_pkl(config: LeagueConfig, top5_root: Path) -> Path:
    return config.results_dir(top5_root) / "elo_series_dev.pkl"


def _phi_csv(config: LeagueConfig, top5_root: Path) -> Path:
    return config.results_dir(top5_root) / "phi_selection_dev.csv"


def load_snapshots(config: LeagueConfig, top5_root: Path) -> dict:
    snap = {}
    for pkl in sorted(_snap_dir(config, top5_root).glob("dc_*.pkl")):
        s = pkl.stem.split("_")[1]
        with open(pkl, "rb") as f:
            snap[s] = pickle.load(f)
    return snap


def load_elo_series(config: LeagueConfig, top5_root: Path) -> pd.DataFrame:
    with open(_elo_pkl(config, top5_root), "rb") as f:
        es = pickle.load(f)
    es["date"] = pd.to_datetime(es["date"])
    return es


def select_phi(config: LeagueConfig, top5_root: Path,
               dev_raw: pd.DataFrame, force: bool = False) -> float:
    """Select DC phi on DEV outer folds. Saves phi_selection_dev.csv.

    Uses config.outer_folds (4-fold chronological OOF) for phi selection,
    matching BL1 v7 10_walk_forward_baselines.py.
    """
    csv = _phi_csv(config, top5_root)
    if csv.exists() and not force:
        df = pd.read_csv(csv)
        best = float(df.loc[df["brier"].idxmin(), "phi"])
        print(f"[artefacts/{config.key}] phi={best} (from cache)", flush=True)
        return best

    config.results_dir(top5_root).mkdir(parents=True, exist_ok=True)
    dev_raw["y"] = dev_raw.apply(
        lambda r: metrics.label_from_scores(r["home_score"], r["away_score"]), axis=1)

    rows = []
    best_phi, best_brier = PHI_CANDIDATES[0], np.inf
    for phi in PHI_CANDIDATES:
        all_y, all_p = [], []
        for val_season in config.outer_folds:
            val_df = dev_raw[dev_raw["season"] == val_season].sort_values(
                ["date", "home_team"], kind="stable").reset_index(drop=True)
            if val_df.empty:
                continue
            cutoff = val_df["date"].min()
            train_df = dev_raw[dev_raw["date"] < cutoff].copy()
            if len(train_df) < 100:
                continue
            params = dc.fit(train_df, phi=phi, today=cutoff,
                            regularization=DC_REG, max_iter=DC_MAX_ITER)
            for _, r in val_df.iterrows():
                p = dc.predict_match(r["home_team"], r["away_team"], params)
                all_p.append([p["p_away"], p["p_draw"], p["p_home"]])
                all_y.append(int(r["y"]))
        if not all_y:
            continue
        b = metrics.brier(np.array(all_y), np.array(all_p))
        ll = metrics.logloss(np.array(all_y), np.array(all_p))
        rows.append({"phi": phi, "brier": b, "logloss": ll, "n": len(all_y)})
        print(f"  [phi_select/{config.key}] phi={phi}: Brier={b:.4f} n={len(all_y)}", flush=True)
        if b < best_brier:
            best_brier = b; best_phi = phi

    pd.DataFrame(rows).to_csv(csv, index=False)
    print(f"[artefacts/{config.key}] selected phi={best_phi}", flush=True)
    return best_phi


def build_dc_snapshots(config: LeagueConfig, top5_root: Path,
                       dev_raw: pd.DataFrame, phi: float,
                       force: bool = False) -> dict:
    """Fit DC snapshot[S] = fit on matches with season < S, today = min_date(S).

    Builds snapshots for all dev_seasons + calibration_season.
    Saves each to dc_snapshots/dc_{season}.pkl.
    """
    snap_dir = _snap_dir(config, top5_root)
    snap: dict = {}
    all_seasons = list(config.dev_seasons) + [config.calibration_season]

    season_starts = partitions.compute_season_starts_from_data(
        config.raw_pkl(top5_root), tuple(all_seasons))

    for s in all_seasons:
        pkl_path = snap_dir / f"dc_{s}.pkl"
        if pkl_path.exists() and not force:
            with open(pkl_path, "rb") as f:
                snap[s] = pickle.load(f)
            continue
        prior = dev_raw[dev_raw["season"] < s]
        if len(prior) < 100:
            print(f"[dc_snap/{config.key}] {s}: only {len(prior)} prior — skip", flush=True)
            continue
        today = season_starts.get(s)
        if today is None:
            today = pd.Timestamp(dev_raw[dev_raw["season"] == s]["date"].min())
        params = dc.fit(prior, phi=phi, today=today,
                        regularization=DC_REG, max_iter=DC_MAX_ITER)
        snap[s] = params
        with open(pkl_path, "wb") as f:
            pickle.dump(params, f)
        print(f"[dc_snap/{config.key}] {s}: fit on {len(prior)} matches, "
              f"today={today.date()}, teams={len(params.attack)}", flush=True)
    return snap


def build_elo_series(config: LeagueConfig, top5_root: Path,
                     dev_raw: pd.DataFrame, force: bool = False) -> pd.DataFrame:
    """Cumulative Elo series over all dev+calib matches. Saves to elo_series_dev.pkl."""
    pkl_path = _elo_pkl(config, top5_root)
    if pkl_path.exists() and not force:
        return load_elo_series(config, top5_root)

    config.results_dir(top5_root).mkdir(parents=True, exist_ok=True)
    es = elo_mod.compute_elo_series(
        dev_raw[["date", "home_team", "away_team", "home_score", "away_score"]],
        k_competitive=ELO_K, k_friendly=ELO_K, initial_ratings={})
    es = es.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    with open(pkl_path, "wb") as f:
        pickle.dump(es, f)
    print(f"[elo/{config.key}] Elo series: {len(es)} rows", flush=True)
    return es


def generate_artefacts(config: LeagueConfig, top5_root: Path,
                       force: bool = False) -> tuple[float, dict, pd.DataFrame]:
    """Full artefact pipeline: phi selection → DC snapshots → Elo series.

    Returns (best_phi, snap, elo_series).
    """
    dev_raw = partitions.load_development(config.raw_pkl(top5_root), config.dev_seasons)
    # Include calibration season for DC snapshot (never for Elo since same as dev)
    calib_raw = _read_pkl_season(config.raw_pkl(top5_root), config.calibration_season)
    dev_all = pd.concat([dev_raw, calib_raw], ignore_index=True)
    dev_all = dev_all.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    phi = select_phi(config, top5_root, dev_raw.copy(), force=force)
    snap = build_dc_snapshots(config, top5_root, dev_all, phi, force=force)
    elo_series = build_elo_series(config, top5_root, dev_all, force=force)
    return phi, snap, elo_series


def _read_pkl_season(pkl_path: Path, season: str) -> pd.DataFrame:
    raw = partitions._read_pkl(pkl_path)
    df = raw[raw["season"] == season].dropna(subset=["home_score", "away_score"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    return df
