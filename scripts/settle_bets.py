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


# Tracks which source produced the last fetch_scores() result.
# Read by callers (and health-monitoring) to know whether the primary
# TheOddsAPI was used or the ESPN fallback kicked in. Values:
#   "odds_api" | "espn_fallback" | "none"
LAST_SCORES_SOURCE: str = "none"


def _fetch_scores_espn_fallback() -> dict[str, dict]:
    """ESPN public scoreboard fallback — no API key, ~30-60s lag.

    Re-uses the existing _fetch_espn_wm_scores() implementation from
    src.data.odds_api and adapts its schema to the dict expected by settle().
    """
    try:
        from src.data.odds_api import _fetch_espn_wm_scores
    except Exception as e:
        print(f"[settle] ESPN-Fallback Import-Fehler: {e}")
        return {}
    try:
        raw = _fetch_espn_wm_scores()
    except Exception as e:
        print(f"[settle] ESPN-Fallback fetch failed: {e}")
        return {}
    # ESPN uses full country names; ledger may use short forms or aliases.
    _ALIASES: dict[str, list[str]] = {
        "United States": ["USA", "US"],
        "South Korea":   ["Korea Republic", "Korea"],
        "Ivory Coast":   ["Côte d'Ivoire", "Cote d'Ivoire"],
        "Türkiye":       ["Turkey"],
        "DR Congo":      ["Congo DR", "Congo"],
        "Bosnia & Herzegovina": ["Bosnia"],
        "Curacao":       ["Curaçao"],
    }
    # Build reverse map: alias → canonical ESPN name
    _REVERSE: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            _REVERSE[alias.lower()] = canonical

    results: dict[str, dict] = {}
    for m in raw:
        if not m.get("completed"):
            continue
        home = m.get("home", "")
        away = m.get("away", "")
        hs, as_ = m.get("home_score"), m.get("away_score")
        if hs is None or as_ is None:
            continue
        entry = {
            "home": home,
            "away": away,
            "home_score": int(hs),
            "away_score": int(as_),
        }
        results[m.get("match_id", f"espn_{home}_vs_{away}")] = entry
        results[f"{home} vs {away}"] = entry
        # Also register alias keys so ledger entries with short names resolve.
        home_aliases = _ALIASES.get(home, [])
        away_aliases = _ALIASES.get(away, [])
        for ha in (home_aliases or [home]):
            for aa in (away_aliases or [away]):
                if ha != home or aa != away:
                    results[f"{ha} vs {aa}"] = entry
    return results


def fetch_scores() -> dict[str, dict]:
    """Returns {match_id: {home, away, home_score, away_score, completed}}.

    Fallback chain:
      1. TheOddsAPI scores endpoint (primary, requires ODDS_API_KEY)
      2. ESPN public scoreboard (no key, slightly higher lag)

    Transient API problems (401 Quota/Auth, 429 Rate-Limit, 5xx, timeout)
    trigger the ESPN fallback instead of returning empty — keeping settlement
    alive during API outages. Sets module-level LAST_SCORES_SOURCE so callers
    can report which source was used.
    """
    global LAST_SCORES_SOURCE
    LAST_SCORES_SOURCE = "none"
    fallback_reason: str | None = None

    try:
        from scripts._http_retry import retry_request
        r = retry_request(
            "GET",
            API_URL,
            params={"apiKey": _api_key(), "daysFrom": 3},
            timeout=15,
            log_prefix="[settle/odds_api]",
        )
    except requests.RequestException as e:
        fallback_reason = f"network: {e}"
        r = None
    if r is not None and r.status_code in (401, 403, 429):
        fallback_reason = f"odds_api {r.status_code} (Quota/Auth/Rate-Limit)"
        r = None
    if r is not None and r.status_code >= 500:
        fallback_reason = f"odds_api {r.status_code} (server error)"
        r = None
    if r is not None:
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            fallback_reason = f"odds_api HTTP: {e}"
            r = None

    if r is not None:
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
        LAST_SCORES_SOURCE = "odds_api"
        return results

    # ----- ESPN fallback -----
    print(f"[settle] TheOddsAPI nicht verfügbar ({fallback_reason}) — "
          "schalte auf ESPN-Fallback.")
    espn = _fetch_scores_espn_fallback()
    if espn:
        LAST_SCORES_SOURCE = "espn_fallback"
        print(f"[settle] ESPN-Fallback aktiv: {len(espn)//2} completed match(es).")
    else:
        print("[settle] ESPN-Fallback lieferte keine Scores — skipping settlement.")
    return espn


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

    # BTTS — beide Teams treffen
    if market == "btts_yes":
        return "won" if (home_g >= 1 and away_g >= 1) else "lost"
    if market == "btts_no":
        return "won" if (home_g == 0 or away_g == 0) else "lost"

    # Over/Under
    for line in ("2.5", "1.5", "3.5", "0.5", "3.0", "4.5", "4.0", "5.5"):
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
