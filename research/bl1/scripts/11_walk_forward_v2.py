"""FLAGSHIP-BL1 CORRECTED — Walk-forward v2 (strict as-of, extended folds).

CHANGES vs 10_walk_forward_baselines.py:
- Extended to 6 outer folds: 1819, 1920, 2021, 2122, 2223, 2324
  (1819 + 1920 exist only to seed chronological calibrators for later folds)
- DC season-start snapshots: DC_snapshot[S] fit on all matches with
  season < S, today = start_of_S. 10 snapshots total. Every row in season S
  uses DC_snapshot[S].
- Elo state: single cumulative compute_elo_series() over full dev+calib
  chronologically. Each row's elo_home_pre / elo_away_pre stamped by the
  iteration itself. No per-fold re-init.
- 2526 holdout still excluded from every training and every prediction.

Outputs:
  research/bl1/results/dc_snapshots/dc_{season}.pkl
  research/bl1/results/oof_dev_v2.csv       (dev folds 1819..2324)
  research/bl1/results/oof_2425_v2.csv      (calib-season predictions,
                                              still reserved for final fit)
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.models import dixon_coles  # noqa: E402
from src.models.elo import compute_elo_series, elo_win_probability, ELO_DEFAULT, update_ratings  # noqa: E402

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
CALIB_SEASON = "2425"
HOLDOUT_SEASON = "2526"
OUTER_FOLDS = ["1819", "1920", "2021", "2122", "2223", "2324"]

BEST_PHI = 0.0012
DC_REG = 0.005
DC_MAX_ITER = 1500
ELO_K = 20.0

DATASET_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
RES = ROOT / "research" / "bl1" / "results"
SNAP_DIR = RES / "dc_snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)


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


def _build_dc_snapshots(df_all_dev: pd.DataFrame) -> dict[str, dixon_coles.DixonColesParams]:
    """One DC fit per season boundary. snapshot[S] = fit on rows with season < S,
    with today = min(date in S). Returned dict maps season → params.

    We include CALIB_SEASON ('2425') so that predictions on 2425 use a snapshot
    fit strictly on earlier seasons. Never uses 2526.
    """
    snapshots: dict[str, dixon_coles.DixonColesParams] = {}
    all_seasons = DEV_SEASONS + [CALIB_SEASON]
    for s in all_seasons:
        prior = df_all_dev[df_all_dev["season"] < s]
        if len(prior) < 100:
            print(f"[dc_snapshot] {s}: only {len(prior)} prior matches — skip (too small)", flush=True)
            continue
        today = df_all_dev[df_all_dev["season"] == s]["date"].min()
        params = dixon_coles.fit(prior, phi=BEST_PHI, today=today,
                                  regularization=DC_REG, max_iter=DC_MAX_ITER)
        snapshots[s] = params
        with open(SNAP_DIR / f"dc_{s}.pkl", "wb") as f:
            pickle.dump(params, f)
        print(f"[dc_snapshot] {s}: fit on {len(prior)} matches, today={today.date()}, teams={len(params.attack)}",
              flush=True)
    return snapshots


def _predict_dc_row(row, snap: dict) -> np.ndarray:
    """[p_away, p_draw, p_home] using season-appropriate snapshot."""
    s = row["season"]
    if s not in snap:
        return np.array([0.297, 0.253, 0.450])  # base rate fallback
    p = snap[s]
    home, away = row["home_team"], row["away_team"]
    if home not in p.attack or away not in p.attack:
        return np.array([0.297, 0.253, 0.450])
    d = dixon_coles.predict_match(home, away, p)
    return np.array([d["p_away"], d["p_draw"], d["p_home"]])


def main() -> None:
    with open(DATASET_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()
    raw["y"] = raw.apply(_label, axis=1)
    raw = raw.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Development + calibration slice (2526 held out)
    df_dev = raw[raw["season"] != HOLDOUT_SEASON].copy()
    print(f"Loaded {len(df_dev)} dev+calib matches. 2526 holdout: "
          f"{(raw['season'] == HOLDOUT_SEASON).sum()} masked", flush=True)

    # Precompute Elo cumulative through dev+calib
    df_elo = compute_elo_series(df_dev, initial_ratings={}, k_competitive=ELO_K, k_friendly=ELO_K)
    df_elo = df_elo.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    # Save for downstream consumers
    with open(RES / "elo_series_dev.pkl", "wb") as f:
        pickle.dump(df_elo, f)
    print(f"Elo series computed ({len(df_elo)} rows).", flush=True)

    # Build DC snapshots
    snap = _build_dc_snapshots(df_dev)

    # OOF for outer folds 1819..2324 (dev) + 2425 (reserved for calibrator fit)
    rows = []
    for fold in OUTER_FOLDS:
        fold_df = df_elo[df_elo["season"] == fold].reset_index(drop=True)
        for _, r in fold_df.iterrows():
            p_dc = _predict_dc_row(r, snap)
            eh, ea = r["elo_home_pre"], r["elo_away_pre"]
            ph_elo, pd_elo, pa_elo = elo_win_probability(eh, ea, neutral=False)
            rows.append({
                "season": fold, "date": r["date"],
                "home_team": r["home_team"], "away_team": r["away_team"],
                "y": int(r["y"]),
                "home_score": int(r["home_score"]), "away_score": int(r["away_score"]),
                "dc_p_away": p_dc[0], "dc_p_draw": p_dc[1], "dc_p_home": p_dc[2],
                "elo_p_away": pa_elo, "elo_p_draw": pd_elo, "elo_p_home": ph_elo,
                "elo_home_pre": eh, "elo_away_pre": ea,
                "ps_open_home": r.get("ps_open_home"), "ps_open_draw": r.get("ps_open_draw"),
                "ps_open_away": r.get("ps_open_away"),
                "ps_close_home": r.get("ps_close_home"), "ps_close_draw": r.get("ps_close_draw"),
                "ps_close_away": r.get("ps_close_away"),
            })
    dev = pd.DataFrame(rows)
    dev.to_csv(RES / "oof_dev_v2.csv", index=False)
    print(f"Wrote {len(dev)} rows to oof_dev_v2.csv (folds {OUTER_FOLDS})", flush=True)

    # 2425 predictions (reserved for calibrator fit only)
    calib_df = df_elo[df_elo["season"] == CALIB_SEASON].reset_index(drop=True)
    rows_c = []
    for _, r in calib_df.iterrows():
        p_dc = _predict_dc_row(r, snap)
        eh, ea = r["elo_home_pre"], r["elo_away_pre"]
        ph_elo, pd_elo, pa_elo = elo_win_probability(eh, ea, neutral=False)
        rows_c.append({
            "season": CALIB_SEASON, "date": r["date"],
            "home_team": r["home_team"], "away_team": r["away_team"],
            "y": int(r["y"]),
            "dc_p_away": p_dc[0], "dc_p_draw": p_dc[1], "dc_p_home": p_dc[2],
            "elo_p_away": pa_elo, "elo_p_draw": pd_elo, "elo_p_home": ph_elo,
            "ps_open_home": r.get("ps_open_home"), "ps_open_draw": r.get("ps_open_draw"),
            "ps_open_away": r.get("ps_open_away"),
            "ps_close_home": r.get("ps_close_home"), "ps_close_draw": r.get("ps_close_draw"),
            "ps_close_away": r.get("ps_close_away"),
        })
    pd.DataFrame(rows_c).to_csv(RES / "oof_2425_v2.csv", index=False)
    print(f"Wrote {len(rows_c)} rows to oof_2425_v2.csv (reserved for calibrator fit)", flush=True)

    # Aggregate summary (uncalibrated)
    for name, cols in (("DC", ["dc_p_away", "dc_p_draw", "dc_p_home"]),
                        ("Elo", ["elo_p_away", "elo_p_draw", "elo_p_home"])):
        p = dev[cols].to_numpy()
        y = dev["y"].to_numpy()
        print(f"[{name} uncalibrated] n={len(y)} Brier={_brier(y, p):.4f} LogLoss={_logloss(y, p):.4f}",
              flush=True)


if __name__ == "__main__":
    main()
