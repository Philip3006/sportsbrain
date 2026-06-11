"""
Auto-settle open bets using Odds API scores endpoint.
Fetches completed match results, determines win/loss for each market type,
updates ledger P&L. Run after each match day (or on a cron).

Usage:
  python3 scripts/settle_bets.py           # settle all completable open bets
  python3 scripts/settle_bets.py --dry-run # show what would be settled
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "results" / "ledger.csv"
API_URL = "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/scores/"


def _api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if "ODDS_API_KEY" in line:
                    key = line.split("=", 1)[1].strip().strip('"')
                    break
    return key


def fetch_scores() -> dict[str, dict]:
    """Returns {match_id: {home, away, home_score, away_score, completed}}."""
    r = requests.get(
        API_URL,
        params={"apiKey": _api_key(), "daysFrom": 3},
        timeout=15,
    )
    r.raise_for_status()
    results = {}
    for m in r.json():
        if not m.get("completed") or not m.get("scores"):
            continue
        scores = {s["name"]: int(s["score"]) for s in m["scores"]}
        home = m["home_team"]
        away = m["away_team"]
        results[m["id"]] = {
            "home": home,
            "away": away,
            "home_score": scores.get(home, 0),
            "away_score": scores.get(away, 0),
        }
        # Also index by "Home vs Away" string for fallback matching
        results[f"{home} vs {away}"] = results[m["id"]]
    return results


def _settle_market(market: str, home_g: int, away_g: int) -> str | None:
    """
    Returns 'won', 'lost', 'push', or None (unsupported/unresolvable).
    """
    total = home_g + away_g
    diff = home_g - away_g  # positive = home winning

    if market == "home":
        return "won" if diff > 0 else "lost"
    if market == "away":
        return "won" if diff < 0 else "lost"
    if market == "draw":
        return "won" if diff == 0 else "lost"

    # Over/Under
    for line in ("2.5", "1.5", "3.5", "0.5"):
        thresh = float(line)
        if market == f"o/u{line}_over":
            return "won" if total > thresh else "lost"
        if market == f"o/u{line}_under":
            return "won" if total < thresh else "lost"

    # Asian Handicap: ah{line}_{side}
    # line can be -0.5, +0.5, -1.0, -1.5, +1.5 etc.
    if market.startswith("ah"):
        try:
            parts = market[2:].rsplit("_", 1)
            line_val = float(parts[0])
            side = parts[1]  # home or away
        except (ValueError, IndexError):
            return None

        # Adjust score with handicap
        if side == "home":
            adj = diff + line_val  # home margin after handicap
        else:
            adj = -diff + line_val  # away margin after handicap

        # Quarter-ball handicap (e.g. -0.75): split bet
        # We simplify: treat as half-win/half-loss → return push so no P&L change
        # Full-ball (e.g. -1.0): can push
        if adj > 0:
            return "won"
        elif adj < 0:
            return "lost"
        else:
            return "push"  # exact line hit = refund

    return None


def _pnl(result: str, odds: float, stake: float) -> float:
    if result == "won":
        return round((odds - 1) * stake, 2)
    if result == "lost":
        return round(-stake, 2)
    return 0.0  # push = refund


def settle(dry_run: bool = False) -> int:
    if not LEDGER.exists():
        print("Ledger not found.")
        return 0

    scores = fetch_scores()
    print(f"Scores API: {len(scores)//2} completed matches")

    rows = list(csv.DictReader(LEDGER.open()))
    open_bets = [r for r in rows if r["status"] == "open"]
    print(f"Open bets: {len(open_bets)}")

    settled = 0
    for r in open_bets:
        home, away = r["home"], r["away"]
        match_key = f"{home} vs {away}"
        sc = scores.get(r["match_id"]) or scores.get(match_key)
        if not sc:
            continue

        result = _settle_market(r["market"], sc["home_score"], sc["away_score"])
        if result is None:
            print(f"  ⚠️  Unknown market: {r['market']} — skipping")
            continue

        odds = float(r["decimal_odds"])
        stake = float(r["stake_amount"])
        profit = _pnl(result, odds, stake)
        icon = "✅ WON" if result == "won" else ("↩️ PUSH" if result == "push" else "❌ LOST")

        print(f"  {icon} {home} vs {away} [{sc['home_score']}-{sc['away_score']}] "
              f"{r['market']} @ {odds} → P&L: {profit:+.2f}€")

        if not dry_run:
            r["status"] = result if result in ("won", "lost") else "push"
            r["pnl"] = str(profit)

        settled += 1

    if not dry_run and settled:
        fieldnames = rows[0].keys()
        with LEDGER.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"\n✓ {settled} bet(s) settled and ledger updated.")
    elif dry_run:
        print(f"\n[dry-run] {settled} bet(s) would be settled.")
    else:
        print("\nNo bets to settle yet.")

    return settled


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-settle open WM bets")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settle(dry_run=args.dry_run)
