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
from src.data.international import (
    compute_sample_weights,
    fetch_international_results,
    filter_competitive,
    filter_minnow_qualifiers,
    filter_since,
    is_wc2022,
)
from src.ensemble.calibration import (
    brier_score_multiclass,
    expected_calibration_error,
    fit_isotonic,
    save_calibrators,
)
from src.ensemble.combiner import find_optimal_weight
from src.features.builder import build_training_matrix
from src.models import dixon_coles as dc
from src.models import lgbm_model
from src.models.elo import compute_elo_series


def main(
    since: str = "2018-01-01",
    val_since: str = "2023-01-01",
    ensemble_holdout: bool = True,
    gate_min_improvement: float = 0.01,
):
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
    # Key with epoch so DC features cover all training matches.
    # build_training_matrix uses "latest snapshot <= match_date"; keying with
    # epoch ensures the current model is available for every historical match.
    dc_snapshot_map = {pd.Timestamp("2000-01-01"): dc_params}

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

    print("Loading StatsBomb xG data...")
    try:
        from src.data.statsbomb import fetch_statsbomb_xg
        statsbomb_xg = fetch_statsbomb_xg()
        print(f"  {len(statsbomb_xg)} xG match records loaded")
    except Exception as e:
        print(f"  Warning: StatsBomb xG not available ({e}) — skipping xG features")
        statsbomb_xg = None

    print("Building feature matrix (this may take a few minutes)...")
    X, y = build_training_matrix(matches, all_matches, elo_series, dc_snapshot_map,
                                 odds_lookup=odds_lookup, statsbomb_xg=statsbomb_xg)
    print(f"  Features: {X.shape[1]}, Samples: {len(X)}")

    # WC2022 ensemble holdout — pulled out before train/val split so the gate is honest.
    wc2022_mask = is_wc2022(matches).values if ensemble_holdout else np.zeros(len(matches), dtype=bool)
    matches_wc22 = matches[wc2022_mask].reset_index(drop=True)
    X_wc22 = X[wc2022_mask].fillna(0.0).reset_index(drop=True)
    y_wc22 = y[wc2022_mask].reset_index(drop=True)
    if ensemble_holdout:
        print(f"  Ensemble holdout (WC2022): {len(matches_wc22)} matches")

    # Train / validation split (excluding WC2022 holdout)
    val_cutoff = pd.Timestamp(val_since)
    train_mask = (~wc2022_mask) & (matches["date"].values < val_cutoff)
    val_mask = (~wc2022_mask) & (matches["date"].values >= val_cutoff)
    X_train, y_train = X[train_mask], y[train_mask]
    X_val, y_val = X[val_mask], y[val_mask]
    print(f"  Train: {len(X_train)}, Val: {len(X_val)}")

    # Sample weights: up-weight WC/Euro/Copa finals (1.5x), down-weight qualifiers (0.5x)
    weights_train = compute_sample_weights(matches[train_mask]).values
    n_up = int((weights_train > 1.0).sum())
    n_dn = int((weights_train < 1.0).sum())
    print(f"  Sample weights: {n_up} finals up-weighted (1.5x), {n_dn} qualifiers down-weighted (0.5x)")

    # Fill NaN (teams with no history → 0)
    X_train = X_train.fillna(0.0)
    X_val = X_val.fillna(0.0)

    print("Training LightGBM...")
    model = lgbm_model.train(
        X_train, y_train,
        eval_set=(X_val, y_val),
        early_stopping_rounds=50,
        sample_weight=weights_train,
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

    # Ensemble gate: evaluate DC + LGBM blend on WC2022 holdout
    gate_passed = False
    optimal_dc_weight = 0.5
    gate_meta = {"holdout": "wc2022", "passed": False, "reason": "skipped"}
    if ensemble_holdout and len(matches_wc22) > 0:
        print(f"\nEnsemble gate — WC2022 holdout ({len(matches_wc22)} matches)...")
        from src.ensemble.calibration import calibrate as _calibrate

        lgbm_wc22 = lgbm_model.predict_proba(model, X_wc22)
        lgbm_wc22_cal = _calibrate(lgbm_wc22, calibrators)

        dc_probs_list = []
        for _, r in matches_wc22.iterrows():
            p = dc.predict_match(r["home_team"], r["away_team"], dc_params,
                                 neutral=bool(r.get("neutral", False)))
            dc_probs_list.append(p)

        grid = np.arange(0.4, 0.71, 0.05)
        optimal_dc_weight = find_optimal_weight(
            dc_probs_list, lgbm_wc22_cal, y_wc22.values, weight_grid=grid,
        )

        from src.ensemble.combiner import blend
        blended = np.array([
            blend(dcp, lp, dc_weight=optimal_dc_weight)
            for dcp, lp in zip(dc_probs_list, lgbm_wc22_cal)
        ])
        brier_blend = brier_score_multiclass(blended, y_wc22.values)

        dc_arr = np.array([[p["p_away"], p["p_draw"], p["p_home"]] for p in dc_probs_list])
        brier_dc_only = brier_score_multiclass(dc_arr, y_wc22.values)
        brier_lgbm_only = brier_score_multiclass(lgbm_wc22_cal, y_wc22.values)

        improvement = brier_dc_only - brier_blend
        print(f"  DC-only Brier:        {brier_dc_only:.4f}")
        print(f"  LGBM-only Brier:      {brier_lgbm_only:.4f}")
        print(f"  Blend Brier:          {brier_blend:.4f}  (dc_weight={optimal_dc_weight:.2f})")
        print(f"  Improvement vs DC:    {improvement:+.4f}  (min required: {gate_min_improvement:+.4f})")
        gate_passed = bool(improvement >= gate_min_improvement)
        gate_meta = {
            "holdout": "wc2022",
            "n_matches": int(len(matches_wc22)),
            "dc_only_brier": brier_dc_only,
            "lgbm_only_brier": brier_lgbm_only,
            "blend_brier": brier_blend,
            "improvement_vs_dc": improvement,
            "dc_weight": optimal_dc_weight,
            "min_improvement_required": gate_min_improvement,
            "passed": gate_passed,
            "reason": (f"blend improves DC-only by {improvement:.4f} (≥ {gate_min_improvement})"
                       if gate_passed
                       else f"blend improvement {improvement:.4f} below {gate_min_improvement} threshold"),
        }
        print("  Gate result:          " + ("✅ PASS — LGBM eligible for live ensemble" if gate_passed
                                              else "❌ FAIL — scanner stays DC-only"))

    # Save
    out_dir = MODELS_DIR / "lgbm"
    out_dir.mkdir(parents=True, exist_ok=True)
    lgbm_model.save_model(model, out_dir / "model.pkl")
    save_calibrators(calibrators, out_dir / "calibrators.pkl")
    import json as _json
    (out_dir / "gate.json").write_text(_json.dumps(gate_meta, indent=2, default=float))
    print(f"Saved gate metadata: {out_dir / 'gate.json'}")

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
    parser.add_argument("--no-holdout", action="store_true",
                        help="Skip WC2022 ensemble holdout (use entire date range for train/val)")
    parser.add_argument("--gate-min-improvement", type=float, default=0.01,
                        help="Min Brier improvement of blend over DC-only on WC2022 holdout")
    args = parser.parse_args()
    main(since=args.since, val_since=args.val_since,
         ensemble_holdout=not args.no_holdout,
         gate_min_improvement=args.gate_min_improvement)
