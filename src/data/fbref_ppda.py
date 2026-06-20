"""
FBref Team-Season PPDA Fallback.

Wenn StatsBomb-Open-Data für ein Team (oder einen Zeitraum) keine PPDA-Werte
liefert, greift dieser Fallback auf Saison-aggregierte Team-PPDA-Werte
zurück. Daten liegen als JSON-Snapshot in `data/cache/fbref_ppda.json` —
manuell oder via `refresh()`-Stub befüllt.

Format des Snapshots:
{
    "as_of": "2026-06-20",
    "teams": {
        "<canonical-team-name>": {"ppda": 9.8, "season": "2025-26", "league": "..."}
    }
}

Snapshot ist absichtlich klein gehalten: nur Saison-Mittel pro Team — keine
Match-Granularität. Diese Datei rechtfertigt sich als simpler, robuster
Fallback ohne FBref-HTML-Scraping-Brittleness.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.config import DATA_CACHE, canonical_name as _cn

_SNAPSHOT_PATH: Path = DATA_CACHE / "fbref_ppda.json"


def _load_snapshot() -> dict[str, float]:
    if not _SNAPSHOT_PATH.exists():
        return {}
    try:
        data = json.loads(_SNAPSHOT_PATH.read_text())
    except Exception:
        return {}
    teams = data.get("teams") or {}
    out: dict[str, float] = {}
    for team_name, info in teams.items():
        try:
            out[_cn(team_name)] = float(info["ppda"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def get_team_season_ppda(team: str) -> float | None:
    """
    Liefert Saison-Mittel-PPDA für `team` aus dem Snapshot — oder None
    wenn nicht vorhanden. Caller entscheidet über Confederation-Prior.
    """
    snapshot = _load_snapshot()
    return snapshot.get(_cn(team))


def write_snapshot(teams: dict[str, dict], as_of: str) -> None:
    """
    Schreibt einen neuen Snapshot. `teams` = {team_name: {ppda, season, league}}.
    Used by refresh-stub oder manuelle Pflege via CLI/Script.
    """
    _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"as_of": as_of, "teams": teams}
    _SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
