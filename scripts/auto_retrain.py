"""
Auto-retraining script for SportsBrain.

Checks if new WM 2026 matches have been played since the last DC model fit.
If yes (or --force): retrains DC (finals-only) + LightGBM.

Usage:
  python scripts/auto_retrain.py           # retrain only if new WM matches found
  python scripts/auto_retrain.py --force   # always retrain
  python scripts/auto_retrain.py --dry-run # show what would happen without retraining
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR
from src.data.international import fetch_international_results
from src.models import dixon_coles as dc


def _load_latest_dc_params() -> dc.DixonColesParams | None:
    snap_dir = MODELS_DIR / "dixon_coles"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("params_*.pkl"))
    return dc.load(files[-1]) if files else None


_WM2026_START = pd.Timestamp("2026-06-11")
_STACKER_RETRAIN_EVERY = 3  # retrain stacker every N new WM matches


def check_new_wm_matches(fit_date: pd.Timestamp, results: pd.DataFrame) -> int:
    """Count WM 2026 matches played after fit_date."""
    wm = results[
        (results["tournament"] == "FIFA World Cup")
        & (results["date"] >= _WM2026_START)
        & (results["date"] >= fit_date)
        & results["home_score"].notna()
    ]
    return len(wm)


def count_total_wm2026_matches(results: pd.DataFrame) -> int:
    """Total WM 2026 matches with results."""
    return int((
        (results["tournament"] == "FIFA World Cup")
        & (results["date"] >= _WM2026_START)
        & results["home_score"].notna()
    ).sum())


def stacker_wm2026_at_last_train() -> int:
    """Read wm2026_matches_at_train from stacker_features.json (0 if missing)."""
    import json as _json
    path = MODELS_DIR / "lgbm" / "stacker_features.json"
    if not path.exists():
        return 0
    try:
        return int(_json.loads(path.read_text()).get("wm2026_matches_at_train", 0))
    except Exception:
        return 0


def main(force: bool = False, dry_run: bool = False) -> None:
    print("Checking for new WM 2026 matches...")

    dc_params = _load_latest_dc_params()
    fit_date = dc_params.fit_date if dc_params else pd.Timestamp("2000-01-01")
    print(f"  Current DC model fit date: {fit_date.date()}")

    results = fetch_international_results(force=True)
    n_new = check_new_wm_matches(fit_date, results)

    if dry_run:
        if n_new == 0 and not force:
            print(f"  [DRY-RUN] No new WM matches since {fit_date.date()} — retraining would be skipped.")
        elif force:
            print(f"  [DRY-RUN] Would retrain: --force flag set (regardless of new matches).")
        else:
            print(f"  [DRY-RUN] Would retrain: {n_new} new WM match(es) since {fit_date.date()}.")
        print("  [DRY-RUN] No changes made.")
        return

    if n_new == 0 and not force:
        print(f"  No new WM matches since {fit_date.date()} — retraining skipped.")
        return

    if force:
        print("  --force flag set — retraining regardless.")
    else:
        # During WM 2026 we retrain even if today's results are partial — partial data
        # with WC2026_BOOST still beats stale data from last training. The 12h cadence
        # picks up later matches on the next run.
        wm_mask = (
            (results["tournament"] == "FIFA World Cup")
            & (results["date"] >= pd.Timestamp("2026-06-11"))
            & results["home_score"].notna()
        )
        if wm_mask.any():
            latest_wm_date = results[wm_mask]["date"].max()
            if pd.Timestamp(latest_wm_date).date() >= pd.Timestamp.now().date():
                print(
                    f"  Latest WM match on {latest_wm_date.date()} (today) — "
                    "martj42 may be partial, but proceeding (12h cadence will catch later matches)."
                )
        print(f"  {n_new} new WM match(es) since last training — retraining.")

    print("\nStep 1/3: Retraining Dixon-Coles (finals-only)...")
    import scripts.train_dixon_coles as tdc
    tdc.main(finals_only=True)

    print("\nStep 2/3: Retraining LightGBM...")
    try:
        import scripts.train_lgbm as tlg
        tlg.main()
    except Exception as e:
        print(f"  LightGBM retraining failed: {e}")
        print("  DC model was saved — scanner will use DC-only until LightGBM is fixed.")

    # Step 3: Stacker retrain every _STACKER_RETRAIN_EVERY new WM matches
    n_total_wm = count_total_wm2026_matches(results)
    n_at_last = stacker_wm2026_at_last_train()
    n_new_since = n_total_wm - n_at_last
    print(f"\nStep 3/3: Stacker check — WM2026 matches total={n_total_wm}, "
          f"at last stacker train={n_at_last}, new={n_new_since}")
    if n_new_since >= _STACKER_RETRAIN_EVERY or force:
        print(f"  {n_new_since} new WM matches (≥{_STACKER_RETRAIN_EVERY}) — retraining stacker...")
        import scripts.train_stacker as ts
        ts.main(include_wm2026=True, save=True)
        print("  Stacker retrained ✅")
    else:
        print(f"  {n_new_since} new WM matches — stacker retrain deferred "
              f"(trigger at {_STACKER_RETRAIN_EVERY}).")

    print("\nRetraining complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Retrain even if no new WM matches")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without actually retraining")
    args = parser.parse_args()
    main(force=args.force, dry_run=args.dry_run)
