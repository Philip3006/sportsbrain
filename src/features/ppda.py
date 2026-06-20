"""
PPDA (Passes Per Defensive Action) als Team-Form-Feature.

Aggregiert StatsBomb-Match-PPDA über die letzten N Matches eines Teams
(Default 10). Bei Teams ohne ausreichende Historie greift ein
Konföderations-Prior (Bayes-Style Shrinkage):

    ppda_effective = (n_obs * mean_obs + prior_weight * prior) / (n_obs + prior_weight)

Schutzregel: Wenn ein Team weniger als `MIN_MATCHES` (Default 3) PPDA-
Werte in der Historie hat, kommt der Konföderations-Mittelwert als Prior
zum Einsatz; bei `n_obs == 0` ist das Feature gleich dem Prior. Wenn auch
der Prior fehlt (z.B. exotische Konföderation ohne Daten), fällt es auf
den globalen Mittelwert zurück.

Live-Use: Per `PPDA_LIVE_ENABLED`-Flag in `src/config.py` geschaltet. Im
Shadow-Modus (Default `False`) wird das Feature für den Backtest
berechnet, der Live-Scanner gibt jedoch 0.0 zurück, damit existierende
LGBM-Modelle nicht durch unbekannte Feature-Verteilung verzerrt werden.
"""
from __future__ import annotations

import pandas as pd

from src.config import TEAM_CONFEDERATION
from src.data.fbref_ppda import get_team_season_ppda

# Globale Defaults
DEFAULT_WINDOW: int = 10
MIN_MATCHES: int = 3
PRIOR_WEIGHT: float = 3.0
# Falls weder Konföderations-Prior noch FBref-Fallback verfügbar sind:
GLOBAL_FALLBACK_PPDA: float = 11.5  # robuster Mittelwert internationaler Teams


def team_match_ppda_series(
    team: str,
    before_date: pd.Timestamp,
    ppda_df: pd.DataFrame,
    n_games: int = DEFAULT_WINDOW,
) -> pd.Series:
    """
    Gibt PPDA-Werte des Teams aus den letzten `n_games` vor `before_date`
    zurück. NaN-Werte werden gefiltert (Match-Fragmente mit zu wenig Daten).
    """
    if ppda_df.empty:
        return pd.Series(dtype=float)

    mask = (
        ((ppda_df["home_team"] == team) | (ppda_df["away_team"] == team))
        & (ppda_df["date"] < before_date)
    )
    recent = ppda_df[mask].sort_values("date", ascending=False).head(n_games)
    if recent.empty:
        return pd.Series(dtype=float)

    vals: list[float] = []
    for _, row in recent.iterrows():
        v = row["home_ppda"] if row["home_team"] == team else row["away_ppda"]
        if pd.notna(v):
            vals.append(float(v))
    return pd.Series(vals, dtype=float)


def confederation_mean_ppda(
    conf: str,
    before_date: pd.Timestamp,
    ppda_df: pd.DataFrame,
) -> float | None:
    """
    Mittelwert aller PPDA-Werte von Teams einer Konföderation vor `before_date`.
    Returns None wenn die Konföderation in den Daten nicht vorkommt.
    """
    if ppda_df.empty:
        return None
    teams_in_conf = {t for t, c in TEAM_CONFEDERATION.items() if c == conf}
    if not teams_in_conf:
        return None
    mask = (
        ((ppda_df["home_team"].isin(teams_in_conf)) | (ppda_df["away_team"].isin(teams_in_conf)))
        & (ppda_df["date"] < before_date)
    )
    recent = ppda_df[mask]
    if recent.empty:
        return None
    vals: list[float] = []
    for _, row in recent.iterrows():
        if row["home_team"] in teams_in_conf and pd.notna(row["home_ppda"]):
            vals.append(float(row["home_ppda"]))
        if row["away_team"] in teams_in_conf and pd.notna(row["away_ppda"]):
            vals.append(float(row["away_ppda"]))
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _team_prior(team: str, before_date: pd.Timestamp, ppda_df: pd.DataFrame) -> float:
    """Bestimmt einen Prior für `team`: Konföderation → FBref-Fallback → globaler Mittelwert."""
    conf = TEAM_CONFEDERATION.get(team)
    if conf:
        m = confederation_mean_ppda(conf, before_date, ppda_df)
        if m is not None:
            return m
    fbref = get_team_season_ppda(team)
    if fbref is not None:
        return float(fbref)
    return GLOBAL_FALLBACK_PPDA


