"""
Live xG aggregator (Lever 2).

Combines:
  1. StatsBomb open-data (historic: WC 1958-2022, Euro 2000-2024, Copa 2016-2024)
  2. Sofascore RapidAPI (live: WC 2026, updates within minutes of full-time)

The merge eliminates the StatsBomb publication lag (typically months) for
in-progress tournaments. Sofascore takes precedence on duplicates (same date +
team pair) — they have the freshest data.
"""
from __future__ import annotations

import pandas as pd


def fetch_live_xg(force: bool = False) -> pd.DataFrame:
    """Returns merged xG DataFrame: home_team, away_team, date, home_xg, away_xg, tournament."""
    frames: list[pd.DataFrame] = []
    sources: list[str] = []

    # 1. StatsBomb (historic)
    try:
        from src.data.statsbomb import fetch_statsbomb_xg
        sb = fetch_statsbomb_xg(force=force)
        if sb is not None and not sb.empty:
            frames.append(sb)
            sources.append(f"StatsBomb ({len(sb)})")
    except Exception as e:
        print(f"  [xg_live] StatsBomb fetch failed: {e}")

    # 2. Sofascore (WC 2026 live)
    try:
        from src.data.sofascore import fetch_wc2026_xg
        sf = fetch_wc2026_xg(force=force)
        if sf is not None and not sf.empty:
            frames.append(sf)
            sources.append(f"Sofascore WC2026 ({len(sf)})")
    except Exception as e:
        print(f"  [xg_live] Sofascore fetch failed: {e}")

    if not frames:
        return pd.DataFrame(columns=["home_team", "away_team", "date", "home_xg", "away_xg", "tournament"])

    df = pd.concat(frames, ignore_index=True)

    # Dedup: keep the LAST occurrence (Sofascore appended last, so it wins for WC2026).
    # Match on (date normalized to day, home_team, away_team).
    df["_date_day"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.drop_duplicates(subset=["_date_day", "home_team", "away_team"], keep="last")
    df = df.drop(columns=["_date_day"]).reset_index(drop=True)

    print(f"  [xg_live] merged: {' + '.join(sources)} → {len(df)} matches")
    return df
