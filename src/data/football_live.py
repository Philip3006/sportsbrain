"""Generischer ESPN-Scoreboard-Loader für Fußball-Ligen (WM, 2.BL, ...).

Nutzt keine API-Quota (public endpoint), ~30-60s Lag. Rückgabe-Schema:
  {match_key: {"home", "away", "home_score", "away_score", "status",
               "clock_display", "period", "completed", "espn_id", "commence_time"}}
match_key = canonicalized "home_vs_away" für dedup gegen Cache-State.

status ∈ {"pre" (angesetzt), "in" (live), "post" (beendet)}.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import canonical_name

_log = logging.getLogger("sportsbrain.football_live")

ESPN_LEAGUE_CODES = {
    "soccer_germany_bundesliga":  "ger.1",
    "soccer_germany_bundesliga2": "ger.2",
    "soccer_germany_liga3":       "ger.3",
    "soccer_epl":                 "eng.1",
    "soccer_fifa_world_cup":      "fifa.world",
}


def canonical_match_key(home: str, away: str) -> str:
    return f"{canonical_name(home)}__{canonical_name(away)}".lower().replace(" ", "_")


def fetch_scoreboard(sport_key: str) -> dict[str, dict]:
    """Lädt aktuelles Scoreboard für die Liga. Fehler → leeres Dict (fail-silent)."""
    espn_code = ESPN_LEAGUE_CODES.get(sport_key)
    if not espn_code:
        _log.warning("Keine ESPN-Mapping für %s", sport_key)
        return {}
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_code}/scoreboard"
    try:
        from scripts._http_retry import retry_request
        resp = retry_request("GET", url, timeout=10, log_prefix=f"[espn_{espn_code}]")
        resp.raise_for_status()
    except Exception as exc:
        _log.debug("ESPN fetch %s failed: %s", espn_code, exc)
        return {}

    now_iso = datetime.now(timezone.utc).isoformat()
    out: dict[str, dict] = {}
    for e in resp.json().get("events", []):
        comp = e.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue
        status = comp.get("status", {})
        state = status.get("type", {}).get("state", "pre")
        completed = bool(status.get("type", {}).get("completed", False))
        clock = status.get("displayClock", "")
        period = status.get("period", 0)
        home_score = away_score = None
        if state in ("in", "post"):
            try:
                home_score = int(home_c.get("score", 0))
                away_score = int(away_c.get("score", 0))
            except (ValueError, TypeError):
                pass
        home_name = home_c.get("team", {}).get("displayName", "")
        away_name = away_c.get("team", {}).get("displayName", "")
        key = canonical_match_key(home_name, away_name)
        out[key] = {
            "espn_id":       str(e.get("id", "")),
            "home":          home_name,
            "away":          away_name,
            "home_score":    home_score,
            "away_score":    away_score,
            "status":        state,
            "period":        period,
            "clock_display": clock,
            "completed":     completed,
            "commence_time": e.get("date", ""),
            "last_update":   now_iso,
        }
    return out
