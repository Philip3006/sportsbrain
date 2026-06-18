"""
Fetches current market odds for open bets and stores them as closing odds.
Run this 1-2h before each WM match kicks off so CLV can be computed after settlement.

Usage:
  python scripts/update_closing_odds.py
  python scripts/update_closing_odds.py --mock   # dry-run, no API call
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.betting.ledger import LEDGER_PATH, _load, _save, _file_lock
from src.config import canonical_name


def main(mock: bool = False) -> None:
    with _file_lock(LEDGER_PATH):
        _main_locked(mock)


def _main_locked(mock: bool) -> None:
    df = _load(LEDGER_PATH)
    if df.empty:
        print("Ledger is empty — nothing to update.")
        return

    open_mask = df["status"] == "open"
    if not open_mask.any():
        print("No open bets — nothing to update.")
        return

    print(f"Fetching current odds for {open_mask.sum()} open bet(s)...")

    if mock:
        print("  [MOCK] Simulating closing odds (no API call).")
        # Simulate slightly moved odds for demo purposes
        for idx in df[open_mask].index:
            odds = float(df.at[idx, "decimal_odds"])
            df.at[idx, "closing_odds"] = f"{odds * 0.97:.4f}"  # market moved against us
        n = int(open_mask.sum())
    else:
        from src.data.odds_api import fetch_upcoming_matches
        try:
            matches = fetch_upcoming_matches(force=True)
        except Exception as e:
            print(f"  API error: {e}")
            return

        # Build lookup: (canonical_home, canonical_away) → match odds dict
        odds_lookup: dict[tuple, dict] = {}
        for m in matches:
            h = canonical_name(m["home_team"])
            a = canonical_name(m["away_team"])
            odds_lookup[(h, a)] = m

        _MARKET_ODDS_KEY = {
            "home":         "home_odds",
            "draw":         "draw_odds",
            "away":         "away_odds",
            "o/u2.5_over":  "over_odds",
            "o/u2.5_under": "under_odds",
            "ah-0.5_home":  "ah_home_odds",
            "ah+0.5_away":  "ah_away_odds",
            "ah-1.0_home":  "ah1_home_odds",
            "ah+1.0_away":  "ah1_away_odds",
            "ah-1.5_home":  "ah15_home_odds",
            "ah+1.5_away":  "ah15_away_odds",
            "btts_yes":     "btts_yes_odds",
            "btts_no":      "btts_no_odds",
        }

        n = 0
        for idx in df[open_mask].index:
            home = canonical_name(str(df.at[idx, "home"]))
            away = canonical_name(str(df.at[idx, "away"]))
            market = str(df.at[idx, "market"])
            match = odds_lookup.get((home, away))
            if match is None:
                continue
            odds_key = _MARKET_ODDS_KEY.get(market)
            if odds_key is None:
                continue
            closing = float(match.get(odds_key, 0))
            if closing > 1.0:
                df.at[idx, "closing_odds"] = f"{closing:.4f}"
                n += 1

    _save(df, LEDGER_PATH)
    print(f"  Updated closing_odds for {n} bet(s) → {LEDGER_PATH}")

    # Also compute CLV for already-settled bets that now have closing_odds but missing clv.
    # This handles the case where a bet settled before closing odds were fetched (09:00 settlement
    # vs 16:00/20:00 closing-odds update), leaving clv="" even though closing_odds is now known.
    n_clv = 0
    settled_mask = df["status"].isin(["won", "lost"])
    for idx in df[settled_mask].index:
        if str(df.at[idx, "clv"]).strip():
            continue  # already has CLV
        try:
            closing = float(df.at[idx, "closing_odds"] or 0)
            if closing <= 1.0:
                continue
            bet_odds = float(df.at[idx, "decimal_odds"] or 0)
            if bet_odds <= 1.0:
                continue
            if closing >= bet_odds * 3.0:
                continue  # pathological closing odds — skip (data corruption guard)
            clv = max(-0.99, min(2.00, bet_odds / closing - 1.0))
            df.at[idx, "clv"] = f"{clv:.4f}"
            n_clv += 1
        except (ValueError, TypeError):
            pass
    if n_clv:
        _save(df, LEDGER_PATH)
        print(f"  Computed CLV for {n_clv} settled bet(s) that were missing it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Simulate without API call")
    args = parser.parse_args()
    main(mock=args.mock)
