"""
Persistent P&L ledger for live bets.

CSV at results/ledger.csv — one row per bet, human-editable in Excel.
Settlement is automatic via martj42 results CSV, with TheOddsAPI as same-day
primary source for WM 2026 matches.
"""
from __future__ import annotations

import contextlib
import csv
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

import json
from datetime import date

from src.config import RESULTS_DIR, BANKROLL_SNAPSHOT_PATH, BANKROLL_START
from src.betting.value_detector import BetSignal

LEDGER_PATH = RESULTS_DIR / "ledger.csv"


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 10.0):
    """
    Advisory exclusive lock on `path + '.lock'` using fcntl (macOS/Linux).
    Falls back to no-op on platforms without fcntl (Windows).
    Prevents duplicate writes when two scan processes run simultaneously.
    """
    lock_path = path.with_suffix(".lock")
    try:
        import fcntl
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Could not acquire ledger lock within {timeout}s")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
    except ImportError:
        yield  # fcntl unavailable (Windows) — no locking


_WM_SCORES_CACHE: dict = {"ts": 0.0, "data": {}}
_WM_SCORES_TTL = 900  # 15 minutes


def _fetch_completed_wm_scores(api_key: str = "") -> dict[tuple[str, str], tuple[int, int]]:
    """
    Queries TheOddsAPI scores endpoint for recently completed WM 2026 matches.
    Returns {(canonical_home, canonical_away): (home_score, away_score)}.
    Cached for 15 minutes to avoid burning quota during settlement loops.
    Falls back to empty dict on any error (martj42 CSV is the primary fallback).
    """
    import time
    now = time.monotonic()
    if now - _WM_SCORES_CACHE["ts"] < _WM_SCORES_TTL and _WM_SCORES_CACHE["data"]:
        return _WM_SCORES_CACHE["data"]  # type: ignore[return-value]

    if not api_key:
        import os
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent.parent / ".env")
        api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {}
    try:
        import requests
        from src.config import canonical_name
        url = "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/scores/"
        resp = requests.get(url, params={"apiKey": api_key, "daysFrom": 7}, timeout=10)
        if not resp.ok:
            return {}
        scores: dict[tuple[str, str], tuple[int, int]] = {}
        for match in resp.json():
            if not match.get("completed"):
                continue
            home_name = canonical_name(match.get("home_team", ""))
            away_name = canonical_name(match.get("away_team", ""))
            match_scores = match.get("scores") or []
            h_score = next(
                (int(s["score"]) for s in match_scores if s["name"] == match.get("home_team")),
                None,
            )
            a_score = next(
                (int(s["score"]) for s in match_scores if s["name"] == match.get("away_team")),
                None,
            )
            if h_score is not None and a_score is not None:
                scores[(home_name, away_name)] = (h_score, a_score)
        _WM_SCORES_CACHE["ts"] = time.monotonic()
        _WM_SCORES_CACHE["data"] = scores
        return scores
    except Exception:
        return {}

