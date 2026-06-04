"""
Persistent P&L ledger for live bets.

CSV at results/ledger.csv — one row per bet, human-editable in Excel.
Settlement is automatic via martj42 results CSV.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

from src.config import RESULTS_DIR
from src.betting.value_detector import BetSignal

LEDGER_PATH = RESULTS_DIR / "ledger.csv"

_FIELDS = [
    "match_id", "match_date", "home", "away", "market",
    "decimal_odds", "stake_pct", "stake_amount",
    "placed_date", "status", "pnl", "closing_odds", "clv",
]


@dataclass
class BetRecord:
    match_id: str
    match_date: str      # YYYY-MM-DD or "" if unknown
    home: str
    away: str
    market: str          # "home" | "draw" | "away" | "o/u2.5_over" | ...
    decimal_odds: float
    stake_pct: float
    stake_amount: float
    placed_date: str     # YYYY-MM-DD
    status: str          # "open" | "won" | "lost" | "void"
    pnl: float           # 0.0 while open


def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=_FIELDS)
    return pd.read_csv(path, dtype=str)


def _save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def append_bets(
    signals: list[BetSignal],
    bankroll: float,
    path: Path = LEDGER_PATH,
    match_date: str = "",
) -> int:
    """
    Appends new BetSignals as 'open' to the ledger CSV.
    Skips duplicates (same match_id + market already present).
    Returns number of new rows written.
    """
    if not signals:
        return 0

    df = _load(path)
    existing = set(zip(df.get("match_id", pd.Series([])), df.get("market", pd.Series([]))))

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    new_rows = []
    for s in signals:
        key = (s.match_id, s.market)
        if key in existing:
            continue
        new_rows.append({
            "match_id":     s.match_id,
            "match_date":   match_date,
            "home":         s.home,
            "away":         s.away,
            "market":       s.market,
            "decimal_odds": f"{s.decimal_odds:.4f}",
            "stake_pct":    f"{s.stake_pct:.6f}",
            "stake_amount": f"{s.stake_pct * bankroll:.2f}",
            "placed_date":  today,
            "status":        "open",
            "pnl":           "0.0",
            "closing_odds":  "0.0",
            "clv":           "",
        })

    if new_rows:
        new_df = pd.DataFrame(new_rows, columns=_FIELDS)
        df = pd.concat([df, new_df], ignore_index=True)
        _save(df, path)

    return len(new_rows)


def settle_from_results(
    ledger_path: Path = LEDGER_PATH,
    results: pd.DataFrame | None = None,
) -> int:
    """
    Settles open bets against martj42 results.
    1X2: home/away_score determines winner.
    O/U: home_score + away_score vs 2.5.
    Returns number of newly settled bets.
    """
    df = _load(ledger_path)
    if df.empty:
        return 0

    open_mask = df["status"] == "open"
    if not open_mask.any():
        return 0

    if results is None:
        try:
            from src.data.international import fetch_international_results
            results = fetch_international_results()
        except Exception:
            return 0

    # Only settle from WM 2026 matches (never accidentally settle against
    # historical Copa América / friendly data with the same team names).
    wm_results = results[
        (results.get("tournament", pd.Series()) == "FIFA World Cup")
        & (results["date"] >= pd.Timestamp("2026-06-11"))
        & results["home_score"].notna()
    ] if "tournament" in results.columns else results.iloc[:0]

    res_lookup: dict[tuple, dict] = {}
    for _, row in wm_results.iterrows():
        key = (str(row["home_team"]), str(row["away_team"]))
        res_lookup[key] = row

    settled = 0
    for idx in df[open_mask].index:
        row = df.loc[idx]
        home, away = str(row["home"]), str(row["away"])
        market = str(row["market"])
        odds = float(row["decimal_odds"])
        stake = float(row["stake_amount"])

        result = res_lookup.get((home, away))
        if result is None:
            continue

        hg = int(result["home_score"])
        ag = int(result["away_score"])
        total = hg + ag

        # Determine outcome
        if market == "home":
            won = hg > ag
        elif market == "away":
            won = ag > hg
        elif market == "draw":
            won = hg == ag
        elif "over" in market:
            won = total > 2.5
        elif "under" in market:
            won = total <= 2.5
        else:
            continue

        df.at[idx, "status"] = "won" if won else "lost"
        df.at[idx, "pnl"] = f"{stake * (odds - 1):.2f}" if won else f"{-stake:.2f}"
        # CLV: positive = we got better price than closing line
        closing = float(df.at[idx, "closing_odds"] or 0)
        if closing > 1.0:
            clv = odds / closing - 1.0
            df.at[idx, "clv"] = f"{clv:.4f}"
        settled += 1

    if settled:
        _save(df, ledger_path)

    return settled


def count_open_bets(path: Path = LEDGER_PATH) -> int:
    """Returns number of bets with status='open'. 0 if ledger doesn't exist."""
    df = _load(path)
    if df.empty:
        return 0
    return int((df["status"] == "open").sum())


def ledger_summary(path: Path = LEDGER_PATH) -> dict:
    """
    Returns summary stats: n_bets, n_open, n_won, n_lost, total_staked, total_pnl, roi_pct, win_rate.
    """
    df = _load(path)
    if df.empty:
        return {
            "n_bets": 0, "n_open": 0, "n_won": 0, "n_lost": 0,
            "total_staked": 0.0, "total_pnl": 0.0, "roi_pct": 0.0, "win_rate": 0.0,
        }

    n_open = int((df["status"] == "open").sum())
    n_won = int((df["status"] == "won").sum())
    n_lost = int((df["status"] == "lost").sum())
    settled = df[df["status"].isin(["won", "lost"])]

    total_staked = pd.to_numeric(settled["stake_amount"], errors="coerce").sum()
    total_pnl = pd.to_numeric(settled["pnl"], errors="coerce").sum()
    roi_pct = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
    win_rate = (n_won / (n_won + n_lost) * 100) if (n_won + n_lost) > 0 else 0.0

    clv_vals = pd.to_numeric(settled.get("clv", pd.Series([])), errors="coerce").dropna()
    mean_clv = float(clv_vals.mean()) if not clv_vals.empty else None

    return {
        "n_bets": len(df),
        "n_open": n_open,
        "n_won": n_won,
        "n_lost": n_lost,
        "total_staked": float(total_staked),
        "total_pnl": float(total_pnl),
        "roi_pct": float(roi_pct),
        "win_rate": float(win_rate),
        "mean_clv": mean_clv,
    }
