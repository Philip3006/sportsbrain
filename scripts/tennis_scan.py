#!/usr/bin/env python3
"""
Tennis-Scanner (Roadmap J2-D) — Multi-Tournament-Dispatcher.

Ganzjähriger Betrieb: ruft Discovery (TheOddsAPI /sports), iteriert über alle
aktiven ATP/WTA-Turniere ab Kategorie 250 aufwärts, holt Quoten via TheOddsAPI,
detektiert Value pro Match (Match Winner + Set AH + First Set + O/U Sets +
O/U Games + Set Betting), schreibt Ledger + Push + Dashboard-JSON.

Mode-Flag pro Kategorie (src.config.TENNIS_CATEGORY_MODE):
  'live'   → Bets ins Ledger
  'shadow' → nur Logging in results/tennis_scan_shadow_*.md, kein Ledger-Write

Usage:
  python3 scripts/tennis_scan.py [--mock] [--bankroll 100] [--all-live]
                                 [--no-ledger] [--no-push]
  python3 scripts/tennis_scan.py --tournament wimbledon_atp   # nur ein Event
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import requests

from scripts._http_retry import retry_request
from src.config import (
    MIN_EDGE,
    TENNIS_CATEGORY_MODE,
    TENNIS_MIN_EDGE_BY_CATEGORY,
)
from src.models.tennis_elo import compute_tennis_elo, predict_winner, top_players
from src.tennis.elo_source import load_match_history
from src.betting.tennis_detector import (
    detect_set_betting,
    detect_total_games,
    detect_total_sets,
    detect_value_tennis,
)
from src.betting.ledger import append_bets, ledger_summary
from src.notifications.web_push import send_scan_alert as _web_push_scan_alert
from src.notifications.web_dashboard import write_signals_json_all_users
from src.tennis.discovery import discover_active_tournaments
from src.tennis.tournaments import Tournament, get_tournament

_ODDS_API_URL = "https://api.the-odds-api.com/v4"
_MIN_BOOKMAKERS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def category_min_edge(category: str) -> float:
    """Per-Category Edge-Floor mit globalem MIN_EDGE als Fallback."""
    return TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE)


def category_mode(category: str, all_live: bool = False) -> str:
    """'live'|'shadow' für die Kategorie. --all-live override (Backtest-Bypass)."""
    if all_live:
        return "live"
    return TENNIS_CATEGORY_MODE.get(category, "shadow")


def _fetch_both_tours():
    """Sackmann primary, tennis-data.co.uk XLSX als Fallback (J2-I)."""
    df, source = load_match_history()
    if source == "xlsx-fallback":
        print(f"  [elo] Sackmann nicht verfügbar → XLSX-Fallback ({len(df)} Matches)")
    elif source == "empty":
        print("  [elo] WARNING: weder Sackmann noch XLSX verfügbar — Default-Elo")
    return df


# ---------------------------------------------------------------------------
# Odds-Fetch (TheOddsAPI primär, WebSearch-Fallback Stub)
# ---------------------------------------------------------------------------

def _websearch_tennis_fallback(player_a: str, player_b: str,
                                tournament: str = "") -> dict | None:
    """2-way Tennis-Odds via DuckDuckGo (Roadmap J2-I).

    Returns {"a": float, "b": float} oder None. Sanity-Check: 1/a+1/b ∈ [0.95, 1.15].
    """
    try:
        import json as _json
        import re
        import requests as _req
        from ddgs import DDGS
    except Exception:
        return None

    suffix = f" {tournament}" if tournament else ""
    query = f"{player_a} vs {player_b}{suffix} tennis odds decimal"
    try:
        results = DDGS().text(query, max_results=4)
    except Exception:
        return None
    if not results:
        return None

    headers = {"User-Agent": "Mozilla/5.0 (compatible; SportsBrainBot/1.0)"}
    for r in results:
        url = r.get("href", "")
        if not url or any(skip in url for skip in ("twitter", "youtube", "instagram")):
            continue
        try:
            resp = _req.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                continue
            html = resp.text

            # JSON-LD strukturierte Sportdaten
            for block in re.findall(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.DOTALL,
            ):
                try:
                    ld = _json.loads(block)
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        if item.get("@type") in ("SportsEvent", "Event"):
                            offers = item.get("offers", [])
                            if isinstance(offers, list) and len(offers) >= 2:
                                prices = [float(o.get("price", 0))
                                          for o in offers if o.get("price")]
                                if len(prices) >= 2 and all(1.01 < p < 50 for p in prices[:2]):
                                    a, b = prices[0], prices[1]
                                    if 0.95 <= (1/a + 1/b) <= 1.15:
                                        return {"a": a, "b": b}
                except Exception:
                    continue

            # Regex-Fallback: zwei aufeinanderfolgende Decimal-Quoten
            # Muster: "Player A 1.85 ... Player B 2.05"
            pa_short = player_a.split()[-1] if " " in player_a else player_a
            pb_short = player_b.split()[-1] if " " in player_b else player_b
            pat = rf'{re.escape(pa_short)}[^\d]{{0,40}}(\d\.\d{{2}})[^\d]{{0,80}}{re.escape(pb_short)}[^\d]{{0,40}}(\d\.\d{{2}})'
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                a, b = float(m.group(1)), float(m.group(2))
                if all(1.01 < x < 50 for x in (a, b)) and 0.95 <= (1/a + 1/b) <= 1.15:
                    return {"a": a, "b": b}
        except Exception:
            continue
    return None


def _fetch_events_only(api_key: str, sport_key: str) -> list[dict]:
    """TheOddsAPI /events-Endpoint (kein Quoten-Pull, kein Quota-Verbrauch).

    Genutzt als Fallback wenn /odds 422 liefert aber Turnier kurz vorm Start
    ist — gibt mindestens Paarungen + commence_time zurück, sodass WebSearch
    pro Match versuchen kann.
    """
    url = f"{_ODDS_API_URL}/sports/{sport_key}/events"
    try:
        resp = retry_request("GET", url, params={"apiKey": api_key}, timeout=15,
                             log_prefix=f"[tennis-events:{sport_key}]")
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def _parse_event_markets(event: dict, sport_key: str) -> dict | None:
    """Aggregiert beste Quoten aus event.bookmakers über alle benötigten Märkte."""
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    match_id = event.get("id", f"{home}_vs_{away}")
    commence = event.get("commence_time", "")

    bookmakers = event.get("bookmakers", [])
    ws_h2h: dict | None = None
    if len(bookmakers) < _MIN_BOOKMAKERS:
        ws_h2h = _websearch_tennis_fallback(home, away, tournament=sport_key)
        if not ws_h2h:
            print(f"  [skip] {home} vs {away} — nur {len(bookmakers)} Bookies + no WebSearch")
            return None
        print(f"  [websearch] {home} vs {away} — h2h via WebSearch "
              f"({ws_h2h['a']:.2f}/{ws_h2h['b']:.2f})")

    best = {
        "h2h_a": 0.0, "h2h_b": 0.0,
        "spread_a": 0.0, "spread_b": 0.0,
        "fs_a": 0.0, "fs_b": 0.0,
        "totals_over": {}, "totals_under": {},   # line → best odds
        "games_over": {}, "games_under": {},     # line → best odds
        "scorelines": {},                         # "2-1" → best odds
    }

    for bm in bookmakers:
        for mkt in bm.get("markets", []):
            key = mkt.get("key", "")
            outcomes = mkt.get("outcomes", [])

            if key == "h2h":
                for o in outcomes:
                    if o["name"] == home:
                        best["h2h_a"] = max(best["h2h_a"], o["price"])
                    elif o["name"] == away:
                        best["h2h_b"] = max(best["h2h_b"], o["price"])

            elif key == "spreads":
                for o in outcomes:
                    if abs(abs(o.get("point", 0)) - 1.5) < 0.1:
                        if o["name"] == home:
                            best["spread_a"] = max(best["spread_a"], o["price"])
                        elif o["name"] == away:
                            best["spread_b"] = max(best["spread_b"], o["price"])

            elif key == "set_winner":
                for o in outcomes:
                    desc = o.get("description", "").lower()
                    if "set 1" in desc or "1st set" in desc:
                        if o["name"] == home:
                            best["fs_a"] = max(best["fs_a"], o["price"])
                        elif o["name"] == away:
                            best["fs_b"] = max(best["fs_b"], o["price"])

            elif key in ("totals", "alternate_totals"):
                # TheOddsAPI: name="Over"/"Under", point=line
                for o in outcomes:
                    line = float(o.get("point", 0))
                    if o.get("name", "").lower() == "over":
                        prev = best["totals_over"].get(line, 0.0)
                        best["totals_over"][line] = max(prev, o["price"])
                    elif o.get("name", "").lower() == "under":
                        prev = best["totals_under"].get(line, 0.0)
                        best["totals_under"][line] = max(prev, o["price"])

            elif key == "set_betting":
                # name like "2-0", "3-1" usw.
                for o in outcomes:
                    sc = o.get("name", "")
                    if "-" in sc:
                        prev = best["scorelines"].get(sc, 0.0)
                        best["scorelines"][sc] = max(prev, o["price"])

    if (not best["h2h_a"] or not best["h2h_b"]) and ws_h2h:
        best["h2h_a"] = ws_h2h["a"]
        best["h2h_b"] = ws_h2h["b"]

    if not best["h2h_a"] or not best["h2h_b"]:
        return None

    return {
        "match_id": match_id,
        "commence_time": commence,
        "player_a": home,
        "player_b": away,
        "odds_a": best["h2h_a"],
        "odds_b": best["h2h_b"],
        "ah_odds_a": best["spread_a"],
        "ah_odds_b": best["spread_b"],
        "first_set_odds_a": best["fs_a"],
        "first_set_odds_b": best["fs_b"],
        "totals_over": best["totals_over"],
        "totals_under": best["totals_under"],
        "scorelines": best["scorelines"],
        "sport_key": sport_key,
    }


def fetch_tournament_odds(api_key: str, sport_key: str) -> list[dict]:
    """TheOddsAPI-Odds für ein Turnier. Multi-Market: h2h + spreads + set_winner
    + totals + set_betting. Returns leere Liste bei 404/422 (Markt nicht offen)."""
    url = f"{_ODDS_API_URL}/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h,spreads,set_winner,totals,set_betting",
        "oddsFormat": "decimal",
    }
    try:
        resp = retry_request("GET", url, params=params, timeout=15, log_prefix=f"[tennis:{sport_key}]")
        resp.raise_for_status()
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 422):
            # J2-I: Quoten-Markt zu, aber /events ist oft schon offen.
            # Bei Match-Start <48h: WebSearch-Fallback pro Paarung versuchen.
            events = _fetch_events_only(api_key, sport_key)
            now_utc = datetime.utcnow()
            soon = []
            for ev in events:
                try:
                    ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
                    hours = (ct.replace(tzinfo=None) - now_utc).total_seconds() / 3600
                except Exception:
                    continue
                if 0 < hours <= 48:
                    soon.append(ev)
            if not soon:
                print(f"  [{sport_key}] Markt nicht offen (HTTP {code}) — kein Event in 48h-Fenster")
                return []
            print(f"  [{sport_key}] Markt nicht offen (HTTP {code}) — "
                  f"{len(soon)} Matches <48h → WebSearch-Pfad")
            matches = []
            for ev in soon:
                ev.setdefault("bookmakers", [])
                parsed = _parse_event_markets(ev, sport_key)
                if parsed:
                    matches.append(parsed)
            return matches
        raise

    remaining = int(resp.headers.get("x-requests-remaining", 999))
    if remaining < 20:
        try:
            from src.notifications.web_push import send_quota_alert
            send_quota_alert(remaining)
        except Exception:
            pass

    matches = []
    for event in resp.json():
        parsed = _parse_event_markets(event, sport_key)
        if parsed:
            matches.append(parsed)
    return matches


# ---------------------------------------------------------------------------
# Pro-Match Value-Detection (alle Märkte)
# ---------------------------------------------------------------------------

def detect_all_markets(
    m: dict,
    probs: dict,
    bankroll: float,
    min_edge: float,
    tournament: Tournament,
) -> list:
    """Aggregiert alle Detector-Outputs für ein Match."""
    signals = []

    # 1. Match Winner + Set AH + First Set (bestehend)
    signals.extend(detect_value_tennis(
        player_a=m["player_a"],
        player_b=m["player_b"],
        probs=probs,
        odds_a=m["odds_a"],
        odds_b=m["odds_b"],
        bankroll=bankroll,
        match_id=m["match_id"],
        ah_odds_a=m.get("ah_odds_a", 0.0),
        ah_odds_b=m.get("ah_odds_b", 0.0),
        first_set_odds_a=m.get("first_set_odds_a", 0.0),
        first_set_odds_b=m.get("first_set_odds_b", 0.0),
        min_edge=min_edge,
        tour=tournament.tour,
    ))

    # 2. O/U Sets (Phase C)
    totals_over = m.get("totals_over") or {}
    totals_under = m.get("totals_under") or {}
    # Heuristik: line muss zum Format passen — BO3 typisch 2.5, BO5 typisch 3.5
    expected_line = 3.5 if tournament.best_of == 5 else 2.5
    for line, odds_over in totals_over.items():
        if abs(line - expected_line) > 0.6:
            continue  # Game-Total-Lines (z.B. 21.5) hier nicht — getrennt unten
        odds_under = totals_under.get(line, 0.0)
        if odds_under <= 1.0:
            continue
        signals.extend(detect_total_sets(
            player_a=m["player_a"], player_b=m["player_b"],
            p_match_a=probs["p_a"],
            odds_over=odds_over, odds_under=odds_under,
            line=line, best_of=tournament.best_of,
            bankroll=bankroll, match_id=m["match_id"],
            min_edge=min_edge, tour=tournament.tour,
        ))

    # 3. O/U Games (Lines typischerweise 15-30)
    for line, odds_over in totals_over.items():
        if line < 10 or line > 50:
            continue
        odds_under = totals_under.get(line, 0.0)
        if odds_under <= 1.0:
            continue
        signals.extend(detect_total_games(
            player_a=m["player_a"], player_b=m["player_b"],
            p_match_a=probs["p_a"],
            odds_over=odds_over, odds_under=odds_under,
            line=line, best_of=tournament.best_of,
            bankroll=bankroll, match_id=m["match_id"],
            min_edge=min_edge, tour=tournament.tour,
        ))

    # 4. Set Betting (exakte Scorelines)
    scorelines = m.get("scorelines") or {}
    if scorelines:
        signals.extend(detect_set_betting(
            player_a=m["player_a"], player_b=m["player_b"],
            p_match_a=probs["p_a"],
            scoreline_odds=scorelines,
            best_of=tournament.best_of,
            bankroll=bankroll, match_id=m["match_id"],
            min_edge=min_edge, tour=tournament.tour,
        ))

    return signals


# ---------------------------------------------------------------------------
# Mock-Setup (für --mock)
# ---------------------------------------------------------------------------

def _mock_tournament_matches() -> tuple[Tournament, list[dict]]:
    """Synthetic Wimbledon + mock matches für Dry-Run-Tests."""
    t = get_tournament("wimbledon_atp")
    matches = [
        {
            "match_id": "mock_alcaraz_djokovic",
            "commence_time": "2026-07-06T13:00:00Z",
            "player_a": "Carlos Alcaraz",
            "player_b": "Novak Djokovic",
            "odds_a": 1.75, "odds_b": 2.10,
            "ah_odds_a": 2.00, "ah_odds_b": 1.85,
            "first_set_odds_a": 1.72, "first_set_odds_b": 2.10,
            "totals_over": {3.5: 1.95}, "totals_under": {3.5: 1.85},
            "scorelines": {"3-0": 4.50, "3-1": 3.80, "3-2": 4.80,
                           "2-3": 8.0, "1-3": 11.0, "0-3": 21.0},
            "sport_key": "tennis_atp_wimbledon",
        },
    ]
    return t, matches


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _market_label(market: str, a: str, b: str) -> str:
    labels = {
        "home": f"Match Winner: {a}", "away": f"Match Winner: {b}",
        "ah-1.5_a": f"{a} Set AH -1.5", "ah+1.5_b": f"{b} Set AH +1.5",
        "first_set_a": f"1. Satz: {a}", "first_set_b": f"1. Satz: {b}",
    }
    if market in labels:
        return labels[market]
    if market.startswith("o/u_sets_"):
        return f"O/U Sätze {market.split('_')[-2]} {market.split('_')[-1]}"
    if market.startswith("o/u_games_"):
        return f"O/U Games {market.split('_')[-2]} {market.split('_')[-1]}"
    if market.startswith("score_"):
        return f"Set-Score {market[6:]}"
    return market


def format_scan_report(
    per_tournament: dict[str, dict],
    scan_date: str,
) -> str:
    lines = [f"# Tennis Scan {scan_date}\n"]
    total_sigs = sum(len(v["signals"]) for v in per_tournament.values())
    lines += [f"**Aktive Turniere:** {len(per_tournament)} · "
              f"**Signals total:** {total_sigs}\n"]
    for slug, info in per_tournament.items():
        t: Tournament = info["tournament"]
        signals = info["signals"]
        mode = info["mode"]
        emoji = "🔴 LIVE" if mode == "live" else "👤 SHADOW"
        lines += [f"\n## {t.name} ({t.tour.upper()}) · {t.category} · {emoji}",
                  f"Surface: {t.surface} · Best of: {t.best_of} · Matches gescannt: {info['n_matches']}"]
        if not signals:
            lines.append("_Keine Value-Signals._")
            continue
        for s in sorted(signals, key=lambda x: x.ev, reverse=True):
            lines += [
                f"- **{s.home} vs {s.away}** · {_market_label(s.market, s.home, s.away)}",
                f"  Quote {s.decimal_odds:.2f} · Modell {s.model_prob*100:.1f}% · "
                f"EV +{s.ev*100:.1f}% · Stake {s.stake_eur:.2f}€ · {s.confidence}",
            ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Tennis value bet scanner (multi-tournament)")
    parser.add_argument("--mock", action="store_true", help="Use mock tournament (Wimbledon)")
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--no-ledger", action="store_true")
    parser.add_argument("--no-push", "--no-telegram", action="store_true", dest="no_push",
                        help="Skip Web-Push (--no-telegram als Alias für Legacy-Cron-Calls)")
    parser.add_argument("--all-live", action="store_true",
                        help="J2-D: erzwingt mode=live für alle Kategorien (Backtest-Bypass)")
    parser.add_argument("--tournament", default=None,
                        help="Nur ein Turnier scannen (slug aus Registry, z.B. wimbledon_atp)")
    args = parser.parse_args()

    scan_date = datetime.now().strftime("%Y-%m-%d")
    print(f"Tennis Scan — {scan_date}")

    # ---- 1. Discovery aktiver Turniere ----
    if args.mock:
        t, matches = _mock_tournament_matches()
        tournaments = [t]
        mock_map = {t.slug: matches}
    elif args.tournament:
        t = get_tournament(args.tournament)
        if not t:
            print(f"ERROR: Unbekannter slug '{args.tournament}'.")
            sys.exit(1)
        tournaments = [t]
        mock_map = None
    else:
        api_key = os.getenv("ODDS_API_KEY", "")
        if not api_key:
            print("ERROR: ODDS_API_KEY not set.")
            sys.exit(1)
        tournaments = discover_active_tournaments(api_key=api_key)

    if not tournaments:
        print("Keine aktiven Turniere — beende.")
        sys.exit(0)

    tournament_names = ", ".join(t.slug for t in tournaments)
    print(f"  Aktive Turniere ({len(tournaments)}): {tournament_names}")

    # ---- 2. Match-Daten + Elo ----
    print("Loading ATP+WTA match data...")
    all_matches = _fetch_both_tours()
    if all_matches is None or all_matches.empty:
        print("WARNING: Keine Match-Daten — Default-Elo verwendet.")
        from src.models.tennis_elo import TennisEloRatings
        ratings = TennisEloRatings()
        top_grass = []
    else:
        print(f"  {len(all_matches)} matches loaded; computing Elo...")
        ratings = compute_tennis_elo(all_matches, reference_date=datetime.now())
        top_grass = top_players(ratings, surface="grass", n=10)

    # ---- 3. Pro Turnier scannen ----
    per_tournament: dict[str, dict] = {}
    all_live_signals: list = []

    for t in tournaments:
        mode = category_mode(t.category, all_live=args.all_live)
        min_edge = category_min_edge(t.category)

        # Odds holen
        if args.mock:
            upcoming = mock_map.get(t.slug, [])
        else:
            api_key = os.getenv("ODDS_API_KEY", "")
            sport_key = t.sport_keys[0] if t.sport_keys else t.slug
            try:
                upcoming = fetch_tournament_odds(api_key, sport_key)
            except Exception as exc:
                print(f"  [{t.slug}] Odds-Fehler: {exc} — skip")
                continue

        if not upcoming:
            per_tournament[t.slug] = {
                "tournament": t, "signals": [], "n_matches": 0, "mode": mode,
            }
            continue

        # Pro Match Predict + Detect
        signals = []
        for m in upcoming:
            pa, pb = m["player_a"], m["player_b"]
            probs = predict_winner(pa, pb, ratings, t.surface)
            sigs = detect_all_markets(m, probs, args.bankroll, min_edge, t)
            signals.extend(sigs)
            for s in sigs:
                print(f"  [{t.slug}] {pa} vs {pb} — {s.market} EV+{s.ev*100:.1f}% @{s.decimal_odds:.2f}")

        per_tournament[t.slug] = {
            "tournament": t, "signals": signals, "n_matches": len(upcoming), "mode": mode,
        }
        if mode == "live":
            all_live_signals.extend(signals)

    print(f"\n=== Summary ===")
    print(f"Live-Signals: {len(all_live_signals)} (über {sum(1 for v in per_tournament.values() if v['mode']=='live')} Live-Kategorien)")

    # ---- Signal Archive (I9) ----
    _all_tennis_signals = [s for v in per_tournament.values() for s in v["signals"]]
    _tennis_selected = {(s.match_id, s.market) for s in all_live_signals}
    from src.scanner.output import archive_signals
    _n_archived = archive_signals(_all_tennis_signals, _tennis_selected, scan_date, sport="tennis")
    if _n_archived:
        print(f"Archive: {_n_archived} neue Tennis-Signals archiviert.")

    # ---- 4. Report ----
    report_path = ROOT / "results" / f"tennis_scan_{scan_date}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(format_scan_report(per_tournament, scan_date))
    print(f"Report: {report_path}")

    # ---- 5. Ledger (nur Live-Signals) ----
    if all_live_signals and not args.no_ledger:
        n = append_bets(all_live_signals, args.bankroll)
        print(f"Ledger: {n} Live-Bets eingetragen.")

    # ---- 6. Push ----
    if not args.no_push and all_live_signals:
        summary = ledger_summary()
        _web_push_scan_alert(all_live_signals, summary, scan_date)
        print("Push: Notification gesendet.")

    # ---- 7. Dashboard-JSON ----
    dashboard_summary = ledger_summary()
    match_tour_map: dict[str, str] = {}
    match_tournament_map: dict[str, dict] = {}
    kickoff_map: dict[str, str] = {}
    schedule = []
    # Pro Signal: Match-Tour + Tournament-Meta hinterlegen (J2-E)
    for slug, info in per_tournament.items():
        t = info["tournament"]
        meta = {
            "name": t.name, "category": t.category,
            "surface": t.surface, "best_of": t.best_of,
        }
        for s in info["signals"]:
            match_tour_map[s.match_id] = t.tour
            match_tournament_map[s.match_id] = meta
        # Schedule (nur Mock — Live-Fetch wäre Doppel-API-Call)
        if args.mock and mock_map:
            for m in mock_map.get(slug, []):
                mid = m["match_id"]
                kickoff_map[mid] = m.get("commence_time", "")
                schedule.append({
                    "sport": "tennis", "home": m["player_a"], "away": m["player_b"],
                    "kickoff": m.get("commence_time", ""), "tour": t.tour,
                    "tournament": t.name, "category": t.category, "surface": t.surface,
                })

    write_signals_json_all_users(
        tennis=all_live_signals,
        portfolio=dashboard_summary,
        top_elo=top_grass,
        tennis_tour_map=match_tour_map,
        tennis_tournament_map=match_tournament_map,
        kickoff_map=kickoff_map,
        schedule=schedule,
    )
    print("Dashboard: docs/data/signals.json aktualisiert.")


if __name__ == "__main__":
    main()
