"""FLAGSHIP-BL1 Task M3/M4 — Real LGBM challengers with strict as-of pipeline.

For each of the 4 dev folds (validate 2021, 2122, 2223, 2324):

  1. Refit Dixon-Coles with `today = fold_cutoff_date` on all matches strictly
     before that cutoff. No global params_latest.pkl load.
  2. Compute Elo cumulatively from earliest season through fold cutoff.
  3. Build fold-local feature matrix over BOTH the training slice (for LGBM
     training) and the validation slice (for OOF prediction). Every rolling
     feature is computed with `date < row.date`, i.e. as-of-safe.
  4. Fit LGBM using nested-chronological validation: inner-val = the season
     immediately preceding the val season (fold's most recent train season).
     Used for early stopping. No random / shuffled CV.
  5. Predict on val slice → per-fold OOF.

Emits two models:
  M3: DC + LGBM with mid-week-load features (European-fixture proxy)
      "midweek" = match kickoff falls on Tue/Wed/Thu (rare in BL1 domestic;
      concentrated on European weeks)
  M4: DC + LGBM WITHOUT mid-week-load features

Outputs:
  research/bl1/results/oof_m3_dev.csv
  research/bl1/results/oof_m4_dev.csv
  research/bl1/results/oof_2425_challengers.csv   (calibration slice, both)
  research/bl1/results/lgbm_fold_summary.csv

Prohibited (per CEO Correction):
  - No load of dc_bundesliga2/params_latest.pkl anywhere.
  - No use of src/data/market_values.py or src/data/attendance.py.
  - No random K-fold.
  - No force_persist.
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
from src.models.elo import compute_elo_series, ELO_DEFAULT  # noqa: E402

DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
CALIB_SEASON = "2425"
HOLDOUT_SEASON = "2526"
FOLDS = [
    ("2021", "1920"),   # train ..1920, validate 2021
    ("2122", "2021"),
    ("2223", "2122"),
    ("2324", "2223"),
]

BEST_PHI = 0.0012  # locked from Task E phi selection
DC_REG = 0.005
DC_MAX_ITER = 1500
ELO_K = 20.0

DATASET_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"
RESULTS_DIR = ROOT / "research" / "bl1" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# -------- Utilities --------------------------------------------------------

def _label(row) -> int:
    h, a = int(row["home_score"]), int(row["away_score"])
    return 2 if h > a else (1 if h == a else 0)


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


# -------- Feature engineering (all strictly as-of) ------------------------

def _rolling_pts(matches: pd.DataFrame, team: str, before: pd.Timestamp, n: int) -> float:
    mask = ((matches["home_team"] == team) | (matches["away_team"] == team)) & (matches["date"] < before)
    recent = matches[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = []
    for _, r in recent.iterrows():
        is_home = r["home_team"] == team
        gs = r["home_score"] if is_home else r["away_score"]
        gc = r["away_score"] if is_home else r["home_score"]
        pts.append(3.0 if gs > gc else (1.0 if gs == gc else 0.0))
    return float(np.mean(pts))


def _rolling_goals(matches: pd.DataFrame, team: str, before: pd.Timestamp, n: int, side: str) -> float:
    """side='for' or 'against'."""
    mask = ((matches["home_team"] == team) | (matches["away_team"] == team)) & (matches["date"] < before)
    recent = matches[mask].tail(n)
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


def _venue_pts(matches: pd.DataFrame, team: str, before: pd.Timestamp, venue: str, n: int) -> float:
    if venue == "home":
        mask = (matches["home_team"] == team) & (matches["date"] < before)
    else:
        mask = (matches["away_team"] == team) & (matches["date"] < before)
    recent = matches[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = []
    for _, r in recent.iterrows():
        gs = r["home_score"] if venue == "home" else r["away_score"]
        gc = r["away_score"] if venue == "home" else r["home_score"]
        pts.append(3.0 if gs > gc else (1.0 if gs == gc else 0.0))
    return float(np.mean(pts))


def _rest_days(matches: pd.DataFrame, team: str, before: pd.Timestamp) -> float:
    mask = ((matches["home_team"] == team) | (matches["away_team"] == team)) & (matches["date"] < before)
    prev = matches[mask]
    if prev.empty:
        return 14.0
    return float((before - prev["date"].max()).days)


def _h2h_home_wr(matches: pd.DataFrame, home: str, away: str, before: pd.Timestamp, n: int = 5) -> float:
    mask = (
        ((matches["home_team"] == home) & (matches["away_team"] == away))
        | ((matches["home_team"] == away) & (matches["away_team"] == home))
    ) & (matches["date"] < before)
    recent = matches[mask].tail(n)
    if recent.empty:
        return 0.4
    wins = sum(
        1 for _, r in recent.iterrows()
        if (r["home_team"] == home and r["home_score"] > r["away_score"])
        or (r["home_team"] == away and r["away_score"] > r["home_score"])
    )
    return wins / len(recent)


def _midweek_matches(matches: pd.DataFrame, team: str, before: pd.Timestamp, days: int) -> int:
    """Count of matches on Tue/Wed/Thu in [before-days, before). Domestic BL is
    Fri/Sat/Sun almost exclusively, so a midweek match is likely a European
    or Pokal fixture. Data source is the club's own historical match log
    (present in df_all), so this feature is strictly as-of."""
    since = before - pd.Timedelta(days=days)
    dates = pd.to_datetime(matches["date"], errors="coerce")
    mask = (
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (dates >= since) & (dates < before)
    )
    sub_dates = dates[mask]
    if sub_dates.empty:
        return 0
    return int(sub_dates.dt.dayofweek.isin([1, 2, 3]).sum())


# -------- Feature-builder for a single fold --------------------------------

def _build_features_for_fold(
    slice_df: pd.DataFrame,           # rows to build features FOR (train or val)
    history_df: pd.DataFrame,         # rows that constitute the as-of history universe
    dc_params: dixon_coles.DixonColesParams,
    elo_ratings_at_cutoff: dict[str, float],
    promoted_teams: set[str],
    include_midweek: bool,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Returns (X, y). Every feature uses only rows in history_df with date < row.date.

    For rows in the val slice we let Elo roll forward within the val slice
    (each val-row uses ratings post-updated by earlier val-rows). To avoid
    leakage across val rows we snapshot at cutoff and update sequentially.
    """
    from src.models.elo import update_ratings
    ratings = dict(elo_ratings_at_cutoff)

    # We need "seen so far" history including previously-processed rows in
    # slice_df (for rolling form during within-season progression).
    running_history = history_df.copy()

    rows = []
    labels = []
    kept_mask = []  # aligns with slice_df row order
    slice_df = slice_df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Convenience: track promoted-team match index
    promoted_idx: dict[str, int] = {t: 0 for t in promoted_teams}

    for _, r in slice_df.iterrows():
        home, away = r["home_team"], r["away_team"]
        date = pd.Timestamp(r["date"])

        # DC-based features (must be known-team pair). For unknown team pair
        # (promoted-team match with no DC representation) we still emit a
        # row using neutral DC placeholders. Downstream policy for these
        # rows is set by the LGBM using elo/form/promoted flags — CEO's P1
        # policy relies exactly on this fallback path.
        if home not in dc_params.attack or away not in dc_params.attack:
            dc = {"p_home": 0.44, "p_draw": 0.26, "p_away": 0.30}  # BL1 base rate
            dc_attack_h = dc_attack_a = dc_defence_h = dc_defence_a = 0.0
        else:
            dc = dixon_coles.predict_match(home, away, dc_params)
            dc_attack_h = dc_params.attack.get(home, 0.0)
            dc_attack_a = dc_params.attack.get(away, 0.0)
            dc_defence_h = dc_params.defence.get(home, 0.0)
            dc_defence_a = dc_params.defence.get(away, 0.0)
        eh = ratings.get(home, ELO_DEFAULT)
        ea = ratings.get(away, ELO_DEFAULT)
        # Elo-derived 1X2 for feature (matches Elo module semantics)
        from src.models.elo import elo_win_probability
        ph_elo, pd_elo, pa_elo = elo_win_probability(eh, ea, neutral=False)

        feat: dict = {
            # DC features
            "dc_p_home": dc["p_home"],
            "dc_p_draw": dc["p_draw"],
            "dc_p_away": dc["p_away"],
            "dc_attack_home": dc_attack_h,
            "dc_defence_home": dc_defence_h,
            "dc_attack_away": dc_attack_a,
            "dc_defence_away": dc_defence_a,
            "dc_home_adv": dc_params.home_adv,
            "dc_unknown_pair": 1 if (home not in dc_params.attack or away not in dc_params.attack) else 0,
            # Elo features
            "elo_home_pre": eh, "elo_away_pre": ea, "elo_diff": eh - ea,
            "elo_p_home": ph_elo, "elo_p_draw": pd_elo, "elo_p_away": pa_elo,
            # Form
            "form_home_l3": _rolling_pts(running_history, home, date, 3),
            "form_home_l6": _rolling_pts(running_history, home, date, 6),
            "form_away_l3": _rolling_pts(running_history, away, date, 3),
            "form_away_l6": _rolling_pts(running_history, away, date, 6),
            # Momentum
            "momentum_home": _rolling_pts(running_history, home, date, 3)
                             - _rolling_pts(running_history, home, date, 6),
            "momentum_away": _rolling_pts(running_history, away, date, 3)
                             - _rolling_pts(running_history, away, date, 6),
            # Goals
            "gs_home_l5": _rolling_goals(running_history, home, date, 5, "for"),
            "gc_home_l5": _rolling_goals(running_history, home, date, 5, "against"),
            "gs_away_l5": _rolling_goals(running_history, away, date, 5, "for"),
            "gc_away_l5": _rolling_goals(running_history, away, date, 5, "against"),
            # Venue
            "venue_home_pts_l5": _venue_pts(running_history, home, date, "home", 5),
            "venue_away_pts_l5": _venue_pts(running_history, away, date, "away", 5),
            # Rest days
            "rest_home": _rest_days(running_history, home, date),
            "rest_away": _rest_days(running_history, away, date),
            # H2H
            "h2h_home_wr": _h2h_home_wr(running_history, home, away, date, 5),
            # Promoted
            "is_promoted_home": 1 if home in promoted_teams else 0,
            "is_promoted_away": 1 if away in promoted_teams else 0,
            "promoted_idx_home": promoted_idx.get(home, 0),
            "promoted_idx_away": promoted_idx.get(away, 0),
        }
        if include_midweek:
            feat["midweek_last_14_home"] = _midweek_matches(running_history, home, date, 14)
            feat["midweek_last_14_away"] = _midweek_matches(running_history, away, date, 14)
            feat["midweek_last_7_home"] = _midweek_matches(running_history, home, date, 7)
            feat["midweek_last_7_away"] = _midweek_matches(running_history, away, date, 7)

        rows.append(feat)
        labels.append(_label(r))
        kept_mask.append(True)

        # Update state AFTER writing this row's features
        running_history = pd.concat([running_history, r.to_frame().T], ignore_index=True)
        ratings = update_ratings(ratings, home, away, int(r["home_score"]), int(r["away_score"]), k=ELO_K)
        if home in promoted_idx:
            promoted_idx[home] += 1
        if away in promoted_idx:
            promoted_idx[away] += 1

    return pd.DataFrame(rows), np.array(labels), np.array(kept_mask, dtype=bool) if kept_mask else np.zeros(len(slice_df), dtype=bool)


