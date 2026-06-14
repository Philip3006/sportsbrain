"""Phase 2.1 — Trains the stacker meta-learner on walk-forward backtest data.

For each tournament event in TOURNAMENT_EVENTS:
  • Fit DC on matches strictly before the event.
  • For every event match, compute DC probs + Shin-debiased market probs.
  • Store (stacker_features, true_outcome) as one training row.

The final stacker is fit on ALL collected rows — strictly avoiding the leak
where a model is tested on matches that were in its own training set.
This mirrors the discipline in run_backtest.py / walk_forward.run_event_backtest.

Walk-forward LGBM probs are NOT included (the live LGBM was trained including
some of the held-out tournaments). The stacker therefore learns a DC + market
+ context refinement; the live scanner can still cascade through the LGBM blend
first and feed the blended probs in as `lgbm_probs` once a walk-forward-trained
LGBM is available.

Run:
  python3 scripts/train_stacker.py             # train on all backtest tournaments
  python3 scripts/train_stacker.py --report    # also print per-event Brier
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.walk_forward import TOURNAMENT_EVENTS
from src.betting.odds_utils import remove_margin_shin
from src.config import MODELS_DIR
from src.data.betexplorer import load_odds_lookup
from src.data.football_data_intl import fetch_wc_odds
from src.data.international import fetch_international_results, filter_before, filter_competitive
from src.ensemble.calibration import (
    brier_score_multiclass,
    expected_calibration_error,
)
from src.ensemble.stacking import Stacker, build_stacker_features, feature_columns
from src.models import dixon_coles as dc


def _outcome_idx(hg: int, ag: int) -> int:
    if hg > ag:
        return 2
    if hg == ag:
        return 1
    return 0


def collect_event_rows(event: dict, all_matches: pd.DataFrame,
                        odds_lookup: pd.DataFrame | None) -> list[dict]:
    """Returns a list of {features, outcome, ...} dicts for one tournament."""
    start = pd.Timestamp(event["start"])
    end = pd.Timestamp(event["end"])
    train = filter_before(all_matches, start)
    if len(train) < 50:
        print(f"  [{event['name']}] too few training matches ({len(train)}) — skipping")
        return []

    print(f"  [{event['name']}] fitting DC on {len(train)} matches...")
    params = dc.fit(train, today=start, max_iter=1000)
    event_matches = all_matches[(all_matches["date"] >= start) & (all_matches["date"] <= end)]

    rows: list[dict] = []
    for _, m in event_matches.iterrows():
        home, away = m["home_team"], m["away_team"]
        neutral = bool(m.get("neutral", True))
        is_knockout = "stage" in m and "group" not in str(m.get("stage", "")).lower()

        try:
            dc_probs = dc.predict_match(home, away, params, neutral=neutral)
        except ValueError:
            continue

        # Shin-debias market odds when available; otherwise fall back to uniform.
        shin = None
        if odds_lookup is not None:
            odds_match_id = f"{event['name']}_{home}_vs_{away}"
            odds_row = odds_lookup[odds_lookup["match_id"] == odds_match_id]
            if not odds_row.empty:
                r = odds_row.iloc[0]
                h, d, a = float(r.get("home_odds", 0)), float(r.get("draw_odds", 0)), float(r.get("away_odds", 0))
                if all(o > 1.0 for o in (h, d, a)):
                    shin = remove_margin_shin((h, d, a))

        feats = build_stacker_features(
            dc_probs=dc_probs,
            lgbm_probs=None,  # walk-forward LGBM not available — see module docstring
            shin_probs=shin,
            is_knockout=is_knockout,
            is_neutral=neutral,
        )
        rows.append({
            "event": event["name"],
            "home": home, "away": away,
            "features": feats,
            "outcome": _outcome_idx(int(m["home_score"]), int(m["away_score"])),
            "dc_probs": dc_probs,
            "shin_probs": shin,
        })

    print(f"  [{event['name']}] collected {len(rows)} rows")
    return rows


def main(report: bool = False, save: bool = True) -> int:
    print("Loading data...")
    all_matches = filter_competitive(fetch_international_results())
    print(f"  {len(all_matches)} competitive matches")

    # Odds lookup (same construction as run_backtest.py)
    wc_odds = fetch_wc_odds()
    be_odds = load_odds_lookup()
    if not be_odds.empty:
        be_odds = be_odds[~be_odds["tournament"].isin(["WC2018", "WC2022"])]
    frames = [df for df in [wc_odds, be_odds] if not df.empty]
    odds_lookup = pd.concat(frames, ignore_index=True) if frames else None
    if odds_lookup is not None:
        print(f"  {len(odds_lookup)} matches with market odds loaded")

    print("\nWalk-forward feature collection...")
    all_rows: list[dict] = []
    per_event_rows: dict[str, list[dict]] = {}
    for event in TOURNAMENT_EVENTS:
        rows = collect_event_rows(event, all_matches, odds_lookup)
        per_event_rows[event["name"]] = rows
        all_rows.extend(rows)

    if not all_rows:
        print("⚠️  No rows collected — cannot train stacker.")
        return 1

    X = np.vstack([r["features"] for r in all_rows])
    y = np.array([r["outcome"] for r in all_rows], dtype=int)
    print(f"\nTotal training rows: {len(X)}")
    print(f"  Outcome dist: away={int((y == 0).sum())}, draw={int((y == 1).sum())}, home={int((y == 2).sum())}")

    print("\nFitting stacker (LogisticRegression, multinomial)...")
    stacker = Stacker().fit(X, y, C=1.0)
    proba = stacker.predict_proba(X)
    brier = brier_score_multiclass(proba, y)
    ece = expected_calibration_error(proba, y)
    print(f"  In-sample Brier: {brier:.4f}")
    print(f"  In-sample ECE:   {ece:.4f}")

    if report:
        print("\nPer-event in-sample diagnostics (note: NOT walk-forward CV):")
        for evname, rows in per_event_rows.items():
            if not rows:
                continue
            Xe = np.vstack([r["features"] for r in rows])
            ye = np.array([r["outcome"] for r in rows])
            pe = stacker.predict_proba(Xe)
            print(f"  {evname}: n={len(rows)}, Brier={brier_score_multiclass(pe, ye):.4f}")

    if save:
        out_dir = MODELS_DIR / "lgbm"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "stacker.pkl"
        stacker.save(out_path)
        meta_path = out_dir / "stacker_features.json"
        meta_path.write_text(json.dumps({
            "feature_columns": feature_columns(),
            "n_training_samples": stacker.n_training_samples,
            "in_sample_brier": brier,
            "in_sample_ece": ece,
        }, indent=2))
        print(f"\nSaved: {out_path}")
        print(f"Saved: {meta_path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="Print per-event diagnostics")
    parser.add_argument("--no-save", action="store_true", help="Train without saving")
    args = parser.parse_args()
    sys.exit(main(report=args.report, save=not args.no_save))
