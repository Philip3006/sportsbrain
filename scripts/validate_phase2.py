"""Phase 2 Backtest Gate.

Runs a walk-forward evaluation over TOURNAMENT_EVENTS and compares:
  • Plain DC  (current baseline)
  • Stacker   (DC + Shin-market meta-learner from models/lgbm/stacker.pkl)
  • Hier-DC   (DC with confederation cluster prior, cluster_strength=0.03)

A component is enabled in src/config.py when its Brier score is at least
BRIER_IMPROVEMENT_THRESHOLD better than plain DC on the same walk-forward data.

Conformal predictor is fit on walk-forward DC probs (calibration gate: n ≥ 100)
and saved to models/lgbm/conformal.pkl so CONFORMAL_ENABLED can be turned on.

Usage:
  python3 scripts/validate_phase2.py            # run gate, auto-update config
  python3 scripts/validate_phase2.py --dry-run  # report only, no config change
  python3 scripts/validate_phase2.py --verbose  # per-event breakdown
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.walk_forward import TOURNAMENT_EVENTS
from src.betting.odds_utils import remove_margin_shin
from src.config import MODELS_DIR, TEAM_CONFEDERATION
from src.data.betexplorer import load_odds_lookup
from src.data.football_data_intl import fetch_wc_odds
from src.data.international import fetch_international_results, filter_before, filter_competitive
from src.ensemble.calibration import brier_score_multiclass, expected_calibration_error
from src.ensemble.conformal import ConformalPredictor
from src.ensemble.stacking import Stacker, build_stacker_features
from src.models import dixon_coles as dc

# Improvement thresholds to automatically enable a component
_STACKER_THRESHOLD = 0.005      # stacker Brier must beat DC by ≥ 0.005
_HIER_DC_THRESHOLD = 0.003      # hierarchical DC must beat plain DC by ≥ 0.003
_CONFORMAL_MIN_N = 100          # minimum walk-forward samples to fit conformal
_HIER_CLUSTER_STRENGTH = 0.03   # default cluster_strength for hierarchical DC


def _outcome_idx(hg: int, ag: int) -> int:
    if hg > ag:
        return 2
    if hg == ag:
        return 1
    return 0


def collect_event_probs(
    event: dict,
    all_matches: pd.DataFrame,
    odds_lookup: pd.DataFrame | None,
    verbose: bool = False,
) -> list[dict]:
    """Walk-forward: trains DC on data before event, collects predictions on event matches."""
    start = pd.Timestamp(event["start"])
    end   = pd.Timestamp(event["end"])
    train = filter_before(all_matches, start)
    if len(train) < 50:
        if verbose:
            print(f"  [{event['name']}] too few training matches ({len(train)}) — skip")
        return []

    if verbose:
        print(f"  [{event['name']}] fitting DC on {len(train)} matches ...")

    # Plain DC
    params_plain = dc.fit(train, today=start, max_iter=1000)

    # Hierarchical DC (confederation cluster prior)
    params_hier = dc.fit(
        train, today=start, max_iter=1000,
        prior_params=params_plain,
        cluster_map=TEAM_CONFEDERATION,
        cluster_strength=_HIER_CLUSTER_STRENGTH,
    )

    event_matches = all_matches[
        (all_matches["date"] >= start) & (all_matches["date"] <= end)
    ]

    rows: list[dict] = []
    for _, m in event_matches.iterrows():
        home, away = m["home_team"], m["away_team"]
        neutral = bool(m.get("neutral", True))

        try:
            probs_plain = dc.predict_match(home, away, params_plain, neutral=neutral)
            probs_hier  = dc.predict_match(home, away, params_hier,  neutral=neutral)
        except ValueError:
            continue

        # Shin-debiased market probs when available
        shin = None
        if odds_lookup is not None:
            mid = f"{event['name']}_{home}_vs_{away}"
            r = odds_lookup[odds_lookup["match_id"] == mid]
            if not r.empty:
                row_o = r.iloc[0]
                h = float(row_o.get("home_odds", 0))
                d = float(row_o.get("draw_odds", 0))
                a = float(row_o.get("away_odds", 0))
                if all(o > 1.0 for o in (h, d, a)):
                    shin = remove_margin_shin((h, d, a))

        rows.append({
            "event": event["name"],
            "home": home, "away": away,
            "dc_probs":   probs_plain,
            "hier_probs": probs_hier,
            "shin_probs": shin,
            "outcome": _outcome_idx(int(m["home_score"]), int(m["away_score"])),
        })

    if verbose:
        print(f"  [{event['name']}] {len(rows)} prediction rows")
    return rows


def _probs_matrix(rows: list[dict], key: str) -> np.ndarray:
    return np.array([[r[key]["p_away"], r[key]["p_draw"], r[key]["p_home"]] for r in rows])


def _update_config_flag(flag: str, value: bool) -> None:
    """In-place replace `FLAG = False` → `FLAG = True` (or vice-versa) in config.py."""
    cfg = Path(__file__).parent.parent / "src" / "config.py"
    text = cfg.read_text()
    old = f"{flag} = {not value}"
    new = f"{flag} = {value}"
    if old in text:
        cfg.write_text(text.replace(old, new))


def main(dry_run: bool = False, verbose: bool = False) -> int:
    print("Loading historical matches...")
    all_matches = filter_competitive(fetch_international_results())
    print(f"  {len(all_matches)} competitive matches")

    wc_odds  = fetch_wc_odds()
    be_odds  = load_odds_lookup()
    if not be_odds.empty:
        be_odds = be_odds[~be_odds["tournament"].isin(["WC2018", "WC2022"])]
    frames = [df for df in [wc_odds, be_odds] if not df.empty]
    odds_lookup = pd.concat(frames, ignore_index=True) if frames else None
    if odds_lookup is not None:
        print(f"  {len(odds_lookup)} matches with market odds")

    print("\nWalk-forward evaluation ...")
    all_rows: list[dict] = []
    for event in TOURNAMENT_EVENTS:
        rows = collect_event_probs(event, all_matches, odds_lookup, verbose=verbose)
        all_rows.extend(rows)

    if not all_rows:
        print("ERROR: no prediction rows collected.")
        return 1

    n = len(all_rows)
    print(f"\nTotal evaluation rows: {n}")

    outcomes = np.array([r["outcome"] for r in all_rows])
    dc_probs   = _probs_matrix(all_rows, "dc_probs")
    hier_probs = _probs_matrix(all_rows, "hier_probs")

    # Stacker predictions
    stacker_path = MODELS_DIR / "lgbm" / "stacker.pkl"
    stacker_probs = None
    if stacker_path.exists():
        stacker = Stacker.load(stacker_path)
        feat_rows = []
        for r in all_rows:
            f = build_stacker_features(
                dc_probs=r["dc_probs"],
                lgbm_probs=None,
                shin_probs=r["shin_probs"],
                is_knockout=False,
                is_neutral=True,
            )
            feat_rows.append(f)
        X = np.vstack(feat_rows)
        stacker_probs = stacker.predict_proba(X)
    else:
        print("  WARN: stacker.pkl not found — skipping stacker evaluation")

    # Brier scores
    brier_dc    = brier_score_multiclass(dc_probs,   outcomes)
    brier_hier  = brier_score_multiclass(hier_probs, outcomes)
    brier_stack = brier_score_multiclass(stacker_probs, outcomes) if stacker_probs is not None else None

    ece_dc   = expected_calibration_error(dc_probs,   outcomes)
    ece_hier = expected_calibration_error(hier_probs, outcomes)

    print("\n" + "=" * 55)
    print(f"{'Component':<20}  {'Brier':>8}  {'ECE':>8}  {'ΔBrier vs DC':>14}")
    print("-" * 55)
    print(f"{'Plain DC':<20}  {brier_dc:>8.4f}  {ece_dc:>8.4f}  {'(baseline)':>14}")
    print(f"{'Hierarchical DC':<20}  {brier_hier:>8.4f}  {ece_hier:>8.4f}  {(brier_dc - brier_hier):>+14.4f}")
    if brier_stack is not None:
        ece_stack = expected_calibration_error(stacker_probs, outcomes)
        print(f"{'Stacker':<20}  {brier_stack:>8.4f}  {ece_stack:>8.4f}  {(brier_dc - brier_stack):>+14.4f}")
    print("=" * 55)

    # Per-event breakdown
    if verbose:
        print("\nPer-event Brier (plain DC vs hierarchical DC):")
        for event in TOURNAMENT_EVENTS:
            ev_rows = [r for r in all_rows if r["event"] == event["name"]]
            if not ev_rows:
                continue
            ev_out  = np.array([r["outcome"] for r in ev_rows])
            ev_dc   = _probs_matrix(ev_rows, "dc_probs")
            ev_hier = _probs_matrix(ev_rows, "hier_probs")
            print(f"  {event['name']:12s}  n={len(ev_rows):3d}  "
                  f"DC={brier_score_multiclass(ev_dc, ev_out):.4f}  "
                  f"Hier={brier_score_multiclass(ev_hier, ev_out):.4f}")

    # Conformal calibration
    conformal_enabled = n >= _CONFORMAL_MIN_N
    if conformal_enabled:
        cp = ConformalPredictor(alpha=0.10).fit(dc_probs, outcomes)
        coverage = cp.empirical_coverage(dc_probs, outcomes)
        print(f"\nConformal calibration: n={n}, empirical coverage @ 90%: {coverage:.3f}")
        cp_path = MODELS_DIR / "lgbm" / "conformal.pkl"
        if not dry_run:
            cp.save(cp_path)
            print(f"  Saved: {cp_path}")
    else:
        print(f"\nConformal: only {n} rows — need ≥ {_CONFORMAL_MIN_N} to calibrate.")

    # Gate decisions
    enable_stacker = (
        brier_stack is not None and (brier_dc - brier_stack) >= _STACKER_THRESHOLD
    )
    enable_hier = (brier_dc - brier_hier) >= _HIER_DC_THRESHOLD
    enable_conformal = conformal_enabled

    print("\nGate decisions:")
    print(f"  STACKER_ENABLED         → {'✅ ENABLE' if enable_stacker else '❌ keep False'}  "
          f"(need Δ≥{_STACKER_THRESHOLD}, got {(brier_dc - (brier_stack or 0)):+.4f})")
    print(f"  HIERARCHICAL_DC_ENABLED → {'✅ ENABLE' if enable_hier else '❌ keep False'}  "
          f"(need Δ≥{_HIER_DC_THRESHOLD}, got {(brier_dc - brier_hier):+.4f})")
    print(f"  CONFORMAL_ENABLED       → {'✅ ENABLE' if enable_conformal else '❌ keep False'}  "
          f"(need n≥{_CONFORMAL_MIN_N}, got {n})")

    if dry_run:
        print("\n--dry-run: no config changes written.")
        return 0

    if enable_stacker:
        _update_config_flag("STACKER_ENABLED", True)
    if enable_hier:
        _update_config_flag("HIERARCHICAL_DC_ENABLED", True)
    if enable_conformal:
        _update_config_flag("CONFORMAL_ENABLED", True)

    if enable_stacker or enable_hier or enable_conformal:
        print("\n✅ src/config.py updated — flags enabled.")
    else:
        print("\nNo flags changed (thresholds not met).")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report only, no config changes")
    parser.add_argument("--verbose", action="store_true", help="Per-event breakdown")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run, verbose=args.verbose))
