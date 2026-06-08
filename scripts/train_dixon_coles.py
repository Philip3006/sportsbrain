"""
Fits Dixon-Coles model on competitive international matches.
Saves model snapshot to models/dixon_coles/params_{date}.pkl

Default: all competitive matches except OFC qualifiers (Oceania).
OFC teams (NZ, Fiji, Samoa…) run up 8-0 scores vs minnows even with phi-decay,
inflating attack ratings. Non-OFC qualifiers (CONMEBOL, UEFA, CAF, AFC, CONCACAF)
provide genuine form signal and are included.

Usage:
  python scripts/train_dixon_coles.py                    # no-OFC qualifier (default)
  python scripts/train_dixon_coles.py --finals-only      # finals + Nations Leagues only
  python scripts/train_dixon_coles.py --all              # all competitive incl. OFC qualifiers
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

_FINALS_TOURNAMENTS = {t for t in COMPETITIVE_TOURNAMENTS if "qualification" not in t.lower()}

_OFC_TEAMS = {
    "New Zealand", "Fiji", "Vanuatu", "Solomon Islands", "Papua New Guinea",
    "New Caledonia", "Tahiti", "Samoa", "American Samoa", "Tonga",
    "Cook Islands", "Tuvalu", "Micronesia", "Kiribati",
}


def _remove_ofc_qualifiers(df: pd.DataFrame) -> pd.DataFrame:
    qualifier_mask = df["tournament"].str.contains("qualification", case=False, na=False)
    ofc_mask = df["home_team"].isin(_OFC_TEAMS) | df["away_team"].isin(_OFC_TEAMS)
    return df[~(qualifier_mask & ofc_mask)]


def main(since: str | None = None, finals_only: bool = False, all_competitive: bool = False):
    print("Loading data...")
    df = fetch_international_results()
    df = filter_competitive(df)

    if finals_only:
        df = df[df["tournament"].isin(_FINALS_TOURNAMENTS)]
        print(f"  Finals + Nations Leagues only (no qualifiers): {len(df)} matches")
    elif all_competitive:
        if since:
            df = df[df["date"] >= pd.Timestamp(since)]
        print(f"  All competitive matches: {len(df)}")
    else:
        df = _remove_ofc_qualifiers(df)
        if since:
            df = df[df["date"] >= pd.Timestamp(since)]
        print(f"  All competitive excl. OFC qualifiers: {len(df)} matches")

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
    parser.add_argument("--finals-only", action="store_true",
                        help="Use only finals + Nations Leagues (no qualifiers)")
    parser.add_argument("--all", action="store_true",
                        help="Use all competitive matches including OFC qualifiers")
    parser.add_argument("--since", default=None,
                        help="Filter to matches since YYYY-MM-DD")
    args = parser.parse_args()
    main(since=args.since, finals_only=args.finals_only, all_competitive=args.all)
