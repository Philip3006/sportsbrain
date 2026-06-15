"""
Pre-fetches StatsBomb per-player xG data and caches it locally.

Run once before training to populate data/cache/statsbomb_player_xg.pkl.
Also rebuilds the team-level StatsBomb xG cache (statsbomb_xg.pkl).

Runtime: ~10-20 minutes (0.5s/event-file × thousands of matches across WC/Euro/Copa).

Usage:
  python scripts/prefetch_player_xg.py
  python scripts/prefetch_player_xg.py --force   # force re-fetch even if cache is fresh
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.statsbomb import fetch_statsbomb_player_xg, _PLAYER_CACHE_PATH


def main(force: bool = False) -> None:
    print("Fetching StatsBomb per-player xG data...")
    print("(This fetches event data for all WC/Euro/Copa matches — takes ~10-20 min)")

    df = fetch_statsbomb_player_xg(force=force)

    if df.empty:
        print("No player xG data returned. Check network connectivity.")
        return

    print(f"\nDone. {len(df)} player-match records cached at {_PLAYER_CACHE_PATH}")
    print(f"  Tournaments: {sorted(df['tournament'].unique())}")
    print(f"  Date range:  {df['date'].min().date()} – {df['date'].max().date()}")
    print(f"  Teams:       {df['team'].nunique()}")
    print(f"  Players:     {df['player'].nunique()}")

    print("\nTop xG contributors (all-time):")
    top = df.groupby("player")["xg"].sum().sort_values(ascending=False).head(10)
    for player, xg in top.items():
        print(f"  {player:30s} {xg:.2f} xG")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Force re-fetch even if cache is fresh")
    args = parser.parse_args()
    main(force=args.force)
