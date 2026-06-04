"""
Builds feature matrix from historical matches and trains LightGBM.
Saves model + calibrators to models/lgbm/.
Run: python scripts/train_lgbm.py [--since YYYY-MM-DD] [--val-since YYYY-MM-DD]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR
from src.data.betexplorer import load_odds_lookup
from src.data.football_data_intl import fetch_wc_odds
from src.data.international import fetch_international_results, filter_competitive, filter_since, filter_minnow_qualifiers
from src.ensemble.calibration import (
    brier_score_multiclass,
    expected_calibration_error,
    fit_isotonic,
    save_calibrators,
)
from src.features.builder import build_training_matrix
from src.models import dixon_coles as dc
from src.models import lgbm_model
from src.models.elo import compute_elo_series


def main(since: str = "2018-01-01", val_since: str = "2023-01-01"):
    print("Loading data...")
    all_matches = filter_competitive(fetch_international_results())
    matches = filter_since(all_matches, since)
    print(f"  {len(matches)} competitive matches since {since}")

    print("Computing Elo series...")
    # Use full history for Elo warm-up, but only predict on matches since 'since'
    elo_series = compute_elo_series(all_matches)

    print("Applying qualifier filter (removes minnow-mismatch games from GBT training)...")
    matches = filter_minnow_qualifiers(matches, elo_series)

    print("Loading DC model snapshot...")
    snap_dir = MODELS_DIR / "dixon_coles"
    snaps = sorted(snap_dir.glob("params_*.pkl"))
    if not snaps:
        raise RuntimeError("No DC model found. Run: python scripts/train_dixon_coles.py")
    dc_params = dc.load(snaps[-1])
    dc_snapshot_map = {dc_params.fit_date: dc_params}

    print("Loading tournament odds for market-implied features...")
    wc_odds = fetch_wc_odds()
    be_odds = load_odds_lookup()
    if not be_odds.empty:
        be_odds = be_odds[~be_odds["tournament"].isin(["WC2018", "WC2022"])]
    frames = [df for df in [wc_odds, be_odds] if not df.empty]
    odds_lookup = pd.concat(frames, ignore_index=True) if frames else None
    if odds_lookup is not None:
        print(f"  {len(odds_lookup)} matches with market odds loaded "
              f"({odds_lookup['tournament'].nunique()} tournaments)")

    print("Building feature matrix (this may take a few minutes)...")
    X, y = build_training_matrix(matches, all_matches, elo_series, dc_snapshot_map,
                                 odds_lookup=odds_lookup)
    print(f"  Features: {X.shape[1]}, Samples: {len(X)}")

    # Train / validation split
    train_mask = matches["date"].values < pd.Timestamp(val_since)
    X_train, y_train = X[train_mask], y[train_mask]
    X_val, y_val = X[~train_mask], y[~train_mask]
    print(f"  Train: {len(X_train)}, Val: {len(X_val)}")

    # Fill NaN (teams with no history → 0)
    X_train = X_train.fillna(0.0)
    X_val = X_val.fillna(0.0)

    print("Training LightGBM...")
    model = lgbm_model.train(
        X_train, y_train,
        eval_set=(X_val, y_val),
        early_stopping_rounds=50,
    )

    # Evaluate
    val_probs = lgbm_model.predict_proba(model, X_val)
    brier = brier_score_multiclass(val_probs, y_val.values)
    ece = expected_calibration_error(val_probs, y_val.values)
    print(f"\n  Validation Brier score: {brier:.4f}  (lower = better)")
    print(f"  ECE (pre-calibration):  {ece:.4f}  (target < 0.05)")

    # Isotonic calibration
    print("Fitting isotonic calibrators...")
    calibrators = [fit_isotonic(val_probs, y_val.values, i) for i in range(3)]

    from src.ensemble.calibration import calibrate
    cal_probs = calibrate(val_probs, calibrators)
    ece_cal = expected_calibration_error(cal_probs, y_val.values)
    brier_cal = brier_score_multiclass(cal_probs, y_val.values)
    print(f"  Brier score (calibrated): {brier_cal:.4f}")
    print(f"  ECE (calibrated):         {ece_cal:.4f}", end="")
    print(" ✅" if ece_cal < 0.05 else " ⚠️  still above 0.05 threshold")

    # Top SHAP features
    print("\nTop features by SHAP:")
    shap_df = lgbm_model.shap_explain(model, X_val.head(200))
    for _, row in shap_df.head(10).iterrows():
        print(f"  {row['feature']:35s} {row['mean_abs_shap']:.4f}")

    # Save
    out_dir = MODELS_DIR / "lgbm"
    out_dir.mkdir(parents=True, exist_ok=True)
    lgbm_model.save_model(model, out_dir / "model.pkl")
    save_calibrators(calibrators, out_dir / "calibrators.pkl")

    # Save feature column order (needed for inference alignment)
    feature_cols = list(X.columns)
    import json
    (out_dir / "feature_columns.json").write_text(json.dumps(feature_cols))

    print(f"\nSaved model:       {out_dir / 'model.pkl'}")
    print(f"Saved calibrators: {out_dir / 'calibrators.pkl'}")
    print(f"Feature columns:   {out_dir / 'feature_columns.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2018-01-01")
    parser.add_argument("--val-since", default="2023-01-01")
    args = parser.parse_args()
    main(since=args.since, val_since=args.val_since)
