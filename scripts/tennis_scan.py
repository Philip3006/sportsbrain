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
    TENNIS_CATEGORY_SURFACE_MODE,
    TENNIS_MIN_EDGE_BY_CATEGORY,
)
from src.models.tennis_elo import compute_tennis_elo, predict_winner, top_players
from src.tennis.ensemble import predict_winner_ensemble
from src.tennis.elo_source import load_match_history
from src.betting.tennis_detector import (
    detect_set_betting,
    detect_total_games,
    detect_total_sets,
    detect_value_tennis,
)
from src.betting.ledger import append_bets, ledger_summary, clv_by_source
from src.betting.gates import gate_for, adaptive_gate
from scripts._bet_confirm import confirm_bets as _confirm_bets
from src.notifications.web_push import send_scan_alert as _web_push_scan_alert
from src.notifications.web_dashboard import write_signals_json_all_users
from src.tennis.discovery import discover_active_tournaments
from src.tennis.tournaments import Tournament, get_tournament

_ODDS_API_URL = "https://api.the-odds-api.com/v4"
# Schwelle auf 1 gesenkt: Multi-Bookie-Consensus wird jetzt vom Merger
# (src/tennis/odds/) via TE/OP/Betfair übernommen. Selbst ein einzelner Bookie
# in TheOddsAPI ist wertvoller als sofort in Fallback zu gehen.
_MIN_BOOKMAKERS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_clv_adj_edge_delta() -> float:
    """Einmalig: liest Ledger-CLV und gibt Δmin_edge zurück.

    Positives Δ → min_edge sinkt (wir schlagen die Closing-Line).
    Negatives Δ → min_edge steigt (CLV negativ, Signal-Qualität schwach).
    Gibt 0.0 zurück wenn < 20 Settled-Bets mit CLV-Daten vorhanden.
    """
    try:
        clv_stats = clv_by_source(n_min=5)
        value_clv = clv_stats.get("value", {})
        mean_clv = value_clv.get("mean_clv")
        n_settled = value_clv.get("n", 0)
        if mean_clv is None or n_settled < 20:
            return 0.0
        # Δ = clamp(mean_clv * 0.10, -0.015, +0.010)
        return max(-0.015, min(0.010, mean_clv * 0.10))
    except Exception:
        return 0.0


# Computed once at scan start — shared across all tournament iterations.
_CLV_EDGE_DELTA: float = _compute_clv_adj_edge_delta()


def category_min_edge(category: str) -> float:
    """Per-Category Edge-Floor mit CLV-Feedback-Adjustment.

    Reduziert min_edge wenn unsere historische CLV positiv ist (wir schlagen
    die Closing-Line), erhöht wenn negativ. Clamped auf [0.01, base+0.015].
    """
    base = TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE)
    adjusted = max(0.01, base - _CLV_EDGE_DELTA)
    return round(adjusted, 4)


def category_mode(category: str, surface: str = "", all_live: bool = False) -> str:
    """'live'|'shadow'. Surface-spezifischer Lookup (J2-H) schlägt Category-Default."""
    if all_live:
        return "live"
    if surface:
        surface_key = (category, surface.lower())
        if surface_key in TENNIS_CATEGORY_SURFACE_MODE:
            return TENNIS_CATEGORY_SURFACE_MODE[surface_key]
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


