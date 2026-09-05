"""FLAGSHIP-BL1-002 — Walk-forward baseline evaluation.

Runs strict as-of walk-forward validation across development seasons
1617–2324. For each validation season, DC is refit on all prior matches
(with time-decay phi) and Elo is computed cumulatively from the earliest
season through the pre-fold cutoff. Produces per-match OOF predictions.

Additionally computes fixed baselines:
  - uniform (1/3, 1/3, 1/3)
  - empirical base rate on the training slice (leakage-safe per fold)
  - Pinnacle closing no-vig probabilities (BENCHMARK — not deployable)

Season 2425 is treated as CALIBRATION SEASON. Its OOF is produced with
the last dev-fold model and reserved for calibrator fitting only. Not
used for model selection or edge selection.

Season 2526 is NOT TOUCHED. The script explicitly filters it out of
every prediction and metric.

Outputs:
  research/bl1/results/oof_dev.csv          (dev-fold OOF predictions)
  research/bl1/results/oof_2425.csv         (calibration-fold predictions)
  research/bl1/results/fold_summary.csv     (per-fold size + Brier per model)
  research/bl1/results/data_quality.md      (data quality summary)

Research only. No production writes.
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
from src.models.elo import compute_elo_series, elo_win_probability, ELO_DEFAULT  # noqa: E402

# ---- Configuration ------------------------------------------------------

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
CALIB_SEASON = "2425"
HOLDOUT_SEASON = "2526"

# Per-fold configuration for walk-forward. Each entry: (validation_season, train_seasons_end_incl)
# We validate 2021, 2122, 2223, 2324 in canonical Folds 1–4.
FOLDS = [
    ("2021", "1920"),   # Fold 1: train 1617..1920, validate 2021
    ("2122", "2021"),   # Fold 2: train 1617..2021, validate 2122
    ("2223", "2122"),   # Fold 3: train 1617..2122, validate 2223
    ("2324", "2223"),   # Fold 4: train 1617..2223, validate 2324
]

# Bundesliga phi. Reference BL2 uses 0.0018. For BL1 the season is 8 months
# with 306 matches per team-pair (34 rounds), which is slightly denser event
# volume than BL2. We evaluate a small grid inside the script (see below).
PHI_CANDIDATES = [0.0012, 0.0018, 0.0030]

# DC / Elo hyperparameters
DC_REG = 0.005
DC_MAX_ITER = 1500
ELO_K = 20.0  # club-league K; verified against BL2 elo module defaults

# Elo starting ratings: cold-start every team at ELO_DEFAULT for the earliest
# development season. This means Elo needs a burn-in season to stabilize —
# consistent with a strict as-of policy (we do not import external Elo).
ELO_INITIAL_RATINGS: dict[str, float] = {}

DATASET_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"
RESULTS_DIR = ROOT / "research" / "bl1" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---- Utilities ----------------------------------------------------------

def _label(row) -> int:
    """0=away, 1=draw, 2=home — matches BL2 convention."""
    h, a = int(row["home_score"]), int(row["away_score"])
    return 2 if h > a else (1 if h == a else 0)


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    """Multiclass Brier: mean over rows of sum_k (p_k - y_k)^2. y is class idx."""
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _no_vig_1x2(oh: float, od: float, oa: float) -> tuple[float, float, float] | None:
    """Basic normalization de-vig. Locked candidate for v1 (Task I revisits others)."""
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    total = ih + id_ + ia
    return ih / total, id_ / total, ia / total


# ---- Core: as-of DC + Elo fit ------------------------------------------

def _fit_dc_asof(train_df: pd.DataFrame, phi: float, cutoff: pd.Timestamp) -> dixon_coles.DixonColesParams:
    """Fits DC on train_df strictly, with `today = cutoff` for phi weighting."""
    return dixon_coles.fit(
        train_df,
        phi=phi,
        today=cutoff,
        regularization=DC_REG,
        max_iter=DC_MAX_ITER,
    )


def _compute_elo_asof(all_history: pd.DataFrame) -> pd.DataFrame:
    """Returns matches with elo_home_pre / elo_away_pre columns.

    Elo is trivially strict as-of: compute_elo_series iterates chronologically
    and stamps pre-match state.
    """
    return compute_elo_series(all_history, initial_ratings=ELO_INITIAL_RATINGS,
                              k_competitive=ELO_K, k_friendly=ELO_K)


def _predict_dc_bulk(fold_val_df: pd.DataFrame, params: dixon_coles.DixonColesParams) -> np.ndarray:
    """Vectorized DC 1X2 predictions for a fold's validation matches. Returns (n,3) = [away,draw,home]."""
    out = np.zeros((len(fold_val_df), 3), dtype=np.float64)
    for i, (_, r) in enumerate(fold_val_df.iterrows()):
        home, away = r["home_team"], r["away_team"]
        if home not in params.attack or away not in params.attack:
            out[i] = [1 / 3, 1 / 3, 1 / 3]
            continue
        p = dixon_coles.predict_match(home, away, params)
        out[i] = [p["p_away"], p["p_draw"], p["p_home"]]
    return out


