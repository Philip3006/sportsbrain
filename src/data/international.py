import io

import pandas as pd
import requests

from src.config import COMPETITIVE_TOURNAMENTS, INTL_CSV_URL, canonical_name
from src.data.cache import disk_cache


@disk_cache("international_results", max_age_hours=24.0)
def fetch_international_results(force: bool = False) -> pd.DataFrame:
    """
    Downloads the martj42/international_results CSV (~4 MB, ~50k matches).
    Returns cleaned DataFrame with canonical team names.
    """
    response = requests.get(INTL_CSV_URL, timeout=30)
    response.raise_for_status()

    df = pd.read_csv(io.StringIO(response.text))
    df = df.rename(
        columns={
            "date": "date",
            "home_team": "home_team",
            "away_team": "away_team",
            "home_score": "home_score",
            "away_score": "away_score",
            "tournament": "tournament",
            "city": "city",
            "country": "country",
            "neutral": "neutral",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce").astype("Int64")
    df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce").astype("Int64")
    df["neutral"] = df["neutral"].astype(bool)
    df["home_team"] = df["home_team"].map(canonical_name)
    df["away_team"] = df["away_team"].map(canonical_name)

    df = df.dropna(subset=["home_score", "away_score"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[
        ["date", "home_team", "away_team", "home_score", "away_score",
         "tournament", "neutral"]
    ]


def filter_competitive(df: pd.DataFrame) -> pd.DataFrame:
    """Keeps only competitive matches; drops friendlies."""
    mask = df["tournament"].apply(
        lambda t: any(comp in t for comp in COMPETITIVE_TOURNAMENTS)
    )
    return df[mask].reset_index(drop=True)


def filter_minnow_qualifiers(
    df: pd.DataFrame,
    elo_series: pd.DataFrame,
    max_elo_diff: float = 450,
) -> pd.DataFrame:
    """
    Removes qualification matches with extreme Elo imbalance (e.g. Germany 9:0 Andorra).
    These inflate attack/defence parameters and cause overconfidence in the GBT model.
    Finals and group-stage matches are kept even if lopsided — those reflect real strength.
    Only call this for GBT training; DC model needs unfiltered data for all team parameters.
    """
    if elo_series.empty:
        return df

    # Build a map: (date, home_team) -> elo_diff using last known Elo before each match
    latest_elo: dict[str, float] = {}
    for _, row in elo_series.sort_values("date").iterrows():
        latest_elo[row["home_team"]] = float(row["elo_home_post"])
        latest_elo[row["away_team"]] = float(row["elo_away_post"])

    qualifier_keywords = ("qualif", "qualification")

    def is_minnow_game(row) -> bool:
        t = str(row.get("tournament", "")).lower()
        if not any(k in t for k in qualifier_keywords):
            return False
        elo_h = latest_elo.get(row["home_team"], 1500.0)
        elo_a = latest_elo.get(row["away_team"], 1500.0)
        return abs(elo_h - elo_a) > max_elo_diff

    mask = df.apply(is_minnow_game, axis=1)
    removed = mask.sum()
    if removed > 0:
        print(f"  Qualifier filter: removed {removed} minnow-mismatch games "
              f"(|elo_diff| > {max_elo_diff}), kept {(~mask).sum()}")
    return df[~mask].reset_index(drop=True)


def filter_since(df: pd.DataFrame, date: str | pd.Timestamp) -> pd.DataFrame:
    """Returns matches on or after date."""
    return df[df["date"] >= pd.Timestamp(date)].reset_index(drop=True)


def filter_before(df: pd.DataFrame, date: str | pd.Timestamp) -> pd.DataFrame:
    """Returns matches strictly before date (no lookahead)."""
    return df[df["date"] < pd.Timestamp(date)].reset_index(drop=True)
