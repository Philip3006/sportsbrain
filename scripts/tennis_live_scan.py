#!/usr/bin/env python3
"""Live In-Play Tennis Scanner (Hebel 5 Aktivierung).

Holt aktuell laufende Matches via TheOddsAPI /scores (mit ESPN-Fallback),
baut LiveMatchState aus dem aktuellen Set+Games-Stand, ruft predict_live_p_a
(mit Momentum-Adjustment via Hebel 5), holt Live-Odds und emittiert Value-
Signale.

Was NICHT verfügbar ist ohne Point-Level-Feed:
  - break_last5_games_a/b (bleibt 0 — kein Break-Detection ohne Games-History)
  - service_hold_streak_a/b (bleibt 0)
Momentum-Score fällt daher zurück auf Set-Lead + Game-Lead-Diff, was den
Hauptbeitrag stellt.

Usage (Cron alle 2 Min während Match-Slots):
  python3 scripts/tennis_live_scan.py
  python3 scripts/tennis_live_scan.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import requests

from src.tennis.discovery import discover_active_tournaments
from src.tennis.ensemble import predict_winner_ensemble
from src.tennis.elo_source import load_match_history
from src.tennis.live.inplay_model import LiveMatchState, value_live_signal
from src.models.tennis_elo import compute_tennis_elo
from src.data.tennis_scores import fetch_tennis_scores

_LIVE_OUT = ROOT / "results" / "tennis_live_signals.json"


def _fetch_live_odds(sport_key: str, api_key: str) -> list[dict]:
    """TheOddsAPI /odds mit markets=h2h — liefert auch In-Play falls active."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    try:
        r = requests.get(url, params={
            "apiKey": api_key, "regions": "eu", "markets": "h2h",
            "oddsFormat": "decimal",
        }, timeout=10)
        return r.json() if r.ok else []
    except Exception:
        return []


def _match_odds(events: list[dict], key_a: str, key_b: str) -> tuple[float | None, float | None]:
    """Findet h2h-Odds für match ({key_a, key_b} tokenized)."""
    def _norm(s: str) -> str:
        return "".join(c.lower() for c in s if c.isalpha())
    a = _norm(key_a); b = _norm(key_b)
    for ev in events:
        pa = _norm(ev.get("home_team", ""))
        pb = _norm(ev.get("away_team", ""))
        if not (({a, b} & {pa}) and ({a, b} & {pb})):
            continue
        book = (ev.get("bookmakers") or [{}])[0]
        h2h = next((m for m in book.get("markets", []) if m.get("key") == "h2h"), None)
        if not h2h:
            continue
        outs = {_norm(o["name"]): o["price"] for o in h2h.get("outcomes", [])}
        return outs.get(a), outs.get(b)
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-edge", type=float, default=0.05,
                    help="Min-Edge für Live-Signal (Live-Bets riskanter → höher)")
    args = ap.parse_args()

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("ODDS_API_KEY fehlt.", file=sys.stderr); return 1

    print("[live-scan] Discovery ...")
    tournaments = discover_active_tournaments()
    if not tournaments:
        print("Keine aktiven Turniere."); return 0

    print(f"[live-scan] {len(tournaments)} Turniere aktiv")
    sport_keys = [k for t in tournaments for k in (t.sport_keys or [t.slug])]

    print("[live-scan] Elo laden ...")
    matches, _src = load_match_history()
    ratings = compute_tennis_elo(matches, reference_date=datetime.utcnow())

    print("[live-scan] Live-Scores ziehen ...")
    live_scores = fetch_tennis_scores(sport_keys)
    if not live_scores:
        print("Keine Live-Scores."); return 0

    all_signals: list[dict] = []
    for match_key, sc in live_scores.items():
        if sc.get("completed"):
            continue
        sets = sc.get("sets") or []
        if not sets:  # noch nicht begonnen
            continue
        pa = sc.get("player_a") or sc.get("home_team") or ""
        pb = sc.get("player_b") or sc.get("away_team") or ""
        if not pa or not pb:
            continue

        # Aktueller Satz: unvollständig (< 6 Games), sonst nächster
        sets_a_won = sum(1 for a, b in sets if a > b)
        sets_b_won = sum(1 for a, b in sets if b > a)
        last = sets[-1]
        current_a, current_b = (last if (max(last) < 6 or abs(last[0] - last[1]) < 2)
                                else (0, 0))

        # Turnier-Info aus Discovery
        t = next((t for t in tournaments if any(sk in match_key for sk in t.sport_keys)),
                 tournaments[0])

        try:
            probs = predict_winner_ensemble(
                pa, pb, ratings, t.surface,
                best_of=t.best_of, category=t.category,
                tournament_slug=t.slug,
            )
        except Exception as e:
            print(f"  [{pa} vs {pb}] pre-match predict failed: {e}"); continue
        p_pre = probs["p_a"]

        state = LiveMatchState(
            p_pre_match_a=p_pre,
            sets_a=sets_a_won, sets_b=sets_b_won,
            current_set_games_a=current_a, current_set_games_b=current_b,
            best_of=t.best_of, tour=t.tour,
        )

        events = _fetch_live_odds(t.sport_keys[0] if t.sport_keys else t.slug, api_key)
        oa, ob = _match_odds(events, pa, pb)
        if oa is None or ob is None:
            continue

        sigs = value_live_signal(state, oa, ob, min_edge=args.min_edge)
        for s in sigs:
            print(f"  LIVE [{t.slug}] {pa} vs {pb} — {s['side']} @ {oa if s['side']=='a' else ob:.2f}"
                  f" (live_p={s['live_p']:.3f}, edge={s['edge']*100:+.1f}%)")
            all_signals.append({
                **s, "player_a": pa, "player_b": pb, "tournament": t.slug,
                "ts": datetime.now(timezone.utc).isoformat(),
                "odds_a": oa, "odds_b": ob,
            })

    if args.dry_run:
        print(f"[DRY-RUN] {len(all_signals)} Live-Signals gefunden.")
        return 0

    _LIVE_OUT.parent.mkdir(parents=True, exist_ok=True)
    _LIVE_OUT.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                                     "signals": all_signals}, indent=2))
    print(f"[live-scan] {len(all_signals)} Live-Signals → {_LIVE_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
