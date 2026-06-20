"""
StatsBomb open-data PPDA fetcher.

PPDA (Passes Per Defensive Action) ist ein Pressing-Intensity-Metric:
  PPDA_T = opp_passes_in_opp_own_60pct / T_def_actions_in_opp_own_60pct

Niedrige Werte = aggressives, hohes Pressing. Typische Spitzenmannschaften
liegen bei 7-10, defensiv-ausgerichtete Teams bei 14-18.

Quelle: StatsBomb Open Data (FIFA WC, UEFA Euro, Copa América). Same Source
wie `statsbomb.py` (xG), aber separater Pass + separater Cache, damit PPDA
unabhängig vom xG-Cache rebuilt werden kann.

Pitch-Konvention StatsBomb: 120×80, Events werden pro Team aus Sicht des
attackierenden Teams gespeichert (x=0 eigenes Tor, x=120 gegnerisches Tor).
"Pressing-Zone" = x ≥ 48 (60% Pitch ab Mittellinie minus 12 Yards Toleranz).
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from scripts._http_retry import retry_request
from src.config import DATA_CACHE, canonical_name as _cn

_CACHE_PATH = DATA_CACHE / "statsbomb_ppda.pkl"
_CACHE_MAX_AGE_H = 24

_SB_BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
_SB_COMPETITION_IDS = {43, 55, 223}  # WC, Euro, Copa América
_TOURNAMENT_NAMES = {43: "FIFA World Cup", 55: "UEFA Euro", 223: "Copa América"}

# Pitch-Zone-Cutoff (60% des 120-Yard-Pitch ab Mittellinie):
# eine Defensiv-Aktion auf x ≥ PRESSING_X gilt als High-Press.
PRESSING_X: float = 48.0
# Opp-Pass gilt als "in opp's own 60%" wenn x < 72 (von Opp-POV).
OPP_PASS_X_MAX: float = 72.0


def _discover_competitions() -> dict[int, list[int]]:
    url = f"{_SB_BASE}/competitions.json"
    try:
        resp = retry_request("GET", url, timeout=15)
        resp.raise_for_status()
        comps = resp.json()
    except Exception:
        return {43: [106, 107], 55: [43, 282], 223: [282]}

    result: dict[int, list[int]] = {}
    for c in comps:
        cid = c.get("competition_id")
        sid = c.get("season_id")
        if cid in _SB_COMPETITION_IDS and sid is not None:
            result.setdefault(cid, []).append(sid)
    return result


def _cache_is_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    age_h = (time.time() - _CACHE_PATH.stat().st_mtime) / 3600
    return age_h < _CACHE_MAX_AGE_H


def _is_defensive_action(event: dict) -> bool:
    """True wenn Event eine Defensiv-Aktion ist (Tackle/Interception/Foul)."""
    t = (event.get("type") or {}).get("name")
    if t == "Interception":
        return True
    if t == "Foul Committed":
        return True
    if t == "Duel":
        # StatsBomb-Duel inkl. Tackle-Subtypen
        sub = (event.get("duel") or {}).get("type") or {}
        return (sub.get("name") or "").startswith("Tackle")
    return False


def _ppda_from_events(events: list[dict], home: str, away: str) -> dict:
    """
    Berechnet PPDA pro Team aus einer Event-Liste.

    Returns dict mit home_ppda, away_ppda, plus Rohwerte für Debugging.
    NaN wenn Denominator (def. actions) zu klein (<5) → Schutz vor Outliern.
    """
    home_opp_passes = 0  # away-Pässe in away's eigener 60%-Zone
    away_opp_passes = 0  # home-Pässe in home's eigener 60%-Zone
    home_def_actions = 0  # home-Def-Aktionen in away's 60%
    away_def_actions = 0  # away-Def-Aktionen in home's 60%

    for ev in events:
        team_name = (ev.get("team") or {}).get("name") or ""
        loc = ev.get("location") or [None, None]
        if loc[0] is None:
            continue
        x = float(loc[0])
        t = (ev.get("type") or {}).get("name")

        if t == "Pass":
            if x < OPP_PASS_X_MAX:
                if team_name == away:
                    home_opp_passes += 1
                elif team_name == home:
                    away_opp_passes += 1
        elif _is_defensive_action(ev):
            if x >= PRESSING_X:
                if team_name == home:
                    home_def_actions += 1
                elif team_name == away:
                    away_def_actions += 1

    def _safe(num: int, den: int) -> float:
        if den < 5:
            return float("nan")
        return float(num) / float(den)

    return {
        "home_ppda": _safe(home_opp_passes, home_def_actions),
        "away_ppda": _safe(away_opp_passes, away_def_actions),
        "home_opp_passes": home_opp_passes,
        "home_def_actions": home_def_actions,
        "away_opp_passes": away_opp_passes,
        "away_def_actions": away_def_actions,
    }


def _fetch_match_ppda(competition_id: int, season_id: int) -> list[dict]:
    matches_url = f"{_SB_BASE}/matches/{competition_id}/{season_id}.json"
    try:
        resp = retry_request("GET", matches_url, timeout=15)
        resp.raise_for_status()
        matches = resp.json()
    except Exception:
        return []

    rows: list[dict] = []
    tournament = _TOURNAMENT_NAMES.get(competition_id, "")

    for match in matches:
        match_id = match.get("match_id")
        if match_id is None:
            continue
        home = (match.get("home_team") or {}).get("home_team_name") or ""
        away = (match.get("away_team") or {}).get("away_team_name") or ""
        date_str = match.get("match_date", "")
        try:
            date = pd.Timestamp(date_str)
        except Exception:
            continue

        time.sleep(0.5)
        events_url = f"{_SB_BASE}/events/{match_id}.json"
        try:
            ev_resp = retry_request("GET", events_url, timeout=20)
            ev_resp.raise_for_status()
            events = ev_resp.json()
        except Exception:
            continue

        stats = _ppda_from_events(events, home, away)
        rows.append({
            "match_id": match_id,
            "date": date,
            "tournament": tournament,
            "home_team": _cn(home),
            "away_team": _cn(away),
            **stats,
        })

    return rows


def fetch_statsbomb_ppda(force: bool = False) -> pd.DataFrame:
    """
    Returns DataFrame mit Spalten:
        match_id, date, tournament, home_team, away_team,
        home_ppda, away_ppda, home_opp_passes, home_def_actions,
        away_opp_passes, away_def_actions

    Cached 24h. NaN-Werte für PPDA wenn Denominator < 5 (Match-Fragmente).
    """
    if not force and _cache_is_fresh():
        return pd.read_pickle(_CACHE_PATH)

    print("Fetching StatsBomb PPDA data (this may take 1-3 minutes)...")
    competitions = _discover_competitions()
    all_rows: list[dict] = []
    for comp_id, season_ids in competitions.items():
        for season_id in season_ids:
            print(f"  competition={comp_id}, season={season_id}...")
            rows = _fetch_match_ppda(comp_id, season_id)
            all_rows.extend(rows)
            print(f"    {len(rows)} matches fetched.")

    if not all_rows:
        return pd.DataFrame(columns=[
            "match_id", "date", "tournament", "home_team", "away_team",
            "home_ppda", "away_ppda",
            "home_opp_passes", "home_def_actions",
            "away_opp_passes", "away_def_actions",
        ])

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(_CACHE_PATH)
    print(f"  StatsBomb PPDA: {len(df)} matches cached.")
    return df