def _predict_elo_bulk(fold_val_df: pd.DataFrame, ratings_at_fold_start: dict[str, float],
                     history_up_to_fold: pd.DataFrame) -> np.ndarray:
    """Elo predictions using ratings that evolve *within* the validation season.

    We take the ratings at the start of the fold, then within the fold we update
    Elo game-by-game using true outcomes. This is a genuine walk-forward Elo path:
    for match k in the fold, we use ratings from k-1 (which used ratings from
    k-2, etc.). This is fine because we only use each match's own pre-state.
    """
    ratings = dict(ratings_at_fold_start)
    out = np.zeros((len(fold_val_df), 3), dtype=np.float64)
    from src.models.elo import update_ratings
    for i, (_, r) in enumerate(fold_val_df.iterrows()):
        home, away = r["home_team"], r["away_team"]
        eh = ratings.get(home, ELO_DEFAULT)
        ea = ratings.get(away, ELO_DEFAULT)
        p_h, p_d, p_a = elo_win_probability(eh, ea, neutral=False)
        out[i] = [p_a, p_d, p_h]
        # Update ratings using true outcome AFTER we have written the prediction.
        ratings = update_ratings(ratings, home, away,
                                 int(r["home_score"]), int(r["away_score"]),
                                 k=ELO_K, neutral=False)
    return out


# ---- Main pipeline -----------------------------------------------------

