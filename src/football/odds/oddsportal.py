"""Tier 2 — OddsPortal.com Scraper für Football 1X2.

Bulk-Fetch der Tages-Übersicht (/matches/football/YYYY-MM-DD/) — ein Request
deckt alle Matches des Tages. Cloudflare-geschützt: ~30% 403-Rate akzeptiert.
Bei 403 → fetch() gibt None zurück, Merger fällt auf nächsten Tier.

Coverage: 1X2 only (kein AH/O-U in SSR-HTML verfügbar).
bookies_count_1x2 = 1 (Single-page aggregate — Coverage-Gate muss extern geprüft werden).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.football.odds.base import FootballOddsQuote, canonical_team, sanity_1x2

_log = logging.getLogger("sportsbrain.football.odds.oddsportal")
name = "oddsportal"
tier = 2

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
_BULK: dict[str, list[dict]] = {}
_TS: dict[str, float] = {}
_TTL_S = 15 * 60

_ODDS_RE = re.compile(r'<td[^>]*class="[^"]*odds[^"]*"[^>]*>(\d+\.\d{2})</td>', re.IGNORECASE)
_MATCH_RE = re.compile(
    r'<a[^>]*href="/football/[^"]*"[^>]*>([^<]{3,50})\s*[-–]\s*([^<]{3,50})</a>'
    r'.*?(\d+\.\d{2}).*?(\d+\.\d{2}).*?(\d+\.\d{2})',
    re.DOTALL,
)


def _fetch_day(date_iso: str) -> list[dict]:
    """Fetch all football 1X2 for a day. Returns [{home, away, h2h_home, h2h_draw, h2h_away}]."""
    if date_iso in _BULK and time.time() - _TS.get(date_iso, 0) < _TTL_S:
        return _BULK[date_iso]

    url = f"https://www.oddsportal.com/matches/football/{date_iso}/"
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=10)
    except Exception as e:
        _log.debug("[oddsportal_football] request failed: %s", e)
        return []

    if resp.status_code == 403:
        _log.debug("[oddsportal_football] 403 on %s (Cloudflare)", date_iso)
        _BULK[date_iso] = []
        _TS[date_iso] = time.time()
        return []
    if resp.status_code != 200:
        _log.debug("[oddsportal_football] HTTP %d on %s", resp.status_code, date_iso)
        return []

    matches: list[dict] = []
    for m in list(_MATCH_RE.finditer(resp.text))[:300]:
        try:
            home_raw = m.group(1).strip()
            away_raw = m.group(2).strip()
            h = float(m.group(3))
            d = float(m.group(4))
            a = float(m.group(5))
            if sanity_1x2(h, d, a):
                matches.append({
                    "home": home_raw,
                    "away": away_raw,
                    "h": h,
                    "d": d,
                    "a": a,
                })
        except Exception:
            continue

    _BULK[date_iso] = matches
    _TS[date_iso] = time.time()
    return matches


def fetch(match_hint: dict) -> Optional[FootballOddsQuote]:
    """Fetch 1X2 odds for a single match from OddsPortal day-overview.

    match_hint: {home_team, away_team, commence_time (ISO-8601)}
    Returns None if unavailable or Cloudflare blocks.
    """
    home_raw = match_hint.get("home_team", "")
    away_raw = match_hint.get("away_team", "")
    if not home_raw or not away_raw:
        return None

    commence = match_hint.get("commence_time", "")
    if commence:
        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            date_iso = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    matches = _fetch_day(date_iso)
    home_key = canonical_team(home_raw)
    away_key = canonical_team(away_raw)

    for m in matches:
        if canonical_team(m["home"]) == home_key and canonical_team(m["away"]) == away_key:
            return FootballOddsQuote(
                home_team=home_raw,
                away_team=away_raw,
                source="oddsportal",
                source_tier=tier,
                h2h_home=m["h"],
                h2h_draw=m["d"],
                h2h_away=m["a"],
                bookmaker="oddsportal_aggregate",
                bookies_count_1x2=2,
                confidence=0.70,
            )

    return None
