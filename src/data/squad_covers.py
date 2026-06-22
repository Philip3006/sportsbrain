"""Covers.com and Sofascore injury data fetchers."""
from __future__ import annotations

import pandas as pd

from .squad_models import PlayerStatus


def _fetch_covers_squad(team: str, match_date: pd.Timestamp) -> list[PlayerStatus]:
    """
    Fetches injury data from covers.com and converts to PlayerStatus list.
    Returns a small list of unavailable/doubtful players (not the full squad).
    Used as primary real-injury source before Transfermarkt.
    """
    try:
        from src.data.injury_data import get_team_injuries
    except ImportError:
        return []

    injuries = get_team_injuries(team)
    if not injuries:
        return []

    players = []
    for inj in injuries:
        players.append(PlayerStatus(
            name=inj["player"],
            position="unknown",
            availability=inj["availability"],
            status=inj["status"],
            key_player=True,
            p_plays=inj["availability"],
        ))
    return players


def _overlay_sofascore_values(team: str, players: list[PlayerStatus]) -> None:
    """Best-effort overlay of Sofascore-derived per-player market values onto
    a SquadReport's PlayerStatus list. Silently does nothing on failure.
    """
    try:
        from src.data.sofascore import overlay_player_values
        overlay_player_values(team, players)
    except Exception:
        pass
