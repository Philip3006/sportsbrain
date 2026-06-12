"""
Builds walk-forward Dixon-Coles snapshots, one per historical tournament edition.

Each snapshot is fit on all competitive international matches BEFORE the
edition's start date, with time-decay reference set to that start date.
This eliminates temporal leakage when validating models against historical
tournament outcomes (e.g. for rho-stage fitting in fit_rho_stages.py).

Editions covered: WC 1998–2022, Euro 2000–2024, Copa América 2016–2024.

Output: models/dixon_coles/snapshots/params_<tournament>_<year>.pkl
Run:    python3 scripts/build_dc_snapshots.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR
from src.data.international import fetch_international_results, filter_competitive
from src.models import dixon_coles as dc


TOURNAMENTS = ("FIFA World Cup", "UEFA Euro", "Copa América", "Copa America")


def discover_editions(df: pd.DataFrame) -> list[tuple[str, int, pd.Timestamp]]:
    """Returns [(tournament, year, start_date), ...] ordered by date.
    Splits editions per tournament by 60-day gaps.
    """
    out: list[tuple[str, int, pd.Timestamp]] = []
    for t in TOURNAMENTS:
        sub = df[df["tournament"] == t].sort_values("date")
        if sub.empty:
            continue
        gap = sub["date"].diff().dt.days.fillna(99999) > 60
        edition_id = gap.cumsum().astype(int)
        for eid, grp in sub.groupby(edition_id):
            start = grp["date"].min()
            year = int(start.year)
            out.append((t, year, start))
    out.sort(key=lambda x: x[2])
    return out


def main(min_year: int = 1998):
    print("Loading data...")
    all_df = fetch_international_results()
    competitive = filter_competitive(all_df)
    print(f"  {len(competitive)} competitive matches total")

    editions = [e for e in discover_editions(all_df) if e[1] >= min_year]
    print(f"  {len(editions)} editions to snapshot (since {min_year})")

    snap_dir = MODELS_DIR / "dixon_coles" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    for tournament, year, start in editions:
        tag = tournament.replace(" ", "").replace("é", "e").replace("É", "E")
        out_path = snap_dir / f"params_{tag}_{year}.pkl"
        if out_path.exists():
            print(f"  [skip] {tournament} {year} (snapshot exists)")
            continue

        train = competitive[competitive["date"] < start]
        if len(train) < 200:
            print(f"  [skip] {tournament} {year}: only {len(train)} prior matches")
            continue

        print(f"  Fitting {tournament} {year}  (n={len(train)}, cutoff={start.date()})...",
              end="", flush=True)
        params = dc.fit(train, today=start, max_iter=2000)
        dc.save(params, out_path)
        print(f" rho={params.rho:.3f}  home_adv={params.home_adv:.3f}  → {out_path.name}")

    print(f"\nDone. Snapshots in {snap_dir}/")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-year", type=int, default=1998)
    args = parser.parse_args()
    main(min_year=args.min_year)
