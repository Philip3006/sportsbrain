"""Tier 2 — TheOddsAPI Multi-Region Konsens.

Primäre Datenquelle für 2.BL. Strategie:
  1. Falls match_hint["bookmakers"] bereits vorhanden (vom Bulk-Fetch im
     Scanner übergeben) — nutze diese direkt, kein Extra-API-Call.
  2. Sonst: frischer API-Call mit regions=eu,uk,au für maximale Bookie-Coverage.

Liefert FootballOddsQuote mit allen Märkten (1X2 + AH + O/U + BTTS).
Bookies_count_1x2 = Anzahl eindeutiger Bookies die 1X2 liefern → Coverage-Gate.
"""
from __future__ import annotations

import os
import statistics
from typing import Optional

from src.football.odds.base import FootballOddsQuote, canonical_team, sanity_1x2

name = "the_odds_api"
tier = 2

_MULTI_REGIONS = "eu,uk,au"
_MARKETS = "h2h,totals,spreads,btts"
_BULK_TTL_S = 5 * 60


def _median(vals: list[float]) -> float:
    return statistics.median(vals) if vals else 0.0


def _parse_bookmakers(
    bookmakers: list[dict],
    home_raw: str,
    away_raw: str,
) -> FootballOddsQuote:
    """Extrahiert Konsens-Quoten aus bookmakers-Liste (TheOddsAPI-Format)."""
    h_prices: list[float] = []
    d_prices: list[float] = []
    a_prices: list[float] = []
    over_prices: list[float] = []
    under_prices: list[float] = []
    ah_home_prices: list[float] = []
    ah_away_prices: list[float] = []
    btts_y_prices: list[float] = []
    btts_n_prices: list[float] = []
    ou_line_seen: list[float] = []
    ah_line_seen: list[float] = []

    home_key = canonical_team(home_raw)
    away_key = canonical_team(away_raw)

    for bm in bookmakers or []:
        for market in bm.get("markets", []):
            key = market.get("key", "")
            outcomes = market.get("outcomes", [])

            if key == "h2h":
                for o in outcomes:
                    n = canonical_team(o.get("name", ""))
                    p = float(o.get("price", 0.0))
                    if p <= 1.0:
                        continue
                    if n == home_key:
                        h_prices.append(p)
                    elif n == away_key:
                        a_prices.append(p)
                    elif "draw" in n:
                        d_prices.append(p)

            elif key == "totals":
                for o in outcomes:
                    p = float(o.get("price", 0.0))
                    pt = float(o.get("point", 2.5))
                    if p <= 1.0:
                        continue
                    if o.get("name") == "Over":
                        over_prices.append(p)
                        ou_line_seen.append(pt)
                    elif o.get("name") == "Under":
                        under_prices.append(p)

            elif key == "spreads":
                # TheOddsAPI spreads = Asian Handicap in Toren (halbe Linien)
                for o in outcomes:
                    n = canonical_team(o.get("name", ""))
                    p = float(o.get("price", 0.0))
                    pt = float(o.get("point", 0.0))
                    if p <= 1.0:
                        continue
                    if n == home_key:
                        ah_home_prices.append(p)
                        ah_line_seen.append(pt)
                    elif n == away_key:
                        ah_away_prices.append(p)

            elif key == "btts":
                for o in outcomes:
                    p = float(o.get("price", 0.0))
                    if p <= 1.0:
                        continue
                    if o.get("name") == "Yes":
                        btts_y_prices.append(p)
                    elif o.get("name") == "No":
                        btts_n_prices.append(p)

    n_bookies = len(h_prices)  # unique bookies für 1X2 (je bookie 1 Preis)

    q = FootballOddsQuote(
        home_team=home_raw,
        away_team=away_raw,
        source=name,
        source_tier=tier,
        h2h_home=round(_median(h_prices), 3),
        h2h_draw=round(_median(d_prices), 3),
        h2h_away=round(_median(a_prices), 3),
        ou_line=round(_median(ou_line_seen), 1) if ou_line_seen else 2.5,
        ou_over=round(_median(over_prices), 3),
        ou_under=round(_median(under_prices), 3),
        ah_line=round(_median(ah_line_seen), 2) if ah_line_seen else 0.0,
        ah_home=round(_median(ah_home_prices), 3),
        ah_away=round(_median(ah_away_prices), 3),
        btts_yes=round(_median(btts_y_prices), 3),
        btts_no=round(_median(btts_n_prices), 3),
        bookmaker="consensus",
        bookies_count_1x2=n_bookies,
        confidence=min(1.0, 0.3 + 0.07 * n_bookies),
        no_bet_flag=False,
    )
    return q


def fetch(match_hint: dict) -> Optional[FootballOddsQuote]:
    home_raw = match_hint.get("home_team", "")
    away_raw = match_hint.get("away_team", "")
    if not home_raw or not away_raw:
        return None

    # Pfad 1: Bookmakers bereits im hint (Bulk-Fetch aus Scanner — kein Quota-Verbrauch)
    bookmakers = match_hint.get("bookmakers")
    if bookmakers is not None:
        q = _parse_bookmakers(bookmakers, home_raw, away_raw)
        if q.sane_1x2():
            return q
        # Kein valider 1X2 → trotzdem zurückgeben (Merger entscheidet)
        return q if q.h2h_home > 0 else None

    # Pfad 2: Frischer Multi-Region API-Call
    try:
        from src.data.odds_api import fetch_upcoming_matches
    except ImportError:
        return None

    sport_key = match_hint.get("sport_key", "soccer_germany_bundesliga2")
    try:
        matches = fetch_upcoming_matches(
            sport=sport_key,
            markets=_MARKETS,
            regions=_MULTI_REGIONS,
        ) or []
    except Exception:
        return None

    home_key = canonical_team(home_raw)
    away_key = canonical_team(away_raw)
    for m in matches:
        mh = canonical_team(m.get("home_team", ""))
        ma = canonical_team(m.get("away_team", ""))
        if mh == home_key and ma == away_key:
            q = _parse_bookmakers(m.get("bookmakers", []), home_raw, away_raw)
            return q if q.h2h_home > 0 else None

    return None
