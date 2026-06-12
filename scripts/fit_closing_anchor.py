"""
Lever 6: Closing-Line Anchoring.

Tests whether blending the calibrated model output with closing-implied
probabilities (Shin-corrected) improves Brier on historical tournament
matches. Uses WC2018 + WC2022 closing odds (data/raw/wc_odds_fduk.csv).

For each match:
  final_prob = alpha * model_prob + (1 - alpha) * closing_implied_prob

Grid-searches alpha ∈ [0, 1] minimizing multiclass Brier.
If best alpha < 0.95 with meaningful Brier improvement, saves anchor weight
to models/lgbm/anchor.json for scanner use.

Run: python3 scripts/fit_closing_anchor.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR
from src.data.international import fetch_international_results
from src.ensemble.calibration import (
    brier_score_multiclass,
    calibrate,
    load_calibrators,
)
from src.features.builder import build_training_matrix
from src.models import dixon_coles as dc
from src.models import lgbm_model
from src.models.elo import compute_elo_series


def shin_correction(odds: tuple[float, float, float], iterations: int = 12) -> tuple[float, float, float]:
    """Shin (1992) margin removal — returns true probability triple summing to 1."""
    h, d, a = odds
    raw = np.array([1/h, 1/d, 1/a])
    raw_sum = raw.sum()
    z = 0.0
    for _ in range(iterations):
        p = (np.sqrt(z**2 + 4*(1-z) * raw**2 / raw_sum) - z) / (2 * (1 - z))
        z = max(0.0, (p.sum() - 1) / (1 - (p**2).sum()))
    p = (np.sqrt(z**2 + 4*(1-z) * raw**2 / raw_sum) - z) / (2 * (1 - z))
    p = p / p.sum()
    return tuple(p)  # (p_home, p_draw, p_away)


def outcome_label(hg: int, ag: int) -> int:
    if hg > ag:
        return 2
    if hg < ag:
        return 0
    return 1


def main():
    print("Loading FDUK closing odds...")
    odds = pd.read_csv("data/raw/wc_odds_fduk.csv")
    odds = odds.dropna(subset=["close_home", "close_draw", "close_away"])
    print(f"  {len(odds)} matches with closing odds ({odds['tournament'].value_counts().to_dict()})")

    print("Loading international results to attach outcomes...")
    all_matches = fetch_international_results()

    # Build a (home,away) → (home_score, away_score, date) lookup. WC2018/2022 only.
    wc_mask = (all_matches["tournament"] == "FIFA World Cup") & (
        (all_matches["date"].dt.year.isin([2018, 2022]))
    )
    wc_results = all_matches[wc_mask].copy()
    outcome_map: dict[tuple[str, str, str], tuple[int, int, pd.Timestamp, bool]] = {}
    for _, r in wc_results.iterrows():
        # Tournament tag matches "WC2018" / "WC2022"
        ttag = f"WC{r['date'].year}"
        outcome_map[(ttag, r["home_team"], r["away_team"])] = (
            int(r["home_score"]), int(r["away_score"]), r["date"], bool(r["neutral"])
        )

    print("Computing Elo series + DC params...")
    elo_series = compute_elo_series(all_matches)
    snap_dir = MODELS_DIR / "dixon_coles" / "snapshots"
    dc_2018 = dc.load(snap_dir / "params_FIFAWorldCup_2018.pkl")
    dc_2022 = dc.load(snap_dir / "params_FIFAWorldCup_2022.pkl")
    dc_by_tournament = {"WC2018": dc_2018, "WC2022": dc_2022}

    print("Loading LGBM + calibrators...")
    lgbm = lgbm_model.load_model(MODELS_DIR / "lgbm" / "model.pkl")
    cals = load_calibrators(MODELS_DIR / "lgbm" / "calibrators.pkl")

    # Build per-match prediction set.
    print("Building feature matrix for closing-odds matches...")
    rows = []
    market_probs = []
    outcomes = []
    keep_rows = []
    for _, o in odds.iterrows():
        key = (o["tournament"], o["home_team"], o["away_team"])
        if key not in outcome_map:
            continue
        hg, ag, mdate, neutral = outcome_map[key]
        rows.append({
            "date": mdate,
            "home_team": o["home_team"],
            "away_team": o["away_team"],
            "home_score": hg,
            "away_score": ag,
            "neutral": neutral,
            "tournament": "FIFA World Cup",
        })
        ph, pd_, pa = shin_correction((o["close_home"], o["close_draw"], o["close_away"]))
        market_probs.append([pa, pd_, ph])  # [p_away, p_draw, p_home] order
        outcomes.append(outcome_label(hg, ag))
        keep_rows.append(o)

    matches = pd.DataFrame(rows)
    market_probs = np.array(market_probs)
    outcomes = np.array(outcomes)
    print(f"  Matched {len(matches)} matches with outcomes + closing odds")

    # Feature matrix
    feature_cols_path = MODELS_DIR / "lgbm" / "feature_columns.json"
    expected_cols = json.loads(feature_cols_path.read_text())

    # Build per-tournament so DC snapshot is correct
    model_probs = np.zeros((len(matches), 3))
    for ttag, snap in dc_by_tournament.items():
        sub_idx = [i for i, o in enumerate(keep_rows) if o["tournament"] == ttag]
        if not sub_idx:
            continue
        sub_matches = matches.iloc[sub_idx]
        dc_snap_map = {pd.Timestamp("2000-01-01"): snap}
        X, _ = build_training_matrix(
            sub_matches, all_matches, elo_series, dc_snap_map,
            odds_lookup=None, statsbomb_xg=None,
        )
        X = X.reindex(columns=expected_cols, fill_value=0.0).fillna(0.0)
        lgbm_raw = lgbm_model.predict_proba(lgbm, X)
        lgbm_cal = calibrate(lgbm_raw, cals)

        # Blend with DC at gate's optimal dc_weight (0.40)
        gate = json.loads((MODELS_DIR / "lgbm" / "gate.json").read_text())
        dc_w = float(gate.get("dc_weight", 0.5))
        for j, (i, (_, r)) in enumerate(zip(sub_idx, sub_matches.iterrows())):
            p = dc.predict_match(r["home_team"], r["away_team"], snap,
                                 neutral=bool(r.get("neutral", True)))
            dc_vec = np.array([p["p_away"], p["p_draw"], p["p_home"]])
            blended = dc_w * dc_vec + (1 - dc_w) * lgbm_cal[j]
            blended = blended / blended.sum()
            model_probs[i] = blended

    print("\nGrid-search alpha (model weight) on closing anchor...")
    grid = np.arange(0.0, 1.01, 0.05)
    results = []
    for a in grid:
        final = a * model_probs + (1 - a) * market_probs
        final = final / final.sum(axis=1, keepdims=True)
        b = brier_score_multiclass(final, outcomes)
        results.append((float(a), float(b)))

    # Baselines
    b_model = brier_score_multiclass(model_probs, outcomes)
    b_market = brier_score_multiclass(market_probs, outcomes)
    best_a, best_b = min(results, key=lambda x: x[1])

    print(f"\n  Model-only Brier:   {b_model:.4f}")
    print(f"  Market-only Brier:  {b_market:.4f}")
    print(f"  Best blend alpha:   {best_a:.2f}  →  Brier {best_b:.4f}")
    print(f"  Improvement vs model-only: {b_model - best_b:+.4f}")

    print("\nAlpha sweep (selected):")
    for a, b in results:
        if a in (0.0, 0.25, 0.5, 0.75, 1.0) or abs(a - best_a) < 0.01:
            mark = "  ←best" if abs(a - best_a) < 0.01 else ""
            print(f"  α={a:.2f}  Brier={b:.4f}{mark}")

    # Save anchor only if meaningful improvement and alpha < 0.95
    threshold = 0.005
    use_anchor = (best_a < 0.95) and (b_model - best_b >= threshold)
    out = {
        "alpha": best_a,
        "brier_model_only": b_model,
        "brier_market_only": b_market,
        "brier_blend": best_b,
        "improvement_vs_model": b_model - best_b,
        "use_anchor": use_anchor,
        "threshold": threshold,
        "n_matches": int(len(matches)),
        "reason": (f"alpha={best_a:.2f}<0.95 and Brier improved by "
                   f"{b_model - best_b:.4f} ≥ {threshold}" if use_anchor
                   else "no meaningful improvement — anchor disabled"),
    }
    out_path = MODELS_DIR / "lgbm" / "anchor.json"
    out_path.write_text(json.dumps(out, indent=2, default=float))
    print(f"\nSaved: {out_path}")
    print("  use_anchor =", use_anchor)
    if use_anchor:
        print(f"  Scanner will blend with α={best_a:.2f} when closing odds available")


if __name__ == "__main__":
    main()
