"""
One-off: scrapes historical 1X2 odds from Betexplorer for all backtest tournaments.
Results saved to data/raw/tournament_odds.csv — run once, then backtest works.

Usage:
  python scripts/fetch_tournament_odds.py
  python scripts/fetch_tournament_odds.py --tournament WC2022
  python scripts/fetch_tournament_odds.py --dry-run   # tests 3 matches per tournament
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.betexplorer import TOURNAMENT_SLUGS, scrape_all_tournaments, scrape_tournament
from src.config import DATA_RAW


def main():
    parser = argparse.ArgumentParser(description="Scrape historical tournament odds from Betexplorer")
    parser.add_argument("--tournament", default=None, choices=list(TOURNAMENT_SLUGS.keys()),
                        help="Scrape one tournament only (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Test mode: only fetch 3 matches per tournament")
    args = parser.parse_args()

    out = DATA_RAW / "tournament_odds.csv"
    max_matches = 3 if args.dry_run else None

    if args.dry_run:
        print("DRY RUN — fetching max 3 matches per tournament to test connectivity.")

    if args.tournament:
        df_new = scrape_tournament(args.tournament, max_matches=max_matches)
        if not df_new.empty:
            out.parent.mkdir(parents=True, exist_ok=True)
            # Merge with existing data (remove old rows for this tournament first)
            if out.exists():
                existing = pd.read_csv(out)
                existing = existing[existing["tournament"] != args.tournament]
                df_new = pd.concat([existing, df_new], ignore_index=True)
            df_new.to_csv(out, index=False)
            print(f"\nSaved {len(df_new)} total rows to {out}")
            print(df_new[df_new["tournament"] == args.tournament].head(5).to_string(index=False))
    else:
        print("Scraping all tournaments (this takes ~5-10 minutes due to rate limiting)...")
        df = scrape_all_tournaments(max_matches=max_matches)
        if not df.empty:
            print(f"\nTotal rows scraped: {len(df)}")
            print(df.groupby("tournament").size().to_string())
            print(f"\nSaved to {out}")
        else:
            print("\nNo data scraped — check connectivity or Betexplorer page structure.")
            print("If the page structure changed, update src/data/betexplorer.py selectors.")


if __name__ == "__main__":
    main()