def _fetch_event_odds(api_key: str, sport_key: str, event_id: str) -> dict | None:
    """TheOddsAPI Single-Event-Odds-Endpoint.

    Nutzung: Wenn Bulk-/odds/ 422 liefert (Tennis-typisch bei kurz-anstehenden
    Turnieren), sind pro-Event-Odds trotzdem verfügbar. Diese Route liefert
    das vollständige Event-Objekt inkl. bookmakers[] wie /odds.
    """
    url = f"{_ODDS_API_URL}/sports/{sport_key}/events/{event_id}/odds"
    # Single-Event-Endpoint akzeptiert nur h2h/spreads/totals (kein set_betting/set_winner).
    params = {
        "apiKey": api_key,
        "regions": "eu,us,uk,au",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }
    try:
        resp = retry_request("GET", url, params=params, timeout=15,
                             log_prefix=f"[tennis-event-odds:{sport_key}]")
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


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
        if ws_h2h:
            print(f"  [websearch] {home} vs {away} — h2h via WebSearch "
                  f"({ws_h2h['a']:.2f}/{ws_h2h['b']:.2f})")
        # Kein früher Return mehr: bei fehlenden Odds übernimmt Agent 8
        # (Modell-Implied) im main-Loop und markiert is_display_only.

    best = {
        "h2h_a": 0.0, "h2h_b": 0.0,
        "spread_a": 0.0, "spread_b": 0.0,   # game-spread (nicht als Set-AH verwenden!)
        "spread_line_a": 0.0, "spread_line_b": 0.0,
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
                # TheOddsAPI "spreads" = Spiel-Handicap (total games), NICHT Satz-Handicap.
                # LeoVegas bietet z.B. ±1.5 Spiele an, Bovada ±2.5, Coolbet ±4.5 —
                # alle sind game-level, nicht set-level. Wir haben kein Spiel-Handicap-Modell,
                # daher: Spread-Daten loggen aber NICHT als AH-Odds verwenden, um falschen
                # Set-AH-EV zu vermeiden.
                for o in outcomes:
                    pt = abs(o.get("point", 0))
                    if o["name"] == home:
                        best["spread_a"] = max(best["spread_a"], o["price"])
                        best["spread_line_a"] = pt
                    elif o["name"] == away:
                        best["spread_b"] = max(best["spread_b"], o["price"])
                        best["spread_line_b"] = pt

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

    is_display_only = not best["h2h_a"] or not best["h2h_b"]
    if is_display_only:
        print(f"  [display-only] {home} vs {away} — keine Marktquote, Modell-Preis nur zur Anzeige")

    return {
        "match_id": match_id,
        "commence_time": commence,
        "player_a": home,
        "player_b": away,
        "odds_a": best["h2h_a"],
        "odds_b": best["h2h_b"],
        "is_display_only": is_display_only,
        "ah_odds_a": 0.0,   # Spiel-Handicap ≠ Satz-Handicap: nicht verwenden
        "ah_odds_b": 0.0,   # bis wir eine echte Satz-AH-Quelle haben
        "game_spread_a": best["spread_a"],   # raw game-spread (für zukünftiges Modell)
        "game_spread_b": best["spread_b"],
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
        "regions": "eu,us,uk,au",
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
            print(f"  [{sport_key}] Bulk-/odds HTTP {code} — "
                  f"{len(soon)} Matches <48h → Single-Event-Odds-Pfad")
            matches = []
            for ev in soon:
                # Erst Single-Event-Odds probieren (schlägt WebSearch qualitativ um Faktor 20)
                full_ev = _fetch_event_odds(api_key, sport_key, ev["id"])
                if full_ev and full_ev.get("bookmakers"):
                    parsed = _parse_event_markets(full_ev, sport_key)
                    if parsed:
                        matches.append(parsed)
                        continue
                # Als letzter Fallback: WebSearch pro Paarung
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

    # 1. Match Winner + Set AH + First Set (Phase 4c: source_tier gate + serve-stats)
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
        first_set_source_tier=int(m.get("source_tier", 2)),  # TheOddsAPI = Tier 2
        serve_stats_a=m.get("_serve_stats_a"),
        serve_stats_b=m.get("_serve_stats_b"),
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


def _load_open_bet_keys(bankroll: float = 100.0) -> set[tuple[str, str]]:
    """Lädt (match_id, market) aller offenen Bets aus dem Ledger für Duplicate-Annotation."""
    try:
        from src.betting.ledger import _load, _resolve_ledger_path
        import pandas as pd
        df = _load(_resolve_ledger_path(None))
        open_mask = df.get("status", pd.Series([])) == "open"
        ids = df.get("match_id", pd.Series([]))[open_mask]
        mkts = df.get("market", pd.Series([]))[open_mask]
        return set(zip(ids, mkts))
    except Exception:
        return set()


def _shortlist_score(s) -> float:
    """Gewichteter Score für Top-Picks-Ranking.

    Basiert auf historischer Markt-Performance (signal_history.jsonl):
      ah+1.5_b        → 81% WR, +49% ROI  (Faktor 1.8)
      o/u_games 1.6-2.2 → solide          (Faktor 1.1)
      away odds>2.5   → 51% WR, +3% ROI   (Faktor 0.3)
      EV 15-20% non-AH → -7.8% ROI        (Faktor 0.4)
      HIGH conf        → +30% Bonus
    """
    ev = s.ev * 100
    mkt = s.market
    odds = s.decimal_odds

    # Absolute Ausschlüsse
    if mkt in ("away", "home") and (ev < 25 or odds > 2.5):
        return -1.0
    if 15 <= ev < 20 and mkt not in ("ah+1.5_b", "ah-1.5_a"):
        return -1.0

    score = ev
    if mkt in ("ah+1.5_b", "ah-1.5_a"):
        score *= 1.8
    elif mkt.startswith("o/u_games") and 1.6 <= odds <= 2.2:
        score *= 1.1

    if s.confidence == "HIGH":
        score *= 1.3
    return score


def _build_shortlist(all_signals: list, open_keys: set, max_n: int = 5) -> list:
    """Wählt die besten max_n Signals nach _shortlist_score, je 1 Signal pro Match."""
    scored = []
    for s in all_signals:
        if (s.match_id, s.market) in open_keys:
            continue
        sc = _shortlist_score(s)
        if sc > 0:
            scored.append((sc, s))
    scored.sort(key=lambda x: -x[0])

    seen_matches: set[str] = set()
    picks = []
    for _, s in scored:
        if len(picks) >= max_n:
            break
        if s.match_id in seen_matches:
            continue
        seen_matches.add(s.match_id)
        picks.append(s)
    return picks


def format_scan_report(
    per_tournament: dict[str, dict],
    scan_date: str,
) -> str:
    open_keys = _load_open_bet_keys()
    all_signals = [s for v in per_tournament.values() for s in v["signals"]]

    lines = [f"# Tennis Scan {scan_date}\n"]
    total_sigs = sum(len(v["signals"]) for v in per_tournament.values())
    lines += [f"**Aktive Turniere:** {len(per_tournament)} · "
              f"**Signals total:** {total_sigs}\n"]

    # TOP-PICKS Shortlist
    shortlist = _build_shortlist(all_signals, open_keys, max_n=5)
    if shortlist:
        lines.append("## ⭐ TOP-PICKS — nur diese spielen\n")
        lines.append("> Filter: EV-gewichtet · Markt-historisch · max. 1 Signal pro Match\n")
        for s in shortlist:
            ah_note = " _(SET handicap)_" if s.market in ("ah-1.5_a", "ah+1.5_b") else ""
            conf_icon = "🟢" if s.confidence == "HIGH" else "🟡"
            lines += [
                f"- {conf_icon} **{s.home} vs {s.away}** · {_market_label(s.market, s.home, s.away)}{ah_note}",
                f"  Quote {s.decimal_odds:.2f} · EV +{s.ev*100:.1f}% · Stake {s.stake_eur:.2f}€ · {s.confidence}",
            ]
        lines.append("\n---\n")

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
        tour_tag = f"[{t.tour.upper()}]"
        has_ah = any(s.market in ("ah-1.5_a", "ah+1.5_b") for s in signals)
        if has_ah:
            lines.append("  > ⚠️ **Satz-AH vorhanden** — beim Buchmacher **Sätze-Handicap** wählen, NICHT Spiele-Handicap!")
        for s in sorted(signals, key=lambda x: x.ev, reverse=True):
            ah_note = " _(Satz-AH = SET handicap)_" if s.market in ("ah-1.5_a", "ah+1.5_b") else ""
            dup_note = " ⚠️ **bereits offen**" if (s.match_id, s.market) in open_keys else ""
            in_shortlist = any(s.match_id == p.match_id and s.market == p.market for p in shortlist)
            star = " ⭐" if in_shortlist else ""
            lines += [
                f"- {tour_tag} **{s.home} vs {s.away}** · {_market_label(s.market, s.home, s.away)}{ah_note}{dup_note}{star}",
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
    parser.add_argument("--no-ledger", action="store_true",
                        help="Skip Ledger-Write komplett (auch die Confirmation-Prompt).")
    parser.add_argument("--auto-log", action="store_true",
                        help="Skip Confirmation-Prompt, alle Live-Signals direkt loggen. "
                             "Standardmäßig fragt der Scanner pro Bet j/n.")
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

    # ---- 2. Match-Daten + Elo + RollingState (FND-MODEL1-001) ----
    print("Loading ATP+WTA match data...")
    all_matches = _fetch_both_tours()
    if all_matches is None or all_matches.empty:
        print("WARNING: Keine Match-Daten — Default-Elo verwendet.")
        from src.models.tennis_elo import TennisEloRatings
        ratings = TennisEloRatings()
        top_grass = []
        live_state = None  # No history → LGBM bypassed (fail-safe)
    else:
        print(f"  {len(all_matches)} matches loaded; computing Elo...")
        ratings = compute_tennis_elo(all_matches, reference_date=datetime.now())
        top_grass = top_players(ratings, surface="grass", n=10)
        # Build rolling state from the same historical corpus (once per scan).
        from src.tennis.elo_source import build_live_rolling_state
        try:
            live_state = build_live_rolling_state(all_matches)
            _n_state_players = sum(1 for v in live_state.form.values() if len(v) > 0)
            print(f"  Rolling state: {_n_state_players} players with form history.")
        except Exception as _exc:  # noqa: BLE001 — intentional broad catch for fail-safe path
            print(f"  WARNING: Rolling state build failed ({_exc}) — LGBM bypassed.")
            live_state = None  # Fail-safe: ensemble.py returns Elo-only

    # ---- 3. Pro Turnier scannen ----
    per_tournament: dict[str, dict] = {}
    all_live_signals: list = []
    all_model_evals: dict = {}  # {match_key: {p_a, p_b, implied_a, implied_b, odds_a, odds_b, source}}

    for t in tournaments:
        mode = category_mode(t.category, surface=t.surface, all_live=args.all_live)
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
                "matches": [],
            }
            continue

        # Pro Match Predict + Detect
        signals = []
        _pred_sources = {"elo": 0, "ensemble": 0}
        for m in upcoming:
            pa, pb = m["player_a"], m["player_b"]
            probs = predict_winner_ensemble(
                pa, pb, ratings, t.surface,
                best_of=t.best_of, category=t.category,
                tournament_slug=t.slug,
                match_date=m.get("commence_time", ""),
                state=live_state,
            )
            _pred_sources[probs.get("source", "elo")] += 1

            # Unknown-Player-Gate: kein Signal wenn einer der Spieler unbekannt.
            # Verhindert Fake-EV aus Default-Elo (p≈0.5 → EV bei jeder Quote < 2.0).
            if probs.get("low_confidence"):
                _who = [p for p, f in [(pa, "unknown_player_a"), (pb, "unknown_player_b")] if probs.get(f)]
                print(f"  [SKIP] {pa} vs {pb} — Unbekannter Spieler: {', '.join(_who)} (kein Elo-Profil)")
                m["no_bet_flag"] = True
                # O1-8A: Status persist — kein p_a/p_b (keine Fake-Wahrscheinlichkeiten).
                all_model_evals[f"{pa} vs {pb}"] = {
                    "status": "UNKNOWN_PLAYER",
                    "reason": f"Kein Elo-Profil: {', '.join(_who)}",
                    "odds_a": float(m.get("odds_a") or 0.0),
                    "odds_b": float(m.get("odds_b") or 0.0),
                }
                continue

            # Match hat keine TheOddsAPI-Quote → Multi-Source-Merger versuchen
            # (Agent 5 Tennis-Explorer/OddsPortal, dann Agent 3 Betfair, dann
            # Agent 10 WebSearch, dann Agent 8 Implied als Display-only).
            if m.get("is_display_only"):
                from src.tennis.odds.merger import fetch_best_odds
                hint = {
                    "player_a": pa, "player_b": pb,
                    "commence_time": m.get("commence_time", ""),
                    "surface": t.surface, "best_of": t.best_of,
                    "category": t.category,
                    "model_p_a": probs.get("p_a", 0.5),
                    "sport_key": m.get("sport_key", ""),
                }
                quote = fetch_best_odds(hint, ratings=ratings, allow_implied=True)
                if quote and not quote.no_bet_flag:
                    m["odds_a"] = quote.h2h_a
                    m["odds_b"] = quote.h2h_b
                    m["odds_source"] = quote.source
                    m["source_tier"] = quote.source_tier
                    m["is_display_only"] = False
                    # Phase 4b: Pinnacle AH primär; falls kein AH → ah_implied Fallback
                    _ah_a = float(quote.__dict__.get("ah_1_5_a") or 0.0)
                    _ah_b = float(quote.__dict__.get("ah_1_5_b") or 0.0)
                    if not (_ah_a > 1.0 and _ah_b > 1.0):
                        try:
                            from src.tennis.odds.ah_implied import fetch_ah_fallback as _fetch_ah
                            _ah_q = _fetch_ah(hint, ratings=ratings)
                            if _ah_q:
                                _ah_a = _ah_q.h2h_a
                                _ah_b = _ah_q.h2h_b
                        except Exception:
                            pass
                    sigs = detect_value_tennis(
                        player_a=pa, player_b=pb, probs=probs,
                        odds_a=quote.h2h_a, odds_b=quote.h2h_b,
                        bankroll=args.bankroll, match_id=m["match_id"],
                        min_edge=min_edge, tour=t.tour,
                        ah_odds_a=_ah_a, ah_odds_b=_ah_b,
                    )
                    signals.extend(sigs)
                    for s in sigs:
                        print(f"  [{t.slug}:{quote.source}] {pa} vs {pb} — {s.market} EV+{s.ev*100:.1f}% @{s.decimal_odds:.2f}")
                    continue

                # Fallback: Agent 8 Implied (Display-only) — kein echtes Signal, aber
                # model_eval speichern damit PWA die Modellwahrscheinlichkeit zeigen kann.
                if quote is None:
                    # O1-8A: NO_ODDS — Modell konnte evaluieren, aber keine Marktquote.
                    # Model-Wahrscheinlichkeiten korrekt; Markt-Odds fehlen.
                    all_model_evals[f"{pa} vs {pb}"] = {
                        "status": "NO_ODDS",
                        "p_a": round(probs.get("p_a", 0.5) * 100, 1),
                        "p_b": round(probs.get("p_b", 0.5) * 100, 1),
                        "implied_a": 0.0, "implied_b": 0.0,
                        "odds_a": 0.0, "odds_b": 0.0,
                        "source": probs.get("source", "elo"),
                    }
                    continue
                q = quote
                m["odds_a"] = q.h2h_a
                m["odds_b"] = q.h2h_b
                m["odds_source"] = q.source
                m["source_tier"] = q.source_tier
                m["no_bet_flag"] = True
                _oa = float(q.h2h_a or 0.0)
                _ob = float(q.h2h_b or 0.0)
                _impl_sum = (1.0 / _oa + 1.0 / _ob) if _oa > 1 and _ob > 1 else 0.0
                _impl_a = round((1.0 / _oa / _impl_sum) * 100, 1) if _impl_sum else 0.0
                all_model_evals[f"{pa} vs {pb}"] = {
                    "status": "EVALUATED_NO_BET",
                    "p_a": round(probs.get("p_a", 0.5) * 100, 1),
                    "p_b": round(probs.get("p_b", 0.5) * 100, 1),
                    "implied_a": _impl_a,
                    "implied_b": round(100.0 - _impl_a, 1) if _impl_a else 0.0,
                    "odds_a": _oa, "odds_b": _ob,
                    "source": probs.get("source", "elo"),
                }
                continue

            sigs = detect_all_markets(m, probs, args.bankroll, min_edge, t)
            signals.extend(sigs)
            for s in sigs:
                print(f"  [{t.slug}] {pa} vs {pb} — {s.market} EV+{s.ev*100:.1f}% @{s.decimal_odds:.2f}")
            # model_eval immer speichern (mit UND ohne Signal) damit PWA bei Runden-
            # fortschritt aktuelle Paarungen zeigt statt veraltete Vorrundenmatches.
            _oa = float(m.get("odds_a") or 0.0)
            _ob = float(m.get("odds_b") or 0.0)
            _impl_sum = (1.0/_oa + 1.0/_ob) if _oa > 1 and _ob > 1 else 0.0
            _impl_a = round((1.0/_oa/_impl_sum)*100, 1) if _impl_sum else 0.0
            all_model_evals[f"{pa} vs {pb}"] = {
                "status": "EVALUATED_NO_BET" if not sigs else "ACTIONABLE",
                "p_a": round(probs.get("p_a", 0.5)*100, 1),
                "p_b": round(probs.get("p_b", 0.5)*100, 1),
                "implied_a": _impl_a,
                "implied_b": round(100.0 - _impl_a, 1) if _impl_a else 0.0,
                "odds_a": _oa, "odds_b": _ob,
                "source": probs.get("source", "elo"),
            }

        per_tournament[t.slug] = {
            "tournament": t, "signals": signals, "n_matches": len(upcoming), "mode": mode,
            "matches": upcoming,
        }
        if mode == "live":
            all_live_signals.extend(signals)
        if _pred_sources["ensemble"] > 0 or _pred_sources["elo"] > 0:
            print(f"  [{t.slug}] Prediction-Source: ensemble={_pred_sources['ensemble']}, elo-only={_pred_sources['elo']}")

    print(f"\n=== Summary ===")
    print(f"Live-Signals: {len(all_live_signals)} (über {sum(1 for v in per_tournament.values() if v['mode']=='live')} Live-Kategorien)")

    # ---- Signal Archive (I9) ----
    _all_tennis_signals = [s for v in per_tournament.values() for s in v["signals"]]
    _tennis_selected = {(s.match_id, s.market) for s in all_live_signals}
    # Meta-Dict: match_id → {tournament, category, surface, tour}
    _meta_by_match: dict[str, dict] = {}
    for slug, info in per_tournament.items():
        t = info["tournament"]
        for m in info.get("matches", []):
            _meta_by_match[m["match_id"]] = {
                "tournament": t.name, "category": t.category,
                "surface": t.surface, "tour": t.tour,
            }
    from src.scanner.output import archive_signals
    _n_archived = archive_signals(
        _all_tennis_signals, _tennis_selected, scan_date,
        sport="tennis", meta_by_match=_meta_by_match,
    )
    if _n_archived:
        print(f"Archive: {_n_archived} neue Tennis-Signals archiviert.")

    # ---- 4. Report ----
    report_path = ROOT / "results" / f"tennis_scan_{scan_date}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(format_scan_report(per_tournament, scan_date))
    print(f"Report: {report_path}")

    # ---- 5. Ledger (nur Live-Signals) — feedback_no_auto_log: NIE ohne Bestätigung ----
    if all_live_signals and not args.no_ledger:
        if args.auto_log:
            n = append_bets(all_live_signals, args.bankroll)
            print(f"Ledger: {n} Live-Bets eingetragen (--auto-log).")
        else:
            confirmed = _confirm_bets(all_live_signals, args.bankroll)
            if confirmed:
                n = append_bets(confirmed, args.bankroll)
                print(f"Ledger: {n} bestätigte Bets eingetragen.")
            else:
                print("Ledger: keine Bets eingetragen (keine Bestätigung).")

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
        _sport_key = t.sport_keys[0] if getattr(t, "sport_keys", None) else t.slug
        meta = {
            "name": t.name, "category": t.category,
            "surface": t.surface, "best_of": t.best_of,
            "sport_key": _sport_key,  # Wave 3B.1: stable fixture identity
        }
        for s in info["signals"]:
            match_tour_map[s.match_id] = t.tour
            match_tournament_map[s.match_id] = meta
        # Schedule: alle geparsten Matches (mit oder ohne Signal), damit PWA
        # den vollständigen Tag zeigen kann — nicht nur Value-Bets.
        for m in info.get("matches", []):
            mid = m["match_id"]
            kickoff_map[mid] = m.get("commence_time", "")
            schedule.append({
                "sport": "tennis",
                "home": m["player_a"], "away": m["player_b"],
                "kickoff": m.get("commence_time", ""),
                "tour": t.tour,
                "tournament": t.name, "category": t.category,
                "surface": t.surface, "best_of": t.best_of,
                "odds_home": m.get("odds_a", 0.0),
                "odds_away": m.get("odds_b", 0.0),
                "odds_source": m.get("odds_source", "the_odds_api"),
                "is_display_only": bool(m.get("is_display_only", False)),
                "sport_key": m.get("sport_key", _sport_key),  # Wave 3B.1
            })

    # ---- 7b. Sekundär-Coverage via tennisexplorer.com (Skip im Mock/Turnier-Filter) ----
    # Fügt Matches hinzu, die TheOddsAPI nicht listet (Kitzbühel, Los Cabos,
    # Prag WTA, Challenger, ITF). Nur Schedule + Odds, keine EV-Signale
    # (fehlende Meta wie Surface/Category für zuverlässige Elo-Prediction).
    if not args.mock and not args.tournament:
        try:
            from src.data.tennis_secondary_odds import fetch_te_upcoming_matches
            from src.tennis.tournaments import canonical_match_key, get_tournament_by_te
            existing_keys = {
                canonical_match_key(g["home"], g["away"]) for g in schedule
            }
            te_matches = fetch_te_upcoming_matches(min_bookies=2, max_matches=150)
            added = 0
            enriched = 0
            dup_skipped = 0
            for m in te_matches:
                key = canonical_match_key(m["player_a"], m["player_b"])
                if key in existing_keys:
                    dup_skipped += 1
                    continue
                existing_keys.add(key)

                # Registry-Enrichment: bekannte Turniere bekommen category/surface/best_of.
                te_slug = m.get("te_slug", "")
                te_tour = m.get("te_tour", "")
                reg = get_tournament_by_te(te_slug, te_tour) if te_slug and te_tour else None
                if reg is not None:
                    category = reg.category
                    surface = reg.surface
                    best_of = reg.best_of
                    tournament_name = reg.name
                    enriched += 1

                    # Signal-Detection für TE-Matches mit Registry-Match: nutze
                    # h2h-only-detection (kein AH/O/U verfügbar via TE).
                    _te_mode = category_mode(reg.category, surface=reg.surface, all_live=args.all_live)
                    # Governance tier: shadow tournaments are evaluated but never produce signals.
                    # This check is INDEPENDENT of _te_mode so that even if "challenger_atp" is
                    # accidentally added to TENNIS_CATEGORY_MODE, signals are still blocked.
                    _is_shadow_reg = reg.is_shadow
                    if _te_mode == "live" or _is_shadow_reg:
                        # Guardrail: TE listet oft ITF/Qualifier/Junior unter demselben
                        # Turnier-Slug wie ATP-Main-Draw. Signal-Detection nur wenn
                        # BEIDE Spieler eine echte Elo-Historie haben (nicht Default 1500).
                        from src.tennis.name_norm import to_elo_name_from_te
                        _ea = to_elo_name_from_te(m["player_a"])
                        _eb = to_elo_name_from_te(m["player_b"])
                        _ra = ratings.get_overall(_ea)
                        _rb = ratings.get_overall(_eb)
                        if _ra != 1500.0 and _rb != 1500.0:
                            try:
                                _probs = predict_winner_ensemble(
                                    m["player_a"], m["player_b"], ratings, reg.surface,
                                    best_of=reg.best_of, category=reg.category,
                                    name_source="te",
                                )
                                _oa = float(m.get("odds_a") or 0.0)
                                _ob = float(m.get("odds_b") or 0.0)
                                _is = (1.0/_oa + 1.0/_ob) if _oa > 1 and _ob > 1 else 0.0
                                _ia = round((1.0/_oa/_is)*100, 1) if _is else 0.0
                                if _is_shadow_reg:
                                    # Shadow evaluation: store probs for measurement, NO signals.
                                    print(f"  [shadow:{reg.slug}] {m['player_a']} vs {m['player_b']} — shadow eval only")
                                    all_model_evals[f"{m['player_a']} vs {m['player_b']}"] = {
                                        "status": "SHADOW_EVALUATED",
                                        "tier": "shadow",
                                        "tournament": reg.name,
                                        "p_a": round(_probs.get("p_a", 0.5)*100, 1),
                                        "p_b": round(_probs.get("p_b", 0.5)*100, 1),
                                        "implied_a": _ia,
                                        "implied_b": round(100.0 - _ia, 1) if _ia else 0.0,
                                        "odds_a": _oa, "odds_b": _ob,
                                        "source": _probs.get("source", "elo"),
                                    }
                                else:
                                    # Production evaluation: detect signals normally.
                                    _min_edge = category_min_edge(reg.category)
                                    _sigs = detect_value_tennis(
                                        player_a=m["player_a"], player_b=m["player_b"],
                                        probs=_probs, odds_a=m.get("odds_a", 0.0),
                                        odds_b=m.get("odds_b", 0.0), bankroll=args.bankroll,
                                        match_id=m["match_id"], min_edge=_min_edge, tour=reg.tour,
                                    )
                                    if _sigs:
                                        all_live_signals.extend(_sigs)
                                        for s in _sigs:
                                            print(f"  [te:{reg.slug}] {m['player_a']} vs {m['player_b']} — {s.market} EV+{s.ev*100:.1f}% @{s.decimal_odds:.2f}")
                                    all_model_evals[f"{m['player_a']} vs {m['player_b']}"] = {
                                        "status": "EVALUATED_NO_BET" if not _sigs else "ACTIONABLE",
                                        "p_a": round(_probs.get("p_a", 0.5)*100, 1),
                                        "p_b": round(_probs.get("p_b", 0.5)*100, 1),
                                        "implied_a": _ia,
                                        "implied_b": round(100.0 - _ia, 1) if _ia else 0.0,
                                        "odds_a": _oa, "odds_b": _ob,
                                        "source": _probs.get("source", "elo"),
                                    }
                            except Exception as e:
                                print(f"  [te-signal:{reg.slug}] {e}")
                        else:
                            # O1-8A: UNKNOWN_PLAYER — Qualifier/Wildcard ohne Elo-Profil im Turnier-Draw.
                            _who_te = [m["player_a"] if _ra == 1500.0 else None,
                                       m["player_b"] if _rb == 1500.0 else None]
                            _up_entry: dict = {
                                "status": "UNKNOWN_PLAYER",
                                "reason": f"Kein Elo-Profil (z.B. Qualifier): {', '.join(w for w in _who_te if w)}",
                                "odds_a": float(m.get("odds_a") or 0.0),
                                "odds_b": float(m.get("odds_b") or 0.0),
                            }
                            if _is_shadow_reg:
                                _up_entry["tier"] = "shadow"
                            all_model_evals[f"{m['player_a']} vs {m['player_b']}"] = _up_entry
                else:
                    category = ""
                    surface = ""
                    best_of = 0
                    tournament_name = m.get("te_tournament", "")
                    # Kein Registry-Match — trotzdem Elo-Prediction speichern (Display-only).
                    # Defaults: hard/BO3. Nur wenn beide Spieler echte Elo-Profile haben.
                    from src.tennis.name_norm import to_elo_name_from_te
                    _ea2 = to_elo_name_from_te(m["player_a"])
                    _eb2 = to_elo_name_from_te(m["player_b"])
                    _ra2 = ratings.get_overall(_ea2)
                    _rb2 = ratings.get_overall(_eb2)
                    if _ra2 != 1500.0 and _rb2 != 1500.0:
                        try:
                            _probs2 = predict_winner_ensemble(
                                m["player_a"], m["player_b"], ratings, "hard",
                                best_of=3, name_source="te",
                            )
                            _oa2 = float(m.get("odds_a") or 0.0)
                            _ob2 = float(m.get("odds_b") or 0.0)
                            _is2 = (1.0 / _oa2 + 1.0 / _ob2) if _oa2 > 1 and _ob2 > 1 else 0.0
                            _ia2 = round((1.0 / _oa2 / _is2) * 100, 1) if _is2 else 0.0
                            all_model_evals[f"{m['player_a']} vs {m['player_b']}"] = {
                                "status": "EVALUATED_NO_BET",
                                "p_a": round(_probs2.get("p_a", 0.5) * 100, 1),
                                "p_b": round(_probs2.get("p_b", 0.5) * 100, 1),
                                "implied_a": _ia2,
                                "implied_b": round(100.0 - _ia2, 1) if _ia2 else 0.0,
                                "odds_a": _oa2, "odds_b": _ob2,
                                "source": "elo",
                            }
                        except Exception:
                            pass
                    else:
                        # O1-8A: UNSUPPORTED_TOURNAMENT — kein Registry + Elo unbekannt.
                        all_model_evals[f"{m['player_a']} vs {m['player_b']}"] = {
                            "status": "UNSUPPORTED_TOURNAMENT",
                            "reason": f"Turnier nicht im Portfolio: {tournament_name or 'unbekannt'}",
                            "odds_a": float(m.get("odds_a") or 0.0),
                            "odds_b": float(m.get("odds_b") or 0.0),
                        }

                schedule.append({
                    "sport": "tennis",
                    "home": m["player_a"], "away": m["player_b"],
                    "kickoff": m.get("commence_time", ""),
                    "tour": te_tour,
                    "tournament": tournament_name,
                    "category": category, "surface": surface, "best_of": best_of,
                    "odds_home": m.get("odds_a", 0.0),
                    "odds_away": m.get("odds_b", 0.0),
                    "source": "tennisexplorer",
                    "sport_key": m.get("sport_key", f"te:{m.get('te_slug', 'unknown')}"),  # Wave 3B.1
                })
                added += 1
            print(f"Sekundär-Coverage (TE): {added} zusätzliche Matches hinzugefügt "
                  f"(davon {enriched} Registry-enriched, {dup_skipped} Duplikate übersprungen)")
        except Exception as e:
            print(f"[te-coverage] fetch failed: {e}")

    # Build all_odds for open-bet enrichment (current_odds display in PWA)
    tennis_all_odds: dict[str, dict] = {}
    for _info in per_tournament.values():
        for _m in _info.get("matches", []):
            _pa, _pb = _m.get("player_a", ""), _m.get("player_b", "")
            if not _pa or not _pb:
                continue
            _entry: dict = {}
            if _m.get("odds_a"): _entry["home"] = float(_m["odds_a"])
            if _m.get("odds_b"): _entry["away"] = float(_m["odds_b"])
            if _m.get("ah_odds_a"): _entry["ah1.5_a"] = float(_m["ah_odds_a"])
            if _m.get("ah_odds_b"): _entry["ah1.5_b"] = float(_m["ah_odds_b"])
            if _m.get("first_set_odds_a"): _entry["first_set_a"] = float(_m["first_set_odds_a"])
            if _m.get("first_set_odds_b"): _entry["first_set_b"] = float(_m["first_set_odds_b"])
            for _line, _price in (_m.get("totals_over") or {}).items():
                _entry[f"over{_line}_games"] = float(_price)
            for _line, _price in (_m.get("totals_under") or {}).items():
                _entry[f"under{_line}_games"] = float(_price)
            if _entry:
                tennis_all_odds[f"{_pa} vs {_pb}"] = _entry

    # O1-8 Monitoring: Alert for PRODUCTION_SUPPORTED fixtures starting within 90 min
    # that have no authoritative eval. These represent scanner misses, not intentionally
    # unsupported events (which would have UNSUPPORTED_TOURNAMENT status).
    try:
        from datetime import datetime as _dt, timezone as _tz
        from src.tennis.tournaments import PRODUCTION_SUPPORTED_CATEGORIES as _PROD_CATS
        _now_ts = _dt.now(_tz.utc).timestamp()
        _t90_ts = _now_ts + 90 * 60
        _t90_alerts: list[str] = []
        for _se in schedule:
            _cat = _se.get("category", "")
            if _cat not in _PROD_CATS:
                continue
            _kick = _se.get("kickoff", "")
            if not _kick:
                continue
            try:
                _kick_ts = _dt.fromisoformat(_kick.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if _kick_ts > _t90_ts:
                continue  # not yet within 90 min
            _home, _away = _se.get("home", ""), _se.get("away", "")
            _eval_entry = all_model_evals.get(f"{_home} vs {_away}")
            _missing = (
                _eval_entry is None
                or _eval_entry.get("status") in ("DISCOVERED", "EVALUATION_PENDING")
            )
            if _missing:
                _t90_alerts.append(f"  ⚠️ [{_cat}] {_home} vs {_away} @ {_kick}")
        if _t90_alerts:
            print(f"\n[O1-8-MONITOR] {len(_t90_alerts)} PRODUCTION_SUPPORTED match(es) within T-90 with no eval:")
            for _a in _t90_alerts:
                print(_a)
    except Exception as _mon_err:
        print(f"[O1-8-MONITOR] monitoring check failed: {_mon_err}")

    # Wave 3B: enrich schedule with canonical event state (initial/current start,
    # status, source authority). Sidecar persists scheduled_start_initial across scans.
    try:
        from src.tennis.event_state import enrich_schedule_with_canonical_state
        schedule = enrich_schedule_with_canonical_state(schedule)
    except (ImportError, OSError, RuntimeError, ValueError) as _es_err:
        print(f"[tennis_scan] canonical event state enrichment skipped: {_es_err}")

    write_signals_json_all_users(
        tennis=all_live_signals,
        portfolio=dashboard_summary,
        top_elo=top_grass,
        tennis_tour_map=match_tour_map,
        tennis_tournament_map=match_tournament_map,
        kickoff_map=kickoff_map,
        schedule=schedule,
        all_odds=tennis_all_odds if tennis_all_odds else None,
        model_evals=all_model_evals if all_model_evals else None,
    )
    print("Dashboard: docs/data/signals.json aktualisiert.")


if __name__ == "__main__":
    main()
