from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.betting.odds_utils import remove_margin_shin
from src.betting.value_detector import BetSignal, detect_value
from src.data.international import filter_before
from src.models import dixon_coles as dc
from src.models.elo import compute_elo_series

TOURNAMENT_EVENTS = [
    {"name": "WC2018",   "start": "2018-06-14", "end": "2018-07-15"},
    {"name": "WC2022",   "start": "2022-11-20", "end": "2022-12-18"},
    {"name": "EURO2020", "start": "2021-06-11", "end": "2021-07-11"},
    {"name": "EURO2024", "start": "2024-06-14", "end": "2024-07-14"},
    {"name": "CA2024",   "start": "2024-06-20", "end": "2024-07-14"},
]


def run_event_backtest(
    event: dict[str, str],
    all_matches: pd.DataFrame,
    odds_lookup: pd.DataFrame | None = None,
    phi: float = 0.0065,
    min_edge: float = 0.03,
    bankroll: float = 1000.0,
) -> pd.DataFrame:
    """
    Walk-forward backtest for a single tournament event.
    Trains DC model on all_matches before event start (no lookahead).
    Predicts each event match, checks odds_lookup for market prices.
    odds_lookup: DataFrame with columns [match_id, home_odds, draw_odds, away_odds,
                                          home_score, away_score].
    Returns DataFrame of bet results.
    """
    start = pd.Timestamp(event["start"])
    end = pd.Timestamp(event["end"])

    train_matches = filter_before(all_matches, start)
    if len(train_matches) < 50:
        return pd.DataFrame()

    print(f"  [{event['name']}] Training on {len(train_matches)} matches before {event['start']}")
    params = dc.fit(train_matches, phi=phi, today=start, max_iter=1000)

    elo_series = compute_elo_series(train_matches)

    event_matches = all_matches[
        (all_matches["date"] >= start) & (all_matches["date"] <= end)
    ].copy()

    if event_matches.empty:
        return pd.DataFrame()

    results = []
    for _, row in event_matches.iterrows():
        home, away = row["home_team"], row["away_team"]
        neutral = bool(row.get("neutral", True))  # tournament matches often neutral
        match_id = f"{event['name']}_{home}_vs_{away}"

        # Get market odds from lookup if available
        raw_odds = None
        closing_odds = None  # (close_home, close_draw, close_away) if available
        if odds_lookup is not None:
            odds_row = odds_lookup[odds_lookup["match_id"] == match_id]
            if not odds_row.empty:
                r = odds_row.iloc[0]
                raw_odds = (
                    float(r.get("home_odds", 0)),
                    float(r.get("draw_odds", 0)),
                    float(r.get("away_odds", 0)),
                )
                if any(o <= 1.0 for o in raw_odds):
                    raw_odds = None
                # Closing line proxy (H-Max/D-Max/A-Max from football-data.co.uk)
                if all(c in r.index for c in ("close_home", "close_draw", "close_away")):
                    ch = r["close_home"]
                    cd = r["close_draw"]
                    ca = r["close_away"]
                    try:
                        ch, cd, ca = float(ch), float(cd), float(ca)
                        if all(o > 1.0 for o in (ch, cd, ca)):
                            closing_odds = {"home": ch, "draw": cd, "away": ca}
                    except (ValueError, TypeError):
                        pass

        probs = dc.predict_match(home, away, params, neutral=neutral)
        probs_arr = np.array([probs["p_away"], probs["p_draw"], probs["p_home"]])

        actual_hg = int(row["home_score"])
        actual_ag = int(row["away_score"])
        if actual_hg > actual_ag:
            outcome = 2
        elif actual_hg == actual_ag:
            outcome = 1
        else:
            outcome = 0

        base_row = {
            "event": event["name"],
            "match_date": row["date"],
            "home": home,
            "away": away,
            "match_id": match_id,
            "p_home": probs["p_home"],
            "p_draw": probs["p_draw"],
            "p_away": probs["p_away"],
            "actual_outcome": outcome,
        }

        if raw_odds:
            signals = detect_value(
                home, away, probs_arr, raw_odds,
                bankroll=bankroll, min_edge=min_edge, match_id=match_id,
            )
            for sig in signals:
                market_outcome = {"home": 2, "draw": 1, "away": 0}[sig.market]
                won = int(outcome == market_outcome)
                pnl = sig.stake_pct * bankroll * (sig.decimal_odds - 1) if won else -sig.stake_pct * bankroll

                # Odds gap: H-Max / bet_odds - 1 (how much better price is available at H-Max)
                # Positive = you could get a better price by shopping; not trivially 0
                # True CLV (bet-price / closing-price from same source) is not computable
                # without separate opening/closing data — this is the best available proxy.
                odds_gap = None
                if closing_odds and sig.market in closing_odds:
                    close_val = closing_odds[sig.market]
                    if close_val > 1.0 and sig.decimal_odds > 1.0:
                        odds_gap = close_val / sig.decimal_odds - 1.0

                results.append({
                    **base_row,
                    "market":       sig.market,
                    "model_prob":   sig.model_prob,
                    "decimal_odds": sig.decimal_odds,
                    "ev":           sig.ev,
                    "kelly_f":      sig.kelly_f,
                    "stake":        sig.stake_pct * bankroll,
                    "won":          won,
                    "pnl":          pnl,
                    "odds_gap":     odds_gap,
                    "has_bet":      True,
                })
        else:
            results.append({**base_row, "has_bet": False})

    return pd.DataFrame(results)


