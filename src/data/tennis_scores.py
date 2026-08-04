"""Tennis-Score-Fetching (Roadmap TENNIS P1.1).

Primary:  TheOddsAPI /v4/sports/{sport_key}/scores?daysFrom=3
Fallback: ESPN public API site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard
Last-Res: Empty result (Aufrufer skipped Settlement)

Output-Schema (kanonisch):
  {
    "<match_id_or_players_key>": {
      "player_a": str,
      "player_b": str,
      "status": "completed"|"retired"|"walkover"|"cancelled"|"scheduled"|"in_progress",
      "sets": [(a_games, b_games), ...],
      "winner": "a"|"b"|None,
      "retired_by": "a"|"b"|None,
      "best_of": 3|5,
      "source": "odds_api"|"espn"|"cache",
      "kickoff_utc": ISO8601 str | None,
    }
  }
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).parent.parent.parent


def canonical_match_key(a: str, b: str) -> str:
    """J8-B3: robuste Norm für Score-Dict-Lookups vs. Ledger-Namen.

    NFD-strip + lowercase + non-alnum-collapse + sortierte Reihenfolge (a|b vs. b|a matcht).
    """
    def _clean(s: str) -> str:
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        s = re.sub(r"[^a-z0-9]+", "", s.lower())
        return s
    ca, cb = _clean(a), _clean(b)
    return f"{ca}|{cb}" if ca <= cb else f"{cb}|{ca}"

# Modul-Level Diagnostik: welche Quelle hat den letzten Fetch bedient
LAST_TENNIS_SCORES_SOURCE: str = "none"


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


def _parse_score_string(score_str: str) -> list[tuple[int, int]]:
    """Parsed 'Set 1: 6-3, Set 2: 4-6, Set 3: 7-5' oder '6-3, 6-4' oder Nested JSON.

    Rueckgabe: [(6,3), (4,6), (7,5)]. Bei Parse-Fehler leer.
    """
    if not score_str:
        return []
    sets: list[tuple[int, int]] = []
    for chunk in score_str.split(","):
        chunk = chunk.strip()
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", chunk)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            # Tiebreak-Zahlen (>7) filtern - nur game-count
            if 0 <= a <= 20 and 0 <= b <= 20:
                sets.append((a, b))
    return sets


def _parse_odds_api_scores(payload: list[dict], sport_key: str) -> dict[str, dict]:
    """Convert TheOddsAPI /scores response to canonical schema."""
    out: dict[str, dict] = {}
    best_of = 5 if "grand_slam" in sport_key or any(s in sport_key for s in ("australian_open", "french_open", "wimbledon", "us_open")) else 3

    for game in payload:
        mid = game.get("id", "")
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        completed = game.get("completed", False)
        scores = game.get("scores") or []

        # Odds API 'scores' schema:
        # [{"name": "Player A", "score": "6-3,4-6,7-5"}, {"name": "Player B", "score": "3-6,6-4,5-7"}]
        sets_a: list[int] = []
        sets_b: list[int] = []
        for s in scores:
            name = s.get("name", "")
            games = _parse_score_string(str(s.get("score", "")))
            if name == home:
                sets_a = [g[0] for g in games]  # falls Format "6-3,4-6" mit Sub-Splits
            elif name == away:
                sets_b = [g[0] for g in games]

        # Bauen wir sets aus beiden Listen (paarweise zip)
        sets: list[tuple[int, int]] = []
        for a, b in zip(sets_a, sets_b):
            sets.append((int(a), int(b)))

        # Wenn schon completed aber winner unklar, berechnen wir aus sets
        winner = None
        if completed and sets:
            a_sets = sum(1 for a, b in sets if a > b)
            b_sets = sum(1 for a, b in sets if b > a)
            if a_sets > b_sets:
                winner = "a"
            elif b_sets > a_sets:
                winner = "b"

        status = "completed" if completed else "in_progress" if sets else "scheduled"

        entry = {
            "player_a": home,
            "player_b": away,
            "status": status,
            "sets": sets,
            "winner": winner,
            "retired_by": None,
            "best_of": best_of,
            "source": "odds_api",
            "kickoff_utc": game.get("commence_time"),
        }
        out[mid] = entry
        out[f"{home} vs {away}"] = entry
        out[canonical_match_key(home, away)] = entry
    return out


def fetch_tennis_scores_odds_api(sport_keys: list[str], days_from: int = 3) -> dict[str, dict]:
    """Ruft TheOddsAPI /scores fuer jeden Tennis-Sport-Key."""
    global LAST_TENNIS_SCORES_SOURCE
    key = _api_key()
    if not key:
        return {}
    all_scores: dict[str, dict] = {}
    for sk in sport_keys:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sk}/scores/"
            r = requests.get(url, params={"apiKey": key, "daysFrom": days_from}, timeout=10)
            if r.status_code == 200:
                parsed = _parse_odds_api_scores(r.json(), sk)
                all_scores.update(parsed)
            elif r.status_code in (422, 404):
                # Sport-Key derzeit inaktiv - kein Fehler
                continue
            else:
                print(f"[tennis_scores] {sk}: HTTP {r.status_code}")
        except Exception as e:
            print(f"[tennis_scores] {sk}: {e}")
        time.sleep(0.25)  # Rate-Limit-Respekt
    if all_scores:
        LAST_TENNIS_SCORES_SOURCE = "odds_api"
    return all_scores


def _parse_espn_note(text: str) -> dict | None:
    """Parsed ESPN notes-Eintrag 'Player1 bt Player2 6-3 7-6 (7-5) 6-4' zu kanonischem Schema.

    Tiebreak-Sub-Scores werden entfernt: '7-6 (7-5)' → '7-6'.
    Setzungs-Prefixe werden entfernt: '(1) Djokovic' → 'Djokovic'.
    Nationalitäten werden entfernt: 'Osaka (JPN)' → 'Osaka'.
    """
    if " bt " not in text or not text.strip():
        return None
    # Tiebreak-Sub-Scores entfernen: (7-5) oder (7)
    clean = re.sub(r"\s*\(\d+-\d+\)", "", text)
    clean = re.sub(r"\s*\(\d+\)", "", clean)
    # 'ret' am Ende abschneiden
    is_retired = bool(re.search(r"\bret\b", clean, re.IGNORECASE))
    clean = re.sub(r"\s+ret\.?\s*$", "", clean, flags=re.IGNORECASE).strip()

    m = re.match(r"^(.+?)\s+bt\s+(.+?)\s+((?:\d+-\d+\s*)+)$", clean)
    if not m:
        return None

    def _clean_name(s: str) -> str:
        s = re.sub(r"^\(\d+\)\s*", "", s.strip())          # Seeding entfernen
        s = re.sub(r"\s*\([A-Z]{2,3}\)\s*$", "", s).strip()  # Nationalität entfernen
        return s

    winner = _clean_name(m.group(1))
    loser = _clean_name(m.group(2))
    sets = [(int(a), int(b)) for a, b in re.findall(r"(\d+)-(\d+)", m.group(3))]
    if not sets:
        return None

    a_sets = sum(1 for a, b in sets if a > b)
    b_sets = sum(1 for a, b in sets if b > a)
    winner_side = "a"  # winner ist immer player_a im Eintrag

    status = "retired" if is_retired else "completed"
    return {
        "player_a": winner,
        "player_b": loser,
        "status": status,
        "sets": sets,
        "winner": winner_side,
        "retired_by": None,
        "best_of": 5 if max(a_sets, b_sets) >= 3 else 3,
        "source": "espn_notes",
        "kickoff_utc": None,
    }


def fetch_tennis_scores_espn(tour: str = "atp", dates: str | None = None) -> dict[str, dict]:
    """ESPN: site.api.espn.com/apis/site/v2/sports/tennis/{atp|wta}/scoreboard.

    dates: YYYYMMDD — wenn angegeben, werden auch historische Matches geladen
           (ESPN liefert dann alle Turniermatches der laufenden Woche).
           Notes-Parsing ist primär für historische Scores; linescores für Live.
    """
    global LAST_TENNIS_SCORES_SOURCE
    url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard"
    params = {}
    if dates:
        params["dates"] = dates
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return {}
        events = r.json().get("events", [])
    except Exception as e:
        print(f"[tennis_scores] ESPN {tour} failed: {e}")
        return {}

    out: dict[str, dict] = {}

    def _store(entry: dict) -> None:
        pa, pb = entry["player_a"], entry["player_b"]
        key_fwd = f"{pa} vs {pb}"
        key_rev = f"{pb} vs {pa}"
        ck = canonical_match_key(pa, pb)
        # Bereits vorhandene Einträge nur überschreiben wenn wir mehr Daten haben
        existing = out.get(ck)
        if existing and existing.get("sets") and not entry.get("sets"):
            return
        out[key_fwd] = entry
        out[key_rev] = {**entry, "player_a": pb, "player_b": pa,
                        "winner": ("b" if entry["winner"] == "a" else "a") if entry["winner"] else None}
        out[ck] = entry

    for ev in events:
        for grouping in ev.get("groupings", [ev]):
            for comp in grouping.get("competitions", grouping.get("groupings", [])):
                # Notes-basiertes Parsing (funktioniert für historische Matches)
                for note in comp.get("notes", []):
                    text = note.get("text", "")
                    # Doppel-Matches überspringen
                    if " & " in text:
                        continue
                    entry = _parse_espn_note(text)
                    if entry:
                        _store(entry)

                # Linescores-Parsing (funktioniert für Live/sehr aktuelle Matches)
                try:
                    competitors = comp.get("competitors", [])
                    if len(competitors) != 2:
                        continue
                    a_data = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                    b_data = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
                    home = a_data.get("athlete", {}).get("displayName", "")
                    away = b_data.get("athlete", {}).get("displayName", "")
                    if not home or not away:
                        continue

                    a_lines = a_data.get("linescores") or []
                    b_lines = b_data.get("linescores") or []
                    sets: list[tuple[int, int]] = []
                    for a_ls, b_ls in zip(a_lines, b_lines):
                        try:
                            sets.append((int(a_ls.get("value", 0)), int(b_ls.get("value", 0))))
                        except Exception:
                            continue

                    if not sets:
                        continue  # Notes-Eintrag reicht

                    detail = comp.get("status", {}).get("type", {}).get("detail", "").lower()
                    is_retired = "retired" in detail or "retirement" in detail
                    is_walkover = "walkover" in detail or "w.o." in detail
                    completed = comp.get("status", {}).get("type", {}).get("completed", False)

                    winner = None
                    if a_data.get("winner"):
                        winner = "a"
                    elif b_data.get("winner"):
                        winner = "b"
                    elif sets:
                        a_s = sum(1 for a, b in sets if a > b)
                        b_s = sum(1 for a, b in sets if b > a)
                        if a_s > b_s:
                            winner = "a"
                        elif b_s > a_s:
                            winner = "b"

                    status = "in_progress"
                    if is_walkover:
                        status = "walkover"
                    elif is_retired:
                        status = "retired"
                    elif completed:
                        status = "completed"

                    best_of = 5 if max((sum(1 for a, b in sets if a > b),
                                       sum(1 for a, b in sets if b > a)), default=0) >= 3 else 3
                    _store({
                        "player_a": home, "player_b": away, "status": status,
                        "sets": sets, "winner": winner, "retired_by": None,
                        "best_of": best_of, "source": "espn", "kickoff_utc": ev.get("date"),
                    })
                except Exception:
                    continue

    if out:
        LAST_TENNIS_SCORES_SOURCE = "espn"
    return out


def fetch_tennis_scores(sport_keys: list[str] | None = None) -> dict[str, dict]:
    """Kombinierter Fetcher: Odds-API + ESPN (immer beide, ESPN ergänzt Lücken).

    sport_keys: Aktive Tennis-Sport-Keys aus discover_active_tournaments().
                Wenn None, wird ESPN direkt aufgerufen (kein Odds-API-Call).
    """
    scores: dict[str, dict] = {}
    if sport_keys:
        scores = fetch_tennis_scores_odds_api(sport_keys)

    # Immer ESPN zusätzlich fetchen — überschreibt Odds-API-Einträge die
    # "completed" aber winner=None/sets=[] haben (API liefert kein Ergebnis).
    espn_atp = fetch_tennis_scores_espn("atp")
    espn_wta = fetch_tennis_scores_espn("wta")
    espn_all = {**espn_atp, **espn_wta}
    for key, espn_entry in espn_all.items():
        existing = scores.get(key)
        if existing is None:
            scores[key] = espn_entry
        elif not existing.get("winner") and espn_entry.get("winner"):
            # ESPN hat Sieger, odds_api hat ihn nicht → ESPN gewinnt
            scores[key] = espn_entry
    return scores
