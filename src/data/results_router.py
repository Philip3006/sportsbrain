"""Routet Result-Loader pro Liga. Ersetzt hardcoded WM-Filter in settle_from_results.

Rückgabe je Loader: dict {(canonical_home, canonical_away): (home_score, away_score)}
— identisches Format wie _fetch_completed_wm_scores(), damit die Settlement-Loop
league-agnostisch bleibt.
"""
from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from src.config import canonical_name, league_config

_log = logging.getLogger("sportsbrain.results_router")

ScoreDict = dict[tuple[str, str], tuple[int, int]]


def fetch_results_for_league(
    league_short: str,
    match_dates: Iterable[str] | None = None,
) -> ScoreDict:
    """Lädt fertige Endergebnisse für alle offenen Bets einer Liga.

    league_short ist der Registry-Short-Code (wm2026 | bl2 | ...). Unbekannte
    Ligen liefern leeres Dict, damit Settle silent skippen kann (Warnung im Log).
    match_dates ist optional; wenn gegeben, engt der Loader das Datumsfenster ein
    (spart API-Quota bei TheOddsAPI-Scores).
    """
    # Registry-Lookup nur zur Validierung — Short-Code steht direkt im Ledger.
    cfg = _cfg_by_short(league_short)
    if cfg is None:
        _log.warning("Unbekannter league-Short '%s' — keine Result-Route", league_short)
        return {}
    source = cfg.get("result_source", "")
    arg = cfg.get("result_source_arg", "")
    if source == "martj42":
        return _from_martj42(arg)
    if source == "football_data":
        return _from_football_data(arg, cfg.get("start_date"))
    _log.warning("Result-Source '%s' hat keinen Loader — Ledger-Zeilen bleiben open", source)
    return {}


def _cfg_by_short(short: str) -> dict | None:
    from src.config import LEAGUE_REGISTRY
    for cfg in LEAGUE_REGISTRY.values():
        if cfg.get("short") == short:
            return cfg
    return None


def _from_martj42(tournament: str) -> ScoreDict:
    """WM/EM/Copa: martj42 International-Results CSV. Filter auf tournament + WM-Start."""
    try:
        from src.data.international import fetch_international_results
        df = fetch_international_results()
    except Exception as exc:
        _log.debug("martj42 fetch failed: %s", exc)
        return {}
    if df.empty or "tournament" not in df.columns:
        return {}
    mask = (df["tournament"] == tournament) & df["home_score"].notna()
    # WM 2026: nur ab 2026-06-11 (nicht versehentlich historische WMs settlen)
    if tournament == "FIFA World Cup":
        mask &= df["date"] >= pd.Timestamp("2026-06-11")
    subset = df[mask]
    out: ScoreDict = {}
    for _, r in subset.iterrows():
        key = (canonical_name(str(r["home_team"])), canonical_name(str(r["away_team"])))
        out[key] = (int(r["home_score"]), int(r["away_score"]))
    return out


def _from_football_data(fbdata_code: str, season_start_iso: str | None) -> ScoreDict:
    """Club-Ligen: football-data.co.uk aktuelle Saison. season_start_iso bestimmt
    welche Saison (2026-08-01 → 2627 = 2026/27).

    Fallback: Wenn das CSV noch nicht verfügbar ist (neue Saison, <Spieltag 5),
    versucht TheOddsAPI /scores als Ersatz-Quelle.
    """
    if not fbdata_code:
        return {}
    season = _season_code(season_start_iso)
    if not season:
        return {}

    df = None
    try:
        from src.data.football_data import fetch_season
        df = fetch_season(fbdata_code, season)
    except Exception as exc:
        _log.debug("football-data %s/%s fetch failed: %s", fbdata_code, season, exc)

    if df is not None and not df.empty:
        out: ScoreDict = {}
        for _, r in df.iterrows():
            try:
                hg = int(r["home_score"])
                ag = int(r["away_score"])
            except (KeyError, ValueError, TypeError):
                continue
            key = (canonical_name(str(r["home_team"])), canonical_name(str(r["away_team"])))
            out[key] = (hg, ag)
        if out:
            return out

    # Fallback: TheOddsAPI /scores (kostenpflichtig, aber 1 Req/match) — nur wenn CSV leer
    _log.info("football-data CSV leer für %s/%s — versuche TheOddsAPI scores fallback",
              fbdata_code, season)
    return _from_odds_api_scores(fbdata_code)


def _from_odds_api_scores(fbdata_code: str) -> ScoreDict:
    """TheOddsAPI /scores als Fallback wenn football-data CSV noch nicht verfügbar."""
    sport_key_map = {
        "D2": "soccer_germany_bundesliga2",
        "D1": "soccer_germany_bundesliga",
        "E0": "soccer_epl",
    }
    sport_key = sport_key_map.get(fbdata_code)
    if not sport_key:
        return {}
    try:
        from src.data.odds_api import fetch_scores
        scores = fetch_scores(sport=sport_key, days_from=3) or []
    except Exception as exc:
        _log.debug("TheOddsAPI scores fallback failed: %s", exc)
        return {}
    out: ScoreDict = {}
    for m in scores:
        if not m.get("completed"):
            continue
        home = canonical_name(str(m.get("home_team", "")))
        away = canonical_name(str(m.get("away_team", "")))
        scores_raw = m.get("scores") or []
        h_score = a_score = None
        for sc in scores_raw:
            if sc.get("name") == m.get("home_team"):
                try:
                    h_score = int(sc["score"])
                except (KeyError, ValueError, TypeError):
                    pass
            elif sc.get("name") == m.get("away_team"):
                try:
                    a_score = int(sc["score"])
                except (KeyError, ValueError, TypeError):
                    pass
        if h_score is not None and a_score is not None:
            out[(home, away)] = (h_score, a_score)
    return out


def _season_code(season_start_iso: str | None) -> str | None:
    """'2026-08-01' → '2627' (football-data-Format YYYY→YY-YY)."""
    if not season_start_iso:
        return None
    try:
        y1 = int(season_start_iso.split("-")[0])
    except (ValueError, IndexError):
        return None
    return f"{y1 % 100:02d}{(y1 + 1) % 100:02d}"
