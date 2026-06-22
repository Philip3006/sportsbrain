"""Empirical scoreline prior from historical WC matches.

Builds a normalized (max_goals+1 x max_goals+1) matrix from historical
international results (WC finals + qualifiers), with exponential recency
weighting so that modern matches dominate the prior.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

_DEFAULT_DATA = Path(__file__).parent.parent.parent / "data" / "cache" / "international_results.pkl"
_HALF_LIFE_YEARS = 10   # WM 2022 weighs ~3× more than WM 1990
_REFERENCE_YEAR = 2026  # decay relative to WM 2026
_WC_KEYWORDS = ("World Cup", "WC", "FIFA")


def build_wc_prior(
    data_path: Path | str | None = None,
    max_goals: int = 10,
    half_life_years: float = _HALF_LIFE_YEARS,
    reference_year: int = _REFERENCE_YEAR,
) -> np.ndarray:
    """Build an empirical (max_goals+1 x max_goals+1) prior matrix.

    Loads historical WC matches, applies exponential recency weighting,
    fills the scoreline matrix, normalises to sum=1.

    Args:
        data_path:       Path to international_results.pkl (default: data/cache/).
        max_goals:       Max goals per team to track (matches DC matrix size).
        half_life_years: Decay half-life. Scores older by this many years get ½ the weight.
        reference_year:  Year to decay relative to (default 2026 = current WM).

    Returns:
        Normalised (max_goals+1 x max_goals+1) numpy array.
    """
    path = Path(data_path) if data_path else _DEFAULT_DATA
    df: pd.DataFrame = pd.read_pickle(path)

    # Filter WC-related matches
    mask = df["tournament"].str.contains("|".join(_WC_KEYWORDS), case=False, na=False)
    wc = df[mask].dropna(subset=["home_score", "away_score", "date"]).copy()

    # Parse year from date
    wc["year"] = pd.to_datetime(wc["date"], errors="coerce").dt.year
    wc = wc.dropna(subset=["year"])

    # Exponential recency weight: w = 2^(-(reference_year - year) / half_life)
    decay_lambda = math.log(2) / half_life_years
    wc["weight"] = np.exp(-decay_lambda * (reference_year - wc["year"]))

    # Fill scoreline matrix
    matrix = np.zeros((max_goals + 1, max_goals + 1))
    for _, row in wc.iterrows():
        h = min(int(row["home_score"]), max_goals)
        a = min(int(row["away_score"]), max_goals)
        matrix[h, a] += row["weight"]

    # Normalise
    total = matrix.sum()
    if total > 0:
        matrix /= total

    return matrix