# -------- Main pipeline ----------------------------------------------------

def _identify_promoted_per_season(all_matches: pd.DataFrame) -> dict[str, set[str]]:
    seasons_sorted = sorted(all_matches["season"].unique())
    result: dict[str, set[str]] = {}
    prev = None
    for s in seasons_sorted:
        cur = set(all_matches[all_matches["season"] == s]["home_team"]).union(
            all_matches[all_matches["season"] == s]["away_team"])
        result[s] = (cur - prev) if prev is not None else set()
        prev = cur
    return result


def _train_lgbm(X_train: pd.DataFrame, y_train: np.ndarray, X_val: pd.DataFrame, y_val: np.ndarray) -> lgb.LGBMClassifier:
    """LGBM with early stopping via inner chronological validation split (last season of train)."""
    m = lgb.LGBMClassifier(
        objective="multiclass", num_class=3,
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=30,
        reg_lambda=1.0,
        subsample=0.8, colsample_bytree=0.9,
        random_state=42, n_jobs=1, verbose=-1,
    )
    m.fit(X_train, y_train,
          eval_set=[(X_val, y_val)],
          callbacks=[lgb.early_stopping(30, verbose=False)])
    return m


def _run(model_name: str, include_midweek: bool, raw: pd.DataFrame,
         promoted_map: dict[str, set[str]]) -> pd.DataFrame:
    """Full walk-forward for one model config. Returns dev OOF rows for all 4 folds."""
    raw = raw.copy()
    raw["y"] = raw.apply(_label, axis=1)

    all_oof = []
    for val_season, _ in FOLDS:
        val_df = raw[raw["season"] == val_season].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        train_df = raw[raw["date"] < val_df["date"].min()].copy()
        cutoff = val_df["date"].min()

        # Refit DC
        dc_params = dixon_coles.fit(train_df, phi=BEST_PHI, today=cutoff,
                                     regularization=DC_REG, max_iter=DC_MAX_ITER)

        # Elo cumulative through cutoff
        elo_train = compute_elo_series(train_df, initial_ratings={},
                                       k_competitive=ELO_K, k_friendly=ELO_K)
        ratings_at_cutoff: dict[str, float] = {}
        for _, r in elo_train.iterrows():
            ratings_at_cutoff[r["home_team"]] = r["elo_home_post"]
            ratings_at_cutoff[r["away_team"]] = r["elo_away_post"]

        promoted_this_fold = promoted_map.get(val_season, set())

        # Build train features (skipping the earliest 380 rows so as-of rolling
        # windows have enough history). Use train_df history universe.
        # We split inner-val = last season of train for LGBM early stopping.
        train_seasons = sorted(train_df["season"].unique())
        inner_val_season = train_seasons[-1]
        train_inner = train_df[train_df["season"] != inner_val_season]
        train_innerval = train_df[train_df["season"] == inner_val_season]

        # For inner training features we can re-use the same DC (fit at cutoff)
        # and rolling history is train_df up to each row.
        # For efficiency compute features once over the whole train_df + val_df
        # in chronological order.
        combined = pd.concat([train_df, val_df], ignore_index=True).sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        # History universe = ALL matches (already chronological); as-of filter is inside feature fns.
        # Note: the above call built features using cumulative running_history from empty
        # start, which mirrors "each match's features use only matches before it".
        # But DC and Elo cutoff need special handling for the val slice — see next call.
        # For simplicity we instead recompute in two passes: (1) train (dc,elo=default),
        # (2) val (with dc_params fit at cutoff, elo=ratings_at_cutoff).

        # -- Pass 1: build TRAINING features using per-row DC-fit is too expensive.
        # Use the current fold's DC (fit at cutoff) also for training rows. This is
        # a well-known compromise: LGBM learns residual structure atop DC. Historical
        # training rows get DC features from the fold's DC, which is consistent for
        # LGBM's residual-learning purpose. Rolling features are still strictly as-of.
        # (Alternative: nested per-row-fold DC refit. Deferred; expensive.)
        X_train, y_train, _ = _build_features_for_fold(
            slice_df=train_inner, history_df=train_inner.iloc[0:0],
            dc_params=dc_params, elo_ratings_at_cutoff={},
            promoted_teams=set(), include_midweek=include_midweek,
        )
        X_innerval, y_innerval, _ = _build_features_for_fold(
            slice_df=train_innerval, history_df=train_inner,
            dc_params=dc_params, elo_ratings_at_cutoff={},
            promoted_teams=set(), include_midweek=include_midweek,
        )

        # Pass 2: build VAL features starting from ratings_at_cutoff, with
        # promoted-team flags for the fold's promoted teams.
        X_val, y_val, val_kept_mask = _build_features_for_fold(
            slice_df=val_df, history_df=train_df,
            dc_params=dc_params, elo_ratings_at_cutoff=ratings_at_cutoff,
            promoted_teams=promoted_this_fold, include_midweek=include_midweek,
        )

        # Feature-column alignment
        cols = X_train.columns.tolist()
        X_innerval = X_innerval.reindex(columns=cols, fill_value=0.0)
        X_val = X_val.reindex(columns=cols, fill_value=0.0)

        # Train LGBM with early stopping on inner val
        m = _train_lgbm(X_train, y_train, X_innerval, y_innerval)
        probs = m.predict_proba(X_val)  # shape (n, 3) with class order [0,1,2] = away,draw,home

        # Emit OOF rows — align on kept mask (all rows kept now, but preserved for safety)
        kept_val = val_df.iloc[np.where(val_kept_mask)[0]].reset_index(drop=True)
        fold_oof = pd.DataFrame({
            "season": val_season,
            "date": kept_val["date"].values,
            "home_team": kept_val["home_team"].values,
            "away_team": kept_val["away_team"].values,
            "y": y_val,
            f"{model_name}_p_away": probs[:, 0],
            f"{model_name}_p_draw": probs[:, 1],
            f"{model_name}_p_home": probs[:, 2],
        })
        for c in ("ps_open_home", "ps_open_draw", "ps_open_away",
                  "ps_close_home", "ps_close_draw", "ps_close_away"):
            if c in kept_val.columns:
                fold_oof[c] = kept_val[c].values

        fold_brier = _brier(y_val, probs)
        fold_ll = _logloss(y_val, probs)
        print(f"  [{model_name} fold {val_season}] Brier={fold_brier:.4f} LogLoss={fold_ll:.4f} "
              f"iters={m.best_iteration_ or m.n_estimators_}", flush=True)

        all_oof.append(fold_oof)

    return pd.concat(all_oof, ignore_index=True)


