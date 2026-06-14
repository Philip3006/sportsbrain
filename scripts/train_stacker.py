"""Phase 2.1 — Trains the stacker meta-learner on walk-forward backtest data.

For each tournament event in TOURNAMENT_EVENTS:
  • Fit DC on matches strictly before the event.
  • For every event match, compute DC probs + Shin-debiased market probs.
  • Store (stacker_features, true_outcome) as one training row.

Additionally, WM 2026 matches with known results can be appended (--include-wm2026).
The current DC snapshot is used for those rows — this is slightly in-sample for DC
but acceptable for a re-calibration layer that learns relative model/market trust.

Run:
  python3 scripts/train_stacker.py                   # historical tournaments only
  python3 scripts/train_stacker.py --include-wm2026  # + WM 2026 completed matches
  python3 scripts/train_stacker.py --report           # also print per-event Brier
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

_WM2026_START = pd.Timestamp("2026-06-11")


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


def _load_odds_history_shin() -> dict[str, tuple[float, float, float]]:
    """Reads data/odds_history.json and returns {canonical_key: (p_home, p_draw, p_away)}.

    canonical_key = "HomeTeam_vs_AwayTeam" (lowercase, spaces→underscore).
    Uses the first (earliest) snapshot for each match — opening market is most
    reliable for calibration before line movement.
    """
    path = Path(__file__).parent.parent / "data" / "odds_history.json"
    if not path.exists():
        return {}
    try:
        import json as _json
        snapshots = _json.loads(path.read_text())
    except Exception:
        return {}

    result: dict[str, tuple[float, float, float]] = {}
    for snap in snapshots:
        for match_key, odds in snap.get("odds", {}).items():
            h = float(odds.get("home", 0))
            d = float(odds.get("draw", 0))
            a = float(odds.get("away", 0))
            if not all(o > 1.0 for o in (h, d, a)):
                continue
            canonical = match_key.lower().replace(" ", "_").replace("&", "and")
            if canonical not in result:  # keep first snapshot = opening odds
                result[canonical] = remove_margin_shin((h, d, a))
    return result


def _match_key(home: str, away: str) -> str:
    return f"{home.lower().replace(' ', '_')}_vs_{away.lower().replace(' ', '_')}"


def collect_wm2026_rows(
    dc_params: dc.DixonColesParams,
    all_matches: pd.DataFrame,
    odds_shin: dict,
) -> list[dict]:
    """Collect completed WM 2026 matches as stacker training rows.

    Uses the current DC snapshot (already trained on these matches) for probs —
    acceptable for a re-calibration layer whose job is weighting DC vs. market.
    """
    wm = all_matches[
        (all_matches["tournament"] == "FIFA World Cup")
        & (all_matches["date"] >= _WM2026_START)
        & all_matches["home_score"].notna()
        & all_matches["away_score"].notna()
    ]
    if wm.empty:
        return []

    rows: list[dict] = []
    for _, m in wm.iterrows():
        home, away = m["home_team"], m["away_team"]
        try:
            dc_probs = dc.predict_match(home, away, dc_params, neutral=True)
        except ValueError:
            continue

        key = _match_key(home, away)
        shin = odds_shin.get(key)

        feats = build_stacker_features(
            dc_probs=dc_probs,
            lgbm_probs=None,
            shin_probs=shin,
            is_knockout=False,
            is_neutral=True,
        )
        rows.append({
            "event": "WC2026",
            "home": home, "away": away,
            "features": feats,
            "outcome": _outcome_idx(int(m["home_score"]), int(m["away_score"])),
            "dc_probs": dc_probs,
            "shin_probs": shin,
        })

    print(f"  [WC2026] {len(rows)} completed match rows appended")
    return rows


def main(report: bool = False, save: bool = True, include_wm2026: bool = False) -> int:
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

    n_wm2026_rows = 0
    if include_wm2026:
        print("\nAppending WM 2026 completed matches...")
        snap_dir = MODELS_DIR / "dixon_coles"
        dc_files = sorted(snap_dir.glob("params_*.pkl")) if snap_dir.exists() else []
        if dc_files:
            current_params = dc.load(dc_files[-1])
            odds_shin = _load_odds_history_shin()
            wm26_rows = collect_wm2026_rows(current_params, all_matches, odds_shin)
            all_rows.extend(wm26_rows)
            n_wm2026_rows = len(wm26_rows)
        else:
            print("  No DC snapshot found — skipping WM 2026 rows")

    X = np.vstack([r["features"] for r in all_rows])
    y = np.array([r["outcome"] for r in all_rows], dtype=int)
    print(f"\nTotal training rows: {len(X)} (historical={len(X)-n_wm2026_rows}, WC2026={n_wm2026_rows})")
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
            "wm2026_matches_at_train": n_wm2026_rows,
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
    parser.add_argument("--include-wm2026", action="store_true",
                        help="Append completed WM 2026 matches to training data")
    args = parser.parse_args()
    sys.exit(main(report=args.report, save=not args.no_save, include_wm2026=args.include_wm2026))
