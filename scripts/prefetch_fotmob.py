"""
Pre-fetches Fotmob player ratings for WM 2026 matches and caches them locally.

Scans fotmob.com for all international matches since WM 2026 start (June 11),
downloads per-player ratings and team ratings, stores in data/cache/fotmob/.

Runtime: ~2s/match × number of completed matches (rate-limited).
Each completed match is cached permanently — re-runs only fetch new matches.

Usage:
    python scripts/prefetch_fotmob.py
    python scripts/prefetch_fotmob.py --since 2026-06-11
    python scripts/prefetch_fotmob.py --force   # re-scrape even if cached
"""
import argparse
import sys
import pickle
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.fotmob import fetch_tournament_ratings
from src.config import DATA_CACHE

_FOTMOB_DF_PATH = DATA_CACHE / "fotmob_ratings.pkl"


def main(since: str = "2026-06-11", until: str | None = None, force: bool = False) -> None:
    until = until or pd.Timestamp.now().strftime("%Y-%m-%d")

    print(f"Fetching Fotmob ratings: {since} → {until}")
    print("(2s rate-limit per match — only fetches uncached matches)")

    df = fetch_tournament_ratings(since, until)

    if df.empty:
        print("No ratings data returned. Check network / date range.")
        return

    _FOTMOB_DF_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_FOTMOB_DF_PATH, "wb") as f:
        pickle.dump(df, f)

    team_rows = df[df["player"] == "__team__"]
    player_rows = df[df["player"] != "__team__"]

    print(f"\nDone. Cached at {_FOTMOB_DF_PATH}")
    print(f"  Match-team records:  {len(team_rows)}")
    print(f"  Player records:      {len(player_rows)}")
    print(f"  Teams:               {df['team'].nunique()}")
    print(f"  Date range:          {df['date'].min().date()} – {df['date'].max().date()}")

    print("\nTeam average ratings (latest match):")
    latest = (
        team_rows.sort_values("date", ascending=False)
        .groupby("team")
        .first()
        .reset_index()
        .sort_values("rating", ascending=False)
        .head(10)
    )
    for _, row in latest.iterrows():
        print(f"  {row['team']:25s} {row['rating']:.2f}  ({row['date'].date()})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-06-11", help="Start date YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--force", action="store_true", help="Re-scrape cached matches")
    args = parser.parse_args()
    main(since=args.since, until=args.until, force=args.force)
