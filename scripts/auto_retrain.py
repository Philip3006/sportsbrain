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


def check_new_wm_matches(fit_date: pd.Timestamp, results: pd.DataFrame) -> int:
    """Count WM 2026 matches played after fit_date."""
    wm = results[
        (results["tournament"] == "FIFA World Cup")
        & (results["date"] >= pd.Timestamp("2026-06-11"))
        & (results["date"] >= fit_date)
        & results["home_score"].notna()
    ]
    return len(wm)


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

    print("\nStep 1/2: Retraining Dixon-Coles (finals-only)...")
    import scripts.train_dixon_coles as tdc
    tdc.main(finals_only=True)

    print("\nStep 2/2: Retraining LightGBM...")
    try:
        import scripts.train_lgbm as tlg
        tlg.main()
    except Exception as e:
        print(f"  LightGBM retraining failed: {e}")
        print("  DC model was saved — scanner will use DC-only until LightGBM is fixed.")
        return

    print("\nRetraining complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Retrain even if no new WM matches")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without actually retraining")
    args = parser.parse_args()
    main(force=args.force, dry_run=args.dry_run)
