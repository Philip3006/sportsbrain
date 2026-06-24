"""Match-History-Loader mit Sackmann-Primary, XLSX-Fallback (Roadmap J2-I).

Sackmann-GitHub-Repos (tennis_atp/tennis_wta) sind seit 2026-06 nicht mehr
öffentlich erreichbar. Wenn Sackmann leer/down ist, fällt der Loader auf
tennis-data.co.uk-XLSX zurück und konvertiert in Sackmann-Schema, sodass
existierender Elo-Code (`compute_tennis_elo`) unverändert läuft.
"""
from __future__ import annotations

import pandas as pd

from src.data.tennis_data import fetch_atp_matches, fetch_wta_matches
from src.data.tennis_odds import fetch_full_tour_odds


_SURFACE_NORM = {"hard": "Hard", "clay": "Clay", "grass": "Grass", "carpet": "Carpet"}


def _xlsx_to_sackmann(odds_df: pd.DataFrame) -> pd.DataFrame:
    """Convert tennis-data XLSX schema → Sackmann column names."""
    if odds_df.empty:
        return pd.DataFrame()
    out = odds_df.rename(columns={
        "Date": "tourney_date",
        "Tournament": "tourney_name",
        "Winner": "winner_name",
        "Loser": "loser_name",
        "WRank": "winner_rank",
        "LRank": "loser_rank",
        "Round": "round",
    }).copy()
    surf_lower = out.get("surface_std", pd.Series(["hard"] * len(out))).astype(str).str.lower()
    out["surface"] = surf_lower.map(_SURFACE_NORM).fillna("Hard")
    out["tourney_level"] = "A"
    out["score"] = ""
    keep = ["tourney_date", "tourney_name", "tourney_level", "surface",
            "winner_name", "loser_name", "score", "round",
            "winner_rank", "loser_rank"]
    return out[[c for c in keep if c in out.columns]].dropna(subset=["winner_name", "loser_name"])


def load_match_history() -> tuple[pd.DataFrame, str]:
    """Return (combined ATP+WTA matches, source-tag).

    source-tag ∈ {"sackmann", "xlsx-fallback", "empty"} — for logging.
    """
    try:
        atp = fetch_atp_matches()
    except Exception:
        atp = pd.DataFrame()
    try:
        wta = fetch_wta_matches()
    except Exception:
        wta = pd.DataFrame()

    if not atp.empty or not wta.empty:
        frames = [df for df in (atp, wta) if not df.empty]
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values("tourney_date").reset_index(drop=True)
        return combined, "sackmann"

    # Fallback: tennis-data.co.uk XLSX (2019-2025 cached)
    try:
        xlsx = fetch_full_tour_odds(tours=["atp", "wta"])
    except Exception:
        xlsx = pd.DataFrame()
    if xlsx.empty:
        return pd.DataFrame(), "empty"

    sack_like = _xlsx_to_sackmann(xlsx)
    sack_like = sack_like.sort_values("tourney_date").reset_index(drop=True)
    return sack_like, "xlsx-fallback"
