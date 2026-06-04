"""
Fits Dixon-Coles model on competitive international matches.
Saves model snapshot to models/dixon_coles/params_{date}.pkl

By default uses finals + Nations Leagues only (no qualifiers).
Qualifiers inflate attack params for teams like Japan (14-0 vs Bangladesh)
and NZ (12-0 vs Tonga), making them appear stronger than at WC level.

Usage:
  python scripts/train_dixon_coles.py                    # finals-only (default)
  python scripts/train_dixon_coles.py --all              # all competitive incl. qualifiers
  python scripts/train_dixon_coles.py --since 2018-01-01 # all competitive since date
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR, COMPETITIVE_TOURNAMENTS
from src.data.international import fetch_international_results, filter_competitive
from src.models import dixon_coles

# Finals-only tournaments: no qualifiers, no weak-minnow blowouts.
_FINALS_TOURNAMENTS = {t for t in COMPETITIVE_TOURNAMENTS if "qualification" not in t.lower()}


def main(since: str | None = None, finals_only: bool = True):
    print("Loading data...")
    df = fetch_international_results()
    df = filter_competitive(df)

    if finals_only:
        df = df[df["tournament"].isin(_FINALS_TOURNAMENTS)]
        print(f"  Finals + Nations Leagues only (no qualifiers): {len(df)} matches")
    elif since:
        df = df[df["date"] >= pd.Timestamp(since)]
        print(f"  All competitive since {since}: {len(df)} matches")
    else:
        print(f"  All competitive matches: {len(df)}")

    print("Fitting Dixon-Coles model (this may take ~60-120 seconds)...")
    params = dixon_coles.fit(df, max_iter=2000)

    snap_dir = MODELS_DIR / "dixon_coles"
    snap_dir.mkdir(parents=True, exist_ok=True)
    out_path = snap_dir / f"params_{params.fit_date.strftime('%Y%m%d')}.pkl"
    dixon_coles.save(params, out_path)

    print(f"Saved: {out_path}")
    print(f"  Teams: {len(params.attack)}")
    print(f"  Home advantage (gamma): {params.home_adv:.4f}")
    print(f"  Rho (low-score correction): {params.rho:.4f}")
    print(f"  Fit date: {params.fit_date.date()}")

    top_attack = sorted(params.attack.items(), key=lambda x: x[1], reverse=True)[:8]
    print("\n  Top 8 attack ratings:")
    for team, val in top_attack:
        print(f"    {team}: {val:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="Use all competitive matches including qualifiers")
    parser.add_argument("--since", default=None,
                        help="When --all: only matches since YYYY-MM-DD")
    args = parser.parse_args()
    main(since=args.since, finals_only=not args.all)
