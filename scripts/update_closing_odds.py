"""
Fetches current market odds for open bets and stores them as closing odds.
Run this 1-2h before each WM match kicks off so CLV can be computed after settlement.

Usage:
  python scripts/update_closing_odds.py
  python scripts/update_closing_odds.py --mock   # dry-run, no API call
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.betting.ledger import LEDGER_PATH, _load, _save, _file_lock
from src.config import canonical_name


_FLAT_MARKET_KEYS = {
    "home":         "home_odds",
    "draw":         "draw_odds",
    "away":         "away_odds",
    "o/u1.5_over":  "over15_odds",
    "o/u1.5_under": "under15_odds",
    "o/u2.5_over":  "over_odds",
    "o/u2.5_under": "under_odds",
    "o/u3.5_over":  "over35_odds",
    "o/u3.5_under": "under35_odds",
    "ah-0.5_home":  "ah_home_odds",
    "ah+0.5_away":  "ah_away_odds",
    "ah-1.0_home":  "ah1_home_odds",
    "ah+1.0_away":  "ah1_away_odds",
    "ah-1.5_home":  "ah15_home_odds",
    "ah+1.5_away":  "ah15_away_odds",
    "btts_yes":     "btts_yes_odds",
    "btts_no":      "btts_no_odds",
    "dc_1x":        "dc_1x_odds",
    "dc_x2":        "dc_x2_odds",
    "dc_12":        "dc_12_odds",
}

_RE_OU = re.compile(r"^o/u(\d+(?:\.\d+)?)_(over|under)$")
_RE_AH_SIDED = re.compile(r"^ah([+-]?\d+(?:\.\d+)?)_(home|away)$")
_RE_AH_BARE = re.compile(r"^ah([+-]?\d+(?:\.\d+)?)$")


def _resolve_closing_odds(match: dict, market: str) -> float | None:
    """Returns valid (>1.0) closing odds for a given bet market, or None.

    Tries flat fields first (h2h, fixed O/U lines, fixed AH lines, BTTS, DC),
    then falls back to dynamic `totals_lines` / `spreads` dicts for quarter-balls
    and arbitrary handicaps (e.g. `o/u3.0_over`, `ah+0.5_home`).
    """
    key = _FLAT_MARKET_KEYS.get(market)
    if key is not None:
        try:
            v = float(match.get(key, 0) or 0)
        except (ValueError, TypeError):
            v = 0.0
        return v if v > 1.0 else None

    m = _RE_OU.match(market)
    if m:
        line, side = float(m.group(1)), m.group(2)
        totals = match.get("totals_lines") or {}
        try:
            v = float(totals.get(line, {}).get(side, 0) or 0)
        except (ValueError, TypeError):
            v = 0.0
        return v if v > 1.0 else None

    m = _RE_AH_SIDED.match(market)
    if m:
        line, side = float(m.group(1)), m.group(2)
        home_line = line if side == "home" else -line
        spreads = match.get("spreads") or {}
        try:
            v = float(spreads.get(home_line, {}).get(side, 0) or 0)
        except (ValueError, TypeError):
            v = 0.0
        return v if v > 1.0 else None

    m = _RE_AH_BARE.match(market)
    if m:
        line = float(m.group(1))
        spreads = match.get("spreads") or {}
        try:
            v = float(spreads.get(line, {}).get("home", 0) or 0)
        except (ValueError, TypeError):
            v = 0.0
        return v if v > 1.0 else None

    return None  # unknown market (e.g. scorer_*) — caller skips


def main(mock: bool = False, backfill_only: bool = False) -> None:
    with _file_lock(LEDGER_PATH):
        _main_locked(mock, backfill_only)


def _main_locked(mock: bool, backfill_only: bool = False) -> None:
    df = _load(LEDGER_PATH)
    if df.empty:
        print("Ledger is empty — nothing to update.")
        return

    if backfill_only:
        print("Backfill-only mode: skipping odds fetch, recomputing CLV from existing closing_odds.")
        _backfill_clv(df)
        return

    open_mask = df["status"] == "open"
    if not open_mask.any():
        print("No open bets — running CLV backfill only.")
        _backfill_clv(df)
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

        n = 0
        for idx in df[open_mask].index:
            home = canonical_name(str(df.at[idx, "home"]))
            away = canonical_name(str(df.at[idx, "away"]))
            market = str(df.at[idx, "market"])
            match = odds_lookup.get((home, away))
            if match is None:
                continue
            closing = _resolve_closing_odds(match, market)
            if closing is not None:
                df.at[idx, "closing_odds"] = f"{closing:.4f}"
                n += 1

    _save(df, LEDGER_PATH)
    print(f"  Updated closing_odds for {n} bet(s) → {LEDGER_PATH}")

    _backfill_clv(df)


def _backfill_clv(df) -> None:
    """Computes CLV for settled bets that have valid closing_odds but empty clv.

    Handles the case where a bet settled before closing odds were fetched (settle.yml
    runs hourly, closing_odds.yml separately), leaving clv="" even though closing_odds
    is now known. Also covers historical bets and `void` status.
    """
    import pandas as pd

    def _empty(v) -> bool:
        # Treat NaN (from pandas load of empty CSV cells) and blank strings as empty.
        return pd.isna(v) or not str(v).strip()

    def _to_float(v) -> float:
        if pd.isna(v):
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    n_clv = 0
    settled_mask = df["status"].isin(["won", "lost", "void"])
    for idx in df[settled_mask].index:
        if not _empty(df.at[idx, "clv"]):
            continue
        closing = _to_float(df.at[idx, "closing_odds"])
        if closing <= 1.0:
            continue
        bet_odds = _to_float(df.at[idx, "decimal_odds"])
        if bet_odds <= 1.0:
            continue
        if closing >= bet_odds * 3.0:
            continue  # pathological closing odds — skip (data corruption guard)
        clv = max(-0.99, min(2.00, bet_odds / closing - 1.0))
        df.at[idx, "clv"] = f"{clv:.4f}"
        n_clv += 1
    if n_clv:
        _save(df, LEDGER_PATH)
        print(f"  Computed CLV for {n_clv} settled bet(s) that were missing it.")
    else:
        print("  No CLV backfill needed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Simulate without API call")
    parser.add_argument("--backfill-only", action="store_true",
                        help="Skip API fetch; only recompute CLV from existing closing_odds")
    args = parser.parse_args()
    main(mock=args.mock, backfill_only=args.backfill_only)
