"""
Sofascore live xG fetcher for WM 2026.

Eliminates the StatsBomb 1-2 day publication lag during the running tournament:
Sofascore publishes per-match xG within minutes of full-time.

Endpoints (RapidAPI sofascore.p.rapidapi.com):
  GET tournaments/get-matches?tournamentId=16&seasonId=58210
      → list of WC 2026 events with status (Ended / In progress / Not started)
  GET matches/get-statistics?matchId=<event_id>
      → Expected goals + many other stats per period

Needs API_FOOTBALL_KEY in .env (same RapidAPI key works for both api-football
and sofascore — different host header).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import requests

from src.config import DATA_CACHE, canonical_name as _cn

_CACHE_PATH = DATA_CACHE / "sofascore_xg.pkl"
_CACHE_MAX_AGE_H = 3.0  # WM phase: refresh every 3h to pick up newly-finished matches

_HOST = "sofascore.p.rapidapi.com"
_BASE = f"https://{_HOST}"

# Sofascore unique-tournament IDs (verified for FIFA WC = 16)
WC2026_TOURNAMENT_ID = 16
WC2026_SEASON_ID = 58210


def _get_api_key() -> str | None:
    return os.getenv("API_FOOTBALL_KEY")


def _headers() -> dict[str, str]:
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            "API_FOOTBALL_KEY not set in environment — needed for Sofascore RapidAPI access"
        )
    return {"X-RapidAPI-Key": key, "X-RapidAPI-Host": _HOST}


def _cache_is_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    age_h = (time.time() - _CACHE_PATH.stat().st_mtime) / 3600
    return age_h < _CACHE_MAX_AGE_H


def fetch_wc2026_event_ids() -> list[dict]:
    """Returns list of {event_id, date, home, away, status, home_score, away_score}.
    Only returns matches with a numeric event_id (skips draft/postponed entries).
    """
    out: list[dict] = []
    seen: set[int] = set()
    # Sofascore wrapper paginates last/next; we iterate both up to a safe ceiling
    for course in ("last", "next"):
        for page in range(0, 8):
            try:
                r = requests.get(
                    f"{_BASE}/tournaments/get-matches",
                    headers=_headers(),
                    params={
                        "tournamentId": WC2026_TOURNAMENT_ID,
                        "seasonId": WC2026_SEASON_ID,
                        "course": course,
                        "page": page,
                    },
                    timeout=15,
                )
            except Exception as e:
                print(f"  [sofascore] tournament fetch failed (course={course} page={page}): {e}")
                break
            if r.status_code != 200 or not r.text:
                break
            j = r.json()
            events = j.get("events") or []
            if not events:
                break
            new_in_page = 0
            for ev in events:
                eid = ev.get("id")
                if eid is None or eid in seen:
                    continue
                seen.add(eid)
                new_in_page += 1
                ts = ev.get("startTimestamp") or 0
                date = pd.Timestamp(ts, unit="s") if ts else pd.NaT
                out.append({
                    "event_id": int(eid),
                    "date": date,
                    "home_team": _cn(ev.get("homeTeam", {}).get("name", "")),
                    "away_team": _cn(ev.get("awayTeam", {}).get("name", "")),
                    "home_score": ev.get("homeScore", {}).get("current"),
                    "away_score": ev.get("awayScore", {}).get("current"),
                    "status": ev.get("status", {}).get("type", ""),
                    "status_desc": ev.get("status", {}).get("description", ""),
                })
            if new_in_page == 0:
                break
            time.sleep(0.3)
    return out


def fetch_match_xg(event_id: int) -> tuple[float | None, float | None]:
    """Returns (home_xg, away_xg) from matches/get-statistics for a finished event.
    Sofascore returns multiple periods; we take 'ALL' period's Expected goals row.
    """
    try:
        r = requests.get(
            f"{_BASE}/matches/get-statistics",
            headers=_headers(),
            params={"matchId": event_id},
            timeout=15,
        )
    except Exception as e:
        print(f"  [sofascore] stats fetch failed for {event_id}: {e}")
        return None, None
    if r.status_code != 200 or not r.text:
        return None, None
    j = r.json()
    # Structure: statistics → [{period: "ALL"/"1ST"/"2ND", groups: [{statisticsItems: [...]}]}]
    for period in (j.get("statistics") or []):
        if period.get("period") != "ALL":
            continue
        for group in period.get("groups", []):
            for stat in group.get("statisticsItems", []):
                key = (stat.get("key") or "").lower()
                name = (stat.get("name") or "").lower()
                if key == "expected_goals" or "expected goals" in name:
                    try:
                        return float(stat.get("home")), float(stat.get("away"))
                    except (TypeError, ValueError):
                        return None, None
    return None, None


def fetch_wc2026_xg(force: bool = False) -> pd.DataFrame:
    """
    Returns DataFrame with columns: home_team, away_team, date, home_xg, away_xg, tournament.
    Same schema as fetch_statsbomb_xg() so it can be concatenated.
    Cached 3h.
    """
    if not force and _cache_is_fresh():
        try:
            return pd.read_pickle(_CACHE_PATH)
        except Exception:
            pass

    if not _get_api_key():
        print("  [sofascore] API_FOOTBALL_KEY missing — returning empty xG")
        return pd.DataFrame(columns=["home_team", "away_team", "date", "home_xg", "away_xg", "tournament"])

    print("  [sofascore] fetching WC 2026 fixtures...")
    fixtures = fetch_wc2026_event_ids()
    finished = [f for f in fixtures if f.get("status") == "finished" or "ended" in f.get("status_desc", "").lower()]
    print(f"  [sofascore] {len(fixtures)} total events, {len(finished)} finished — fetching xG for finished...")

    rows = []
    for i, f in enumerate(finished, start=1):
        home_xg, away_xg = fetch_match_xg(f["event_id"])
        if home_xg is None:
            continue
        rows.append({
            "home_team": f["home_team"],
            "away_team": f["away_team"],
            "date": f["date"],
            "home_xg": home_xg,
            "away_xg": away_xg,
            "tournament": "FIFA World Cup",
        })
        time.sleep(0.4)  # rate-limit politely
        if i % 8 == 0:
            print(f"    {i}/{len(finished)} processed")

    df = pd.DataFrame(rows)
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(_CACHE_PATH)
    print(f"  [sofascore] cached {len(df)} WC 2026 xG records → {_CACHE_PATH.name}")
    return df