_FIELDS = [
    "match_id", "match_date", "home", "away", "market",
    "decimal_odds", "stake_pct", "stake_amount",
    "placed_date", "status", "pnl", "closing_odds", "clv",
    "pinnacle_ref_odds",
    "source", "model_prob",
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
    df = pd.read_csv(path, dtype=str)
    # Schema migration: backfill new columns for legacy ledgers.
    if "source" not in df.columns:
        df["source"] = "value"
    else:
        df["source"] = df["source"].fillna("value").replace("", "value")
    if "model_prob" not in df.columns:
        df["model_prob"] = ""
    return df


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

    with _file_lock(path):
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
                "stake_amount": f"{s.stake_eur if s.stake_eur > 0 else s.stake_pct * bankroll:.2f}",
                "placed_date":  today,
                "status":        "open",
                "pnl":           "0.0",
                "closing_odds":  "0.0",
                "clv":           "",
                "pinnacle_ref_odds": f"{s.b365_odds:.4f}" if s.b365_odds > 1.0 else "",
                "source":        "value",
                "model_prob":    f"{s.model_prob:.6f}" if getattr(s, "model_prob", 0.0) > 0 else "",
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
    with _file_lock(ledger_path):
        return _settle_from_results_locked(ledger_path, results)


def _settle_from_results_locked(
    ledger_path: Path,
    results: pd.DataFrame | None,
) -> int:
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

    from src.config import canonical_name as _cn
    res_lookup: dict[tuple, dict] = {}
    for _, row in wm_results.iterrows():
        # Canonicalize so "Czech Republic" in martj42 CSV matches "Czechia" in ledger.
        key = (_cn(str(row["home_team"])), _cn(str(row["away_team"])))
        res_lookup[key] = row

    # Try TheOddsAPI scores endpoint first (same-day results, no lag)
    live_scores = _fetch_completed_wm_scores()

    settled = 0
    newly_settled_indices: list = []
    for idx in df[open_mask].index:
        row = df.loc[idx]
        home, away = str(row["home"]), str(row["away"])
        market = str(row["market"])
        odds = float(row["decimal_odds"])
        stake = float(row["stake_amount"])

        # Look up score: live API first, then martj42 CSV
        # score_key uses canonical names (e.g. "Czechia") so it matches res_lookup
        # which is also canonicalized — prevents "Czech Republic" vs "Czechia" mismatch.
        from src.config import canonical_name
        score_key = (canonical_name(home), canonical_name(away))
        if score_key in live_scores:
            hg, ag = live_scores[score_key]
        else:
            result = res_lookup.get(score_key)
            if result is None:
                continue
            hg = int(result["home_score"])
            ag = int(result["away_score"])
        total = hg + ag

        # Determine outcome
        push = False
        if market == "home":
            won = hg > ag
        elif market == "away":
            won = ag > hg
        elif market == "draw":
            won = hg == ag
        elif market.startswith("o/u") and ("_over" in market or "_under" in market):
            try:
                side = "over" if "_over" in market else "under"
                line = float(market.split("o/u")[1].split("_")[0])
                is_quarter = int(round(line * 4)) % 2 == 1
                if not is_quarter:
                    # Whole-ball or half-ball: simple comparison (no push for half-ball)
                    if side == "over":
                        # whole-ball push: total == lower → void (e.g. O/U 2.0 at total=2)
                        if total == line:
                            push = True
                            won = False
                        else:
                            won = total > line
                    else:
                        if total == line:
                            push = True
                            won = False
                        else:
                            won = total < line
                else:
                    # Quarter-ball: half WIN or half LOSS at the pivot value
                    # lower = floor(line), upper = ceil(line)
                    lower = int(line) if line > int(line) else int(line) - 1
                    # Pivot = the integer between lower and upper
                    pivot = lower + 1 if (line % 1) == 0.25 else lower + 1
                    if side == "over":
                        if total > line + 0.5:   # ≥ pivot+1: full WIN
                            won = True
                        elif total == pivot - 1 and (line % 1) == 0.25:
                            # e.g. O/U 2.25: total=2 → half LOSS
                            won = False
                            # Store half-loss: settled as lost with half pnl
                            df.at[idx, "status"] = "lost"
                            df.at[idx, "pnl"] = f"{-stake / 2:.2f}"
                            newly_settled_indices.append(idx)
                            settled += 1
                            continue
                        elif total == pivot and (line % 1) == 0.75:
                            # e.g. O/U 2.75: total=3 → half WIN
                            df.at[idx, "status"] = "won"
                            df.at[idx, "pnl"] = f"{stake * (odds - 1) / 2:.2f}"
                            newly_settled_indices.append(idx)
                            settled += 1
                            continue
                        else:
                            won = False
                    else:  # under
                        if total < line - 0.5:   # ≤ pivot-1: full WIN
                            won = True
                        elif total == pivot - 1 and (line % 1) == 0.25:
                            # e.g. O/U 2.25 under: total=2 → half WIN
                            df.at[idx, "status"] = "won"
                            df.at[idx, "pnl"] = f"{stake * (odds - 1) / 2:.2f}"
                            newly_settled_indices.append(idx)
                            settled += 1
                            continue
                        elif total == pivot and (line % 1) == 0.75:
                            # e.g. O/U 2.75 under: total=3 → half LOSS
                            df.at[idx, "status"] = "lost"
                            df.at[idx, "pnl"] = f"{-stake / 2:.2f}"
                            newly_settled_indices.append(idx)
                            settled += 1
                            continue
                        else:
                            won = False
            except (ValueError, IndexError):
                continue
        elif market.startswith("ah") and ("_home" in market or "_away" in market):
            side = "home" if "_home" in market else "away"
            try:
                line = float(market.split("ah")[1].split("_")[0])
            except (ValueError, IndexError):
                continue
            diff = hg - ag
            if side == "home":
                need = -line  # goals home must win by to cover
                is_quarter = int(round(need * 4)) % 2 == 1
                lo = int(need)
                if not is_quarter:
                    if need == lo:  # whole-ball
                        won = diff > need if diff != need else (push := True) and False
                    else:
                        won = diff > need
                elif need % 1 < 0.5:  # x.25: pivot=lo (whole-ball lower)
                    if diff > lo:
                        won = True
                    elif diff == lo:
                        df.at[idx, "status"] = "lost"
                        df.at[idx, "pnl"] = f"{-stake / 2:.2f}"
                        newly_settled_indices.append(idx); settled += 1; continue
                    else:
                        won = False
                else:  # x.75: pivot=lo+1 (whole-ball upper)
                    if diff > lo + 1:
                        won = True
                    elif diff == lo + 1:
                        df.at[idx, "status"] = "won"
                        df.at[idx, "pnl"] = f"{stake * (odds - 1) / 2:.2f}"
                        newly_settled_indices.append(idx); settled += 1; continue
                    else:
                        won = False
            else:  # away
                need = line  # goals away can spot (home must exceed to beat handicap)
                is_quarter = int(round(need * 4)) % 2 == 1
                lo = int(need)
                if not is_quarter:
                    if need == lo:  # whole-ball
                        won = diff < need if diff != need else (push := True) and False
                    else:
                        won = diff < need
                elif need % 1 < 0.5:  # x.25: pivot=lo
                    if diff < lo:
                        won = True
                    elif diff == lo:
                        df.at[idx, "status"] = "won"
                        df.at[idx, "pnl"] = f"{stake * (odds - 1) / 2:.2f}"
                        newly_settled_indices.append(idx); settled += 1; continue
                    else:
                        won = False
                else:  # x.75: pivot=lo+1
                    if diff < lo + 1:
                        won = True
                    elif diff == lo + 1:
                        df.at[idx, "status"] = "lost"
                        df.at[idx, "pnl"] = f"{-stake / 2:.2f}"
                        newly_settled_indices.append(idx); settled += 1; continue
                    else:
                        won = False
        elif market == "btts_yes":
            won = (hg >= 1) and (ag >= 1)
        elif market == "btts_no":
            won = (hg == 0) or (ag == 0)
        elif market == "dc_1x":
            # Home Win or Draw — only away win loses
            won = hg >= ag
        elif market == "dc_x2":
            # Draw or Away Win — only home win loses
            won = ag >= hg
        elif market == "dc_12":
            # Home Win or Away Win — draw loses
            won = hg != ag
        elif market == "goals_2_4":
            won = 2 <= (hg + ag) <= 4
        elif market in ("h1_goals_2_4", "h2_goals_2_4"):
            # Half-time scores not automatically available — mark void until manual entry
            df.at[idx, "status"] = "void"
            df.at[idx, "pnl"] = "0.00"
            newly_settled_indices.append(idx); settled += 1; continue
        elif market in ("ah-1.5_a", "ah+1.5_b"):
            # Tennis set handicap — not settled via football results, skip silently
            continue
        else:
            # Unknown market type: log and skip to avoid silent data corruption
            import warnings
            warnings.warn(f"settle_from_results: unknown market type '{market}' — skipping")
            continue

        if push:
            df.at[idx, "status"] = "void"
            df.at[idx, "pnl"] = "0.00"
        else:
            df.at[idx, "status"] = "won" if won else "lost"
            df.at[idx, "pnl"] = f"{stake * (odds - 1):.2f}" if won else f"{-stake:.2f}"
        # CLV: positive = we got better price than closing line.
        # Guard: closing_odds > odds*3 (e.g. odds crashed to 0.25 from 2.50) indicates
        # data corruption — cap CLV to [-99%, +200%] to protect mean_clv stats.
        closing = float(df.at[idx, "closing_odds"] or 0)
        if 1.0 < closing < odds * 3.0:
            clv = max(-0.99, min(2.00, odds / closing - 1.0))
            df.at[idx, "clv"] = f"{clv:.4f}"
        newly_settled_indices.append(idx)
        settled += 1

    if settled:
        _save(df, ledger_path)
        # Send Web Push notification for each newly settled bet
        try:
            from src.notifications.web_push import send_settlement_alert
            summary = ledger_summary(ledger_path)
            for idx in newly_settled_indices:
                send_settlement_alert(df.loc[idx].to_dict(), summary)
        except Exception:
            pass

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
    n_void = int((df["status"] == "void").sum())
    # Include void in staked/pnl so ROI denominator is accurate (push returns stake, pnl=0)
    settled = df[df["status"].isin(["won", "lost", "void"])]

    total_staked = pd.to_numeric(settled["stake_amount"], errors="coerce").sum()
    total_pnl = pd.to_numeric(settled["pnl"], errors="coerce").sum()
    roi_pct = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
    win_rate = (n_won / (n_won + n_lost) * 100) if (n_won + n_lost) > 0 else 0.0

    clv_vals = pd.to_numeric(
        df[df["status"].isin(["won", "lost"])].get("clv", pd.Series([])), errors="coerce"
    ).dropna()
    mean_clv = float(clv_vals.mean()) if not clv_vals.empty else None

    # ROI breakdown per market type
    by_market: dict = {}
    if not settled.empty and "market" in settled.columns:
        for mkt, grp in settled.groupby("market"):
            m_staked = pd.to_numeric(grp["stake_amount"], errors="coerce").sum()
            m_pnl = pd.to_numeric(grp["pnl"], errors="coerce").sum()
            m_won = int((grp["status"] == "won").sum())
            m_lost = int((grp["status"] == "lost").sum())
            by_market[str(mkt)] = {
                "n": len(grp),
                "pnl": float(m_pnl),
                "roi_pct": float(m_pnl / m_staked * 100) if m_staked > 0 else 0.0,
                "won": m_won,
                "lost": m_lost,
            }

    return {
        "n_bets": len(df),
        "n_open": n_open,
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "total_staked": float(total_staked),
        "total_pnl": float(total_pnl),
        "roi_pct": float(roi_pct),
        "win_rate": float(win_rate),
        "mean_clv": mean_clv,
        "by_market": by_market,
    }


def _current_iso_week(today: date | None = None) -> tuple[int, int]:
    d = today or date.today()
    iso = d.isocalendar()
    return iso[0], iso[1]


def _live_bankroll(ledger_path: Path = LEDGER_PATH) -> float:
    """BANKROLL_START + total realized P&L (excluding open bets)."""
    s = ledger_summary(ledger_path)
    return round(BANKROLL_START + s["total_pnl"], 2)


def peek_bankroll_snapshot(
    snapshot_path: Path = BANKROLL_SNAPSHOT_PATH,
    ledger_path: Path = LEDGER_PATH,
) -> float:
    """Read-only: returns cached snapshot if valid for current ISO week,
    otherwise the live computed bankroll. Does NOT write to disk."""
    year, week = _current_iso_week()
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text())
            if data.get("iso_year") == year and data.get("iso_week") == week:
                return float(data["bankroll"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return _live_bankroll(ledger_path)


def get_bankroll_snapshot(
    snapshot_path: Path = BANKROLL_SNAPSHOT_PATH,
    ledger_path: Path = LEDGER_PATH,
) -> float:
    """Returns bankroll for the current ISO week. On the first call of a new
    ISO week, recomputes from the ledger and persists the snapshot. Within
    the same week, returns the cached value so stakes stay stable across
    intra-week wins/losses (drawdown smoothing — user choice over HWM)."""
    year, week = _current_iso_week()
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text())
            if data.get("iso_year") == year and data.get("iso_week") == week:
                return float(data["bankroll"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    bankroll = _live_bankroll(ledger_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps({
        "iso_year": year,
        "iso_week": week,
        "snapshot_date": date.today().isoformat(),
        "bankroll": bankroll,
    }, indent=2))
    return bankroll