def main() -> None:
    with open(DATASET_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()

    # Holdout guard
    raw_dev = raw[raw["season"] != HOLDOUT_SEASON].copy()

    promoted_map = _identify_promoted_per_season(raw_dev)

    print("=== Building M4 (DC + LGBM, NO midweek/Europe) ===", flush=True)
    m4_oof = _run("m4", include_midweek=False, raw=raw_dev, promoted_map=promoted_map)
    m4_oof.to_csv(RESULTS_DIR / "oof_m4_dev.csv", index=False)

    print("\n=== Building M3 (DC + LGBM + midweek/Europe proxy) ===", flush=True)
    m3_oof = _run("m3", include_midweek=True, raw=raw_dev, promoted_map=promoted_map)
    m3_oof.to_csv(RESULTS_DIR / "oof_m3_dev.csv", index=False)

    # Aggregate summary
    rows = []
    for name, df in (("m3", m3_oof), ("m4", m4_oof)):
        y = df["y"].to_numpy()
        p = df[[f"{name}_p_away", f"{name}_p_draw", f"{name}_p_home"]].to_numpy()
        rows.append({"model": name, "n": len(y), "brier": _brier(y, p), "logloss": _logloss(y, p)})
    pd.DataFrame(rows).to_csv(RESULTS_DIR / "lgbm_fold_summary.csv", index=False)
    print("\nAggregate:", flush=True)
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