def team_rolling_ppda(
    team: str,
    before_date: pd.Timestamp,
    ppda_df: pd.DataFrame,
    n_games: int = DEFAULT_WINDOW,
    min_matches: int = MIN_MATCHES,
    prior_weight: float = PRIOR_WEIGHT,
) -> float:
    """
    Rolling-PPDA für `team`:
      - n_obs >= min_matches → reines Sample-Mittel
      - n_obs < min_matches → Bayes-Shrinkage gegen Konföderations-Prior
      - n_obs == 0          → Prior pur
    """
    series = team_match_ppda_series(team, before_date, ppda_df, n_games=n_games)
    n_obs = len(series)
    if n_obs >= min_matches:
        return float(series.mean())

    prior = _team_prior(team, before_date, ppda_df)
    if n_obs == 0:
        return prior
    return float((series.sum() + prior_weight * prior) / (n_obs + prior_weight))


def ppda_lambda_multipliers(
    ppda_home: float,
    ppda_away: float,
    baseline: float = GLOBAL_FALLBACK_PPDA,
    z_scale: float = 5.0,
    boost: float = 0.025,
    clip: float = 0.10,
) -> tuple[float, float]:
    """
    Liefert (mult_lh, mult_la) zur Multiplikation der Dixon-Coles-Lambdas λ_home/λ_away.

    Idee: niedriges PPDA = aggressives Pressing → mehr Ball-Wins hoch im Feld →
    mehr eigene Torchancen. Eigenes Attack-Lambda wird leicht angehoben,
    Effekt auf das Opp-Lambda bleibt 0 (Counter-Vulnerabilität gleicht den
    Defence-Bonus statistisch ungefähr aus — neutrale Default-Annahme).

    Multiplier deckelt bei ±10% (clip). Bei NaN-Inputs → 1.0 (neutral).
    """
    if not (ppda_home == ppda_home):  # NaN check
        z_h = 0.0
    else:
        z_h = (baseline - ppda_home) / z_scale
    if not (ppda_away == ppda_away):
        z_a = 0.0
    else:
        z_a = (baseline - ppda_away) / z_scale

    mult_lh = float(min(max((1.0 + boost * z_h), 1.0 - clip), 1.0 + clip))
    mult_la = float(min(max((1.0 + boost * z_a), 1.0 - clip), 1.0 + clip))
    return mult_lh, mult_la


def ppda_features(
    home: str,
    away: str,
    before_date: pd.Timestamp,
    ppda_df: pd.DataFrame | None,
    n_games: int = DEFAULT_WINDOW,
) -> dict[str, float]:
    """
    Liefert ppda_home, ppda_away, ppda_diff. Returns 0er-Dict bei fehlendem ppda_df
    (Konsistenz mit anderen builder-Features im Fehlerfall).
    """
    if ppda_df is None or ppda_df.empty:
        return {"ppda_home": 0.0, "ppda_away": 0.0, "ppda_diff": 0.0}

    ph = team_rolling_ppda(home, before_date, ppda_df, n_games=n_games)
    pa = team_rolling_ppda(away, before_date, ppda_df, n_games=n_games)
    return {
        "ppda_home": ph,
        "ppda_away": pa,
        # Positiv = Home pressing aggressiver als Away (niedrigeres home, höheres away)
        "ppda_diff": pa - ph,
    }
