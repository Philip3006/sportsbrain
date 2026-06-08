from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class CLVRecord:
    match_id: str
    market: str           # "home" | "draw" | "away"
    bet_odds: float       # decimal odds at bet placement
    closing_odds: float   # Pinnacle closing decimal odds
    clv: float            # bet_odds / closing_odds - 1 (positive = beat close)


def compute_clv(bet_odds: float, closing_odds: float) -> float:
    """CLV = bet_odds / closing_odds - 1. Positive = beat the closing line."""
    if closing_odds <= 0 or bet_odds <= 0:
        return 0.0
    return bet_odds / closing_odds - 1.0


def track_bets(
    bet_signals: list,
    closing_odds_df: pd.DataFrame,
    match_id_col: str = "match_id",
    market_col: str = "market",
    close_home_col: str = "ps_close_home",
    close_draw_col: str = "ps_close_draw",
    close_away_col: str = "ps_close_away",
) -> list[CLVRecord]:
    """
    Matches each BetSignal to its Pinnacle closing line.
    closing_odds_df must have: match_id_col, market_col, and closing price cols.
    Returns list of CLVRecord.
    """
    close_col_map = {
        "home": close_home_col,
        "draw": close_draw_col,
        "away": close_away_col,
    }
    records = []

    for sig in bet_signals:
        row = closing_odds_df[closing_odds_df[match_id_col] == sig.match_id]
        if row.empty:
            continue
        row = row.iloc[0]
        col = close_col_map.get(sig.market)
        if col is None or col not in row.index:
            continue
        close = float(row[col])
        if close <= 1.0:
            continue

        records.append(CLVRecord(
            match_id=sig.match_id,
            market=sig.market,
            bet_odds=sig.decimal_odds,
            closing_odds=close,
            clv=compute_clv(sig.decimal_odds, close),
        ))

    return records


def clv_summary(records: list[CLVRecord]) -> dict[str, float]:
    """
    Aggregates CLV records.
    mean_clv > 0 is the primary signal that the model has genuine edge.
    """
    if not records:
        return {
            "mean_clv": 0.0,
            "median_clv": 0.0,
            "pct_positive_clv": 0.0,
            "n_records": 0,
        }
    clvs = np.array([r.clv for r in records])
    return {
        "mean_clv": float(clvs.mean()),
        "median_clv": float(np.median(clvs)),
        "pct_positive_clv": float((clvs > 0).mean()),
        "n_records": len(records),
    }
