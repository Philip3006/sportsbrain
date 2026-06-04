"""
One-off script: fetch and cache all historical data needed for training.
Run once before training: python scripts/bootstrap_data.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.international import fetch_international_results, filter_competitive

if __name__ == "__main__":
    print("Fetching international results...")
    df = fetch_international_results(force=True)
    print(f"  Total matches: {len(df)}")
    print(f"  Date range: {df['date'].min().date()} → {df['date'].max().date()}")

    competitive = filter_competitive(df)
    print(f"  Competitive matches: {len(competitive)}")
    print(f"  Teams: {competitive['home_team'].nunique()} unique")
    print("Done. Data cached to data/cache/international_results.pkl")