def run_all_backtests(
    all_matches: pd.DataFrame,
    odds_lookup: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Runs walk-forward over all TOURNAMENT_EVENTS.
    Returns (results_df, summary_metrics).
    """
    frames = []
    for event in TOURNAMENT_EVENTS:
        df = run_event_backtest(event, all_matches, odds_lookup)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(), {}

    all_results = pd.concat(frames, ignore_index=True)
    metrics = compute_backtest_metrics(all_results)
    return all_results, metrics


def compute_backtest_metrics(results: pd.DataFrame) -> dict[str, float]:
    """
    Portfolio-level metrics from backtest results.
    Only considers rows where has_bet=True.
    """
    bets = results[results.get("has_bet", False) == True].copy() if "has_bet" in results.columns else results.copy()

    if bets.empty or "pnl" not in bets.columns:
        return {"n_bets": 0, "roi": 0.0, "win_rate": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}

    n_bets = len(bets)
    total_staked = bets["stake"].sum() if "stake" in bets.columns else 1.0
    total_pnl = bets["pnl"].sum()
    roi = total_pnl / total_staked if total_staked > 0 else 0.0
    win_rate = float(bets["won"].mean()) if "won" in bets.columns else 0.0

    # Sharpe on daily P&L
    if "match_date" in bets.columns and "pnl" in bets.columns:
        daily = bets.groupby("match_date")["pnl"].sum()
        sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown on cumulative P&L
    cum = bets["pnl"].cumsum()
    peak = cum.cummax()
    drawdown = (cum - peak)
    max_dd = float(drawdown.min())

    # Odds-gap metrics: H-Max / bet_odds - 1 for bets where H-Max is available
    # Positive = there was a better price available at market maximum; measures "best-odds shopping" value
    odds_gap_metrics = {}
    if "odds_gap" in bets.columns:
        gaps = bets["odds_gap"].dropna()
        if len(gaps) > 0:
            mean_gap = float(gaps.mean())
            # Projected ROI if bets were placed at H-Max: rough estimate (multiplicative gain)
            projected_roi = (1 + roi) * (1 + mean_gap) - 1
            odds_gap_metrics = {
                "n_with_hmax":          int(len(gaps)),
                "mean_odds_gap":        mean_gap,
                "projected_roi_hmax":   projected_roi,
            }

    return {
        "n_bets": n_bets,
        "total_staked": float(total_staked),
        "total_pnl": float(total_pnl),
        "roi": float(roi),
        "win_rate": win_rate,
        "sharpe": float(sharpe),
        "max_drawdown": max_dd,
        **odds_gap_metrics,
    }