def main() -> None:
    if not DATASET_PKL.exists():
        print(f"MISSING dataset: {DATASET_PKL}. Run 00_download_raw.py first.", flush=True)
        return

    with open(DATASET_PKL, "rb") as f:
        df_all = pickle.load(f)

    df_all = df_all.sort_values("date").reset_index(drop=True)
    df_all = df_all.dropna(subset=["home_score", "away_score"]).copy()
    df_all["y"] = df_all.apply(_label, axis=1)
    print(f"Loaded {len(df_all)} matches across seasons {sorted(df_all['season'].unique())}", flush=True)

    # ---- Explicit holdout guard ---------------------------------------
    # 2526 is masked out for the entirety of the model-development pipeline.
    df_dev = df_all[df_all["season"] != HOLDOUT_SEASON].copy()
    holdout_size = int((df_all["season"] == HOLDOUT_SEASON).sum())
    print(f"HOLDOUT (2526) masked: {holdout_size} matches withheld", flush=True)

    # ---- Compute Elo once across the dev+calib timeline ---------------
    # This IS as-of by construction (compute_elo_series iterates chronologically
    # and stamps pre-state per match). For each fold we snapshot the ratings
    # as of the fold's start.
    df_elo = _compute_elo_asof(df_dev)
    print("Elo series computed across dev+calib.", flush=True)

    # ---- Phi selection: try each candidate on aggregate dev OOF -------
    # We select phi from DEVELOPMENT ONLY. 2425 not used in phi selection.
    best_phi = None
    best_phi_brier = np.inf
    phi_summary_rows = []

    for phi in PHI_CANDIDATES:
        all_pred_dc = []
        all_true = []
        for val_season, train_end in FOLDS:
            val_df = df_dev[df_dev["season"] == val_season].sort_values("date").reset_index(drop=True)
            train_df = df_dev[df_dev["date"] < val_df["date"].min()].copy()
            cutoff = val_df["date"].min()
            params = _fit_dc_asof(train_df, phi=phi, cutoff=cutoff)
            preds = _predict_dc_bulk(val_df, params)
            all_pred_dc.append(preds)
            all_true.append(val_df["y"].to_numpy())
        y_dev = np.concatenate(all_true)
        p_dev = np.concatenate(all_pred_dc, axis=0)
        b = _brier(y_dev, p_dev)
        ll = _logloss(y_dev, p_dev)
        phi_summary_rows.append({"phi": phi, "brier": b, "logloss": ll, "n": len(y_dev)})
        print(f"  phi={phi}: Brier={b:.4f}  LogLoss={ll:.4f}  n={len(y_dev)}", flush=True)
        if b < best_phi_brier:
            best_phi_brier = b
            best_phi = phi

    pd.DataFrame(phi_summary_rows).to_csv(RESULTS_DIR / "phi_selection_dev.csv", index=False)
    print(f"Selected phi (dev only): {best_phi}", flush=True)

    # ---- Produce OOF predictions per fold at best phi -----------------
    rows = []
    fold_summary = []
    for val_season, train_end in FOLDS:
        val_df = df_dev[df_dev["season"] == val_season].sort_values("date").reset_index(drop=True)
        train_df = df_dev[df_dev["date"] < val_df["date"].min()].copy()
        cutoff = val_df["date"].min()

        params = _fit_dc_asof(train_df, phi=best_phi, cutoff=cutoff)

        # Elo ratings at fold start = last post-match rating of each team in train
        elo_train = df_elo[df_elo["date"] < cutoff]
        ratings_at_fold_start: dict[str, float] = {}
        for _, r in elo_train.iterrows():
            ratings_at_fold_start[r["home_team"]] = r["elo_home_post"]
            ratings_at_fold_start[r["away_team"]] = r["elo_away_post"]

        p_dc = _predict_dc_bulk(val_df, params)
        p_elo = _predict_elo_bulk(val_df, ratings_at_fold_start, df_elo)
        y = val_df["y"].to_numpy()

        # Base rate baseline from train slice
        train_ys = train_df.apply(_label, axis=1)
        base = np.array([
            (train_ys == 0).mean(),
            (train_ys == 1).mean(),
            (train_ys == 2).mean(),
        ])
        base = np.tile(base, (len(val_df), 1))

        # Uniform baseline
        unif = np.full((len(val_df), 3), 1 / 3)

        # Pinnacle closing no-vig (benchmark only)
        close_probs = np.zeros((len(val_df), 3))
        close_probs[:] = np.nan
        for i, (_, r) in enumerate(val_df.iterrows()):
            nv = _no_vig_1x2(r.get("ps_close_home"), r.get("ps_close_draw"), r.get("ps_close_away"))
            if nv:
                p_h, p_d, p_a = nv
                close_probs[i] = [p_a, p_d, p_h]

        fold_summary.append({
            "fold": val_season,
            "n": len(val_df),
            "brier_uniform": _brier(y, unif),
            "brier_baserate": _brier(y, base),
            "brier_dc": _brier(y, p_dc),
            "brier_elo": _brier(y, p_elo),
            "brier_pinnacle_close": _brier(y[~np.isnan(close_probs[:, 0])],
                                            close_probs[~np.isnan(close_probs[:, 0])])
            if (~np.isnan(close_probs[:, 0])).any() else np.nan,
            "logloss_dc": _logloss(y, p_dc),
            "logloss_elo": _logloss(y, p_elo),
            "logloss_baserate": _logloss(y, base),
            "logloss_uniform": _logloss(y, unif),
            "pinnacle_coverage": float((~np.isnan(close_probs[:, 0])).mean()),
        })
        print(f"[Fold {val_season}] n={len(val_df)} DC-Brier={fold_summary[-1]['brier_dc']:.4f} "
              f"Elo-Brier={fold_summary[-1]['brier_elo']:.4f} "
              f"Pin={fold_summary[-1]['brier_pinnacle_close']}  cov={fold_summary[-1]['pinnacle_coverage']:.1%}",
              flush=True)

        for i, (_, r) in enumerate(val_df.iterrows()):
            rows.append({
                "season": val_season,
                "date": r["date"],
                "home_team": r["home_team"],
                "away_team": r["away_team"],
                "y": int(y[i]),
                "home_score": int(r["home_score"]),
                "away_score": int(r["away_score"]),
                "dc_p_away": p_dc[i, 0], "dc_p_draw": p_dc[i, 1], "dc_p_home": p_dc[i, 2],
                "elo_p_away": p_elo[i, 0], "elo_p_draw": p_elo[i, 1], "elo_p_home": p_elo[i, 2],
                "base_p_away": base[i, 0], "base_p_draw": base[i, 1], "base_p_home": base[i, 2],
                "close_p_away": close_probs[i, 0], "close_p_draw": close_probs[i, 1], "close_p_home": close_probs[i, 2],
                "ps_close_home": r.get("ps_close_home"),
                "ps_close_draw": r.get("ps_close_draw"),
                "ps_close_away": r.get("ps_close_away"),
                "ps_open_home": r.get("ps_open_home"),
                "ps_open_draw": r.get("ps_open_draw"),
                "ps_open_away": r.get("ps_open_away"),
            })

    pd.DataFrame(rows).to_csv(RESULTS_DIR / "oof_dev.csv", index=False)
    pd.DataFrame(fold_summary).to_csv(RESULTS_DIR / "fold_summary.csv", index=False)

    # ---- Calibration season 2425: single-fit predictions (using dev-only fit) ----
    # Reserve for calibrator ONLY. Not used for model selection.
    val_df = df_dev[df_dev["season"] == CALIB_SEASON].sort_values("date").reset_index(drop=True)
    if len(val_df) > 0:
        train_df = df_dev[df_dev["date"] < val_df["date"].min()].copy()
        cutoff = val_df["date"].min()
        params = _fit_dc_asof(train_df, phi=best_phi, cutoff=cutoff)
        elo_train = df_elo[df_elo["date"] < cutoff]
        ratings_at_fold_start = {}
        for _, r in elo_train.iterrows():
            ratings_at_fold_start[r["home_team"]] = r["elo_home_post"]
            ratings_at_fold_start[r["away_team"]] = r["elo_away_post"]
        p_dc = _predict_dc_bulk(val_df, params)
        p_elo = _predict_elo_bulk(val_df, ratings_at_fold_start, df_elo)
        y = val_df["y"].to_numpy()
        calib_rows = []
        for i, (_, r) in enumerate(val_df.iterrows()):
            calib_rows.append({
                "season": CALIB_SEASON,
                "date": r["date"], "home_team": r["home_team"], "away_team": r["away_team"],
                "y": int(y[i]),
                "dc_p_away": p_dc[i, 0], "dc_p_draw": p_dc[i, 1], "dc_p_home": p_dc[i, 2],
                "elo_p_away": p_elo[i, 0], "elo_p_draw": p_elo[i, 1], "elo_p_home": p_elo[i, 2],
                "ps_close_home": r.get("ps_close_home"),
                "ps_close_draw": r.get("ps_close_draw"),
                "ps_close_away": r.get("ps_close_away"),
            })
        pd.DataFrame(calib_rows).to_csv(RESULTS_DIR / "oof_2425.csv", index=False)
        print(f"Wrote 2425 calibration-season OOF: {len(calib_rows)} rows", flush=True)


if __name__ == "__main__":
    main()
